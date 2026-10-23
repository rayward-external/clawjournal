import json
import pytest

from clawjournal.workbench import index, upload_originals as originals
from clawjournal.redaction import conversation_pairs
from test_review_snapshots import conn, trace, preview, create

SETTINGS = {"projects_confirmed": True, "source_filter": ["codex"]}


def selected(conn):
    index.upsert_sessions(conn, [trace()])
    index.set_hold_state(conn, "test-trace", "released", changed_by="test")
    return create(conn, preview(conn))


def test_original_preview_is_the_reviewed_revision_without_private_metadata(conn):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    assert document["sessions"] == [{"session_id": "test-trace", "messages": trace()["messages"]}]
    assert set(document) == {"schema", "sessions"}


def test_checked_export_pairs_exact_final_redaction_and_scans_both_sides(conn, monkeypatch):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    calls = []
    monkeypatch.setattr(conversation_pairs, "_scan", lambda original, redacted, *args: calls.append((original, redacted)))
    final = {"session_id": "test-trace", "messages": [{"role": "user", "content": "[EMAIL]"}],
             "revision_hash": index.compute_content_revision(trace())}
    raw = originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document), redacted=[final])
    assert json.loads(raw) == document
    assert calls == [(document["sessions"][0], final)]


@pytest.mark.parametrize("problem", ["consent", "revision", "selection", "hold", "projects"])
def test_original_export_refuses_stale_or_unapproved_data(conn, monkeypatch, problem):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    final = {"session_id": "test-trace", "revision_hash": index.compute_content_revision(trace())}
    settings = dict(SETTINGS)
    approved = originals.digest(document)
    if problem == "consent": approved = ""
    if problem == "revision": final["revision_hash"] = "changed"
    if problem == "selection": final["session_id"] = "different"
    if problem == "hold": index.set_hold_state(conn, "test-trace", "pending_review", changed_by="test")
    if problem == "projects": settings["projects_confirmed"] = False
    def unexpected(*args): raise AssertionError("Must reject before scans or egress")
    monkeypatch.setattr(conversation_pairs, "_scan", unexpected)
    with pytest.raises(ValueError):
        originals.checked_bytes(conn, share_id, settings, approved_digest=approved, redacted=[final])


def test_missing_original_scanner_fails_closed(conn, monkeypatch):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    # Test fixtures intentionally bypass scanners. Original sharing must refuse it.
    with pytest.raises(ValueError, match="scanner|secret",):
        originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document),
            redacted=[{"session_id": "test-trace", "messages": [], "revision_hash": index.compute_content_revision(trace())}])


def test_hold_during_scanning_blocks_original_download(conn, monkeypatch):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    def hold(*args):
        index.set_hold_state(conn, "test-trace", "pending_review", changed_by="test")
    monkeypatch.setattr(conversation_pairs, "_scan", hold)
    with pytest.raises(ValueError, match="blocked"):
        originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document),
            redacted=[{"session_id": "test-trace", "messages": [], "revision_hash": index.compute_content_revision(trace())}])
