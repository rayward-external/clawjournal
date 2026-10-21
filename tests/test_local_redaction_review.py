from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import re
import sqlite3
from threading import Thread

import pytest

from clawjournal.redaction import local_review, conversation_pairs as pairs
from clawjournal.redaction import ongoing_contributions as ongoing
from clawjournal.redaction.evaluation import Store
from clawjournal.workbench.daemon import WorkbenchHandler
from test_redaction_contributions import capture, prepare, approval


@pytest.fixture
def review_server(tmp_path, monkeypatch):
    from clawjournal import config
    from clawjournal.workbench import index, daemon
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "profile")
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "profile" / "index.db")
    daemon._open_request_admission()
    store = Store(tmp_path / "evaluation", initialize=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), WorkbenchHandler)
    server._redaction_eval_store = store.root
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = local_review._token()
    host = f"127.0.0.1:{server.server_address[1]}"

    def request(path, body=None, *, auth=True, origin=True, host_override=None):
        conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        headers = {"Host": host_override or host}
        if auth:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = "http://" + host if origin is True else origin
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request("POST" if body is not None else "GET", path,
                     json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    yield store, request, token
    server.shutdown()
    server.server_close()
    thread.join()
    store.close()


def data(html, element):
    return json.loads(re.search(r'id="' + element + r'"[^>]*>(.*?)</script>', html, re.S).group(1))


def test_shell_and_originals_require_local_origin_and_api_auth(review_server):
    store, request, token = review_server
    cid = capture(store)
    shell = request("/redaction-eval", auth=False)
    assert shell[0] == 200 and "alice@private.invalid" not in shell[2]
    assert shell[1]["Cache-Control"] == "no-store"
    page = f"/api/redaction-eval/page?kind=review&id={cid}"
    assert request(page, auth=False)[0] == 401
    assert request(page, origin="https://other.example")[0] == 403
    assert request("/redaction-eval", host_override="attacker.example")[0] == 403
    response = request(page)
    assert response[0] == 200 and "alice@private.invalid" in response[2]
    assert "connect-src 'self'" in response[2]
    assert token not in (store.root / "reviews" / f"{cid}.html").read_text()


def test_pair_consent_is_validated_persisted_and_replay_protected(review_server):
    store, request, _ = review_server
    html = request("/api/redaction-eval/page?kind=pairs")[2]
    document = data(html, "pair-data")
    document.update(rights=True, share_original=True, share_redacted=True)
    endpoint = "/api/redaction-eval/pairs-consent"
    assert request(endpoint, document, auth=False)[0] == 401
    assert request(endpoint, document, origin=False)[0] == 403
    assert request(endpoint, document, origin="null")[0] == 403
    bad = {**document, "share_original": "true"}
    assert request(endpoint, bad)[0] == 400
    assert not pairs.status(store)["enabled"]
    assert request(endpoint, document)[0] == 200
    assert "pairEnabled:true" in request("/api/redaction-eval/page?kind=pairs")[2]
    with_store = Store(store.root)
    try:
        assert pairs.status(with_store)["enabled"]
    finally:
        with_store.close()
    assert request(endpoint, document)[0] == 400
    pairs.disable(store)
    assert request(endpoint, document)[0] == 400
    assert not pairs.status(store)["enabled"]


def test_corrections_save_revisions_and_reject_stale_page(review_server):
    store, request, _ = review_server
    cid = capture(store)
    page = request(f"/api/redaction-eval/page?kind=review&id={cid}")[2]
    current = data(page, "capture-data")
    doc = {k: current[k] for k in ("capture_id", "pair_hash", "revision", "policy", "annotations")}
    doc["annotations"][0]["note"] = "Local test correction"
    response = request("/api/redaction-eval/corrections", doc)
    assert response[0] == 200
    result = json.loads(response[2])
    assert result["saved"] and result["revision"] == doc["revision"] + 1
    assert store.get(cid)["annotations"][0]["note"] == "Local test correction"
    assert request("/api/redaction-eval/corrections", doc)[0] == 400
    doc["revision"] = result["revision"]
    doc["annotations"][0]["note"] = "Second saved correction"
    assert request("/api/redaction-eval/corrections", doc)[0] == 200
    assert store.get(cid)["revision"] == result["revision"] + 1


def test_ongoing_and_manual_approval_use_existing_validators(review_server):
    store, request, _ = review_server
    page = request("/api/redaction-eval/page?kind=ongoing")[2]
    current = data(page, "settings-data")
    grant = {k: current[k] for k in ("nonce", "version", "scope", "purpose")}
    grant.update(rights=True, accept_automated_review=True, consent=True)
    assert request("/api/redaction-eval/ongoing-consent", grant)[0] == 200
    assert ongoing.status(store)["mode"] == "ongoing"
    _, pid = prepare(store)
    document = approval(store, pid)
    assert request("/api/redaction-eval/snippet-approve", {**document, "reviewed_labels": False})[0] == 400
    assert request("/api/redaction-eval/snippet-approve", document)[0] == 200
    assert store.conn.execute("SELECT state FROM contribution_proposals WHERE id=?", (pid,)).fetchone()[0] == "approved"
    assert request("/api/redaction-eval/snippet-withdraw", {"proposal_id": pid})[0] == 200
    assert request("/api/redaction-eval/snippet-approve", document)[0] == 400


def test_storage_failure_never_returns_saved_or_echoes_private_errors(review_server, monkeypatch):
    _, request, _ = review_server
    def unavailable(*args):
        raise sqlite3.OperationalError("private-content-must-not-appear")
    monkeypatch.setattr(local_review, "_save", unavailable)
    status, _, raw = request("/api/redaction-eval/corrections", {})
    assert status == 503 and "private-content" not in raw and '"saved":true' not in raw
