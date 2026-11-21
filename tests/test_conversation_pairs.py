import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawjournal.redaction import conversation_pairs as pairs, pair_contract as c
from clawjournal.redaction.evaluation import Store
from test_redaction_contributions import store, enable_ongoing


@pytest.fixture(autouse=True)
def confirmed_projects(monkeypatch):
    from clawjournal import config
    monkeypatch.setattr(config, "load_config", lambda: {"projects_confirmed": True, "excluded_projects": []})


def permission(store):
    pairs.settings_page(store, "https://research.example.org")
    row = store.conn.execute("SELECT * FROM pair_permission").fetchone()
    return {"nonce": row["nonce"], "version": c.TERMS_VERSION, "purpose": c.PURPOSE,
            "destination": row["destination"], "scope": c.SCOPE, "retention_days": 30,
            "rights": True, "share_original": True, "share_redacted": True}


def capture(store):
    return store.capture({"messages": [{"role": "user", "content": "Email fictional@example.org; loss 0.123"}]},
        group="fixture", split="dev", source_kind="synthetic", provenance="Fictional fixture",
        preview=lambda s: ({**s, "messages": [{"role": "user", "content": "Email [EMAIL]; loss 0.123"}]}, {}))


def test_snippet_consent_never_includes_originals(store):
    enable_ongoing(store)
    cid = capture(store)
    assert pairs.status(store)["enabled"] is False
    with pytest.raises(ValueError, match="permission is off"):
        pairs.export_pair(store, cid)


@pytest.mark.parametrize("field", ["rights", "share_original", "share_redacted"])
@pytest.mark.parametrize("value", [False, 1, "true"])
def test_separate_pair_permission_requires_exact_bools(store, field, value):
    document = permission(store); document[field] = value
    with pytest.raises(ValueError, match="Explicit"):
        pairs.enable(store, document)
    assert not pairs.status(store)["enabled"]


def test_old_captures_wrong_purpose_and_replayed_consent_rejected(store):
    cid = capture(store)
    doc = permission(store)
    with pytest.raises(ValueError, match="Explicit"):
        pairs.enable(store, {**doc, "purpose": "General research"})
    pairs.enable(store, doc)
    with pytest.raises(ValueError, match="only new"):
        pairs.export_pair(store, cid)
    pairs.disable(store)
    with pytest.raises(ValueError, match="stale"):
        pairs.enable(store, doc)


def test_full_conversations_paired_hashes_preserved_without_label_notes(store, monkeypatch):
    pairs.enable(store, permission(store))
    cid = capture(store)
    monkeypatch.setattr(pairs, "_scan", lambda *args: None)
    path = pairs.export_pair(store, cid)
    bundle = c.validate(json.loads(path.read_text()))
    assert bundle["original"] == store.get(cid)["original"]
    assert bundle["redacted"] == store.get(cid)["redacted"]
    assert "fictional@example.org" in str(bundle["original"])
    assert "fictional@example.org" not in str(bundle["redacted"])
    assert "only to evaluate and improve the redaction" in bundle["consent"]["purpose"]
    assert pairs.export_pair(store, cid).read_bytes() == path.read_bytes()
    pairs.disable(store)
    assert not path.exists()
    with pytest.raises(ValueError, match="off"):
        pairs.export_pair(store, cid)


def test_disable_during_scanning_and_retention_remove_pairs(store, monkeypatch):
    pairs.enable(store, permission(store)); cid = capture(store)
    monkeypatch.setattr(pairs, "_scan", lambda *args: pairs.disable(store))
    with pytest.raises(ValueError, match="off"):
        pairs.export_pair(store, cid)
    assert not list((store.root / "pairs").glob("*.json"))


def test_capture_delete_removes_pair_file(store, monkeypatch):
    pairs.enable(store, permission(store)); cid = capture(store)
    monkeypatch.setattr(pairs, "_scan", lambda *args: None)
    path = pairs.export_pair(store, cid)
    store.delete(cid)
    assert not path.exists()


def test_original_is_only_scanned_locally_and_secrets_block(store, monkeypatch):
    from clawjournal.redaction import betterleaks
    seen = []
    monkeypatch.setattr(betterleaks, "scan_text", lambda text: SimpleNamespace(bypassed=False,binary_missing=False,scan_error=None,findings=[]))
    monkeypatch.setattr(pairs, "_scan_candidate", lambda candidate, directory: seen.append(candidate))
    pairs._scan({"messages": ["fictional@example.org"]}, {"messages": ["[EMAIL]"]}, "fixture", store.root)
    assert "fictional@example.org" not in json.dumps(seen)
    monkeypatch.setattr(betterleaks, "scan_text", lambda text: SimpleNamespace(bypassed=False,binary_missing=False,scan_error=None,findings=["possible-secret"]))
    with pytest.raises(ValueError, match="possible secrets"):
        pairs._scan({}, {}, "fixture", store.root)


def test_original_scan_ignores_placeholders_but_blocks_credentials(monkeypatch):
    from clawjournal.redaction import betterleaks
    monkeypatch.setattr(betterleaks, "scan_text", lambda text: SimpleNamespace(bypassed=False,binary_missing=False,scan_error=None,findings=[]))
    # JSON escaping once joined the placeholder to the next line and read as a secret.
    pairs._scan_original({"messages": [{"content": 'token = [REDACTED]\nprint("done")'}]})
    with pytest.raises(ValueError, match="Possible credentials"):
        pairs._scan_original({"messages": [{"tool_uses": [{"input": {"cmd": 'TOKEN="hf_' + "A1b2C3d4" * 5 + '"'}}]}]})


def test_upload_uses_exact_destination_and_validates_receipt(store, monkeypatch):
    pairs.enable(store, permission(store)); cid = capture(store)
    monkeypatch.setattr(pairs, "_scan", lambda *args: None)
    token_file = store.root / "token"; token_file.write_text("fake-verified-token")
    sent = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            return json.dumps({"bundle_id": json.loads(sent[-1].data)["bundle_id"], "status": "accepted"}).encode()
    class Opener:
        def open(self, request, timeout): sent.append(request); return Response()
    monkeypatch.setattr(pairs.urllib.request, "build_opener", lambda *args: Opener())
    result = pairs.upload(store, cid, token_file)
    assert result["status"] == "accepted"
    assert sent[0].full_url == "https://research.example.org/api/redaction-pairs"
    assert json.loads(sent[0].data)["original"] == store.get(cid)["original"]
    assert sent[0].get_header("Authorization") == "Bearer fake-verified-token"


class Receipt:
    def __init__(self, receipt): self.receipt = receipt
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size): return json.dumps(self.receipt).encode()


def test_hosted_withdrawal_survives_local_capture_deletion(store, monkeypatch):
    pairs.enable(store, permission(store)); cid = capture(store)
    monkeypatch.setattr(pairs, "_scan", lambda *args: None)
    token_file = store.root / "token"; token_file.write_text("fake-verified-token")
    sent = []
    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            if request.full_url.endswith("/withdraw"):
                return Receipt({"bundle_id": request.full_url.split("/")[-2], "status": "withdrawn"})
            return Receipt({"bundle_id": json.loads(request.data)["bundle_id"], "status": "accepted"})
    monkeypatch.setattr(pairs.urllib.request, "build_opener", lambda *args: Opener())
    bundle_id = pairs.upload(store, cid, token_file)["bundle_id"]
    store.delete(cid)
    assert store.conn.execute("SELECT COUNT(*) FROM conversation_pairs").fetchone()[0] == 0
    assert pairs.withdraw_hosted(store, bundle_id, token_file)["status"] == "withdrawn"
    assert sent[-1].full_url == f"https://research.example.org/api/redaction-pairs/{bundle_id}/withdraw"


def test_upload_does_not_hold_the_store_lock_while_sending(store, monkeypatch):
    pairs.enable(store, permission(store)); cid = capture(store)
    monkeypatch.setattr(pairs, "_scan", lambda *args: None)
    token_file = store.root / "token"; token_file.write_text("fake-verified-token")
    class Opener:
        def open(self, request, timeout):
            other = Store(store.root)  # e.g. list/status/review, or Disable
            try:
                pairs.disable(other)
            finally:
                other.close()
            return Receipt({"bundle_id": json.loads(request.data)["bundle_id"], "status": "accepted"})
    monkeypatch.setattr(pairs.urllib.request, "build_opener", lambda *args: Opener())
    bundle_id = pairs.upload(store, cid, token_file)["bundle_id"]
    assert not pairs.status(store)["enabled"]
    # Admitted before Disable, so it stays withdrawable.
    assert store.conn.execute("SELECT destination FROM hosted_pairs WHERE id=?",
                              (bundle_id,)).fetchone()[0] == "https://research.example.org"


def test_pair_transport_refuses_redirects():
    assert pairs._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.org") is None


def test_pair_export_cannot_bypass_project_confirmation_or_holds(store, monkeypatch):
    from clawjournal import config
    monkeypatch.setattr(config, "load_config", lambda: {"projects_confirmed": False})
    with pytest.raises(ValueError, match="Confirm project"):
        pairs._check_controls({})
    monkeypatch.setattr(config, "load_config", lambda: {"projects_confirmed": True, "excluded_projects": ["private"]})
    with pytest.raises(ValueError, match="excluded"):
        pairs._check_controls({"source": "claude", "project": "private"})
    with pytest.raises(ValueError, match="held"):
        pairs._check_controls({"hold_state": "pending_review"})


def test_pair_export_honors_workbench_exclude_project_policies(monkeypatch):
    from clawjournal import config
    from clawjournal.workbench import index
    monkeypatch.setattr(index, "CONFIG_DIR", config.CONFIG_DIR)
    monkeypatch.setattr(index, "INDEX_DB", config.CONFIG_DIR / "index.db")
    monkeypatch.setattr(index, "BLOBS_DIR", config.CONFIG_DIR / "blobs")
    conn = index.open_index()
    try:
        index.add_policy(conn, "exclude_project", "private-client")
    finally:
        conn.close()
    session = {"source": "claude", "project": "private-client", "session_id": "not-indexed"}
    with pytest.raises(ValueError, match="excluded"):
        pairs._check_controls(session)
    pairs._check_controls({**session, "project": "other-project"})
