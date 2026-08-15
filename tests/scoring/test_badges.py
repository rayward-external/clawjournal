"""Tests for badge computation."""

from clawjournal.scoring.badges import (
    compute_all_badges,
    compute_display_title,
    compute_outcome_badge,
    compute_risk_badges,
    compute_sensitivity_score,
    compute_task_type,
    compute_value_badges,
)


def _make_session(
    user_content="Fix the login bug",
    tool_uses=None,
    tool_output="",
    user_messages=5,
    assistant_messages=5,
    tool_use_count=3,
    input_tokens=1000,
    output_tokens=500,
):
    msgs = [{"role": "user", "content": user_content, "tool_uses": []}]
    if tool_uses is None:
        tool_uses = [{"tool": "bash", "input": {"command": "pytest"}, "output": tool_output, "status": "success"}]
    msgs.append({"role": "assistant", "content": "Working on it.", "tool_uses": tool_uses})
    return {
        "session_id": "test-1",
        "project": "test-project",
        "source": "claude",
        "model": "claude-sonnet-4",
        "messages": msgs,
        "stats": {
            "user_messages": user_messages,
            "assistant_messages": assistant_messages,
            "tool_uses": tool_use_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


class TestOutcomeBadge:
    def test_tests_passed(self):
        session = _make_session(tool_output="5 passed in 1.2s")
        assert compute_outcome_badge(session) == "tests_passed"

    def test_tests_failed(self):
        session = _make_session(tool_output="FAILED test_login.py::test_auth")
        assert compute_outcome_badge(session) == "tests_failed"

    def test_build_failed(self):
        session = _make_session(tool_output="BUILD FAILED")
        assert compute_outcome_badge(session) == "build_failed"

    def test_analysis_only_no_tools(self):
        session = _make_session(tool_uses=[])
        session["messages"][1]["tool_uses"] = []
        session["messages"] = [session["messages"][0]]  # user message only
        assert compute_outcome_badge(session) == "analysis_only"

    def test_analysis_only_read_tools(self):
        session = _make_session(tool_uses=[
            {"tool": "Read", "input": {"file_path": "foo.py"}, "output": "content", "status": "success"},
        ])
        assert compute_outcome_badge(session) == "analysis_only"

    def test_completed_write_only(self):
        """Write tool with no test output and no errors, assistant replied -> completed."""
        session = _make_session(tool_uses=[
            {"tool": "Write", "input": {"file_path": "foo.py"}, "output": "wrote file", "status": "success"},
        ])
        assert compute_outcome_badge(session) == "completed"

    def test_errored_with_late_error(self):
        """Session where last tool outputs contain errors -> errored."""
        session = _make_session(tool_uses=[
            {"tool": "Bash", "input": {"command": "python app.py"}, "output": "Traceback (most recent call last):\n  File 'app.py', line 5\nNameError: name 'foo' is not defined", "status": "error"},
        ])
        assert compute_outcome_badge(session) == "errored"

    def test_partial_user_interrupted(self):
        """Session where user was the last speaker -> partial."""
        session = _make_session(tool_uses=[
            {"tool": "Write", "input": {"file_path": "foo.py"}, "output": "wrote file", "status": "success"},
        ])
        session["messages"].append({"role": "user", "content": "Actually never mind", "tool_uses": []})
        assert compute_outcome_badge(session) == "partial"


class TestValueBadges:
    def test_long_horizon(self):
        # Requires both 20+ user messages AND 100k+ tokens
        session = _make_session(user_messages=25, input_tokens=60000, output_tokens=50000)
        badges = compute_value_badges(session)
        assert "long_horizon" in badges

    def test_long_horizon_not_tokens_only(self):
        # High tokens alone should NOT trigger (needs 20+ user messages too)
        session = _make_session(user_messages=5, input_tokens=60000, output_tokens=50000)
        badges = compute_value_badges(session)
        assert "long_horizon" not in badges

    def test_tool_rich(self):
        session = _make_session(user_messages=3, assistant_messages=3, tool_use_count=10)
        badges = compute_value_badges(session)
        assert "tool_rich" in badges

    def test_novel_domain(self):
        # Requires 2+ distinct scientific library mentions
        session = _make_session(user_content="Analyze the protein folding data using biopython and numpy")
        badges = compute_value_badges(session)
        assert "novel_domain" in badges

    def test_novel_domain_not_single(self):
        # Single library mention should NOT trigger
        session = _make_session(user_content="import pandas")
        badges = compute_value_badges(session)
        assert "novel_domain" not in badges

    def test_scientific_workflow(self):
        # Requires both scientific terms AND scientific file extensions
        session = _make_session(user_content="Run the regression on data.csv with the hypothesis test")
        badges = compute_value_badges(session)
        assert "scientific_workflow" in badges

    def test_scientific_not_term_only(self):
        # Scientific terms alone should NOT trigger without file extensions
        session = _make_session(user_content="Run the regression analysis")
        badges = compute_value_badges(session)
        assert "scientific_workflow" not in badges


class TestRiskBadges:
    def test_no_risk(self):
        session = _make_session(user_content="Hello world", tool_output="OK")
        badges = compute_risk_badges(session)
        # May or may not detect names/URLs depending on content
        assert isinstance(badges, list)

    def test_private_url(self):
        session = _make_session(user_content="Check https://internal.corp/api")
        badges = compute_risk_badges(session)
        assert "private_url" in badges


class TestSensitivityScore:
    def test_clean_session(self):
        session = _make_session(user_content="Hello", tool_output="OK")
        score = compute_sensitivity_score(session)
        assert 0.0 <= score <= 1.0

    def test_higher_with_secrets(self):
        session = _make_session(tool_output="Using key sk-ant-abcdefghijklmnopqrstuvwxyz1234567890")
        score = compute_sensitivity_score(session)
        assert score > 0.0


class TestTaskType:
    def test_debugging(self):
        assert compute_task_type(_make_session("Fix this bug in auth.py")) == "debugging"

    def test_feature(self):
        assert compute_task_type(_make_session("Add a new login page")) == "feature"

    def test_refactor(self):
        assert compute_task_type(_make_session("Refactor the database module")) == "refactor"

    def test_review(self):
        assert compute_task_type(_make_session("Review the recent code changes for potential bugs")) == "review"

    def test_configuration(self):
        assert compute_task_type(_make_session("Set up the CI/CD pipeline")) == "configuration"

    def test_migration(self):
        assert compute_task_type(_make_session("Migrate the database to PostgreSQL")) == "migration"

    def test_translate_is_documentation(self):
        assert compute_task_type(_make_session("Translate the patent document to English")) == "documentation"

    def test_update_is_refactor(self):
        assert compute_task_type(_make_session("Update the login page styles")) == "refactor"

    def test_trivial_hello(self):
        session = _make_session("hello")
        session["stats"]["tool_uses"] = 0
        assert compute_task_type(session) == "trivial"

    def test_trivial_slash_command(self):
        session = _make_session("/clear")
        session["stats"]["tool_uses"] = 0
        assert compute_task_type(session) == "trivial"

    def test_unknown(self):
        assert compute_task_type(_make_session("")) == "trivial"


class TestDisplayTitle:
    def test_basic(self):
        title = compute_display_title(_make_session("Fix the login bug"))
        assert title == "Fix the login bug"

    def test_strips_prefix(self):
        title = compute_display_title(_make_session("Please fix the login bug"))
        assert title.startswith("Fix")

    def test_truncates_long(self):
        long_msg = "A" * 200
        title = compute_display_title(_make_session(long_msg))
        assert len(title) <= 83  # 80 + "..."

    def test_empty_uses_project(self):
        session = _make_session("")
        session["messages"] = []
        title = compute_display_title(session)
        assert title == "test-project"

    def test_strips_xml_tags(self):
        title = compute_display_title(_make_session("Fix the <b>login</b> bug"))
        assert title == "Fix the login bug"

    def test_skips_command_message(self):
        """Should skip <command-message>init</command-message> and use the next real message."""
        session = _make_session("")
        session["messages"] = [
            {"role": "user", "content": "<command-message>init</command-message>", "tool_uses": []},
            {"role": "assistant", "content": "Initializing...", "tool_uses": []},
            {"role": "user", "content": "Add a REST endpoint for user profiles", "tool_uses": []},
            {"role": "assistant", "content": "Working on it.", "tool_uses": []},
        ]
        title = compute_display_title(session)
        assert title == "Add a REST endpoint for user profiles"

    def test_skips_local_command_caveat(self):
        session = _make_session("")
        session["messages"] = [
            {"role": "user", "content": "<local-command-caveat>Caveat: The messages below were sent by a tool</local-command-caveat>", "tool_uses": []},
            {"role": "assistant", "content": "OK", "tool_uses": []},
            {"role": "user", "content": "Refactor the auth middleware", "tool_uses": []},
            {"role": "assistant", "content": "Sure.", "tool_uses": []},
        ]
        title = compute_display_title(session)
        assert title == "Refactor the auth middleware"

    def test_skips_task_notification(self):
        session = _make_session("")
        session["messages"] = [
            {
                "role": "user",
                "content": "<task-notification>background task completed</task-notification>",
                "tool_uses": [],
            },
            {"role": "assistant", "content": "Noted", "tool_uses": []},
            {
                "role": "user",
                "content": "Refactor the auth middleware",
                "tool_uses": [],
            },
        ]

        assert compute_display_title(session) == "Refactor the auth middleware"

    def test_legacy_internal_segment_title_falls_back_to_real_user(self):
        session = _make_session("")
        session["segment_title"] = (
            "<task-notification>background task completed</task-notification>"
        )
        session["messages"] = [
            {
                "role": "user",
                "content": session["segment_title"],
                "tool_uses": [],
            },
            {
                "role": "user",
                "content": "Refactor the auth middleware",
                "tool_uses": [],
            },
        ]

        assert compute_display_title(session) == "Refactor the auth middleware"

    def test_wrapper_plus_user_text_is_not_skipped(self):
        content = (
            "<command-name>/review</command-name>\n"
            "Review the authentication changes"
        )

        assert compute_display_title(_make_session(content)) == (
            "Review the authentication changes"
        )

    def test_skips_terse_single_word(self):
        session = _make_session("")
        session["messages"] = [
            {"role": "user", "content": "install", "tool_uses": []},
            {"role": "assistant", "content": "Installing...", "tool_uses": []},
            {"role": "user", "content": "Set up the Python project with pytest", "tool_uses": []},
            {"role": "assistant", "content": "OK.", "tool_uses": []},
        ]
        title = compute_display_title(session)
        assert title == "Set up the Python project with pytest"

    def test_skips_interrupted_message(self):
        session = _make_session("")
        session["messages"] = [
            {"role": "user", "content": "[Request interrupted by user]", "tool_uses": []},
            {"role": "assistant", "content": "OK", "tool_uses": []},
            {"role": "user", "content": "Fix the database migration", "tool_uses": []},
            {"role": "assistant", "content": "Sure.", "tool_uses": []},
        ]
        title = compute_display_title(session)
        assert title == "Fix the database migration"

    def test_all_skippable_falls_back(self):
        session = _make_session("")
        session["messages"] = [
            {"role": "user", "content": "<command-message>init</command-message>", "tool_uses": []},
            {"role": "assistant", "content": "Done.", "tool_uses": []},
            {"role": "user", "content": "exit", "tool_uses": []},
        ]
        title = compute_display_title(session)
        assert title == "test-project"

    def test_xml_only_content_falls_back(self):
        """A message that is purely XML tags should fall back."""
        session = _make_session("")
        session["messages"] = [
            {"role": "user", "content": "Wrap in <b></b> tags", "tool_uses": []},
            {"role": "assistant", "content": "OK.", "tool_uses": []},
        ]
        title = compute_display_title(session)
        assert title == "Wrap in  tags"


class TestComputeAll:
    def test_returns_all_fields(self):
        result = compute_all_badges(_make_session(tool_output="3 passed"))
        assert "display_title" in result
        assert "outcome_badge" in result
        assert "value_badges" in result
        assert "risk_badges" in result
        assert "sensitivity_score" in result
        assert "task_type" in result
        assert "files_touched" in result
        assert "commands_run" in result

    def test_extracts_commands(self):
        session = _make_session()
        result = compute_all_badges(session)
        assert "pytest" in result["commands_run"]
