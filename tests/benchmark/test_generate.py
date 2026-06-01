"""Tests for the benchmark generator: JSON extraction + a golden pipeline run with a fake backend."""

import json
import re
from datetime import datetime, timezone

import pytest

from clawjournal.benchmark import schema as bm
from clawjournal.benchmark.generate import (
    AgentBackendCaller,
    _blob_extract,
    _extract_json_object,
    _read_agent_output,
    generate_benchmark,
)
from clawjournal.redaction.anonymizer import Anonymizer

NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
NO_ANON = Anonymizer(enabled=False)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
class TestExtractJson:
    def test_plain_object(self):
        assert _extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_prose_wrapped_and_nested(self):
        assert _extract_json_object('Here you go: {"a": {"b": 2}} — done') == {"a": {"b": 2}}

    def test_braces_inside_strings_dont_confuse_the_scanner(self):
        assert _extract_json_object('{"s": "a}b{c", "n": 1}') == {"s": "a}b{c", "n": 1}

    @pytest.mark.parametrize("bad", ["", "   ", "no json here", "{unbalanced"])
    def test_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            _extract_json_object(bad)


# ---------------------------------------------------------------------------
# Fake backend + fixtures
# ---------------------------------------------------------------------------
def _ins(conn, sid, *, fvs=5, modes='["wrong_assumption"]', source="codex",
         start_time="2026-05-28T00:00:00+00:00", learning="a concrete lesson"):
    conn.execute(
        "INSERT INTO sessions (session_id, project, source, indexed_at, start_time, "
        "review_status, ai_failure_value_score, ai_failure_modes, ai_learning_summary) "
        "VALUES (?,?,?,?,?, 'new', ?, ?, ?)",
        (sid, "proj", source, start_time, start_time, fvs, modes, learning),
    )
    conn.commit()


def _after(text, marker):
    i = text.index(marker) + len(marker)
    return text[i:text.index("\n", i)].strip()


def _stub_id(prompt):
    m = re.search(r'"id":\s*"([^"]+)"', prompt)
    return m.group(1) if m else None


def _seed():
    return {
        "domain": "clawjournal-dev", "user_goal": "g", "failure_moment": "m",
        "root_cause_categories": ["verification_skipped"], "seductive_wrong_move": "w",
        "correct_behavior": "c", "evidence_snippet": "e", "recovery": "self_recovered",
        "generalizable_trap": "t", "severity": "medium",
    }


def _design(scenario="Confirm the importer against the real tracker."):
    return {
        "title": "Confirm readiness", "scenario": scenario, "seed_inputs": "repo @ commit",
        "the_trap": "cite the green suite as proof", "ideal_trajectory": ["read real data"],
        "pass_criteria": ["answers NO"], "fail_signals": ["says ready"],
        "grading": "judge", "difficulty": "hard", "points": 5,
    }


THEMES = [
    {"name": "Tests-as-proof", "taxonomy": ["verification_skipped"], "frequency": 2,
     "evidence_session_ids": ["s1"], "lesson": "validate the invariant"},
    {"name": "Auth-wall", "taxonomy": ["collaboration_error"], "frequency": 1,
     "evidence_session_ids": ["s2"], "lesson": "hand over a complete path"},
]
STUBS = [
    {"id": "S1", "theme": "Tests-as-proof", "domains": ["clawjournal-dev"],
     "source_agents": ["codex", "claude"], "grounded_session_ids": ["s1"],
     "concept": "tests pass != done", "why_personalized": "recurs"},
    {"id": "S2", "theme": "Auth-wall", "domains": ["pr-review"], "source_agents": ["codex"],
     "grounded_session_ids": ["s2"], "concept": "auth wall handoff", "why_personalized": "recurs"},
    {"id": "S3", "theme": "Tests-as-proof", "domains": ["clawjournal-dev"], "source_agents": ["codex"],
     "grounded_session_ids": ["s3"], "concept": "pii leak task", "why_personalized": "recurs"},
]
DESIGN = {"S1": _design(), "S2": _design(), "S3": _design(scenario="email ops@rayward.ai to begin")}
CRITIQUE = {
    "S1": {"discriminating": True, "gameable": False, "leakage": False, "measurable": True,
           "verdict": "keep", "notes": "", "staging_notes": "revert the importer",
           "readiness": "needs_staging", "leakage_risk": "low", "privacy_risk": "high"},
    "S2": {"verdict": "drop", "readiness": "retired", "leakage_risk": "low", "privacy_risk": "low"},
    "S3": {"verdict": "keep", "readiness": "ready", "leakage_risk": "low", "privacy_risk": "low"},
}


class FakeCaller:
    resolved = "claude"

    def __init__(self, *, themes=THEMES, stubs=STUBS, design=DESIGN, critique=CRITIQUE,
                 raise_sessions=(), raise_architect=False):
        self.themes, self.stubs, self.design, self.critique = themes, stubs, design, critique
        self.raise_sessions = set(raise_sessions)
        self.raise_architect = raise_architect
        self.stages: list[str] = []

    def __call__(self, *, stage, system_prompt, task_prompt):
        self.stages.append(stage)
        if stage == "deepread":
            sid = _after(task_prompt, "session_id: ")
            if sid in self.raise_sessions:
                raise RuntimeError("deepread boom")
            return _seed()
        if stage == "architect":
            if self.raise_architect:
                raise RuntimeError("architect boom")
            return {"themes": self.themes, "stubs": self.stubs}
        if stage == "design":
            return self.design[_stub_id(task_prompt)]
        if stage == "critique":
            return self.critique[_stub_id(task_prompt)]
        raise AssertionError(f"unexpected stage {stage}")


class TestGoldenPipeline:
    def test_full_run(self, index_conn):
        for sid in ("s1", "s2", "s3"):
            _ins(index_conn, sid)
        caller = FakeCaller()
        b = generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW, max_workers=2)

        assert b.backend == "claude"
        assert len(b.themes) == 2
        # S2 dropped (verdict=drop); S3 dropped (email PII in agent-facing scenario) → only S1
        assert [t.id for t in b.tasks] == ["S1"]
        t = b.tasks[0]
        assert t.readiness == "needs_staging"
        assert t.privacy_risk == "high"
        assert t.source_agents == ["codex", "claude"]
        assert t.critique.staging_notes == "revert the importer"
        assert t.points == 5
        assert set(b.source_session_ids) == {"s1", "s2", "s3"}
        assert b.window_start == "2026-05-24T12:00:00+00:00"
        assert "architect" in caller.stages and caller.stages.count("deepread") == 3
        bm.validate_or_raise(b)  # the assembled benchmark is valid

    def test_revised_pass_criteria_override(self, index_conn):
        _ins(index_conn, "s1")
        crit = {"S1": {**CRITIQUE["S1"], "revised_pass_criteria": ["tightened criterion"]}}
        caller = FakeCaller(stubs=[STUBS[0]], design={"S1": _design()}, critique=crit)
        b = generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW)
        assert b.tasks[0].pass_criteria == ["tightened criterion"]


class TestRobustness:
    def test_deepread_error_skips_session(self, index_conn):
        _ins(index_conn, "s1")
        _ins(index_conn, "s2")
        caller = FakeCaller(stubs=[STUBS[0]], design={"S1": _design()}, critique={"S1": CRITIQUE["S1"]},
                            raise_sessions={"s2"})
        b = generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW)
        assert set(b.source_session_ids) == {"s1"}  # s2's deep-read raised → skipped

    def test_no_candidates_raises(self, index_conn):
        with pytest.raises(ValueError, match="no failure-signal"):
            generate_benchmark(index_conn, caller=FakeCaller(), anonymizer=NO_ANON, now=NOW)

    def test_all_tasks_dropped_raises(self, index_conn):
        _ins(index_conn, "s1")
        caller = FakeCaller(stubs=[STUBS[1]], design={"S2": _design()}, critique={"S2": CRITIQUE["S2"]})
        with pytest.raises(ValueError, match="no tasks survived"):
            generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW)


class TestThemeAndArchitectRobustness:
    def test_malformed_theme_dropped_run_survives(self, index_conn):
        _ins(index_conn, "s1")
        caller = FakeCaller(themes=[{"taxonomy": ["x"]}, {"name": "Good", "frequency": 1}],
                            stubs=[STUBS[0]], design={"S1": _design()}, critique={"S1": CRITIQUE["S1"]})
        b = generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW)
        assert [t.name for t in b.themes] == ["Good"]   # nameless theme dropped
        assert [t.id for t in b.tasks] == ["S1"]         # run survived

    def test_non_list_themes_yields_empty(self, index_conn):
        _ins(index_conn, "s1")
        caller = FakeCaller(themes="oops", stubs=[STUBS[0]],
                            design={"S1": _design()}, critique={"S1": CRITIQUE["S1"]})
        b = generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW)
        assert b.themes == []

    def test_architect_error_raises_runtime(self, index_conn):
        _ins(index_conn, "s1")
        with pytest.raises(RuntimeError, match="architect stage failed"):
            generate_benchmark(index_conn, caller=FakeCaller(raise_architect=True),
                               anonymizer=NO_ANON, now=NOW)

    def test_zero_stubs_raises(self, index_conn):
        _ins(index_conn, "s1")
        with pytest.raises(ValueError, match="no task stubs"):
            generate_benchmark(index_conn, caller=FakeCaller(stubs=[]), anonymizer=NO_ANON, now=NOW)

    def test_zero_themes_ok(self, index_conn):
        _ins(index_conn, "s1")
        caller = FakeCaller(themes=[], stubs=[STUBS[0]],
                            design={"S1": _design()}, critique={"S1": CRITIQUE["S1"]})
        b = generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW)
        assert b.themes == [] and [t.id for t in b.tasks] == ["S1"]


class TestBadTaskBody:
    @pytest.mark.parametrize("bad", ["five", "3.5", [3]])
    def test_bad_field_drops_only_that_task(self, index_conn, bad):
        # a non-int points (or non-iterable list field) must drop just that task
        _ins(index_conn, "s1")
        caller = FakeCaller(
            stubs=[STUBS[0], STUBS[1]],
            design={"S1": {**_design(), "points": bad}, "S2": _design()},
            critique={"S1": CRITIQUE["S1"], "S2": dict(CRITIQUE["S1"])})
        b = generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW)
        assert [t.id for t in b.tasks] == ["S2"]  # S1 dropped, run survived

    def test_non_iterable_trajectory_drops_task(self, index_conn):
        _ins(index_conn, "s1")
        caller = FakeCaller(
            stubs=[STUBS[0], STUBS[1]],
            design={"S1": {**_design(), "ideal_trajectory": 5}, "S2": _design()},
            critique={"S1": CRITIQUE["S1"], "S2": dict(CRITIQUE["S1"])})
        b = generate_benchmark(index_conn, caller=caller, anonymizer=NO_ANON, now=NOW)
        assert [t.id for t in b.tasks] == ["S2"]


class TestReadAgentOutput:
    def test_claude_stdout(self, tmp_path):
        assert _read_agent_output("claude", '{"a": 1}', tmp_path / "none.json") == {"a": 1}

    def test_codex_output_file(self, tmp_path):
        f = tmp_path / "out.json"
        f.write_text('{"b": 2}')
        assert _read_agent_output("codex", "ignored stdout", f) == {"b": 2}

    def test_openclaw_envelope_unwrapped(self, tmp_path):
        env = json.dumps({"reply": {"content": [{"text": '{"domain": "x"}'}]}})
        assert _read_agent_output("openclaw", env, tmp_path / "none.json") == {"domain": "x"}

    def test_openclaw_bare_json_falls_back(self, tmp_path):
        assert _read_agent_output("openclaw", '{"domain": "y"}', tmp_path / "none.json") == {"domain": "y"}


class TestBlobExtract:
    def test_missing_blob_path(self):
        assert _blob_extract(None, NO_ANON) == "(no trace blob available)"

    def test_unreadable_blob(self, tmp_path):
        assert _blob_extract(str(tmp_path / "nope.json"), NO_ANON) == "(trace blob unavailable)"

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "b.json"
        p.write_text("{not json")
        assert _blob_extract(str(p), NO_ANON) == "(trace blob unavailable)"

    def test_anonymizes_usernames_and_flattens_list_content(self, tmp_path):
        p = tmp_path / "b.json"
        p.write_text(json.dumps({"messages": [
            {"role": "user", "content": "work in /Users/acmeuser/proj as acmeuser"},
            {"role": "assistant", "content": [{"text": "part1"}, {"text": "part2"}]},
        ]}))
        out = _blob_extract(str(p), Anonymizer(extra_usernames=["acmeuser"], enabled=True))
        assert "acmeuser" not in out          # username stripped (incl. inside the path)
        assert "part1 part2" in out           # list content flattened
        assert "[user]" in out and "[assistant]" in out

    def test_head_tail_truncation_keeps_terminal_turns(self, tmp_path):
        p = tmp_path / "b.json"
        msgs = [{"role": "user", "content": f"MSG{i:04d}-" + ("x" * 80)} for i in range(80)]
        p.write_text(json.dumps({"messages": msgs}))
        out = _blob_extract(str(p), NO_ANON, max_chars=1000)
        assert "MSG0000" in out and "MSG0079" in out and "…" in out
        assert len(out) <= 1010


class TestDefaultModel:
    def test_claude_defaults_to_sonnet(self, monkeypatch):
        monkeypatch.setattr("clawjournal.benchmark.generate.resolve_backend", lambda b: "claude")
        assert AgentBackendCaller(backend="auto").model == "sonnet"

    def test_codex_keeps_cli_default(self, monkeypatch):
        monkeypatch.setattr("clawjournal.benchmark.generate.resolve_backend", lambda b: "codex")
        assert AgentBackendCaller(backend="auto").model is None

    def test_explicit_model_overrides_default(self, monkeypatch):
        monkeypatch.setattr("clawjournal.benchmark.generate.resolve_backend", lambda b: "claude")
        assert AgentBackendCaller(backend="auto", model="opus").model == "opus"


class TestStageTimeouts:
    """The heavy synthesis stages (architect/design) get a longer subprocess
    ceiling than per-item reads; unknown stages fall back to the default."""

    def _timeout_for(self, monkeypatch, stage):
        monkeypatch.setattr("clawjournal.benchmark.generate.resolve_backend", lambda b: "claude")
        captured = {}

        def fake_run(**kw):
            captured.update(kw)

            class _R:
                stdout = '{"ok": true}'
                stderr = ''
                returncode = 0
            return _R()

        monkeypatch.setattr("clawjournal.benchmark.generate.run_default_agent_task", fake_run)
        AgentBackendCaller(backend="auto")(stage=stage, system_prompt="sys", task_prompt="task")
        return captured["timeout_seconds"]

    def test_architect_gets_the_longest_ceiling(self, monkeypatch):
        assert self._timeout_for(monkeypatch, "architect") == 600

    def test_design_ceiling(self, monkeypatch):
        assert self._timeout_for(monkeypatch, "design") == 360

    def test_deepread_ceiling(self, monkeypatch):
        assert self._timeout_for(monkeypatch, "deepread") == 240

    def test_unknown_stage_uses_default(self, monkeypatch):
        assert self._timeout_for(monkeypatch, "mystery") == 240


class TestProgressMessages:
    def test_incremental_human_messages(self, index_conn):
        for sid in ("s1", "s2", "s3"):
            _ins(index_conn, sid)
        msgs: list[str] = []
        generate_benchmark(index_conn, caller=FakeCaller(), anonymizer=NO_ANON, now=NOW,
                           max_workers=2, progress=msgs.append)
        blob = " | ".join(msgs)
        assert "Reading your recent failures (" in blob   # incremental count
        assert "Grouping failures into themes" in blob
        assert "Writing & reviewing benchmark tasks (" in blob
        assert "Finalizing" in blob and "Done —" in blob
