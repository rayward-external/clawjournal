"""Scan/score orchestration (§7.1/§7.2, §14): _ensure_corpus is really exercised."""

from unittest.mock import MagicMock

from clawjournal import cli_skill
from clawjournal.cli_skill import ALL_HISTORY_DAYS, _ensure_corpus


def _wire(monkeypatch, unscored):
    calls = {"scan": [], "unscored": [], "scored": []}
    monkeypatch.setattr(cli_skill, "_scan_source_filter", lambda: None)
    monkeypatch.setattr(cli_skill, "_config_sources", lambda: ["claude", "codex"])
    monkeypatch.setattr("clawjournal.cli._run_scan",
                        lambda source_filter=None: calls["scan"].append(source_filter))
    monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: MagicMock())

    def fake_unscored(conn, *, limit, source, since):
        calls["unscored"].append({"limit": limit, "source": source, "since": since})
        return list(unscored)
    monkeypatch.setattr("clawjournal.workbench.index.query_unscored_sessions", fake_unscored)
    monkeypatch.setattr("clawjournal.cli._score_single_session",
                        lambda conn, sid, **kw: calls["scored"].append(sid) or {})
    return calls


def test_weekly_scans_and_scores_last_7_days(monkeypatch):
    calls = _wire(monkeypatch, [{"session_id": "a"}, {"session_id": "b"}])
    _ensure_corpus(7, do_scan=True, do_score=True, score_limit=25, backend="auto", model=None)
    assert calls["scan"] == [None]                       # scan ran
    assert calls["unscored"][0]["since"] is not None     # bounded 7-day window
    assert calls["unscored"][0]["limit"] == 25           # score cap honored
    assert calls["scored"] == ["a", "b"]                 # each unscored session scored


def test_first_run_all_history_uses_no_since(monkeypatch):
    calls = _wire(monkeypatch, [])
    _ensure_corpus(ALL_HISTORY_DAYS, do_scan=False, do_score=True,
                   score_limit=25, backend="auto", model=None)
    assert calls["scan"] == []                           # --no-scan honored
    assert calls["unscored"][0]["since"] is None         # all history


def test_no_score_flag_skips_scoring(monkeypatch):
    calls = _wire(monkeypatch, [{"session_id": "a"}])
    _ensure_corpus(7, do_scan=True, do_score=False, score_limit=25, backend="auto", model=None)
    assert calls["scan"] == [None]
    assert calls["unscored"] == [] and calls["scored"] == []


def test_config_source_scope_mapping(monkeypatch):
    import clawjournal.config as cfg
    from clawjournal.cli_skill import _config_sources, _scan_source_filter
    from clawjournal.workbench.index import FAILURE_VALUE_SOURCE_SCOPE

    monkeypatch.setattr(cfg, "load_config", lambda: {"source": "claude"})
    assert _config_sources() == ["claude"]           # a specific scope constrains selection
    assert _scan_source_filter() == "claude"

    monkeypatch.setattr(cfg, "load_config", lambda: {"source": "all"})
    assert set(_config_sources()) == set(FAILURE_VALUE_SOURCE_SCOPE)   # 'all' = coding scope
    assert _scan_source_filter() is None
