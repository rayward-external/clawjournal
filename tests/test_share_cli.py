"""Tests for the interactive share wizard CLI surface."""
import argparse
from types import SimpleNamespace

import pytest

from clawjournal import share_cli


# ---- argument parsing -------------------------------------------------------

def _parse(argv):
    p = argparse.ArgumentParser()
    share_cli.add_share_cli_args(p)
    return p.parse_args(argv)


def test_arg_parsing_defaults():
    a = _parse([])
    assert a.time_range == "today"
    assert a.limit == 40
    assert a.summary is False
    assert a.accept_terms is False
    assert a.certify_ownership is False
    assert a.yes is False
    assert a.indices == []


def test_arg_parsing_flags():
    a = _parse(["--weekly", "--min-failure-value", "4", "--search", "judge",
                "--accept-terms", "--certify-ownership", "3", "7"])
    assert a.time_range == "weekly"
    assert a.min_failure_value == 4
    assert a.search == "judge"
    assert a.accept_terms and a.certify_ownership
    assert a.indices == [3, 7]


def test_codex_claude_shortcuts():
    assert _parse(["--codex"]).source == "codex"
    assert _parse(["--claude"]).source == "claude"


# ---- review must not mutate global review_status (#5) -----------------------

def test_review_does_not_import_update_session():
    import inspect
    src = inspect.getsource(share_cli)
    assert "update_session" not in src, "share wizard must not touch review_status"


def test_review_is_share_local(monkeypatch):
    # assume_yes includes everything and returns the in-memory selection; no DB write.
    recs = [
        {"row": {"session_id": "a", "display_title": "A"}, "status": "clear"},
        {"row": {"session_id": "b", "display_title": "B"}, "status": "review"},
    ]
    included = share_cli.step_review(conn=None, scrubbed=recs, assume_yes=True)
    assert {s["row"]["session_id"] for s in included} == {"a", "b"}


# ---- consent handling (#6): --yes must NOT auto-accept ----------------------

def _consent_doc():
    return {
        "consent_version": "c1", "retention_policy_version": "r1",
        "consent_text": "I understand.", "retention_text": "We keep a hash.",
    }


def test_yes_does_not_auto_accept_consent(monkeypatch):
    monkeypatch.setattr(share_cli, "hosted_destination",
                        lambda: {"can_submit": True, "message": "", "support_contact": None})
    monkeypatch.setattr(share_cli.share_flow, "consent", _consent_doc)

    def _fail_submit(*a, **k):
        raise AssertionError("submit must not be called without explicit consent")
    monkeypatch.setattr(share_cli.share_flow, "submit", _fail_submit)

    args = SimpleNamespace(yes=True, accept_terms=False, certify_ownership=False)
    result = share_cli.step_submit(conn=None, settings={}, share_id="s1",
                                   package_ai=False, args=args)
    assert result is None  # packaged but not submitted


def test_explicit_consent_flags_allow_submit(monkeypatch):
    monkeypatch.setattr(share_cli, "hosted_destination",
                        lambda: {"can_submit": True, "message": "", "support_contact": None})
    monkeypatch.setattr(share_cli.share_flow, "consent", _consent_doc)
    monkeypatch.setattr(share_cli.share_flow, "upload_status",
                        lambda: {"token_valid": True, "verified_email": "me@uni.edu"})
    called = {}

    def _ok_submit(conn, share_id, **k):
        called.update(k)
        return {"ok": True, "receipt_id": "R1", "session_count": 1}
    monkeypatch.setattr(share_cli.share_flow, "submit", _ok_submit)

    args = SimpleNamespace(yes=True, accept_terms=True, certify_ownership=True)
    result = share_cli.step_submit(conn=None, settings={}, share_id="s1",
                                   package_ai=False, args=args)
    assert result and result["receipt_id"] == "R1"
    assert called["accept_terms"] is True and called["ownership_certification"] is True


# ---- hosted unavailable -> download-only fallback (#8) ----------------------

def test_hosted_unavailable_skips_submit(monkeypatch):
    monkeypatch.setattr(share_cli, "hosted_destination",
                        lambda: {"can_submit": False, "message": "closed",
                                 "support_contact": "x@y.edu"})

    def _fail_consent():
        raise AssertionError("consent must not be fetched when submit unavailable")
    monkeypatch.setattr(share_cli.share_flow, "consent", _fail_consent)

    args = SimpleNamespace(yes=False, accept_terms=False, certify_ownership=False)
    result = share_cli.step_submit(conn=None, settings={}, share_id="s1",
                                   package_ai=False, args=args)
    assert result is None


# ---- blocked-package recovery (#7) ------------------------------------------

def _recs(*ids):
    return [{"row": {"session_id": i, "display_title": i}} for i in ids]


def test_blocked_recovery_removes_and_retries(monkeypatch):
    monkeypatch.setattr(share_cli, "gate_blockers", lambda conn, ids: [])
    monkeypatch.setattr(share_cli, "build_zip", lambda d: b"zip")
    calls = []

    def _package(conn, session_ids, settings, *, ai_pii, note=None):
        calls.append(list(session_ids))
        if "bad" in session_ids:  # first attempt: block "bad"
            return {"ok": False, "blocked_sessions": ["bad"], "error": "blocked"}
        return {"ok": True, "share_id": "share123", "export_dir": "/tmp/x",
                "manifest": {"sessions": session_ids}}
    monkeypatch.setattr(share_cli.share_flow, "package", _package)

    args = SimpleNamespace(yes=True, note=None)
    share_id, export_dir = share_cli.step_package(
        conn=None, settings={}, included=_recs("good", "bad"), package_ai=False, args=args)
    assert share_id == "share123"
    assert len(calls) == 2  # retried after removing "bad"
    assert "bad" not in calls[1]


def test_blocked_all_blocked_aborts(monkeypatch):
    monkeypatch.setattr(share_cli, "gate_blockers", lambda conn, ids: [])

    def _package(conn, session_ids, settings, *, ai_pii, note=None):
        return {"ok": False, "blocked_sessions": list(session_ids), "error": "blocked"}
    monkeypatch.setattr(share_cli.share_flow, "package", _package)

    args = SimpleNamespace(yes=True, note=None)
    with pytest.raises(SystemExit):
        share_cli.step_package(conn=None, settings={}, included=_recs("a", "b"),
                               package_ai=False, args=args)
