import json
import pytest

from clawjournal.workbench import index, upload_originals as originals
from clawjournal.redaction import conversation_pairs
from test_review_snapshots import conn, trace, preview, create

SETTINGS = {"projects_confirmed": True, "source_filter": ["codex"]}


def selected(conn, content=None):
    index.upsert_sessions(conn, [trace(content) if content is not None else trace()])
    index.set_hold_state(conn, "test-trace", "released", changed_by="test")
    return create(conn, preview(conn))


def test_original_preview_is_the_reviewed_revision_without_private_metadata(conn):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    assert document["sessions"] == [{"session_id": "test-trace", "messages": trace()["messages"]}]
    assert set(document) == {"schema", "sessions"}


@pytest.mark.parametrize("content", [
    'token = [REDACTED]\nprint("done")',
    'token = "hf_' + 'A1b2C3d4' * 5 + '"\n# Invented test credential',
    'Accuracy: 0.987; λ=1\nPath: C:\\research\\results.csv',
])
def test_checked_export_preserves_approved_original_without_rescanning(conn, monkeypatch, content):
    share_id = selected(conn, content)
    document = originals.prepare(conn, share_id, SETTINGS)
    def regated(*args): raise AssertionError("No additional scan belongs in original packaging")
    monkeypatch.setattr(conversation_pairs, "_scan_original", regated)
    monkeypatch.setattr(conversation_pairs, "_scan_candidate", regated)
    final = {"session_id": "test-trace", "messages": [{"role": "user", "content": "[EMAIL]"}],
             "revision_hash": index.compute_content_revision(trace(content))}
    raw = originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document), redacted=[final])
    assert json.loads(raw) == document
    assert json.loads(raw)["sessions"][0]["messages"][0]["content"] == content
    assert final["messages"] == [{"role": "user", "content": "[EMAIL]"}]


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


def test_hold_during_preparation_blocks_original_download(conn):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    def hold(*args):
        index.set_hold_state(conn, "test-trace", "pending_review", changed_by="test")
        return SETTINGS
    with pytest.raises(ValueError, match="blocked"):
        originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document),
            redacted=[{"session_id": "test-trace", "messages": [], "revision_hash": index.compute_content_revision(trace())}],
            current_settings=hold)


@pytest.mark.parametrize("changed", [{"projects_confirmed": False}, {"source_filter": ["claude"]}, {"excluded_projects": ["synthetic-project"]}])
def test_changed_config_during_preparation_blocks_original_export(conn, changed):
    share_id = selected(conn)
    document = originals.prepare(conn, share_id, SETTINGS)
    with pytest.raises(ValueError):
        originals.checked_bytes(conn, share_id, SETTINGS, approved_digest=originals.digest(document),
            redacted=[{"session_id": "test-trace", "messages": [], "revision_hash": index.compute_content_revision(trace())}],
            current_settings=lambda: {**SETTINGS, **changed})
