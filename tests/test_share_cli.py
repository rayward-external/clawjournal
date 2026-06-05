"""Tests for the interactive share wizard CLI surface."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


# ---- time-range windows (rolling 24h / 168h) --------------------------------

def _row(fv=None, end_delta_hours=None, **extra):
    r = {"hold_state": None, "shared_at": None, "ai_failure_value_score": fv}
    if end_delta_hours is not None:
        r["end_time"] = (datetime.now(timezone.utc) - timedelta(hours=end_delta_hours)).isoformat()
    r.update(extra)
    r.setdefault("session_id", "s")
    return r


def test_in_time_range_rolling_windows():
    assert share_cli._in_time_range(_row(end_delta_hours=1), "today")
    assert not share_cli._in_time_range(_row(end_delta_hours=30), "today")
    assert share_cli._in_time_range(_row(end_delta_hours=30), "weekly")
    assert not share_cli._in_time_range(_row(end_delta_hours=200), "weekly")
    assert share_cli._in_time_range(_row(end_delta_hours=200), "all")
    assert not share_cli._in_time_range({"end_time": None}, "today")
    assert share_cli._in_time_range({"end_time": None}, "all")


# ---- queue selection: filtering + failure-value ranking (#4) ----------------

_SETTINGS = {"excluded_projects": []}


def _qargs(**kw):
    base = dict(time_range="all", project=None, min_failure_value=None, search=None, limit=40)
    base.update(kw)
    return SimpleNamespace(**base)


def test_select_excludes_shared_and_held():
    rows = [_row(fv=5, session_id="ok"),
            _row(fv=5, session_id="shared", shared_at="2026-01-01"),
            _row(fv=5, session_id="held", hold_state="pending_review")]
    out = share_cli.select_queue_rows(rows, _SETTINGS, _qargs())
    assert [r["session_id"] for r in out] == ["ok"]


def test_select_ranked_by_failure_value_nulls_last():
    rows = [_row(fv=2, session_id="b"), _row(fv=5, session_id="a"),
            _row(fv=None, session_id="z"), _row(fv=4, session_id="c")]
    out = share_cli.select_queue_rows(rows, _SETTINGS, _qargs())
    assert [r["session_id"] for r in out] == ["a", "c", "b", "z"]


def test_select_filters_search_project_minfv():
    rows = [
        _row(fv=5, session_id="a", display_title="fix the judge", project="evalproj"),
        _row(fv=1, session_id="b", display_title="hello world", project="evalproj"),
        _row(fv=5, session_id="c", display_title="judge again", project="other"),
    ]
    assert {r["session_id"] for r in share_cli.select_queue_rows(rows, _SETTINGS, _qargs(search="judge"))} == {"a", "c"}
    assert {r["session_id"] for r in share_cli.select_queue_rows(rows, _SETTINGS, _qargs(min_failure_value=4))} == {"a", "c"}
    assert {r["session_id"] for r in share_cli.select_queue_rows(rows, _SETTINGS, _qargs(project="evalproj"))} == {"a", "b"}


def test_select_limit():
    rows = [_row(fv=5, session_id=str(i)) for i in range(10)]
    assert len(share_cli.select_queue_rows(rows, _SETTINGS, _qargs(limit=3))) == 3


# ---- title logic ------------------------------------------------------------

def test_resolve_title_modes():
    r = {"display_title": "raw first msg", "ai_display_title": "AI Title"}
    assert share_cli.resolve_title(r, summarized=False) == "raw first msg"
    assert share_cli.resolve_title(r, summarized=True) == "AI Title"
    assert share_cli.resolve_title({"display_title": ""}, False) == "Untitled"
    assert share_cli.resolve_title({"display_title": "x"}, True) == "x"  # falls back when no AI title


def test_looks_like_system_prompt():
    assert share_cli._looks_like_system_prompt("You are a strict evaluation judge")
    assert share_cli._looks_like_system_prompt("Your task is to ...")
    assert not share_cli._looks_like_system_prompt("哪些 benchmark 适合做成 RL 环境")
    assert not share_cli._looks_like_system_prompt("Read the file and fix the bug")


def test_user_prompt_title_strips_preamble(monkeypatch):
    detail = {"messages": [
        {"role": "user", "content": 'You are a judge. Compare.\n\n{"candidate": 2}'},
    ]}
    monkeypatch.setattr(share_cli, "get_session_detail", lambda conn, sid: detail)
    assert share_cli.user_prompt_title(None, {"session_id": "x"}) == '{"candidate": 2}'


# ---- step_redact AI consistency (preview == shipped, #2/#3) -----------------

def _fake_rec(coverage, status="review"):
    return {"redacted": {"messages": []}, "count": 0,
            "buckets": {k: 0 for k in share_cli.share_flow.BUCKET_KEYS},
            "th_hits": 0, "ai_findings": [], "ai_coverage": coverage, "status": status}


def test_step_redact_degrades_to_rules_only_when_ai_unavailable(monkeypatch):
    monkeypatch.setattr(share_cli, "get_session_detail", lambda conn, sid: {"messages": []})
    seen = []

    def fake_build(conn, detail, settings, use_ai, **k):
        seen.append(use_ai)
        return _fake_rec("rules_only" if use_ai else "disabled")
    monkeypatch.setattr(share_cli, "build_redaction_record", fake_build)

    chosen = [{"session_id": "a", "display_title": "A"}]
    scrubbed, package_ai = share_cli.step_redact(None, {}, chosen, assume_yes=True,
                                                 ai_pii_requested=True)
    assert package_ai is False                       # degraded so what ships == what was shown
    assert scrubbed[0]["ai_coverage"] == "disabled"  # rebuilt rules-only
    assert seen == [True, False]                     # tried AI, then rebuilt without


def test_step_redact_keeps_ai_when_uniformly_full(monkeypatch):
    monkeypatch.setattr(share_cli, "get_session_detail", lambda conn, sid: {"messages": []})
    monkeypatch.setattr(share_cli, "build_redaction_record",
                        lambda conn, detail, settings, use_ai, **k: _fake_rec("full", "clear"))
    chosen = [{"session_id": "a", "display_title": "A"}]
    _scrubbed, package_ai = share_cli.step_redact(None, {}, chosen, assume_yes=True,
                                                  ai_pii_requested=True)
    assert package_ai is True


# ---- download path resolution (regression: a stray 'y' must not be a path) --

def test_resolve_download_dest(tmp_path):
    default = tmp_path / "bundle.zip"
    fname = "bundle.zip"
    assert share_cli._resolve_download_dest("y", default, fname) == default
    assert share_cli._resolve_download_dest("", default, fname) == default
    assert share_cli._resolve_download_dest("yes", default, fname) == default
    assert share_cli._resolve_download_dest("n", default, fname) is None
    custom = tmp_path / "sub" / "out.zip"
    assert share_cli._resolve_download_dest(str(custom), default, fname) == custom
    d = tmp_path / "adir"; d.mkdir()
    assert share_cli._resolve_download_dest(str(d), default, fname) == d / fname
