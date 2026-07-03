"""Scan/score orchestration (§7.1/§7.2, §14): _ensure_corpus is really exercised."""

from unittest.mock import MagicMock

from clawjournal import cli_skill
from clawjournal.cli_skill import ALL_HISTORY_DAYS, _ensure_corpus


def _wire(monkeypatch, unscored, held=()):
    calls = {"scan": [], "unscored": [], "scored": [], "score_kw": []}
    monkeypatch.setattr(cli_skill, "_scan_source_filter", lambda cfg=None: None)
    monkeypatch.setattr(cli_skill, "_config_sources", lambda cfg=None: ["claude", "codex"])
    monkeypatch.setattr(cli_skill, "_config_excluded_projects", lambda cfg=None, conn=None: [])
    monkeypatch.setattr("clawjournal.cli._run_scan",
                        lambda source_filter=None: calls["scan"].append(source_filter))

    # the only raw conn.execute in _ensure_corpus is the held-candidates query
    mock_conn = MagicMock()
    mock_conn.execute.return_value.fetchall.return_value = [{"session_id": h} for h in held]
    monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: mock_conn)

    def fake_unscored(conn, *, limit, source, since):
        calls["unscored"].append({"limit": limit, "source": source, "since": since})
        return list(unscored)
    monkeypatch.setattr("clawjournal.workbench.index.query_unscored_sessions", fake_unscored)
    # blocking flows through _select._release_blocked_ids, which uses select's own
    # module-level release_gate_blockers reference — patch there.
    monkeypatch.setattr("clawjournal.skill.select.release_gate_blockers",
                        lambda conn, ids, **kw: [{"session_id": s} for s in ids if s in held])

    def fake_score(conn, sid, **kw):
        calls["scored"].append(sid)
        calls["score_kw"].append(kw)
        return {}
    monkeypatch.setattr("clawjournal.cli._score_single_session", fake_score)
    return calls


def test_weekly_scans_and_scores_last_7_days(monkeypatch):
    calls = _wire(monkeypatch, [{"session_id": "a"}, {"session_id": "b"}])
    _ensure_corpus(7, do_scan=True, do_score=True, score_limit=25)
    assert calls["scan"] == [None]                       # scan ran
    assert calls["unscored"][0]["since"] is not None     # bounded 7-day window
    assert calls["unscored"][0]["limit"] == 26           # score_limit + 0 held + 1 probe
    assert calls["scored"] == ["a", "b"]                 # each unscored session scored


def test_scoring_never_uses_the_distill_model(monkeypatch):
    # fix #4: `--model` tunes the distill call only; scoring must fall back to its own
    # (fast) default, never the frontier distill model.
    calls = _wire(monkeypatch, [{"session_id": "a"}])
    _ensure_corpus(7, do_scan=False, do_score=True, score_limit=25)
    assert calls["scored"] == ["a"]
    assert all("model" not in kw for kw in calls["score_kw"])   # no distill model leaked in


def test_held_sessions_are_not_scored(monkeypatch):
    # fix #3: an egress-blocked (held/embargoed) session must not be sent to the model.
    calls = _wire(monkeypatch, [{"session_id": "ok"}, {"session_id": "held"}], held=["held"])
    _ensure_corpus(7, do_scan=False, do_score=True, score_limit=25)
    assert calls["scored"] == ["ok"]                     # 'held' filtered out before scoring
    assert calls["unscored"][0]["limit"] == 27           # 25 + 1 held + 1 probe


def test_held_rows_do_not_starve_shareable_ones(monkeypatch):
    # a page whose head is all held must still let shareable rows through (over-fetch + cap).
    page = [{"session_id": "h1"}, {"session_id": "h2"}, {"session_id": "ok"}]
    calls = _wire(monkeypatch, page, held=["h1", "h2"])
    _ensure_corpus(7, do_scan=False, do_score=True, score_limit=1)
    assert calls["unscored"][0]["limit"] == 4            # 1 + 2 held + 1 probe
    assert calls["scored"] == ["ok"]                     # shareable row reached, not starved


def test_first_run_all_history_uses_no_since(monkeypatch):
    calls = _wire(monkeypatch, [])
    _ensure_corpus(ALL_HISTORY_DAYS, do_scan=False, do_score=True, score_limit=25)
    assert calls["scan"] == []                           # --no-scan honored
    assert calls["unscored"][0]["since"] is None         # all history


def test_no_score_flag_skips_scoring(monkeypatch):
    calls = _wire(monkeypatch, [{"session_id": "a"}])
    _ensure_corpus(7, do_scan=True, do_score=False, score_limit=25)
    assert calls["scan"] == [None]
    assert calls["unscored"] == [] and calls["scored"] == []


def test_excluded_projects_honor_db_policies(index_conn):
    # #1: a workbench DB exclude_project policy (not config.json) must be gated out of
    # the skill path too — the same effective egress set as export/share.
    from clawjournal.cli_skill import _config_excluded_projects
    from clawjournal.workbench.index import add_policy
    assert _config_excluded_projects({"excluded_projects": []}, index_conn) == []
    add_policy(index_conn, "exclude_project", "client-acme")
    eff = _config_excluded_projects({"excluded_projects": []}, index_conn)
    assert any("client-acme" in p for p in eff)   # DB policy merged in


def test_config_source_scope_mapping(monkeypatch):
    import clawjournal.config as cfg
    from clawjournal.cli_skill import _config_sources, _scan_source_filter
    from clawjournal.workbench.index import FAILURE_VALUE_SOURCE_SCOPE

    monkeypatch.setattr(cfg, "load_config", lambda: {"source": "claude"})
    assert _config_sources() == ["claude"]           # a specific scope constrains selection
    assert _scan_source_filter() == "claude"

    monkeypatch.setattr(cfg, "load_config", lambda: {"source": "both"})
    assert _config_sources() == ["claude", "codex"]  # legacy 'both' must NOT widen to opencode/openclaw

    monkeypatch.setattr(cfg, "load_config", lambda: {"source": "all"})
    assert set(_config_sources()) == set(FAILURE_VALUE_SOURCE_SCOPE)   # 'all' = coding scope
    assert _scan_source_filter() is None
