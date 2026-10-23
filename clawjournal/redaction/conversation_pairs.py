"""Separate opt-in original/redacted pairs, solely for redaction improvement."""
import json
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request
import uuid

from . import pair_contract as contract
from .contributions import _directory, _scan_candidate, contributor_profile
from .evaluation import private_write


def schema(store):
    store.conn.execute("""CREATE TABLE IF NOT EXISTS pair_permission (
        id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER, nonce TEXT,
        generation TEXT, since REAL, destination TEXT, version TEXT)""")
    store.conn.execute("""CREATE TABLE IF NOT EXISTS conversation_pairs (
        id TEXT PRIMARY KEY, capture_id TEXT REFERENCES captures(id) ON DELETE CASCADE,
        generation TEXT, revision INTEGER, destination TEXT NOT NULL,
        UNIQUE(capture_id,generation,revision))""")
    # Pairs admitted for upload. No capture reference: hosted withdrawal must
    # outlive local deletion and retention. Holds IDs and destinations only.
    store.conn.execute("""CREATE TABLE IF NOT EXISTS hosted_pairs (
        id TEXT PRIMARY KEY, destination TEXT NOT NULL, admitted REAL NOT NULL)""")


def status(store):
    schema(store)
    row = store.conn.execute("SELECT * FROM pair_permission WHERE id=1").fetchone()
    return {"enabled": bool(row and row["enabled"] and row["version"] == contract.TERMS_VERSION),
            "purpose": contract.PURPOSE, "destination": row["destination"] if row else None,
            "retention_days": contract.RETENTION_DAYS}


def settings_page(store, destination="https://data.rayward.ai"):
    destination = contract.origin(destination)
    schema(store)
    with store.conn:
        row = store.conn.execute("SELECT * FROM pair_permission WHERE id=1").fetchone()
        if row and row["enabled"] and row["destination"] != destination:
            raise ValueError("Disable paired sharing before choosing another destination")
        if not row or not row["enabled"]:
            store.conn.execute("INSERT OR REPLACE INTO pair_permission VALUES(1,0,?,?,0,?,?)",
                (str(uuid.uuid4()), str(uuid.uuid4()), destination, contract.TERMS_VERSION))
    row = store.conn.execute("SELECT * FROM pair_permission WHERE id=1").fetchone()
    data = {"nonce": row["nonce"], "version": contract.TERMS_VERSION, "purpose": contract.PURPOSE,
            "destination": destination, "scope": contract.SCOPE, "retention_days": contract.RETENTION_DAYS}
    template = Path(__file__).with_name("pair_settings.html").read_text()
    encoded = contract.canonical(data).replace("<", "\\u003c")
    path = store.root / "pair-settings.html"
    private_write(path, template.replace("__PAIR_DATA__", encoded))
    return path


def enable(store, document):
    schema(store)
    expected = {"nonce", "version", "purpose", "destination", "scope", "retention_days", "rights", "share_original", "share_redacted"}
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("Separate original-conversation consent is required")
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        row = store.conn.execute("SELECT * FROM pair_permission WHERE id=1").fetchone()
        if (not row or row["enabled"] or document["nonce"] != row["nonce"]
                or document["destination"] != row["destination"]):
            raise ValueError("Paired permission is stale or already enabled; reopen settings")
        if (document["version"] != contract.TERMS_VERSION or document["purpose"] != contract.PURPOSE
                or document["scope"] != contract.SCOPE or type(document["retention_days"]) is not int
                or document["retention_days"] != contract.RETENTION_DAYS
                or any(document[k] is not True for k in ("rights", "share_original", "share_redacted"))):
            raise ValueError("Explicit permission for originals solely for redaction improvement is required")
        store.conn.execute("UPDATE pair_permission SET enabled=1,since=?,generation=?,nonce=?,version=? WHERE id=1",
            (time.time(), str(uuid.uuid4()), str(uuid.uuid4()), contract.TERMS_VERSION))
    return status(store)


def disable(store):
    schema(store)
    with store.conn:
        store.conn.execute("UPDATE pair_permission SET enabled=0,generation=?,nonce=? WHERE id=1",
                           (str(uuid.uuid4()), str(uuid.uuid4())))
    for row in store.conn.execute("SELECT DISTINCT capture_id FROM conversation_pairs"):
        purge_capture(store, row[0])
    return {**status(store), "delivered_copies_recalled": False}


def purge_capture(store, capture_id):
    if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_pairs'").fetchone():
        return
    directory = store.root / "pairs"
    if directory.is_symlink():
        raise ValueError("Refusing symlink pair directory")
    for row in store.conn.execute("SELECT id FROM conversation_pairs WHERE capture_id=?", (capture_id,)):
        if not contract.is_uuid(row[0]):
            raise ValueError("Invalid stored pair ID")
        (directory / (row[0] + ".pair.json")).unlink(missing_ok=True)


def grant(store, capture_id):
    schema(store)
    row = store.conn.execute("SELECT * FROM pair_permission WHERE id=1").fetchone()
    capture = store.conn.execute("SELECT created,expires FROM captures WHERE id=?", (capture_id,)).fetchone()
    if not row or not row["enabled"] or row["version"] != contract.TERMS_VERSION:
        raise ValueError("Original-conversation permission is off or outdated")
    if not capture or capture["created"] <= row["since"] or capture["expires"] <= time.time():
        raise ValueError("Original sharing covers only new, unexpired captures after this opt-in")
    return dict(row)


def _scan_original(original):
    from . import betterleaks
    from .secrets import scan_text
    # Originals never enter online credential verification or an AI backend.
    raw = contract.canonical(original)
    report = betterleaks.scan_text(raw)
    if report.bypassed or report.binary_missing or report.scan_error or report.findings:
        raise ValueError("Original contains possible secrets or local scanner is unavailable")
    if any(m["type"] not in {"email", "ip_address", "private_url"} for m in scan_text(raw)):
        raise ValueError("Possible credentials in original; this conversation cannot be shared")


def _scan(original, redacted, pair_id, directory):
    _scan_original(original)
    _scan_candidate({"example": {"contribution_id": pair_id}, "redacted": redacted}, directory)


def _check_controls(original):
    from .. import config
    from ..workbench import index
    settings = config.load_config()
    if settings.get("projects_confirmed") is not True:
        raise ValueError("Confirm project exclusions with config --confirm-projects before paired export")
    path = config.CONFIG_DIR / "index.db"
    conn = None
    try:
        if path.exists():
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            # Same merge as the share path: config exclusions plus workbench
            # exclude_project policies, normalized to stored project names.
            excluded = index.get_effective_share_settings(conn, settings)["excluded_projects"]
        else:
            excluded = config.normalize_excluded_project_names(list(settings.get("excluded_projects") or []))
        if index.session_matches_excluded_projects(original, excluded):
            raise ValueError("Conversation belongs to an excluded project")
        if original.get("review_status") == "blocked" or ("hold_state" in original and
                index.effective_hold_state(original["hold_state"], original.get("embargo_until")) not in {"released", "auto_redacted"}):
            raise ValueError("Conversation is held or blocked")
        if conn is None:
            return  # Explicitly captured imports may have no indexed session.
        session_id = original.get("session_id")
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row:
            selected, family = index._active_family_controls_for_session_ids(conn, [session_id])
            logical_id = selected[session_id]["logical_session_id"]
            if (index.release_gate_blockers(conn, [session_id]) or family.get(logical_id, (None,))[0] == "blocked"
                    or index.session_matches_excluded_projects(dict(row), excluded)):
                raise ValueError("Indexed conversation is held, blocked, or excluded")
    except sqlite3.Error:
        raise ValueError("Cannot verify current conversation sharing controls") from None
    finally:
        if conn is not None:
            conn.close()


def export_pair(store, capture_id):
    permission = grant(store, capture_id)
    capture = store.get(capture_id)
    _check_controls(capture["original"])
    profile = contributor_profile(capture["split"])
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO conversation_pairs VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), capture_id, permission["generation"], capture["revision"], permission["destination"]))
    pair_id = store.conn.execute("SELECT id FROM conversation_pairs WHERE capture_id=? AND generation=? AND revision=?",
        (capture_id, permission["generation"], capture["revision"])).fetchone()[0]
    consent = {"version": contract.TERMS_VERSION, "purpose": contract.PURPOSE,
        "destination": permission["destination"], "scope": contract.SCOPE,
        "retention_days": contract.RETENTION_DAYS, "grant_id": permission["generation"],
        "rights": True, "share_original": True, "share_redacted": True}
    annotation_keys = ("unit", "start", "end", "decision", "entity_type", "review_status")
    payload = {"schema": contract.SCHEMA, "bundle_id": pair_id, "contributor_id": profile["contributor"],
        "split": profile["split"], "original": capture["original"], "redacted": capture["redacted"],
        "annotations": [{k: a[k] for k in annotation_keys} for a in capture["annotations"]],
        "redaction_stage": "local-preview-plus-final-secret-gate", "consent": consent,
        "original_sha256": contract.digest(capture["original"]), "redacted_sha256": contract.digest(capture["redacted"])}
    contract.validate(payload)
    directory = _directory(store.root / "pairs")
    _scan(payload["original"], payload["redacted"], pair_id, directory)
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        if grant(store, capture_id) != permission or store.get(capture_id)["revision"] != capture["revision"]:
            raise ValueError("Original-sharing permission or capture changed during scanning")
        _check_controls(capture["original"])
        path = directory / (pair_id + ".pair.json")
        private_write(path, contract.canonical(payload))
    return path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _token(token_file):
    with Path(token_file).open("rb") as stream:
        token = stream.read(8193).decode().strip()
    if not token or len(token) > 8192 or "\n" in token or "\r" in token:
        raise ValueError("Expected a file containing one verified upload token")
    return token


def upload(store, capture_id, token_file):
    path = export_pair(store, capture_id)
    raw = path.read_bytes()
    payload = contract.validate(json.loads(raw))
    token = _token(token_file)
    # Serialize Disable with the final send admission, and record the admitted
    # pair before sending so it stays withdrawable. The lock is released before
    # the request: holding it for the whole send locks out other local commands.
    # No retry/redirect can silently move originals to a different destination.
    with store.conn:
        store.conn.execute("BEGIN IMMEDIATE")
        current = grant(store, capture_id)
        _check_controls(payload["original"])
        saved_pair = store.conn.execute("SELECT revision FROM conversation_pairs WHERE id=?", (payload["bundle_id"],)).fetchone()
        if (current["generation"] != payload["consent"]["grant_id"]
                or current["destination"] != payload["consent"]["destination"]
                or not saved_pair or saved_pair["revision"] != store.get(capture_id)["revision"]):
            raise ValueError("Original-sharing permission changed before upload")
        store.conn.execute("INSERT OR IGNORE INTO hosted_pairs VALUES(?,?,?)",
                           (payload["bundle_id"], current["destination"], time.time()))
    request = urllib.request.Request(current["destination"] + "/api/redaction-pairs",
        data=raw, headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=45) as response:
            result = json.loads(response.read(65537))
    except (OSError, ValueError):
        raise ValueError("Pair upload did not return a confirmed receipt; retry the same pair after checking the service") from None
    if not isinstance(result, dict) or result.get("bundle_id") != payload["bundle_id"] or result.get("status") != "accepted":
        raise ValueError("Pair upload returned an invalid receipt")
    return {"bundle_id": payload["bundle_id"], "status": "accepted", "destination": current["destination"]}


def withdraw_hosted(store, bundle_id, token_file):
    schema(store)
    row = (store.conn.execute("SELECT destination FROM hosted_pairs WHERE id=?", (bundle_id,)).fetchone()
           or store.conn.execute("SELECT destination FROM conversation_pairs WHERE id=?", (bundle_id,)).fetchone())
    if not row or not contract.is_uuid(bundle_id):
        raise ValueError("Unknown hosted pair; contact its service administrator if the local record expired")
    destination = contract.origin(row["destination"])
    request = urllib.request.Request(destination + "/api/redaction-pairs/" + bundle_id + "/withdraw",
        data=b"", headers={"Authorization": "Bearer " + _token(token_file)})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=45) as response:
            result = json.loads(response.read(65537))
        if result.get("bundle_id") != bundle_id or result.get("status") != "withdrawn":
            raise ValueError()
    except (OSError, ValueError, AttributeError):
        raise ValueError("Hosted withdrawal was not confirmed; contact the service administrator") from None
    return {"bundle_id": bundle_id, "status": "withdrawn", "downloaded_copies_recalled": False}
