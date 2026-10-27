import json
import pytest

from clawjournal.workbench import index, upload_originals as originals
from clawjournal.redaction import conversation_pairs
from test_review_snapshots import conn, trace, preview, create

SETTINGS = {"projects_confirmed": True, "source_filter": ["codex"]}


def selected(conn, *content):
    index.upsert_sessions(conn, [trace(*content)])
    index.set_hold_state(conn, "test-trace", "released", changed_by="test")
    return create(conn, preview(conn))


def test_original_preview_is_the_reviewed_revision_without_private_metadata(conn):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    assert document["sessions"] == [{"session_id": "test-trace", "messages": trace()["messages"]}]
    assert set(document) == {"schema", "sessions"}


def test_checked_export_pairs_exact_final_redaction_and_scans_the_original(conn, monkeypatch):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    calls = []
    monkeypatch.setattr(conversation_pairs, "_scan_original", calls.append)
    def regated(*args): raise AssertionError("The redacted ZIP already passed the share gate")
    monkeypatch.setattr(conversation_pairs, "_scan_candidate", regated)
    final = {"session_id": "test-trace", "messages": [{"role": "user", "content": "[EMAIL]"}],
             "revision_hash": index.compute_content_revision(trace())}
    raw = originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document), redacted=[final])
    assert json.loads(raw) == document
    assert calls == [document["sessions"][0]]


@pytest.mark.parametrize("content, shared", [
    ('token = [REDACTED]\nprint("done")', True),
    ('Accuracy: 0.987; λ=1\nPath: C:\\research\\results.csv', True),
    ('token = "hf_' + 'A1b2C3d4' * 5 + '"\n# Invented test credential', False),
])
def test_original_scan_reads_message_text_not_json_escapes(conn, monkeypatch, content, shared):
    from types import SimpleNamespace
    from clawjournal.redaction import betterleaks
    monkeypatch.setattr(betterleaks, "scan_text", lambda text: SimpleNamespace(
        bypassed=False, binary_missing=False, scan_error=None, findings=[]))
    share_id = selected(conn, content)
    document = originals.prepare(conn, share_id, SETTINGS)
    final = {"session_id": "test-trace", "messages": [{"role": "user", "content": "[REDACTED]"}],
             "revision_hash": index.compute_content_revision(trace(content))}
    check = lambda: originals.checked_bytes(conn, share_id, SETTINGS,
        approved_digest=originals.digest(document), redacted=[final])
    if shared:
        assert json.loads(check())["sessions"][0]["messages"][0]["content"] == content
    else:
        with pytest.raises(ValueError, match="Possible credentials"):
            check()


def test_combined_size_matches_the_hosted_limit(conn, monkeypatch):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    # The originals alone fit; together with their redacted versions they do not.
    monkeypatch.setattr(originals, "MAX_BYTES", len(originals.canonical(document).encode()))
    def unexpected(*args): raise AssertionError("Must reject before scans")
    monkeypatch.setattr(conversation_pairs, "_scan_original", unexpected)
    final = {"session_id": "test-trace", "messages": [{"role": "user", "content": "[REDACTED] " * 200}],
             "revision_hash": index.compute_content_revision(trace())}
    with pytest.raises(ValueError, match="together exceed"):
        originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document), redacted=[final])


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
    monkeypatch.setattr(conversation_pairs, "_scan_original", unexpected)
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
    monkeypatch.setattr(conversation_pairs, "_scan_original", hold)
    with pytest.raises(ValueError, match="blocked"):
        originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document),
            redacted=[{"session_id": "test-trace", "messages": [], "revision_hash": index.compute_content_revision(trace())}])


@pytest.mark.parametrize("changed", [{"projects_confirmed": False}, {"source_filter": ["claude"]}, {"excluded_projects": ["synthetic-project"]}])
def test_changed_config_during_scanning_blocks_original_export(conn, monkeypatch, changed):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    monkeypatch.setattr(conversation_pairs, "_scan_original", lambda *args: None)
    with pytest.raises(ValueError):
        originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document),
            redacted=[{"session_id": "test-trace", "messages": [], "revision_hash": index.compute_content_revision(trace())}],
            current_settings=lambda: {**SETTINGS, **changed})
