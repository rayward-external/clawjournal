"""Explicitly approved, pseudonymized snippet contributions. No upload client.

The private evaluation store retains provenance. Portable bundles deliberately
omit originals, source identifiers, source hashes, free-text notes and paths.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
import uuid

from .evaluation import POLICY, canonical, digest, private_write, text_units

SCHEMA = "clawjournal-redaction-contribution-v1"
AUTO_SCHEMA = "clawjournal-redaction-contribution-v2"
CONSENT = "snippet-training-evaluation-v1"
PURPOSE = "ClawJournal project redaction-model training and evaluation"
MAX_CHARS = 800
MAX_LABELS = 40
TYPES = {"person", "email", "phone_number", "user_name", "address"}
FIRST_NAMES = ["Mira", "Nolan", "Elara", "Tessa", "Alden", "Lena", "Orin"]
LAST_NAMES = ["Fenwick", "Sorrell", "Alderbrook", "Merrin", "Hollis", "Vale"]
ALIASES = {"name": "person", "phone": "phone_number", "home_username": "user_name"}
ATTESTATIONS = ("consent", "rights", "reviewed_context", "reviewed_labels")


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _directory(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Contribution storage must not use symlinks")
    from ..filesystem import classify_state_storage
    if classify_state_storage(path / "state.db").storage_risk == "network":
        raise ValueError("Use local storage for contributions")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _connect(path):
    _directory(path.parent)
    if any(p.is_symlink() for p in (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"))):
        raise ValueError("Refusing symlink database files")
    if not path.exists():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    path.chmod(0o600)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("PRAGMA journal_mode=DELETE")
    return conn


def contributor_profile(split, assigned_id=None):
    """One pseudonym and split per installation, shared across evaluation stores."""
    from ..config import CONFIG_DIR
    if assigned_id is not None and not _uuid(assigned_id):
        raise ValueError("Contributor ID must be a canonical UUID, not a name or email")
    conn = _connect(CONFIG_DIR / "redaction-contributor.db")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS profile(id INTEGER PRIMARY KEY CHECK(id=1), contributor TEXT, split TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT contributor,split FROM profile").fetchone()
        if row:
            if row["split"] != split:
                raise ValueError("This contributor already belongs to a different split")
            if assigned_id and assigned_id != row["contributor"]:
                raise ValueError("This installation already has a different contributor ID")
            result = dict(row)
        else:
            result = {"contributor": assigned_id or str(uuid.uuid4()), "split": split}
            conn.execute("INSERT INTO profile VALUES(1,?,?)", (result["contributor"], split))
        conn.commit()
        return result
    finally:
        conn.close()


def _schema(store):
    store.conn.execute("""CREATE TABLE IF NOT EXISTS contribution_proposals (
            id TEXT PRIMARY KEY, capture_id TEXT NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL, snapshot_hash TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', approved_at REAL,
            unit TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL
    )""")


def purge_capture(store, capture_id):
    """Called by evaluation retention/deletion. Never follows arbitrary paths."""
    from .conversation_pairs import purge_capture as purge_pairs
    purge_pairs(store, capture_id)
    exists = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='contribution_proposals'").fetchone()
    if exists:
        for row in store.conn.execute("SELECT id FROM contribution_proposals WHERE capture_id=?", (capture_id,)):
            if not _uuid(row[0]):
                raise ValueError("Invalid stored contribution ID")
            directory = store.root / "contributions"
            if directory.is_symlink():
                raise ValueError("Refusing symlink contribution directory")
            for suffix in (".html", ".bundle.json"):
                (directory / (row[0] + suffix)).unlink(missing_ok=True)


def _replacement(kind):
    first = secrets.choice(FIRST_NAMES)
    last = secrets.choice(LAST_NAMES)
    n = secrets.randbelow(10000)
    return {
        "person": f"{first} {last}",
        "email": f"{first.lower()}.{last.lower()}{n}@research.example.org",
        "user_name": f"{first.lower()[0]}{last.lower()}{n}",
        "phone_number": f"202-555-01{n % 100:02d}",
        "address": f"{100 + n % 800} {secrets.choice(['Alder', 'Juniper', 'Wisteria'])} Lane, Exampleton",
    }[kind]


def _invented(kind, value):
    first, last = "(?:" + "|".join(FIRST_NAMES) + ")", "(?:" + "|".join(LAST_NAMES) + ")"
    patterns = {
        "person": first + " " + last,
        "email": first.lower() + r"\." + last.lower() + r"[0-9]{1,4}@research\.example\.org",
        "user_name": "[mnetalo]" + last.lower() + r"[0-9]{1,4}",
        "phone_number": r"202-555-01[0-9]{2}",
        "address": r"[1-8][0-9]{2} (?:Alder|Juniper|Wisteria) Lane, Exampleton",
    }
    return bool(re.fullmatch(patterns[kind], value))


def _check_candidates(text, annotations, *, automatic=False):
    """Unreviewed rule matches require a correction, including false positives."""
    from .pii import scan_text_for_pii
    from .secrets import scan_text
    secret_findings = scan_text(text)
    privacy_types = {"email", "ip_address", "private_url"}
    if any(match["type"] not in privacy_types for match in secret_findings):
        raise ValueError("Snippet contains a possible secret. Remove that content before contributing")
    for match in scan_text_for_pii(text) + secret_findings:
        if not all(any(a["start"] <= i < a["end"] for a in annotations)
                   for i in range(match["start"], match["end"])):
            raise ValueError("Snippet has unlabelled possible PII. Review all matches or select a smaller snippet")
        if match["type"] == "email" and not any(a["decision"] == "redact" and a["entity_type"] == "email"
                and a["start"] <= match["start"] and a["end"] >= match["end"] for a in annotations):
            raise ValueError("Email identifiers must be labelled redact under the contribution policy")
        if automatic and any(a["decision"] == "preserve" and a.get("review_status") != "human_reviewed"
                and a["start"] < match["end"] and a["end"] > match["start"] for a in annotations):
            raise ValueError("Draft preservation overlaps possible PII; review this ambiguous match first")


def prepare(store, capture_id, unit, start, end, *, assigned_id=None, automatic=False):
    _schema(store)
    if automatic:
        from . import ongoing_contributions as ongoing
        grant = ongoing.require_grant(store, capture_id)
    capture = store.get(capture_id)
    units = text_units(capture["original"])
    if unit not in units or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(units[unit]):
        raise ValueError("Select a valid snippet range in the original text")
    if end - start > MAX_CHARS:
        raise ValueError("Select a snippet of at most 800 characters")
    source = units[unit][start:end]
    annotations = []
    for a in capture["annotations"]:
        if a["unit"] != unit or a["start"] >= end or a["end"] <= start:
            continue
        if a["start"] < start or a["end"] > end:
            raise ValueError("Snippet cuts through a label; expand or shrink the selection")
        if a["decision"] == "uncertain" or (not automatic and a["review_status"] != "human_reviewed"):
            raise ValueError("Every selected label must be human-reviewed and resolved")
        kind = ALIASES.get(a["entity_type"], a["entity_type"])
        if a["decision"] == "redact" and kind not in TYPES:
            raise ValueError("Invented replacement supports names, emails, usernames, phones and addresses only; select a smaller snippet")
        annotations.append({"start": a["start"] - start, "end": a["end"] - start,
                            "decision": a["decision"], "entity_type": kind if a["decision"] == "redact" else "non_pii"})
        if automatic:
            annotations[-1]["review_status"] = a["review_status"]
    annotations.sort(key=lambda a: a["start"])
    if not 1 <= len(annotations) <= MAX_LABELS:
        raise ValueError("Select a snippet with 1–40 reviewed labels")
    _check_candidates(source, annotations, automatic=automatic)
    # Repeated values use the same replacement within this proposed snippet.
    replacements, originals = {}, set()
    parts, labels, offset, cursor = [], [], 0, 0
    for a in annotations:
        prefix = source[cursor:a["start"]]
        parts.append(prefix); offset += len(prefix)
        value = source[a["start"]:a["end"]]
        if a["decision"] == "redact":
            originals.add(value)
            key = (a["entity_type"], value.casefold())
            if key not in replacements:
                for _ in range(100):
                    candidate = _replacement(a["entity_type"])
                    if candidate.casefold() not in source.casefold() and candidate not in replacements.values():
                        replacements[key] = candidate
                        break
                else:
                    raise ValueError("Could not create a distinct invented identifier")
            value = replacements[key]
        labels.append({**a, "start": offset, "end": offset + len(value)})
        parts.append(value); offset += len(value); cursor = a["end"]
    parts.append(source[cursor:])
    text = "".join(parts)
    if len(text) > MAX_CHARS:
        raise ValueError("Replacements exceed the snippet size limit; select a smaller snippet")
    if any(value.casefold() in text.casefold() for value in originals):
        raise ValueError("An original identifier still occurs in the snippet; label every occurrence")
    profile = contributor_profile(capture["split"], assigned_id)
    proposal_id = str(uuid.uuid4())
    payload = {"schema": SCHEMA, "policy": POLICY, "consent_version": CONSENT,
               "purpose": PURPOSE, "contribution_id": proposal_id,
               "contributor_id": profile["contributor"], "split": profile["split"],
               "text": text, "labels": labels, "identifiers": "invented-substitutions",
               "annotation_scope": "selected-spans-not-exhaustive-NER"}
    if automatic:
        payload.update(schema=AUTO_SCHEMA, context_review_status="unreviewed")
    snapshot_hash = digest({"payload": payload, "pair_hash": capture["pair_hash"], "revision": capture["revision"]})
    with store.conn:
        if automatic:
            store.conn.execute("BEGIN IMMEDIATE")
            ongoing.require_grant(store, capture_id, grant["generation"])
        store.conn.execute("INSERT INTO contribution_proposals(id,capture_id,revision,snapshot_hash,payload,unit,start,end) VALUES(?,?,?,?,?,?,?,?)",
                           (proposal_id, capture_id, capture["revision"], snapshot_hash, canonical(payload), unit, start, end))
        if automatic:
            store.conn.execute("INSERT INTO automatic_proposals VALUES(?,?)", (proposal_id, grant["generation"]))
    return proposal_id, review(store, proposal_id)


def _get(store, proposal_id):
    if not _uuid(proposal_id):
        raise ValueError("Expected a contribution UUID")
    _schema(store)
    row = store.conn.execute("SELECT * FROM contribution_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("Unknown or expired contribution")
    capture = store.get(row["capture_id"])
    if row["revision"] != capture["revision"]:
        raise ValueError("Labels changed after preparation; prepare and approve a new contribution")
    if row["state"] == "withdrawn":
        raise ValueError("Contribution was declined or withdrawn")
    payload = json.loads(row["payload"])
    if digest({"payload": payload, "pair_hash": capture["pair_hash"], "revision": capture["revision"]}) != row["snapshot_hash"]:
        raise ValueError("Contribution snapshot changed")
    return row, capture, payload


def review(store, proposal_id):
    row, capture, payload = _get(store, proposal_id)
    data = {"proposal_id": proposal_id, "snapshot_hash": row["snapshot_hash"],
            "consent_version": CONSENT, "original": text_units(capture["original"])[row["unit"]][row["start"]:row["end"]],
            "payload": payload}
    directory = _directory(store.root / "contributions")
    template = Path(__file__).with_name("contribution_review.html").read_text()
    encoded = canonical(data).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    path = directory / (proposal_id + ".html")
    private_write(path, template.replace("__CONTRIBUTION_DATA__", encoded))
    return path


def approve(store, document):
    if not isinstance(document, dict):
        raise ValueError("Expected a snippet approval object")
    row, _capture, _payload = _get(store, document.get("proposal_id"))
    if _payload["schema"] == AUTO_SCHEMA:
        raise ValueError("Automatic examples remain unreviewed; prepare a separate manual proposal after review")
    if document.get("snapshot_hash") != row["snapshot_hash"] or document.get("consent_version") != CONSENT:
        raise ValueError("Approval does not match the proposed snippet")
    if any(document.get(key) is not True for key in ATTESTATIONS):
        raise ValueError("Explicit contribution consent, rights, context review and label review are required")
    with store.conn:
        store.conn.execute("UPDATE contribution_proposals SET state='approved',approved_at=? WHERE id=?", (time.time(), row["id"]))
    return row["id"]


def withdraw(store, proposal_id):
    if not _uuid(proposal_id):
        raise ValueError("Expected a contribution UUID")
    _schema(store)
    with store.conn:
        changed = store.conn.execute("UPDATE contribution_proposals SET state='withdrawn',approved_at=NULL WHERE id=?", (proposal_id,)).rowcount
        if not changed:
            raise ValueError("Unknown contribution")
    directory = _directory(store.root / "contributions")
    for suffix in (".html", ".bundle.json"):
        (directory / (proposal_id + suffix)).unlink(missing_ok=True)


def _scan_candidate(bundle, directory):
    """Run the existing package gate on the EXACT candidate, never on originals.

    The final verified-only TruffleHog scan retains its existing network behavior.
    If any bytes change we reject, because the user approved the earlier bytes.
    """
    from ..workbench.share_gate import run_share_gate
    from . import betterleaks, trufflehog
    if betterleaks.is_bypassed() or trufflehog.is_bypassed():
        raise ValueError("Contribution export refuses scanner bypass flags")
    raw = canonical(bundle) + "\n"
    # Check Betterleaks locally first; don't send a flagged candidate into live
    # credential verification. There is no ignored-finding override here.
    local_report = betterleaks.scan_text(raw)
    if local_report.binary_missing or local_report.scan_error or local_report.bypassed or local_report.findings:
        raise ValueError("Contribution secret scan failed or found possible secrets; revise the snippet")
    with tempfile.TemporaryDirectory(prefix=".contribution-scan-", dir=directory) as scratch:
        path = Path(scratch) / "sessions.jsonl"
        private_write(path, raw)
        conn = sqlite3.connect(":memory:")
        try:
            report = run_share_gate(path, {"sessions": [{"session_id": bundle["example"]["contribution_id"]}]}, conn=conn)
        finally:
            conn.close()
        if report.bypassed or report.blocking or report.findings or report.gate_redactions or path.read_text() != raw:
            raise ValueError("Contribution gate failed or changed the approved bytes; revise and approve again")


def export_bundle(store, proposal_id):
    row, _capture, payload = _get(store, proposal_id)
    automatic = payload["schema"] == AUTO_SCHEMA
    if automatic:
        from . import ongoing_contributions as ongoing
        authorization = ongoing.authorization(store, row, payload)
    elif row["state"] != "approved":
        raise ValueError("This snippet has not been explicitly approved for contribution")
    else:
        authorization = {"consent_version": CONSENT, "purpose": PURPOSE,
              "payload_sha256": digest(payload), **dict.fromkeys(ATTESTATIONS, True)}
    bundle = {"example": payload, "approval": authorization}
    directory = _directory(store.root / "contributions")
    _scan_candidate(bundle, directory)
    # An approval or source revision can change while external scanners run.
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        current, _capture, current_payload = _get(store, proposal_id)
        if automatic:
            if ongoing.authorization(store, current, current_payload) != authorization:
                raise ValueError("Contribution authorization changed during scanning")
        if (not automatic and current["state"] != "approved") or current_payload != payload:
            raise ValueError("Contribution authorization changed during scanning")
        path = directory / (proposal_id + ".bundle.json")
        private_write(path, canonical(bundle) + "\n")
        store.conn.commit()
        return path
    except BaseException:
        store.conn.rollback()
        raise


def validate_bundle(bundle):
    """Strict allowlist schema: unknown fields cannot smuggle raw provenance."""
    if not isinstance(bundle, dict) or set(bundle) != {"example", "approval"}:
        raise ValueError("Invalid contribution bundle")
    p, a = bundle["example"], bundle["approval"]
    automatic = isinstance(p, dict) and p.get("schema") == AUTO_SCHEMA
    keys = {"schema", "policy", "consent_version", "purpose", "contribution_id", "contributor_id", "split", "text", "labels", "identifiers", "annotation_scope"}
    if automatic:
        keys.add("context_review_status")
    if not isinstance(p, dict) or set(p) != keys or p["schema"] not in (SCHEMA, AUTO_SCHEMA) or p["policy"] != POLICY or p["consent_version"] != CONSENT or p["purpose"] != PURPOSE:
        raise ValueError("Unsupported contribution schema or policy")
    if automatic and p["context_review_status"] != "unreviewed":
        raise ValueError("Automatic contributions must retain unreviewed context status")
    if not _uuid(p["contribution_id"]) or not _uuid(p["contributor_id"]) or p["split"] not in ("train", "dev", "test"):
        raise ValueError("Invalid contributor, contribution or split")
    if p["identifiers"] != "invented-substitutions" or p["annotation_scope"] != "selected-spans-not-exhaustive-NER":
        raise ValueError("Unsupported identifier/annotation scope")
    if not isinstance(p["text"], str) or not 0 < len(p["text"]) <= MAX_CHARS or not isinstance(p["labels"], list) or not 1 <= len(p["labels"]) <= MAX_LABELS:
        raise ValueError("Invalid snippet or labels")
    end = 0
    for label in p["labels"]:
        label_keys = {"start", "end", "decision", "entity_type"}
        if automatic:
            label_keys.add("review_status")
        if not isinstance(label, dict) or set(label) != label_keys:
            raise ValueError("Invalid label schema")
        if automatic and label["review_status"] not in ("assistant_draft", "human_reviewed"):
            raise ValueError("Invalid label review status")
        if type(label["start"]) is not int or type(label["end"]) is not int or not end <= label["start"] < label["end"] <= len(p["text"]):
            raise ValueError("Invalid or overlapping label offsets")
        if not isinstance(label["entity_type"], str) or not isinstance(label["decision"], str):
            raise ValueError("Invalid label type or decision")
        if label["decision"] == "redact":
            if label["entity_type"] not in TYPES:
                raise ValueError("Unsupported identifier type")
            if not _invented(label["entity_type"], p["text"][label["start"]:label["end"]]):
                raise ValueError("Redact labels must contain supported invented stand-ins")
        elif label["decision"] != "preserve" or label["entity_type"] != "non_pii":
            raise ValueError("Invalid label decision")
        end = label["end"]
    if automatic:
        from .ongoing_contributions import validate_authorization
        validate_authorization(a, p)
        return p
    if not isinstance(a, dict) or set(a) != {"consent_version", "purpose", "payload_sha256", *ATTESTATIONS}:
        raise ValueError("Invalid approval schema")
    if a["consent_version"] != CONSENT or a["purpose"] != PURPOSE or a["payload_sha256"] != digest(p) or any(a[k] is not True for k in ATTESTATIONS):
        raise ValueError("Contribution has no matching explicit approval")
    return p


def _structure(payload):
    """Match equivalent snippets even when invented replacement values differ."""
    text, cursor, parts = payload["text"], 0, []
    for a in payload["labels"]:
        if a["decision"] == "redact":
            parts.extend([text[cursor:a["start"]], "<" + a["entity_type"] + ">"])
            cursor = a["end"]
    parts.append(text[cursor:])
    return digest("".join(parts))


def collect(directory, bundle, contributor_id):
    """Maintainer uses ONE assigned pseudonym per contributor across devices.

    Portable consent is an attestation, not cryptographic proof of identity.
    Server authentication and delivery are outside this local collector.
    """
    payload = validate_bundle(bundle)
    if not _uuid(contributor_id):
        raise ValueError("Use a stable maintainer-assigned contributor UUID")
    directory = _directory(directory)
    # Independently rescan incoming bundles; their approval metadata is untrusted.
    _scan_candidate(bundle, directory)
    conn = _connect(directory / "collection.db")
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS contributors(id TEXT PRIMARY KEY, split TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aliases(source_id TEXT PRIMARY KEY, contributor_id TEXT NOT NULL REFERENCES contributors(id));
            CREATE TABLE IF NOT EXISTS examples(id TEXT PRIMARY KEY, contributor_id TEXT NOT NULL REFERENCES contributors(id),
                split TEXT NOT NULL, content_hash TEXT NOT NULL, structure_hash TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT split FROM contributors WHERE id=?", (contributor_id,)).fetchone()
        if existing and existing[0] != payload["split"]:
            raise ValueError("Contributor split conflict: their examples must stay together")
        alias = conn.execute("SELECT contributor_id FROM aliases WHERE source_id=?", (payload["contributor_id"],)).fetchone()
        if alias and alias[0] != contributor_id:
            raise ValueError("This source contributor already maps to another assigned contributor")
        structure, content_hash = _structure(payload), digest(payload["text"])
        duplicates = conn.execute("SELECT split FROM examples WHERE content_hash=? OR structure_hash=?", (content_hash, structure)).fetchall()
        if any(r[0] != payload["split"] for r in duplicates):
            raise ValueError("Duplicate or equivalent snippet would cross dataset splits")
        old = conn.execute("SELECT payload,contributor_id FROM examples WHERE id=?", (payload["contribution_id"],)).fetchone()
        if old:
            if old[0] != canonical(bundle) or old[1] != contributor_id:
                raise ValueError("Contribution ID conflicts with an existing example")
            conn.rollback()
            return {"status": "already-collected", "split": payload["split"]}
        conn.execute("INSERT OR IGNORE INTO contributors VALUES(?,?)", (contributor_id, payload["split"]))
        conn.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (payload["contributor_id"], contributor_id))
        conn.execute("INSERT INTO examples VALUES(?,?,?,?,?,?)", (payload["contribution_id"], contributor_id, payload["split"], content_hash, structure, canonical(bundle)))
        conn.commit()
        return {"status": "collected", "split": payload["split"], "contribution_id": payload["contribution_id"]}
    finally:
        conn.close()
