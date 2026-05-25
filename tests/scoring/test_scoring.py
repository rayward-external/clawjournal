"""Tests for clawjournal.scoring.scoring — pure functions (no LLM calls)."""

import json
from pathlib import Path

import pytest

from clawjournal.redaction.pii import _PII_PROMPT_FILE
from clawjournal.scoring.backends import (
    AgentResult,
    _classify_process_command,
    _detect_current_agent_from_env,
    check_backend_runtime as _check_backend_runtime,
    format_codex_runtime_error as _format_codex_runtime_error,
    resolve_backend,
)
from clawjournal.prompt_sync import (
    SCORING_PROMPT_RUBRIC_FILE,
    SCORING_SKILL_RUBRIC_FILE,
    build_scoring_skill_rubric,
    sync_scoring_skill_rubric,
)
from clawjournal.scoring.scoring import (
    JUDGE_SCHEMA,
    SCORING_BACKEND_CHOICES,
    Segment,
    ScoringResult,
    Step,
    _SCORER_PROMPT_FILE,
    _extract_judge_result_from_value,
    _read_scoring_output,
    _validate_judge_result,
    call_judge,
    compute_basic_metrics,
    compute_heuristic_effort,
    extract_tool_uses,
    format_session_for_judge,
    get_message_text,
    load_scoring_rubric,
    segment_session,
)


# ---------------------------------------------------------------------------
# Helpers to build test messages
# ---------------------------------------------------------------------------


def _user_msg(text: str) -> dict:
    return {"role": "user", "content": text}


def _asst_msg(text: str, tool_uses: list[dict] | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": text}
    if tool_uses:
        msg["tool_uses"] = tool_uses
    return msg


def _tool_use(tool: str, inp: str = "", output: str = "", status: str = "success") -> dict:
    return {
        "tool": tool,
        "input": {"path": inp} if inp else {},
        "output": output,
        "status": status,
    }


def _failure_fields(**overrides) -> dict:
    payload = {
        "ai_quality_score": 4,
        "ai_failure_value_score": 4,
        "ai_recovery_labels": ["user_corrected_recovery"],
        "ai_failure_attribution": "agent_caused",
        "ai_failure_modes": ["wrong_assumption"],
        "ai_failure_evidence": ["User corrected the assumption."],
        "ai_learning_summary": "The trace shows a corrected agent assumption.",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# get_message_text
# ---------------------------------------------------------------------------


class TestGetMessageText:
    def test_string_content(self):
        assert get_message_text({"content": "hello"}) == "hello"

    def test_list_with_text_block(self):
        msg = {"content": [{"text": "hello", "type": "text"}]}
        assert get_message_text(msg) == "hello"

    def test_list_with_string(self):
        msg = {"content": ["hello"]}
        assert get_message_text(msg) == "hello"

    def test_empty(self):
        assert get_message_text({}) == ""
        assert get_message_text({"content": []}) == ""


# ---------------------------------------------------------------------------
# extract_tool_uses
# ---------------------------------------------------------------------------


class TestExtractToolUses:
    def test_from_tool_uses_field(self):
        msg = {"tool_uses": [{"tool": "Read", "input": {}, "output": "ok", "status": "success"}]}
        uses = extract_tool_uses(msg)
        assert len(uses) == 1
        assert uses[0]["tool"] == "Read"

    def test_from_content_blocks(self):
        msg = {
            "content": [
                {"tool": "Bash", "input": {"command": "ls"}, "output": "file.py", "status": "success"},
            ]
        }
        uses = extract_tool_uses(msg)
        assert len(uses) == 1
        assert uses[0]["tool"] == "Bash"
        assert uses[0]["first_arg"] == "ls"

    def test_no_tool_uses(self):
        assert extract_tool_uses({"content": "just text"}) == []
        assert extract_tool_uses({}) == []


# ---------------------------------------------------------------------------
# segment_session
# ---------------------------------------------------------------------------


class TestSegmentSession:
    def test_single_segment(self):
        messages = [
            _user_msg("Fix the bug"),
            _asst_msg("I'll look at it", [_tool_use("Read", "auth.py", "contents")]),
            _asst_msg("Found it", [_tool_use("Edit", "auth.py", "fixed")]),
        ]
        segments = segment_session(messages)
        assert len(segments) == 1
        assert segments[0].user_message == "Fix the bug"
        assert len(segments[0].steps) == 2
        assert segments[0].steps[0].action_tool == "Read"
        assert segments[0].steps[1].action_tool == "Edit"
        assert segments[0].user_response is None

    def test_multi_segment(self):
        messages = [
            _user_msg("Fix the bug"),
            _asst_msg("Done", [_tool_use("Edit", "auth.py", "fixed")]),
            _user_msg("Now add tests"),
            _asst_msg("Writing tests", [_tool_use("Write", "test.py", "ok")]),
        ]
        segments = segment_session(messages)
        assert len(segments) == 2
        assert segments[0].user_message == "Fix the bug"
        assert segments[0].user_response == "Now add tests"
        assert segments[1].user_message == "Now add tests"
        assert segments[1].user_response is None

    def test_no_tool_uses(self):
        messages = [
            _user_msg("Hello"),
            _asst_msg("Hi there"),
        ]
        segments = segment_session(messages)
        assert len(segments) == 1
        assert len(segments[0].steps) == 0

    def test_empty_messages(self):
        assert segment_session([]) == []

    def test_multiple_tool_uses_in_one_message(self):
        messages = [
            _user_msg("Check everything"),
            _asst_msg("Checking", [
                _tool_use("Read", "a.py", "ok"),
                _tool_use("Read", "b.py", "ok"),
                _tool_use("Read", "c.py", "ok"),
            ]),
        ]
        segments = segment_session(messages)
        assert len(segments) == 1
        assert len(segments[0].steps) == 3

    def test_reflect_on_previous_step(self):
        messages = [
            _user_msg("Do it"),
            _asst_msg("Starting", [_tool_use("Read", "f.py", "contents")]),
            _asst_msg("I see the issue"),
        ]
        segments = segment_session(messages)
        assert len(segments) == 1
        assert len(segments[0].steps) == 1
        assert segments[0].steps[0].reflect == "I see the issue"

    def test_assistant_only_session(self):
        messages = [
            _asst_msg("Auto-running", [_tool_use("Bash", "ls", "file.py")]),
        ]
        segments = segment_session(messages)
        assert len(segments) == 1
        assert segments[0].user_message == ""
        assert len(segments[0].steps) == 1


# ---------------------------------------------------------------------------
# compute_basic_metrics
# ---------------------------------------------------------------------------


class TestComputeBasicMetrics:
    def test_basic(self):
        seg = Segment(user_message="test", steps=[
            Step("", "Read", "a", "ok", "success", ""),
            Step("", "Bash", "b", "err", "failure", ""),
            Step("", "Edit", "c", "ok", "success", ""),
        ])
        detail = {
            "user_messages": 2,
            "input_tokens": 5000,
            "output_tokens": 3000,
            "duration_seconds": 120,
            "files_touched": '["a.py", "b.py"]',
            "outcome_badge": "tests_passed",
        }
        m = compute_basic_metrics([seg], detail)
        assert m["total_steps"] == 3
        assert m["tool_failures"] == 1
        assert m["segments"] == 1
        assert m["outcome_badge"] == "tests_passed"
        assert "heuristic_effort" in m
        assert 0.0 <= m["heuristic_effort"] <= 1.0

    def test_empty(self):
        m = compute_basic_metrics([], {})
        assert m["total_steps"] == 0
        assert m["tool_failures"] == 0
        assert m["outcome_badge"] is None

    def test_multiple_segments(self):
        seg1 = Segment(user_message="a", steps=[
            Step("", "Read", "f", "ok", "success", ""),
        ])
        seg2 = Segment(user_message="b", steps=[
            Step("", "Bash", "c", "err", "error", ""),
            Step("", "Edit", "d", "ok", "success", ""),
        ])
        m = compute_basic_metrics([seg1, seg2], {})
        assert m["total_steps"] == 3
        assert m["segments"] == 2
        assert m["tool_failures"] == 1


# ---------------------------------------------------------------------------
# load_scoring_rubric
# ---------------------------------------------------------------------------


class TestLoadScoringRubric:
    def test_rubric_loads(self):
        rubric = load_scoring_rubric()
        assert "substance" in rubric.lower()
        assert "resolution" in rubric.lower()
        assert len(rubric) > 100

    def test_prompt_assets_live_under_package(self):
        assert SCORING_PROMPT_RUBRIC_FILE.parts[-5:] == (
            "clawjournal", "prompts", "agents", "scoring", "rubric.md",
        )
        assert _SCORER_PROMPT_FILE.exists()
        assert _PII_PROMPT_FILE.exists()
        assert _SCORER_PROMPT_FILE.parent == Path(SCORING_PROMPT_RUBRIC_FILE).parent

    def test_skill_rubric_matches_canonical_prompt(self):
        expected = build_scoring_skill_rubric()
        assert SCORING_PROMPT_RUBRIC_FILE.read_text(encoding="utf-8") == expected
        assert SCORING_SKILL_RUBRIC_FILE.read_text(encoding="utf-8") == expected

    def test_sync_scoring_skill_rubric_writes_generated_copy(self, tmp_path, monkeypatch):
        prompt = tmp_path / "rubric.md"
        skill = tmp_path / "RUBRIC.md"
        prompt.write_text("canonical rubric", encoding="utf-8")

        monkeypatch.setattr("clawjournal.prompt_sync.SCORING_PROMPT_RUBRIC_FILE", prompt)
        monkeypatch.setattr("clawjournal.prompt_sync.SCORING_SKILL_RUBRIC_FILE", skill)

        sync_scoring_skill_rubric()

        assert skill.read_text(encoding="utf-8") == "canonical rubric"

    def test_redirect_stub_falls_back_to_builtin_rubric(self, tmp_path, monkeypatch):
        stub = tmp_path / "RUBRIC.md"
        stub.write_text(
            "<!-- Canonical location: clawjournal/prompts/agents/scoring/rubric.md -->\n"
            "<!-- This copy is kept for backward compatibility with tools that read from skills/ -->\n"
            "See [clawjournal/prompts/agents/scoring/rubric.md](../../clawjournal/prompts/agents/scoring/rubric.md) for the canonical scoring rubric.\n"
        )
        monkeypatch.setattr("clawjournal.scoring.scoring._RUBRIC_SEARCH_PATHS", [stub])
        rubric = load_scoring_rubric()
        assert "substance" in rubric
        assert "privacy_flags" in rubric


# ---------------------------------------------------------------------------
# format_session_for_judge
# ---------------------------------------------------------------------------


class TestFormatSessionForJudge:
    def test_single_segment(self):
        seg = Segment(
            user_message="Fix bug",
            steps=[
                Step("Looking at it", "Read", "auth.py", "file contents", "success", ""),
                Step("Fixing", "Edit", "auth.py", "applied fix", "success", ""),
            ],
            user_response="thanks!",
        )
        metrics = {"total_steps": 2, "tool_failures": 0, "input_tokens": 5000,
                   "output_tokens": 3000, "outcome_badge": "tests_passed"}
        text = format_session_for_judge([seg], "Fix bug", metrics)
        assert "## User's Task" in text
        assert "Fix bug" in text
        assert "Step 1:" in text
        assert "Step 2:" in text
        assert "Read(auth.py)" in text
        assert "## Session Metrics" in text
        assert "Outcome: tests_passed" in text
        assert '"thanks!"' in text
        assert "Respond with JSON" in text
        assert "substance" in text

    def test_multi_segment_shows_turns(self):
        seg1 = Segment(user_message="Fix it", steps=[
            Step("", "Read", "f.py", "ok", "success", ""),
        ], user_response="Now test")
        seg2 = Segment(user_message="Now test", steps=[
            Step("", "Bash", "pytest", "pass", "success", ""),
        ])
        text = format_session_for_judge([seg1, seg2], "Fix it\nNow test")
        assert "Turn 1" in text
        assert "Turn 2" in text

    def test_no_user_response(self):
        seg = Segment(user_message="Do it", steps=[
            Step("", "Read", "f.py", "ok", "success", ""),
        ])
        text = format_session_for_judge([seg], "Do it")
        assert "No response — session ended" in text


# ---------------------------------------------------------------------------
# ScoringResult
# ---------------------------------------------------------------------------


class TestScoringResult:
    def test_basic(self):
        r = ScoringResult(segments=[], quality=4, reason="Good session")
        assert r.quality == 4
        assert r.reason == "Good session"
        assert r.summary == ""
        assert r.effort_estimate == 0.0
        assert r.detail_json == "{}"


# ---------------------------------------------------------------------------
# compute_heuristic_effort
# ---------------------------------------------------------------------------


class TestComputeHeuristicEffort:
    def test_zero_inputs(self):
        assert compute_heuristic_effort(0, 0, 0, 0) == 0.0

    def test_maximum_inputs(self):
        effort = compute_heuristic_effort(
            duration_seconds=3600,  # 60 min → capped at 1.0
            tool_calls=100,         # capped at 1.0
            total_tokens=200_000,   # capped at 1.0
            files_touched=40,       # capped at 1.0
        )
        assert effort == 1.0

    def test_moderate_session(self):
        effort = compute_heuristic_effort(
            duration_seconds=600,   # 10 min → 10/60 = 0.167
            tool_calls=10,          # 10/50 = 0.2
            total_tokens=20_000,    # 20k/100k = 0.2
            files_touched=3,        # 3/20 = 0.15
        )
        # 0.3*0.167 + 0.3*0.2 + 0.2*0.2 + 0.2*0.15 = 0.05 + 0.06 + 0.04 + 0.03 = 0.18
        assert 0.1 < effort < 0.3

    def test_none_duration(self):
        effort = compute_heuristic_effort(None, 10, 10_000, 2)
        assert effort >= 0.0

    def test_clamped_to_range(self):
        effort = compute_heuristic_effort(-100, -10, -5000, -3)
        assert effort == 0.0


# ---------------------------------------------------------------------------
# _validate_judge_result backward compatibility
# ---------------------------------------------------------------------------


class TestValidateJudgeResultBackwardCompat:
    """Old-schema results (with 'quality' key) should still validate."""

    def test_old_schema_maps_quality_to_substance(self):
        old_result = {
            "quality": 4,
            "reasoning": "Good session",
            "display_title": "Fix bug",
            "outcome": 4,
            "intent": 4,
            "taste": {"detected": False},
            "task_type": "debugging",
            "outcome_label": "tests_passed",
            "value_labels": ["tool_rich"],
            "risk_level": ["secrets_detected"],
        }
        validated = _validate_judge_result(old_result)
        assert validated["substance"] == 4
        # Old-schema outcome_label=tests_passed is translated into the
        # new-schema `resolved` bucket (tool-output "tests passed" ≈ goal
        # achieved). Anything outside the legacy map gets dropped so it
        # can't leak onto the dashboard as a ghost label.
        assert validated["resolution"] == "resolved"
        assert validated["session_tags"] == ["tool_rich"]  # falls back from value_labels
        assert validated["privacy_flags"] == ["secrets_detected"]  # falls back from risk_level
        assert validated["summary"] == ""  # not present in old schema
        assert validated["effort_estimate"] is None  # not present in old schema, sentinel value

    def test_new_schema_preferred_over_old(self):
        mixed = {
            "substance": 5,
            "quality": 3,  # should be ignored when substance present
            "reasoning": "Great",
            "display_title": "Add feature",
            "summary": "Added a feature.",
            "resolution": "resolved",
            "effort_estimate": 0.7,
            "task_type": "feature",
            "session_tags": ["backend"],
            "value_labels": ["old_label"],  # should be ignored when session_tags present
            "privacy_flags": [],
            "risk_level": ["names_detected"],  # should be ignored
            "project_areas": ["src/"],
        }
        validated = _validate_judge_result(mixed)
        assert validated["substance"] == 5
        assert validated["resolution"] == "resolved"
        assert validated["session_tags"] == ["backend"]
        assert validated["privacy_flags"] == []
        assert validated["summary"] == "Added a feature."
        assert validated["effort_estimate"] == 0.7


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


class TestBackendSelection:
    def test_backend_choices_include_auto(self):
        assert SCORING_BACKEND_CHOICES == ("auto", "claude", "codex", "openclaw")

    def test_detect_current_agent_from_env_codex(self):
        env = {"CODEX_THREAD_ID": "thread-123"}
        assert _detect_current_agent_from_env(env) == "codex"

    def test_detect_current_agent_from_env_openclaw(self):
        env = {"OPENCLAW_STATE_DIR": "/tmp/openclaw"}
        assert _detect_current_agent_from_env(env) == "openclaw"

    def test_detect_current_agent_from_env_unknown(self):
        assert _detect_current_agent_from_env({}) is None

    def test_classify_process_command_codex(self):
        assert _classify_process_command("codex", "") == "codex"
        assert _classify_process_command("", "/opt/homebrew/bin/codex exec") == "codex"

    def test_classify_process_command_claude(self):
        assert _classify_process_command("claude", "") == "claude"
        assert _classify_process_command("", "/usr/local/bin/claude -p") == "claude"

    def test_classify_process_command_openclaw(self):
        assert _classify_process_command("openclaw", "") == "openclaw"
        assert _classify_process_command("", "/usr/local/bin/openclaw agent --json") == "openclaw"

    def test_resolve_backend_explicit(self):
        assert resolve_backend("codex", {}) == "codex"

    def test_resolve_backend_env_override(self):
        env = {"CLAWJOURNAL_SCORER_BACKEND": "claude"}
        assert resolve_backend("auto", env) == "claude"

    def test_resolve_backend_raises_without_current_agent(self, monkeypatch):
        monkeypatch.setattr("clawjournal.scoring.backends._detect_current_agent_from_process_tree", lambda **kw: None)
        with pytest.raises(RuntimeError, match="Could not detect the current agent"):
            resolve_backend("auto", {})

    def test_call_judge_dispatches_to_codex(self, monkeypatch):
        monkeypatch.setattr("clawjournal.scoring.scoring.load_scoring_rubric", lambda: "rubric")

        from clawjournal.scoring.scoring import _SCORE_TASK_PROMPT_CODEX

        captured = {}

        def fake_run(*, backend, cwd, task_prompt=None, **kw):
            captured["task_prompt"] = task_prompt
            captured["backend"] = backend
            captured["codex_output_schema"] = kw.get("codex_output_schema")
            scoring = {
                "substance": 4,
                **_failure_fields(ai_quality_score=4),
                "reasoning": "Good session",
                "display_title": "Fix auth tests",
                "summary": "Fixed auth tests.",
                "resolution": "resolved",
                "effort_estimate": 0.4,
                "task_type": "debugging",
                "session_tags": [],
                "privacy_flags": [],
                "project_areas": [],
            }
            (cwd / "scoring.json").write_text(json.dumps(scoring))
            return AgentResult(stdout="", stderr="", returncode=0, cwd=cwd)

        monkeypatch.setattr("clawjournal.scoring.scoring.run_default_agent_task", fake_run)
        result = call_judge(
            "prompt",
            session_data={"messages": []},
            metadata={"total_steps": 1},
            backend="codex",
        )
        assert result["substance"] == 4
        assert result["task_type"] == "debugging"
        assert captured["backend"] == "codex"
        assert captured["task_prompt"] == _SCORE_TASK_PROMPT_CODEX
        assert captured["codex_output_schema"] is not None

    def test_call_judge_writes_compact_session_metadata(self, monkeypatch):
        monkeypatch.setattr("clawjournal.scoring.scoring.load_scoring_rubric", lambda: "rubric")

        captured = {}

        def fake_run(*, cwd, **kw):
            captured["session_payload"] = json.loads((cwd / "session.json").read_text())
            scoring = {
                "substance": 4,
                **_failure_fields(ai_quality_score=4),
                "reasoning": "Good session",
                "display_title": "Fix auth tests",
                "summary": "Fixed auth tests.",
                "resolution": "resolved",
                "effort_estimate": 0.4,
                "task_type": "debugging",
                "session_tags": [],
                "privacy_flags": [],
                "project_areas": [],
            }
            (cwd / "scoring.json").write_text(json.dumps(scoring))
            return AgentResult(stdout="", stderr="", returncode=0, cwd=cwd)

        monkeypatch.setattr("clawjournal.scoring.scoring.run_default_agent_task", fake_run)
        call_judge(
            "prompt",
            session_data={
                "session_id": "sess-1",
                "project": "demo",
                "display_title": "Example",
                "messages": [{"role": "user", "content": "hello"}],
                "blob_path": "/tmp/blob.json",
                "raw_source_path": "/tmp/raw.jsonl",
                "commands_run": ["x" * 400],
                "files_touched": ["a.py", "b.py"],
            },
            metadata={"total_steps": 1},
            backend="codex",
        )
        assert captured["session_payload"]["session_id"] == "sess-1"
        assert captured["session_payload"]["project"] == "demo"
        assert captured["session_payload"]["files_touched"] == ["a.py", "b.py"]
        assert "messages" not in captured["session_payload"]
        assert "blob_path" not in captured["session_payload"]
        assert "raw_source_path" not in captured["session_payload"]
        assert len(captured["session_payload"]["commands_run"][0]) == 240

    def test_codex_judge_schema_forbids_additional_properties(self):
        assert JUDGE_SCHEMA["type"] == "object"
        assert JUDGE_SCHEMA["additionalProperties"] is False

    def test_check_backend_runtime_codex_is_non_blocking(self):
        env = {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}
        assert _check_backend_runtime("codex", env) is None

    def test_format_codex_runtime_error_for_network_failure(self):
        message = _format_codex_runtime_error(
            1,
            "ERROR failed to connect: failed to lookup address information: nodename nor servname provided, or not known",
        )
        assert "codex exec" in message
        assert "host shell" in message

    def test_format_codex_runtime_error_for_auth_failure(self):
        message = _format_codex_runtime_error(
            1,
            "401 Unauthorized",
        )
        assert "CODEX_API_KEY" in message
        assert "codex login" in message

    def test_format_codex_runtime_error_for_invalid_schema(self):
        message = _format_codex_runtime_error(
            1,
            'ERROR: {"error":{"code":"invalid_json_schema","message":"bad schema"}}',
        )
        assert "structured-output schema" in message
        assert "valid Codex JSON schema" in message

    def test_call_judge_dispatches_to_openclaw(self, monkeypatch):
        monkeypatch.setattr("clawjournal.scoring.scoring.load_scoring_rubric", lambda: "rubric")

        scoring = {
            "substance": 5,
            **_failure_fields(ai_quality_score=5, ai_failure_value_score=5),
            "reasoning": "Excellent session",
            "display_title": "Add retry logic",
            "summary": "Added retry logic to the API client.",
            "resolution": "resolved",
            "effort_estimate": 0.6,
            "task_type": "feature",
            "session_tags": ["backend", "api"],
            "privacy_flags": [],
            "project_areas": ["api/"],
        }
        captured = {}

        def fake_run(*, backend, cwd, openclaw_message=None, **kw):
            captured["openclaw_message"] = openclaw_message
            captured["backend"] = backend
            return AgentResult(stdout=json.dumps(scoring), stderr="", returncode=0, cwd=cwd)

        monkeypatch.setattr("clawjournal.scoring.scoring.run_default_agent_task", fake_run)
        result = call_judge(
            "prompt",
            session_data={"messages": []},
            metadata={"total_steps": 1},
            backend="openclaw",
        )
        assert result["substance"] == 5
        assert result["task_type"] == "feature"
        assert captured["backend"] == "openclaw"
        assert captured["openclaw_message"] is not None
        assert "absolute paths" in captured["openclaw_message"].lower()

    def test_extract_judge_result_from_nested_openclaw_json(self):
        payload = {
            "reply": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({
                            "substance": 4,
                            "reasoning": "Solid session",
                            "display_title": "Refactor auth flow",
                            "summary": "Refactored auth flow.",
                            "resolution": "resolved",
                            "effort_estimate": 0.5,
                            "task_type": "refactor",
                            "session_tags": ["backend"],
                            "privacy_flags": [],
                            "project_areas": ["auth/"],
                        }),
                    }
                ]
            }
        }
        result = _extract_judge_result_from_value(payload)
        assert result["substance"] == 4
        assert result["task_type"] == "refactor"


# ---------------------------------------------------------------------------
# _read_scoring_output
# ---------------------------------------------------------------------------

_VALID_JUDGE = {
    "substance": 4,
    **_failure_fields(ai_quality_score=4),
    "reasoning": "Good",
    "display_title": "Fix bug",
    "summary": "Fixed a bug in the auth module.",
    "resolution": "resolved",
    "effort_estimate": 0.45,
    "task_type": "debugging",
    "session_tags": ["debugging_cycle"],
    "privacy_flags": [],
    "project_areas": ["auth/"],
}


class TestReadScoringOutput:
    def test_reads_from_scoring_json(self, tmp_path):
        (tmp_path / "scoring.json").write_text(json.dumps(_VALID_JUDGE))
        result = AgentResult(stdout="", stderr="", returncode=0, cwd=tmp_path)
        parsed = _read_scoring_output(result, "claude")
        assert parsed["substance"] == 4

    def test_invalid_json_in_scoring_file_raises(self, tmp_path):
        (tmp_path / "scoring.json").write_text("not json {{{")
        result = AgentResult(stdout="", stderr="", returncode=0, cwd=tmp_path)
        with pytest.raises(RuntimeError, match="not valid JSON"):
            _read_scoring_output(result, "claude")

    def test_openclaw_reads_from_stdout(self, tmp_path):
        result = AgentResult(
            stdout=json.dumps(_VALID_JUDGE), stderr="", returncode=0, cwd=tmp_path,
        )
        parsed = _read_scoring_output(result, "openclaw")
        assert parsed["substance"] == 4

    def test_live_backend_output_requires_failure_fields(self, tmp_path):
        legacy_payload = {
            "substance": 4,
            "reasoning": "Good",
            "display_title": "Fix bug",
            "summary": "Fixed a bug.",
            "resolution": "resolved",
            "effort_estimate": 0.4,
            "task_type": "debugging",
            "session_tags": [],
            "privacy_flags": [],
            "project_areas": [],
        }
        result = AgentResult(
            stdout=json.dumps(legacy_payload), stderr="", returncode=0, cwd=tmp_path,
        )
        with pytest.raises(RuntimeError, match="failure-value fields"):
            _read_scoring_output(result, "openclaw")

    def test_openclaw_empty_stdout_raises(self, tmp_path):
        result = AgentResult(stdout="", stderr="", returncode=0, cwd=tmp_path)
        with pytest.raises(RuntimeError, match="did not produce scoring output"):
            _read_scoring_output(result, "openclaw")
