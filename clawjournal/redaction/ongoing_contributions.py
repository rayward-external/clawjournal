"""Revocable, store-scoped permission for future local snippet contributions.

This does not enroll hosted uploads or certify automatically produced labels.
"""
from pathlib import Path
import time
import uuid

from . import contributions as c
from .evaluation import canonical, digest, private_write, text_units

VERSION = "ongoing-snippet-training-v1"
SCOPE = "future-captures-in-this-local-evaluation-store"


def _schema(store):
    c._schema(store)
    store.conn.execute("""CREATE TABLE IF NOT EXISTS contribution_permission (
        id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
        generation TEXT NOT NULL, since REAL NOT NULL, version TEXT NOT NULL,
        nonce TEXT NOT NULL)""")
    store.conn.execute("""CREATE TABLE IF NOT EXISTS automatic_proposals (
        proposal_id TEXT PRIMARY KEY REFERENCES contribution_proposals(id) ON DELETE CASCADE,
        generation TEXT NOT NULL)""")


def status(store):
    _schema(store)
    row = store.conn.execute("SELECT * FROM contribution_permission WHERE id=1").fetchone()
    return {"mode": "ongoing" if row and row["enabled"] and row["version"] == VERSION else "per-snippet",
            "purpose": c.PURPOSE, "scope": SCOPE, "version": VERSION,
            "future_captures_after": row["since"] if row and row["enabled"] else None,
            "uploads_enabled": False}


def settings_page(store):
    _schema(store)
    # Opening the page never enables permission or replaces an existing grant.
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO contribution_permission VALUES(1,0,?,?,?,?)",
                           (str(uuid.uuid4()), time.time(), VERSION, str(uuid.uuid4())))
    row = store.conn.execute("SELECT nonce FROM contribution_permission WHERE id=1").fetchone()
    data = {**status(store), "nonce": row["nonce"]}
    encoded = canonical(data).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    path = store.root / "contribution-settings.html"
    template = Path(__file__).with_name("contribution_settings.html").read_text()
    private_write(path, template.replace("__SETTINGS_DATA__", encoded))
    return path


def import_permission(store, document):
    _schema(store)
    expected = {"nonce", "version", "scope", "purpose", "consent", "rights", "accept_automated_review"}
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("Invalid ongoing permission document")
    if (document["version"] != VERSION or document["scope"] != SCOPE or document["purpose"] != c.PURPOSE
            or any(document[k] is not True for k in ("consent", "rights", "accept_automated_review"))):
        raise ValueError("Explicit ongoing consent, rights and acceptance of automated review are required")
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        row = store.conn.execute("SELECT * FROM contribution_permission WHERE id=1").fetchone()
        if not row or document["nonce"] != row["nonce"]:
            raise ValueError("Permission page is stale or belongs to a different store; reopen settings")
        if row["enabled"] and row["version"] == VERSION:
            raise ValueError("Ongoing permission is already enabled")
        store.conn.execute("UPDATE contribution_permission SET enabled=1,generation=?,since=?,version=?,nonce=? WHERE id=1",
                           (str(uuid.uuid4()), time.time(), VERSION, str(uuid.uuid4())))
    return status(store)


def disable(store):
    _schema(store)
    # The permission change wins before file cleanup. Re-enabling issues a new
    # generation and cutoff, so earlier proposals cannot revive.
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        store.conn.execute("UPDATE contribution_permission SET enabled=0,generation=?,nonce=? WHERE id=1",
                           (str(uuid.uuid4()), str(uuid.uuid4())))
    directory = store.root / "contributions"
    if directory.is_symlink():
        raise ValueError("Permission disabled; refusing symlink contribution directory")
    for row in store.conn.execute("SELECT proposal_id FROM automatic_proposals"):
        if not c._uuid(row[0]):
            raise ValueError("Permission disabled; invalid stored proposal ID")
        (directory / (row[0] + ".bundle.json")).unlink(missing_ok=True)
    return {**status(store), "previously_delivered_copies_recalled": False}


def require_grant(store, capture_id, generation=None):
    _schema(store)
    row = store.conn.execute("SELECT * FROM contribution_permission WHERE id=1").fetchone()
    if not row or not row["enabled"] or row["version"] != VERSION:
        raise ValueError("Ongoing contribution permission is off or outdated")
    if generation is not None and row["generation"] != generation:
        raise ValueError("Ongoing contribution permission changed")
    capture = store.conn.execute("SELECT created,expires FROM captures WHERE id=?", (capture_id,)).fetchone()
    if not capture or capture["created"] <= row["since"] or capture["expires"] <= time.time():
        raise ValueError("Ongoing permission covers only new, unexpired captures after opt-in")
    return dict(row)


def authorization(store, proposal, payload):
    _schema(store)
    row = store.conn.execute("SELECT generation FROM automatic_proposals WHERE proposal_id=?", (proposal["id"],)).fetchone()
    if not row:
        raise ValueError("Automatic proposal has no ongoing grant")
    grant = require_grant(store, proposal["capture_id"], row["generation"])
    return {"mode": "ongoing", "consent_version": VERSION, "purpose": c.PURPOSE,
            "scope": SCOPE, "grant_id": grant["generation"], "payload_sha256": digest(payload),
            "consent": True, "rights": True, "reviewed_context": False, "reviewed_labels": False}


def validate_authorization(approval, payload):
    keys = {"mode", "consent_version", "purpose", "scope", "grant_id", "payload_sha256", *c.ATTESTATIONS}
    if (not isinstance(approval, dict) or set(approval) != keys
            or approval["mode"] != "ongoing" or approval["consent_version"] != VERSION
            or approval["purpose"] != c.PURPOSE or approval["scope"] != SCOPE
            or not c._uuid(approval["grant_id"]) or approval["payload_sha256"] != digest(payload)
            or approval["consent"] is not True or approval["rights"] is not True
            or approval["reviewed_context"] is not False or approval["reviewed_labels"] is not False):
        raise ValueError("Invalid ongoing authorization; permission is not human review")


def _windows(capture):
    """Small context windows, expanded only to avoid cutting annotated spans."""
    units = text_units(capture["original"])
    windows = set()
    for label in capture["annotations"]:
        unit = label["unit"]
        start, end = max(0, label["start"] - 60), min(len(units[unit]), label["end"] + 60)
        while True:
            intersecting = [a for a in capture["annotations"] if a["unit"] == unit and a["start"] < end and a["end"] > start]
            expanded = (min(start, *(a["start"] for a in intersecting)), max(end, *(a["end"] for a in intersecting)))
            if expanded == (start, end):
                break
            start, end = expanded
        windows.add((unit, start, end))
    merged = []
    for unit, start, end in sorted(windows):
        if (merged and merged[-1][0] == unit and start <= merged[-1][2]
                and max(end, merged[-1][2]) - merged[-1][1] <= c.MAX_CHARS):
            merged[-1] = (unit, merged[-1][1], max(end, merged[-1][2]))
        else:
            merged.append((unit, start, end))
    return merged


def process(store, limit=5):
    """Bounded local batch command, suitable for an existing automation runner.

    Labels must already exist. Uncertain/unsupported matches remain local.
    No hosted upload, background enrollment, or automatic gold labels.
    """
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Process limit must be between 1 and 5")
    state = status(store)
    if state["mode"] != "ongoing":
        raise ValueError("Ongoing contribution permission is off")
    results = []
    for entry in store.list():
        if entry["created"] <= state["future_captures_after"]:
            continue
        capture = store.get(entry["id"])
        for unit, start, end in _windows(capture):
            old = store.conn.execute("""SELECT p.id,p.state FROM contribution_proposals p
                JOIN automatic_proposals a ON a.proposal_id=p.id
                WHERE p.capture_id=? AND p.revision=? AND p.unit=? AND p.start=? AND p.end=?
                ORDER BY p.rowid DESC LIMIT 1""", (entry["id"], capture["revision"], unit, start, end)).fetchone()
            if old and old["state"] == "withdrawn":
                continue
            if old and (store.root / "contributions" / (old["id"] + ".bundle.json")).exists():
                continue
            if len(results) >= limit:
                return {"results": results, "uploaded": False}
            proposal_id = old["id"] if old else None
            try:
                if not proposal_id:
                    proposal_id, _ = c.prepare(store, entry["id"], unit, start, end, automatic=True)
                path = c.export_bundle(store, proposal_id)
                results.append({"status": "exported-unreviewed", "proposal_id": proposal_id, "local_bundle": str(path)})
            except ValueError:
                # Do not echo scanner/source content. Fail closed, keep candidate
                # local, and let the normal review tools resolve its labels.
                results.append({"status": "held-for-review", "capture_id": entry["id"], "proposal_id": proposal_id})
                if status(store)["mode"] != "ongoing":
                    break
        if status(store)["mode"] != "ongoing":
            break
    return {"results": results, "uploaded": False}
