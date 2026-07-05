"""Tests for clawjournal.parsing.parser — JSONL parsing and project discovery."""

import hashlib
import json
import sqlite3

import pytest

from clawjournal.parsing.parser import (
    _build_project_name,
    _build_cowork_project_name,
    _discover_aider_projects,
    _build_tool_result_map,
    _build_codex_tool_result_map,
    _extract_assistant_content,
    _extract_model_effort,
    _extract_user_content,
    _find_subagent_only_sessions,
    _iter_jsonl,
    _normalize_timestamp,
    _parse_session_file,
    _parse_subagent_session,
    _parse_tool_input,
    _path_to_dir_name,
    _process_entry,
    _scan_local_agent_sessions,
    discover_projects,
    parse_project_sessions,
    _parse_codex_session_file,
    _parse_openclaw_session_file,
)


def test_iter_jsonl_reads_utf8_explicitly(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    payload = {"message": "research note with Cyrillic: с"}
    path.write_bytes((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

    import builtins

    real_open = builtins.open
    seen_encodings = []

    def open_spy(file, *args, **kwargs):
        if file == path:
            seen_encodings.append(kwargs.get("encoding"))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", open_spy)

    assert list(_iter_jsonl(path)) == [payload]
    assert seen_encodings == ["utf-8"]


class TestExtractModelEffort:
    def test_codex_alias(self):
        assert _extract_model_effort({"effort": "high"}) == "high"

    def test_openai_sdk_alias(self):
        assert _extract_model_effort({"reasoningEffort": "medium"}) == "medium"

    def test_python_sdk_alias(self):
        assert _extract_model_effort({"reasoning_effort": "xhigh"}) == "xhigh"

    def test_nested_in_options(self):
        assert _extract_model_effort({"options": {"reasoningEffort": "low"}}) == "low"

    def test_nested_in_provider_options(self):
        assert _extract_model_effort({"providerOptions": {"effort": "high"}}) == "high"

    def test_strips_whitespace(self):
        assert _extract_model_effort({"effort": "  high  "}) == "high"

    def test_missing(self):
        assert _extract_model_effort({"model": "gpt-5"}) is None

    def test_non_dict(self):
        assert _extract_model_effort("high") is None
        assert _extract_model_effort(None) is None
        assert _extract_model_effort(["high"]) is None

    def test_empty_string_treated_as_missing(self):
        assert _extract_model_effort({"effort": ""}) is None
        assert _extract_model_effort({"effort": "   "}) is None


# --- _build_project_name ---


class TestBuildProjectName:
    def test_documents_prefix(self):
        assert _build_project_name("-Users-alice-Documents-myproject") == "claude:myproject"

    def test_home_prefix(self):
        assert _build_project_name("-home-bob-project") == "claude:project"

    def test_standalone(self):
        assert _build_project_name("standalone") == "claude:standalone"

    def test_deep_documents_path(self):
        result = _build_project_name("-Users-alice-Documents-work-repo")
        assert result == "claude:work-repo"

    def test_downloads_prefix(self):
        assert _build_project_name("-Users-alice-Downloads-thing") == "claude:thing"

    def test_desktop_prefix(self):
        assert _build_project_name("-Users-alice-Desktop-stuff") == "claude:stuff"

    def test_bare_home(self):
        # /Users/alice -> just username, no project
        assert _build_project_name("-Users-alice") == "claude:~home"

    def test_users_common_dir_only(self):
        # /Users/alice/Documents (no project after common dir)
        assert _build_project_name("-Users-alice-Documents") == "claude:~Documents"

    def test_home_bare(self):
        assert _build_project_name("-home-bob") == "claude:~home"

    def test_non_common_dir(self):
        # /Users/alice/code/myproject
        result = _build_project_name("-Users-alice-code-myproject")
        assert result == "claude:code-myproject"

    def test_empty_string(self):
        # Empty string: path="" -> parts=[""] -> meaningful=[""] -> returns "claude:"
        result = _build_project_name("")
        assert result == "claude:"

    def test_linux_deep_path(self):
        assert _build_project_name("-home-bob-projects-app") == "claude:projects-app"

    def test_hyphens_preserved_in_project_name(self):
        result = _build_project_name("-Users-alice-Documents-my-cool-project")
        assert result == "claude:my-cool-project"


class TestReadProjectCwd:
    def test_reads_cwd_from_session_jsonl(self, tmp_path):
        from clawjournal.parsing.parser import _read_project_cwd
        d = tmp_path / "proj"
        d.mkdir()
        (d / "s.jsonl").write_text(
            '{"type":"user","cwd":"/Users/x/Rayward/Codes/llm-gateway-infra"}\n'
        )
        assert _read_project_cwd(d) == "/Users/x/Rayward/Codes/llm-gateway-infra"

    def test_returns_none_when_no_cwd(self, tmp_path):
        from clawjournal.parsing.parser import _read_project_cwd
        d = tmp_path / "proj"
        d.mkdir()
        (d / "s.jsonl").write_text('{"type":"user","message":{"content":"hi"}}\n')
        assert _read_project_cwd(d) is None

    def test_returns_none_for_empty_dir(self, tmp_path):
        from clawjournal.parsing.parser import _read_project_cwd
        d = tmp_path / "empty"
        d.mkdir()
        assert _read_project_cwd(d) is None

    def test_reads_cwd_from_nested_subagent_file(self, tmp_path):
        """Subagent-only projects have no root *.jsonl — cwd lives in a subdir."""
        from clawjournal.parsing.parser import _read_project_cwd
        d = tmp_path / "proj"
        sa = d / "session-uuid" / "subagents"
        sa.mkdir(parents=True)
        (sa / "agent-a.jsonl").write_text(
            '{"type":"user","cwd":"/Users/x/Rayward/Codes/llm-gateway-infra"}\n'
        )
        assert _read_project_cwd(d) == "/Users/x/Rayward/Codes/llm-gateway-infra"

    def test_prefers_root_then_searches_extra_dirs(self, tmp_path):
        """Multiple dirs are searched in order; first cwd found wins."""
        from clawjournal.parsing.parser import _read_project_cwd
        empty = tmp_path / "empty"
        empty.mkdir()
        nested = tmp_path / "la" / "-sessions-proc"
        nested.mkdir(parents=True)
        (nested / "s.jsonl").write_text('{"type":"user","cwd":"/Users/x/code/app"}\n')
        assert _read_project_cwd(empty, nested) == "/Users/x/code/app"

    def test_ignores_none_and_missing_dirs(self, tmp_path):
        from clawjournal.parsing.parser import _read_project_cwd
        missing = tmp_path / "does-not-exist"
        assert _read_project_cwd(None, missing) is None


# --- _normalize_timestamp ---


class TestNormalizeTimestamp:
    def test_none(self):
        assert _normalize_timestamp(None) is None

    def test_string_passthrough(self):
        ts = "2025-01-15T10:00:00+00:00"
        assert _normalize_timestamp(ts) == ts

    def test_int_ms_to_iso(self):
        # 1706000000000 ms = 2024-01-23T09:33:20+00:00
        result = _normalize_timestamp(1706000000000)
        assert result is not None
        assert "2024" in result
        assert "T" in result

    def test_float_ms_to_iso(self):
        result = _normalize_timestamp(1706000000000.0)
        assert result is not None
        assert "T" in result

    def test_other_type_returns_none(self):
        assert _normalize_timestamp([1, 2, 3]) is None
        assert _normalize_timestamp({"ts": 123}) is None


# --- _parse_tool_input ---


class TestParseToolInput:
    def test_read_tool(self, mock_anonymizer):
        result = _parse_tool_input("Read", {"file_path": "/tmp/test.py"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "test.py" in result["file_path"]

    def test_write_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Write", {"file_path": "/tmp/test.py", "content": "abc"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "content" in result

    def test_bash_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Bash",
            {"command": "ls -la", "timeout": 5000, "description": "List files", "run_in_background": False},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["command"] == "ls -la"
        assert result["timeout"] == 5000
        assert result["description"] == "List files"
        assert result["run_in_background"] is False

    def test_grep_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Grep",
            {"pattern": "TODO", "path": "/tmp", "output_mode": "content", "-n": True, "glob": "*.py"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "pattern" in result
        assert "path" in result
        assert result["output_mode"] == "content"
        assert result["-n"] is True
        assert result["glob"] == "*.py"

    def test_glob_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Glob", {"pattern": "*.py", "path": "/tmp"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["pattern"] == "*.py"

    def test_task_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Task", {"prompt": "Search for bugs"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "Search for bugs" in result["prompt"]

    def test_websearch_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "WebSearch", {"query": "python async"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["query"] == "python async"

    def test_webfetch_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "WebFetch", {"url": "https://example.com"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["url"] == "https://example.com"

    def test_edit_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "Edit",
            {"file_path": "/tmp/test.py", "old_string": "foo()", "new_string": "bar()"},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "file_path" in result
        assert "old_string" in result
        assert "new_string" in result

    def test_exec_command_tool(self, mock_anonymizer):
        result = _parse_tool_input("exec_command", {"cmd": "ls -la"}, mock_anonymizer)
        assert isinstance(result, dict)
        assert result["cmd"] == "ls -la"

    def test_shell_command_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "shell_command", {"command": "ls", "workdir": "/tmp"}, mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert result["command"] == "ls"
        assert "workdir" in result

    def test_update_plan_tool(self, mock_anonymizer):
        result = _parse_tool_input(
            "update_plan",
            {"explanation": "New plan", "plan": [{"step": "do it", "status": "pending"}]},
            mock_anonymizer,
        )
        assert isinstance(result, dict)
        assert "explanation" in result
        assert "plan" in result

    def test_unknown_tool(self, mock_anonymizer):
        result = _parse_tool_input("CustomTool", {"foo": "bar"}, mock_anonymizer)
        assert isinstance(result, dict)

    def test_none_tool_name(self, mock_anonymizer):
        result = _parse_tool_input(None, {"data": "value"}, mock_anonymizer)
        assert isinstance(result, dict)

    def test_read_tool_preserves_all_fields(self, mock_anonymizer):
        result = _parse_tool_input(
            "Read", {"file_path": "/tmp/test.py", "offset": 10, "limit": 50}, mock_anonymizer,
        )
        assert "file_path" in result
        assert result["offset"] == 10
        assert result["limit"] == 50

    def test_non_dict_input(self, mock_anonymizer):
        result = _parse_tool_input("Read", "just a string", mock_anonymizer)
        assert isinstance(result, dict)
        assert "raw" in result


# --- _extract_user_content ---


class TestExtractUserContent:
    def test_string_content(self, mock_anonymizer):
        entry = {"message": {"content": "Fix the bug"}}
        result = _extract_user_content(entry, mock_anonymizer)
        assert result == "Fix the bug"

    def test_list_content(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "text", "text": "World"},
                ]
            }
        }
        result = _extract_user_content(entry, mock_anonymizer)
        assert "Hello" in result
        assert "World" in result

    def test_empty_content(self, mock_anonymizer):
        entry = {"message": {"content": ""}}
        assert _extract_user_content(entry, mock_anonymizer) is None

    def test_whitespace_content(self, mock_anonymizer):
        entry = {"message": {"content": "   \n  "}}
        assert _extract_user_content(entry, mock_anonymizer) is None

    def test_missing_message(self, mock_anonymizer):
        entry = {}
        assert _extract_user_content(entry, mock_anonymizer) is None


# --- _extract_assistant_content ---


class TestExtractAssistantContent:
    def test_text_blocks(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "text", "text": "Here's the fix."},
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, include_thinking=True)
        assert result is not None
        assert result["content"] == "Here's the fix."

    def test_thinking_included(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Let me think..."},
                    {"type": "text", "text": "Done."},
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, include_thinking=True)
        assert "thinking" in result
        assert "Let me think..." in result["thinking"]

    def test_thinking_excluded(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "Let me think..."},
                    {"type": "text", "text": "Done."},
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, include_thinking=False)
        assert "thinking" not in result

    def test_tool_uses(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    },
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, include_thinking=True)
        assert result is not None
        assert len(result["tool_uses"]) == 1
        assert result["tool_uses"][0]["tool"] == "Read"

    def test_empty_content(self, mock_anonymizer):
        entry = {"message": {"content": []}}
        assert _extract_assistant_content(entry, mock_anonymizer, True) is None

    def test_non_list_content(self, mock_anonymizer):
        entry = {"message": {"content": "just a string"}}
        assert _extract_assistant_content(entry, mock_anonymizer, True) is None

    def test_non_dict_block_skipped(self, mock_anonymizer):
        entry = {
            "message": {
                "content": [
                    "not a dict",
                    {"type": "text", "text": "Valid."},
                ]
            }
        }
        result = _extract_assistant_content(entry, mock_anonymizer, True)
        assert result is not None
        assert result["content"] == "Valid."


# --- _process_entry ---


class TestProcessEntry:
    def _run(self, entry, anonymizer, include_thinking=True):
        messages = []
        metadata = {
            "session_id": "test", "cwd": None, "git_branch": None,
            "claude_version": None, "model": None,
            "start_time": None, "end_time": None,
        }
        stats = {
            "user_messages": 0, "assistant_messages": 0,
            "tool_uses": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        }
        _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking)
        return messages, metadata, stats

    def test_user_entry(self, mock_anonymizer, sample_user_entry):
        msgs, meta, stats = self._run(sample_user_entry, mock_anonymizer)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert stats["user_messages"] == 1
        assert meta["git_branch"] == "main"

    def test_assistant_entry(self, mock_anonymizer, sample_assistant_entry):
        msgs, meta, stats = self._run(sample_assistant_entry, mock_anonymizer)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        assert stats["assistant_messages"] == 1
        assert stats["input_tokens"] > 0
        assert stats["output_tokens"] > 0

    def test_unknown_type(self, mock_anonymizer):
        entry = {"type": "system", "message": {}}
        msgs, _, _ = self._run(entry, mock_anonymizer)
        assert len(msgs) == 0

    def test_metadata_extraction(self, mock_anonymizer, sample_user_entry):
        _, meta, _ = self._run(sample_user_entry, mock_anonymizer)
        assert meta["cwd"] is not None
        assert meta["claude_version"] == "1.0.0"
        assert meta["start_time"] is not None


# --- _parse_session_file ---


class TestParseSessionFile:
    def test_valid_jsonl(self, tmp_path, mock_anonymizer):
        f = tmp_path / "session.jsonl"
        entries = [
            {"type": "user", "timestamp": 1706000000000,
             "message": {"content": "Hello"}, "cwd": "/tmp/proj"},
            {"type": "assistant", "timestamp": 1706000001000,
             "message": {
                 "model": "claude-sonnet-4-20250514",
                 "content": [{"type": "text", "text": "Hi there!"}],
                 "usage": {"input_tokens": 10, "output_tokens": 5},
             }},
        ]
        f.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _parse_session_file(f, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 2
        assert result["model"] == "claude-sonnet-4-20250514"

    def test_malformed_lines_skipped(self, tmp_path, mock_anonymizer):
        f = tmp_path / "session.jsonl"
        f.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hello"},"cwd":"/tmp"}\n'
            "not valid json\n"
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )
        result = _parse_session_file(f, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 2

    def test_empty_file(self, tmp_path, mock_anonymizer):
        f = tmp_path / "session.jsonl"
        f.write_text("")
        result = _parse_session_file(f, mock_anonymizer)
        assert result is None

    def test_oserror_returns_none(self, tmp_path, mock_anonymizer):
        f = tmp_path / "nonexistent.jsonl"
        result = _parse_session_file(f, mock_anonymizer)
        assert result is None

    def test_blank_lines_skipped(self, tmp_path, mock_anonymizer):
        f = tmp_path / "session.jsonl"
        f.write_text(
            "\n\n"
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},"cwd":"/tmp"}\n'
            "\n"
        )
        result = _parse_session_file(f, mock_anonymizer)
        assert result is not None
        assert len(result["messages"]) == 1


# --- discover_projects + parse_project_sessions ---


class TestDiscoverProjects:
    def _disable_codex(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-claude-projects")
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", tmp_path / "no-local-agent")
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", tmp_path / "no-claude-science")
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("clawjournal.parsing.parser.CURSOR_DIR", tmp_path / "no-cursor")
        monkeypatch.setattr("clawjournal.parsing.parser._CURSOR_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.COPILOT_DIR", tmp_path / "no-copilot")
        monkeypatch.setattr("clawjournal.parsing.parser._AIDER_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser._get_aider_project_index", lambda refresh=False: {})

    def _write_opencode_db(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE session ("
            "id TEXT PRIMARY KEY, "
            "directory TEXT, "
            "time_created INTEGER, "
            "time_updated INTEGER"
            ")"
        )
        conn.execute(
            "CREATE TABLE message ("
            "id TEXT PRIMARY KEY, "
            "session_id TEXT, "
            "time_created INTEGER, "
            "data TEXT"
            ")"
        )
        conn.execute(
            "CREATE TABLE part ("
            "id TEXT PRIMARY KEY, "
            "message_id TEXT, "
            "time_created INTEGER, "
            "data TEXT"
            ")"
        )
        conn.commit()
        return conn

    def test_with_projects(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-alice-Documents-myapp"
        proj.mkdir(parents=True)

        # Write a valid session file
        session = proj / "abc-123.jsonl"
        session.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},"cwd":"/Users/alice/Documents/myapp"}\n'
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hey"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )

        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["display_name"] == "claude:myapp"
        assert projects[0]["session_count"] == 1

    def test_discover_projects_uses_cwd_basename_for_hyphenated_project(
        self, tmp_path, monkeypatch, mock_anonymizer
    ):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        dir_name = "-Users-testuser-Rayward-Codes-llm-gateway-infra"
        proj = projects_dir / dir_name
        proj.mkdir(parents=True)
        (proj / "session1.jsonl").write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},'
            '"cwd":"/Users/testuser/Rayward/Codes/llm-gateway-infra"}\n'
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m",'
            '"content":[{"type":"text","text":"Ok"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)

        projects = discover_projects(source_filter="claude")
        sessions = parse_project_sessions(dir_name, mock_anonymizer)

        assert len(projects) == 1
        assert projects[0]["display_name"] == "claude:llm-gateway-infra"
        assert sessions[0]["project"] == projects[0]["display_name"]

    def test_no_projects_dir(self, tmp_path, monkeypatch):
        self._disable_codex(tmp_path, monkeypatch)
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "nonexistent")
        assert discover_projects() == []

    def test_empty_project_dir(self, tmp_path, monkeypatch):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "empty-project"
        proj.mkdir(parents=True)
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        assert discover_projects() == []

    def test_source_filter_only_runs_requested_discovery(self, monkeypatch):
        called = []

        def _stub(source_name):
            def inner():
                called.append(source_name)
                if source_name == "cursor":
                    return [{
                        "dir_name": "cursor-proj",
                        "display_name": "cursor:proj",
                        "session_count": 1,
                        "total_size_bytes": 1,
                        "source": "cursor",
                    }]
                return []
            return inner

        monkeypatch.setattr("clawjournal.parsing.parser._discover_claude_projects", _stub("claude"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_codex_projects", _stub("codex"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_gemini_projects", _stub("gemini"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_opencode_projects", _stub("opencode"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_openclaw_projects", _stub("openclaw"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_kimi_projects", _stub("kimi"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_cursor_projects", _stub("cursor"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_copilot_projects", _stub("copilot"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_aider_projects", _stub("aider"))
        monkeypatch.setattr("clawjournal.parsing.parser._discover_custom_projects", _stub("custom"))

        projects = discover_projects(source_filter="cursor")

        assert called == ["cursor"]
        assert projects == [{
            "dir_name": "cursor-proj",
            "display_name": "cursor:proj",
            "session_count": 1,
            "total_size_bytes": 1,
            "source": "cursor",
        }]

    def test_parse_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "test-project"
        proj.mkdir(parents=True)

        session = proj / "session1.jsonl"
        session.write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hello"},"cwd":"/Users/testuser/code/test-project"}\n'
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )

        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions("test-project", mock_anonymizer)
        assert len(sessions) == 1
        # Project is the cwd basename (consistent with codex/opencode/openclaw).
        assert sessions[0]["project"] == "claude:test-project"

    def test_parse_project_sessions_uses_cwd_basename_for_hyphenated_project(
        self, tmp_path, monkeypatch, mock_anonymizer
    ):
        """Regression: a project folder whose own name contains hyphens (e.g.
        'llm-gateway-infra') must yield just the basename. The hyphen-encoded
        dir name is ambiguous, so the real cwd in the session content is used —
        otherwise it leaks the full path as 'claude:Rayward-Codes-...'."""
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        dir_name = "-Users-testuser-Rayward-Codes-llm-gateway-infra"
        proj = projects_dir / dir_name
        proj.mkdir(parents=True)
        (proj / "session1.jsonl").write_text(
            '{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},'
            '"cwd":"/Users/testuser/Rayward/Codes/llm-gateway-infra"}\n'
            '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m",'
            '"content":[{"type":"text","text":"Ok"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
        )
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions(dir_name, mock_anonymizer)
        assert len(sessions) == 1
        assert sessions[0]["project"] == "claude:llm-gateway-infra"

    def test_subagent_only_project_uses_cwd_basename(
        self, tmp_path, monkeypatch, mock_anonymizer
    ):
        """Subagent-only native projects (no root *.jsonl) still recover the
        basename from the cwd recorded in the nested subagent transcript."""
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        dir_name = "-Users-testuser-Rayward-Codes-llm-gateway-infra"
        proj = projects_dir / dir_name
        sa = proj / "session-uuid" / "subagents"
        sa.mkdir(parents=True)
        (sa / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Do work", "2026-01-01T00:00:00Z",
                cwd="/Users/testuser/Rayward/Codes/llm-gateway-infra",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Done", "2026-01-01T00:00:01Z",
            )) + "\n"
        )
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions(dir_name, mock_anonymizer)
        assert len(sessions) == 1
        assert sessions[0]["project"] == "claude:llm-gateway-infra"

    def test_parse_nonexistent_project(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "projects")
        assert parse_project_sessions("nope", mock_anonymizer) == []

    def test_discover_codex_projects(self, tmp_path, monkeypatch):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir / "nonexistent")

        codex_sessions = tmp_path / "codex-sessions" / "2026" / "02" / "24"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-1.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "timestamp": "2026-02-24T16:09:59.567Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "session-1",
                        "cwd": "/Users/testuser/Documents/myrepo",
                        "model_provider": "openai",
                    },
                }
            ) + "\n"
        )

        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "codex"
        assert projects[0]["display_name"] == "codex:myrepo"

    def test_parse_codex_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "projects" / "nonexistent")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})

        codex_sessions = tmp_path / "codex-sessions" / "2026" / "02" / "24"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-1.jsonl"
        lines = [
            {
                "timestamp": "2026-02-24T16:09:59.567Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-1",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model_provider": "openai",
                    "git": {"branch": "main"},
                },
            },
            {
                "timestamp": "2026-02-24T16:09:59.568Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-1",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model": "gpt-5.3-codex",
                    "effort": "high",
                },
            },
            {
                "timestamp": "2026-02-24T16:09:59.569Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-2",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model": "gpt-5.3-codex",
                    "effort": "medium",
                },
            },
            {
                "timestamp": "2026-02-24T16:10:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "please list files",
                    "images": [],
                    "local_images": [],
                    "text_elements": [],
                },
            },
            {
                "timestamp": "2026-02-24T16:10:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "ls -la"}),
                },
            },
            {
                "timestamp": "2026-02-24T16:10:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "I checked the directory.",
                },
            },
            {
                "timestamp": "2026-02-24T16:10:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 30,
                            "output_tokens": 40,
                        }
                    },
                    "rate_limits": {},
                },
            },
        ]
        session_file.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        sessions = parse_project_sessions(
            "/Users/testuser/Documents/myrepo",
            mock_anonymizer,
            source="codex",
        )
        assert len(sessions) == 1
        assert sessions[0]["project"] == "codex:myrepo"
        assert sessions[0]["model"] == "gpt-5.3-codex"
        # First turn_context wins — we attribute the session to the mode
        # it was launched in, not whatever it was switched to later.
        assert sessions[0]["model_effort"] == "high"
        # OpenAI input_tokens (120) includes cached_input_tokens (30) as subset
        assert sessions[0]["stats"]["input_tokens"] == 90  # 120 - 30 non-cached
        assert sessions[0]["stats"]["cache_read_tokens"] == 30
        assert sessions[0]["stats"]["output_tokens"] == 40
        assert sessions[0]["messages"][0]["role"] == "user"
        assert sessions[0]["messages"][1]["role"] == "assistant"
        assert sessions[0]["messages"][1]["tool_uses"][0]["tool"] == "exec_command"
        assert sessions[0]["raw_source_path"] == str(session_file)

    def test_codex_thinking_not_duplicated(self, tmp_path, monkeypatch, mock_anonymizer):
        """Reasoning from response_item and agent_reasoning event_msg should not duplicate."""
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "projects" / "nonexistent")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})

        codex_sessions = tmp_path / "codex-sessions" / "2026" / "02" / "25"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-2.jsonl"
        lines = [
            {
                "timestamp": "2026-02-25T10:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "session-2",
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-02-25T10:00:00.001Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/Users/testuser/Documents/myrepo",
                    "model": "gpt-5.3-codex",
                },
            },
            {
                "timestamp": "2026-02-25T10:00:01.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "fix the bug"},
            },
            {
                "timestamp": "2026-02-25T10:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "Planning fix"}, {"text": "Reading code"}],
                },
            },
            {
                "timestamp": "2026-02-25T10:00:02.001Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": "Planning fix"},
            },
            {
                "timestamp": "2026-02-25T10:00:02.002Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": "Reading code"},
            },
            {
                "timestamp": "2026-02-25T10:00:03.000Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "I found the issue."},
            },
        ]
        session_file.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        from clawjournal.redaction.anonymizer import Anonymizer
        anonymizer = Anonymizer()

        result = _parse_codex_session_file(
            session_file, anonymizer, include_thinking=True,
            target_cwd="/Users/testuser/Documents/myrepo",
        )
        assert result is not None
        assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        thinking = assistant_msgs[0]["thinking"]
        paragraphs = [p.strip() for p in thinking.split("\n\n") if p.strip()]
        assert paragraphs == ["Planning fix", "Reading code"]

    def test_discover_opencode_projects(self, tmp_path, monkeypatch):
        self._disable_codex(tmp_path, monkeypatch)
        db_path = tmp_path / "opencode.db"
        conn = self._write_opencode_db(db_path)
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            ("ses_1", "/Users/testuser/work/repo", 1706000000000, 1706000002000),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "opencode"
        assert projects[0]["display_name"] == "opencode:repo"

    def test_parse_opencode_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        db_path = tmp_path / "opencode.db"
        conn = self._write_opencode_db(db_path)

        session_id = "ses_1"
        cwd = "/Users/testuser/work/repo"
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated) VALUES (?, ?, ?, ?)",
            (session_id, cwd, 1706000000000, 1706000005000),
        )

        user_msg_data = {
            "role": "user",
            "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
        }
        assistant_msg_data = {
            "role": "assistant",
            "model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
            "tokens": {
                "input": 120,
                "output": 40,
                "reasoning": 10,
                "cache": {"read": 30, "write": 0},
            },
        }
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("msg_1", session_id, 1706000001000, json.dumps(user_msg_data)),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("msg_2", session_id, 1706000002000, json.dumps(assistant_msg_data)),
        )

        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("prt_1", "msg_1", 1706000001001, json.dumps({"type": "text", "text": "please list files"})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            ("prt_2", "msg_2", 1706000002001, json.dumps({"type": "reasoning", "text": "Thinking..."})),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_3",
                "msg_2",
                1706000002002,
                json.dumps(
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "ls -la"}},
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO part (id, message_id, time_created, data) VALUES (?, ?, ?, ?)",
            (
                "prt_4",
                "msg_2",
                1706000002003,
                json.dumps({"type": "text", "text": "I checked the directory."}),
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", db_path)
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="opencode")
        assert len(sessions) == 1
        assert sessions[0]["project"] == "opencode:repo"
        assert sessions[0]["model"] == "openai/gpt-5.3-codex"
        # input (120) is non-cached; cache.read (30) / cache.write (0) are separate
        # buckets so the cache read prices at 0.1x rather than the full input rate.
        assert sessions[0]["stats"]["input_tokens"] == 120
        assert sessions[0]["stats"]["output_tokens"] == 40
        assert sessions[0]["stats"]["cache_read_tokens"] == 30
        assert sessions[0]["stats"]["cache_creation_tokens"] == 0
        assert sessions[0]["messages"][0]["role"] == "user"
        assert sessions[0]["messages"][1]["role"] == "assistant"
        assert sessions[0]["messages"][1]["tool_uses"][0]["tool"] == "bash"


# --- Subagent-only session discovery and parsing ---


def _make_subagent_entry(role, content, timestamp, cwd=None, session_id=None):
    """Build a minimal JSONL entry matching the subagent file format."""
    entry = {"timestamp": timestamp}
    if role == "user":
        entry["type"] = "user"
        entry["message"] = {"content": content}
        if cwd:
            entry["cwd"] = cwd
            entry["gitBranch"] = "main"
            entry["version"] = "2.1.2"
        if session_id:
            entry["sessionId"] = session_id
    elif role == "assistant":
        entry["type"] = "assistant"
        entry["message"] = {
            "model": "claude-opus-4-5-20251101",
            "content": [{"type": "text", "text": content}],
            "usage": {"input_tokens": 50, "output_tokens": 20},
        }
    return entry


class TestFindSubagentOnlySessions:
    def test_finds_subagent_dirs_without_root_jsonl(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()

        # Session with root JSONL — should NOT be returned.
        (proj / "has-root.jsonl").write_text("{}\n")
        sa_dir = proj / "has-root" / "subagents"
        sa_dir.mkdir(parents=True)
        (sa_dir / "agent-a1.jsonl").write_text("{}\n")

        # Session with only subagent data — SHOULD be returned.
        sa_dir2 = proj / "subagent-only" / "subagents"
        sa_dir2.mkdir(parents=True)
        (sa_dir2 / "agent-b1.jsonl").write_text("{}\n")

        result = _find_subagent_only_sessions(proj)
        assert len(result) == 1
        assert result[0].name == "subagent-only"

    def test_ignores_dirs_without_subagents(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()

        # Directory with only tool-results, no subagents.
        (proj / "tool-only" / "tool-results").mkdir(parents=True)

        result = _find_subagent_only_sessions(proj)
        assert result == []

    def test_ignores_empty_subagent_dirs(self, tmp_path):
        proj = tmp_path / "project"
        (proj / "empty-sa" / "subagents").mkdir(parents=True)

        result = _find_subagent_only_sessions(proj)
        assert result == []

    def test_returns_empty_for_no_dirs(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()
        (proj / "session.jsonl").write_text("{}\n")

        result = _find_subagent_only_sessions(proj)
        assert result == []


class TestParseSubagentSession:
    def test_merges_multiple_files_sorted_by_timestamp(self, tmp_path, mock_anonymizer):
        session = tmp_path / "abc-123"
        sa_dir = session / "subagents"
        sa_dir.mkdir(parents=True)

        # Write entries across two subagent files with interleaved timestamps.
        (sa_dir / "agent-a1.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "First message", "2026-01-10T08:00:00Z",
                cwd="/tmp/proj", session_id="abc-123",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Third reply", "2026-01-10T08:02:00Z",
            )) + "\n"
        )
        (sa_dir / "agent-b2.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "assistant", "Second reply", "2026-01-10T08:01:00Z",
            )) + "\n"
        )

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is not None
        assert result["session_id"] == "abc-123"
        assert len(result["messages"]) == 3
        # Verify sort order: user(08:00), assistant(08:01), assistant(08:02)
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "First message"
        assert result["messages"][1]["content"] == "Second reply"
        assert result["messages"][2]["content"] == "Third reply"
        assert result["model"] == "claude-opus-4-5-20251101"

    def test_returns_none_for_empty_subagents(self, tmp_path, mock_anonymizer):
        session = tmp_path / "empty"
        (session / "subagents").mkdir(parents=True)

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is None

    def test_returns_none_for_no_subagent_dir(self, tmp_path, mock_anonymizer):
        session = tmp_path / "no-sa"
        session.mkdir()

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is None

    def test_returns_none_when_no_messages(self, tmp_path, mock_anonymizer):
        session = tmp_path / "no-msgs"
        sa_dir = session / "subagents"
        sa_dir.mkdir(parents=True)
        # Entry with unknown type — produces no messages.
        (sa_dir / "agent-x.jsonl").write_text(
            json.dumps({"type": "system", "timestamp": "2026-01-01T00:00:00Z"}) + "\n"
        )

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is None

    def test_stats_aggregated(self, tmp_path, mock_anonymizer):
        session = tmp_path / "stats-test"
        sa_dir = session / "subagents"
        sa_dir.mkdir(parents=True)

        (sa_dir / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Hello", "2026-01-10T10:00:00Z", cwd="/tmp/p",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Hi", "2026-01-10T10:00:01Z",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Done", "2026-01-10T10:00:02Z",
            )) + "\n"
        )

        result = _parse_subagent_session(session, mock_anonymizer)
        assert result is not None
        assert result["stats"]["user_messages"] == 1
        assert result["stats"]["assistant_messages"] == 2
        assert result["stats"]["input_tokens"] == 100  # 50 * 2
        assert result["stats"]["output_tokens"] == 40  # 20 * 2


class TestDiscoverSubagentProjects:
    """Verify discover_projects and parse_project_sessions include subagent-only sessions."""

    def _disable_codex(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", tmp_path / "no-local-agent")
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", tmp_path / "no-claude-science")
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("clawjournal.parsing.parser.CURSOR_DIR", tmp_path / "no-cursor")
        monkeypatch.setattr("clawjournal.parsing.parser._CURSOR_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.COPILOT_DIR", tmp_path / "no-copilot")
        monkeypatch.setattr("clawjournal.parsing.parser._AIDER_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser._get_aider_project_index", lambda refresh=False: {})

    def test_discover_includes_subagent_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-alice-Documents-research"
        proj.mkdir(parents=True)

        # One root session.
        (proj / "root-session.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Hi", "2026-01-01T00:00:00Z", cwd="/Users/alice/Documents/research",
            )) + "\n"
        )

        # One subagent-only session.
        sa_dir = proj / "subagent-session" / "subagents"
        sa_dir.mkdir(parents=True)
        (sa_dir / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Build it", "2026-01-02T00:00:00Z", cwd="/Users/alice/Documents/research",
            )) + "\n"
        )

        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 2
        assert projects[0]["display_name"] == "claude:research"

    def test_discover_subagent_only_project(self, tmp_path, monkeypatch, mock_anonymizer):
        """A project with zero root .jsonl but subagent sessions should still appear."""
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "subagent-project"
        proj.mkdir(parents=True)

        sa_dir = proj / "session-uuid" / "subagents"
        sa_dir.mkdir(parents=True)
        (sa_dir / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Do work", "2026-01-01T00:00:00Z", cwd="/Users/alice/Documents/subagent-project",
            )) + "\n"
        )

        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 1

    def test_parse_includes_subagent_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_codex(tmp_path, monkeypatch)
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "mixed-project"
        proj.mkdir(parents=True)

        # Root session.
        (proj / "root.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "Root msg", "2026-01-01T00:00:00Z", cwd="/Users/alice/Documents/mixed-project",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "Root reply", "2026-01-01T00:00:01Z",
            )) + "\n"
        )

        # Subagent-only session.
        sa_dir = proj / "sa-session" / "subagents"
        sa_dir.mkdir(parents=True)
        (sa_dir / "agent-a.jsonl").write_text(
            json.dumps(_make_subagent_entry(
                "user", "SA msg", "2026-01-02T00:00:00Z", cwd="/Users/alice/Documents/mixed-project",
            )) + "\n"
            + json.dumps(_make_subagent_entry(
                "assistant", "SA reply", "2026-01-02T00:00:01Z",
            )) + "\n"
        )

        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions("mixed-project", mock_anonymizer)
        assert len(sessions) == 2
        contents = {s["messages"][0]["content"] for s in sessions}
        assert "Root msg" in contents
        assert "SA msg" in contents


# --- _build_tool_result_map (Claude tool outputs) ---


class TestBuildToolResultMap:
    def test_basic_string_output(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-1",
                            "content": "file contents here",
                            "is_error": False,
                        }
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert "tu-1" in result
        assert result["tu-1"]["status"] == "success"
        assert result["tu-1"]["output"]["text"] == "file contents here"

    def test_error_result(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-2",
                            "content": "Permission denied",
                            "is_error": True,
                        }
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert result["tu-2"]["status"] == "error"

    def test_list_content(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-3",
                            "content": [
                                {"type": "text", "text": "Part one"},
                                {"type": "text", "text": "Part two"},
                            ],
                        }
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert "Part one" in result["tu-3"]["output"]["text"]
        assert "Part two" in result["tu-3"]["output"]["text"]

    def test_empty_content_gives_empty_output(self, mock_anonymizer):
        entries = [
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu-4", "content": ""}
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert result["tu-4"]["output"] == {}

    def test_non_user_entries_ignored(self, mock_anonymizer):
        entries = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu-5", "content": "ignored"}
                    ]
                },
            }
        ]
        result = _build_tool_result_map(entries, mock_anonymizer)
        assert "tu-5" not in result

    def test_tool_output_attached_in_session(self, tmp_path, mock_anonymizer):
        """End-to-end: tool_use in assistant entry gets output from tool_result in user entry."""
        f = tmp_path / "session.jsonl"
        entries = [
            {
                "type": "assistant",
                "timestamp": 1706000001000,
                "message": {
                    "model": "claude-sonnet",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-abc",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            {
                "type": "user",
                "timestamp": 1706000002000,
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-abc",
                            "content": "file1.py\nfile2.py",
                            "is_error": False,
                        }
                    ]
                },
            },
        ]
        f.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = _parse_session_file(f, mock_anonymizer)
        assert result is not None
        tu = result["messages"][0]["tool_uses"][0]
        assert tu["tool"] == "Bash"
        assert tu["id"] == "tu-abc"
        assert tu["status"] == "success"
        assert "file1.py" in tu["output"]["text"]


# --- _build_codex_tool_result_map ---


class TestBuildCodexToolResultMap:
    def test_function_call_output(self, mock_anonymizer):
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Exit code: 0\nWall time: 1 seconds\nOutput:\nhello world\n",
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-1" in result
        assert result["call-1"]["status"] == "success"
        assert result["call-1"]["output"]["exit_code"] == 0
        assert result["call-1"]["output"]["wall_time"] == "1 seconds"
        assert "hello world" in result["call-1"]["output"]["output"]

    def test_custom_tool_call_output(self, mock_anonymizer):
        import json as _json
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-2",
                    "output": _json.dumps({
                        "output": "Successfully applied patch",
                        "metadata": {"exit_code": 0, "duration_seconds": 0.5},
                    }),
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-2" in result
        assert result["call-2"]["output"]["exit_code"] == 0
        assert "Successfully applied patch" in result["call-2"]["output"]["output"]
        assert result["call-2"]["output"]["duration_seconds"] == 0.5

    def test_function_call_output_list_form(self, mock_anonymizer):
        """Codex sessions may deliver output as a list of content blocks (e.g. images)."""
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-img",
                    "output": [
                        {"type": "input_text", "text": "screenshot captured"},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-img" in result
        assert result["call-img"]["status"] == "success"
        assert "screenshot captured" in result["call-img"]["output"]["output"]

    def test_function_call_output_list_image_only(self, mock_anonymizer):
        """List-form output with only images should not raise."""
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-img2",
                    "output": [
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-img2" in result
        assert result["call-img2"]["status"] == "success"

    def test_custom_tool_call_output_list_form(self, mock_anonymizer):
        """Custom tool output may also arrive as a list of content blocks."""
        entries = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-custom-list",
                    "output": [
                        {"type": "input_text", "text": "patch applied"},
                    ],
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-custom-list" in result
        assert "patch applied" in result["call-custom-list"]["output"]["output"]

    def test_non_response_item_ignored(self, mock_anonymizer):
        entries = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-3",
                    "output": "ignored",
                },
            }
        ]
        result = _build_codex_tool_result_map(entries, mock_anonymizer)
        assert "call-3" not in result

    def test_output_attached_end_to_end(self, tmp_path, monkeypatch, mock_anonymizer):
        """Codex tool output is attached to the tool_use in the parsed session."""
        import json as _json
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})

        codex_sessions = tmp_path / "codex-sessions" / "2026" / "02" / "24"
        codex_sessions.mkdir(parents=True)
        session_file = codex_sessions / "rollout-1.jsonl"
        lines = [
            {
                "timestamp": "2026-02-24T16:09:59.567Z",
                "type": "session_meta",
                "payload": {"id": "s1", "cwd": "/home/user/repo", "model_provider": "openai"},
            },
            {
                "timestamp": "2026-02-24T16:10:00.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "run ls"},
            },
            {
                "timestamp": "2026-02-24T16:10:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "call-x",
                    "arguments": _json.dumps({"command": "ls", "workdir": "/home/user/repo"}),
                },
            },
            {
                "timestamp": "2026-02-24T16:10:00.200Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-x",
                    "output": "Exit code: 0\nWall time: 0 seconds\nOutput:\nfoo.py\nbar.py\n",
                },
            },
            {
                "timestamp": "2026-02-24T16:10:01.000Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "Done."},
            },
        ]
        session_file.write_text("\n".join(_json.dumps(l) for l in lines) + "\n")

        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "codex-archived")

        result = _parse_codex_session_file(
            session_file, mock_anonymizer, include_thinking=True,
            target_cwd="/home/user/repo",
        )
        assert result is not None
        assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        tu = assistant_msgs[0]["tool_uses"][0]
        assert tu["tool"] == "shell_command"
        assert tu["status"] == "success"
        assert tu["output"]["exit_code"] == 0
        assert "foo.py" in tu["output"]["output"]


# --- OpenClaw session parsing ---


def _make_openclaw_session_header(session_id="oc-sess-1", cwd="/Users/alice/projects/myapp"):
    return {
        "type": "session",
        "id": session_id,
        "cwd": cwd,
        "timestamp": "2026-02-20T10:00:00.000Z",
    }


def _make_openclaw_user_message(text, timestamp="2026-02-20T10:01:00.000Z"):
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def _make_openclaw_assistant_message(
    text, timestamp="2026-02-20T10:02:00.000Z", model="claude-sonnet-4-20250514",
    thinking=None, tool_calls=None, usage=None,
):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    if text:
        content.append({"type": "text", "text": text})
    for tc in (tool_calls or []):
        content.append(tc)
    msg = {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
        },
    }
    if usage:
        msg["message"]["usage"] = usage
    return msg


def _make_openclaw_tool_result(tool_call_id, output_text, is_error=False):
    return {
        "type": "message",
        "timestamp": "2026-02-20T10:02:30.000Z",
        "message": {
            "role": "toolResult",
            "toolCallId": tool_call_id,
            "content": [{"type": "text", "text": output_text}],
            "isError": is_error,
        },
    }


class TestParseOpenclawSessionFile:
    def test_basic_conversation(self, mock_anonymizer):
        """Parse a simple user/assistant conversation."""
        import tempfile
        from pathlib import Path

        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Hello"),
            _make_openclaw_assistant_message("Hi there!", usage={"input": 50, "output": 20}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result is not None
        assert result["session_id"] == "oc-sess-1"
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "Hello"
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"] == "Hi there!"
        assert result["stats"]["user_messages"] == 1
        assert result["stats"]["assistant_messages"] == 1
        assert result["stats"]["input_tokens"] == 50
        assert result["stats"]["output_tokens"] == 20
        fpath.unlink()

    def test_thinking_included(self, mock_anonymizer):
        """Thinking blocks should be included when include_thinking=True."""
        import tempfile
        from pathlib import Path

        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Explain X"),
            _make_openclaw_assistant_message("Here's the answer", thinking="Let me think about X..."),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer, include_thinking=True)
        assistant_msg = result["messages"][1]
        assert "thinking" in assistant_msg
        assert "Let me think about X" in assistant_msg["thinking"]

        result_no_think = _parse_openclaw_session_file(fpath, mock_anonymizer, include_thinking=False)
        assistant_msg_no_think = result_no_think["messages"][1]
        assert "thinking" not in assistant_msg_no_think
        fpath.unlink()

    def test_tool_calls_with_results(self, mock_anonymizer):
        """Tool calls should be paired with their results."""
        import tempfile
        from pathlib import Path

        tool_call = {
            "type": "toolCall",
            "id": "tc-1",
            "name": "read_file",
            "arguments": {"path": "/tmp/test.py"},
        }
        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Read the file"),
            _make_openclaw_assistant_message("Let me read that", tool_calls=[tool_call]),
            _make_openclaw_tool_result("tc-1", "print('hello')"),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assistant_msg = result["messages"][1]
        assert len(assistant_msg["tool_uses"]) == 1
        tu = assistant_msg["tool_uses"][0]
        assert tu["tool"] == "read_file"
        assert tu["status"] == "success"
        assert "hello" in tu["output"]["text"]
        assert result["stats"]["tool_uses"] == 1
        fpath.unlink()

    def test_error_tool_result(self, mock_anonymizer):
        """Tool results with isError=True should have status 'error'."""
        import tempfile
        from pathlib import Path

        tool_call = {
            "type": "toolCall",
            "id": "tc-err",
            "name": "bash",
            "arguments": {"command": "rm /nope"},
        }
        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Delete it"),
            _make_openclaw_assistant_message("Trying", tool_calls=[tool_call]),
            _make_openclaw_tool_result("tc-err", "Permission denied", is_error=True),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        tu = result["messages"][1]["tool_uses"][0]
        assert tu["status"] == "error"
        fpath.unlink()

    def test_empty_file_returns_none(self, mock_anonymizer):
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result is None
        fpath.unlink()

    def test_no_session_header_returns_none(self, mock_anonymizer):
        import tempfile
        from pathlib import Path

        lines = [_make_openclaw_user_message("Hello")]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result is None
        fpath.unlink()

    def test_model_change_entry(self, mock_anonymizer):
        """model_change entries should update the session model."""
        import tempfile
        from pathlib import Path

        lines = [
            _make_openclaw_session_header(),
            {"type": "model_change", "timestamp": "2026-02-20T10:00:30.000Z",
             "provider": "anthropic", "modelId": "claude-opus-4-20250514"},
            _make_openclaw_user_message("Hello"),
            _make_openclaw_assistant_message("Hi", model=None),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        assert result["model"] == "anthropic/claude-opus-4-20250514"
        fpath.unlink()

    def test_cache_read_tokens(self, mock_anonymizer):
        """cacheRead should land in cache_read_tokens, not be folded into input."""
        import tempfile
        from pathlib import Path

        lines = [
            _make_openclaw_session_header(),
            _make_openclaw_user_message("Do something"),
            _make_openclaw_assistant_message(
                "Done", usage={"input": 100, "output": 50, "cacheRead": 200}
            ),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
            fpath = Path(f.name)

        result = _parse_openclaw_session_file(fpath, mock_anonymizer)
        # input stays non-cached; the cache read is priced separately at 0.1x.
        assert result["stats"]["input_tokens"] == 100
        assert result["stats"]["output_tokens"] == 50
        assert result["stats"]["cache_read_tokens"] == 200
        fpath.unlink()


class TestParseGeminiSessionFile:
    def test_cached_tokens_priced_as_cache_read(self, mock_anonymizer):
        """Gemini `cached` is a subset of `input` (promptTokenCount); it must be
        recovered as a cache read (0.1x), not left folded into input at 1.0x."""
        import tempfile
        from pathlib import Path
        from clawjournal.parsing.parser import _parse_gemini_session_file

        data = {
            "sessionId": "g1",
            "startTime": "2026-01-10T10:00:00Z",
            "lastUpdated": "2026-01-10T10:01:00Z",
            "messages": [
                {"type": "user", "timestamp": "2026-01-10T10:00:00Z", "content": "Hi"},
                {
                    "type": "gemini",
                    "timestamp": "2026-01-10T10:00:30Z",
                    "model": "gemini-2.5-pro",
                    "content": "Hello",
                    "tokens": {"input": 120, "output": 40, "cached": 30},
                },
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            fpath = Path(f.name)

        result = _parse_gemini_session_file(fpath, mock_anonymizer)
        assert result is not None
        # input (120) includes cached (30) as a subset -> non-cached input is 90.
        assert result["stats"]["input_tokens"] == 90
        assert result["stats"]["output_tokens"] == 40
        assert result["stats"]["cache_read_tokens"] == 30
        assert result["stats"]["cache_creation_tokens"] == 0
        fpath.unlink()


class TestDiscoverOpenclawProjects:
    def _disable_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", tmp_path / "no-local-agent")
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", tmp_path / "no-claude-science")
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("clawjournal.parsing.parser.CURSOR_DIR", tmp_path / "no-cursor")
        monkeypatch.setattr("clawjournal.parsing.parser._CURSOR_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.COPILOT_DIR", tmp_path / "no-copilot")
        monkeypatch.setattr("clawjournal.parsing.parser._AIDER_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser._get_aider_project_index", lambda refresh=False: {})

    def test_discover_openclaw_projects(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)

        agents_dir = tmp_path / "openclaw-agents"
        sessions_dir = agents_dir / "agent-abc" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Write two sessions for the same cwd
        for i, sid in enumerate(["sess-1", "sess-2"]):
            lines = [
                _make_openclaw_session_header(session_id=sid, cwd="/Users/alice/projects/myapp"),
                _make_openclaw_user_message(f"Message {i}"),
                _make_openclaw_assistant_message(f"Reply {i}"),
            ]
            (sessions_dir / f"{sid}.jsonl").write_text(
                "\n".join(json.dumps(l) for l in lines) + "\n"
            )

        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", agents_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "openclaw"
        assert projects[0]["session_count"] == 2
        assert projects[0]["display_name"] == "openclaw:myapp"

    def test_parse_openclaw_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)

        agents_dir = tmp_path / "openclaw-agents"
        sessions_dir = agents_dir / "agent-abc" / "sessions"
        sessions_dir.mkdir(parents=True)

        lines = [
            _make_openclaw_session_header(session_id="sess-1", cwd="/Users/alice/projects/myapp"),
            _make_openclaw_user_message("Hello"),
            _make_openclaw_assistant_message("Hi!", usage={"input": 10, "output": 5}),
        ]
        (sessions_dir / "sess-1.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines) + "\n"
        )

        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", agents_dir)
        sessions = parse_project_sessions(
            "/Users/alice/projects/myapp", mock_anonymizer, source="openclaw"
        )
        assert len(sessions) == 1
        assert sessions[0]["source"] == "openclaw"
        assert sessions[0]["project"] == "openclaw:myapp"
        assert sessions[0]["messages"][0]["content"] == "Hello"
        assert sessions[0]["raw_source_path"] == str(sessions_dir / "sess-1.jsonl")

    def test_multiple_agents_same_cwd(self, tmp_path, monkeypatch, mock_anonymizer):
        """Sessions from different agents but same cwd should be grouped."""
        self._disable_others(tmp_path, monkeypatch)

        agents_dir = tmp_path / "openclaw-agents"
        for agent_name, sid in [("agent-1", "s1"), ("agent-2", "s2")]:
            sessions_dir = agents_dir / agent_name / "sessions"
            sessions_dir.mkdir(parents=True)
            lines = [
                _make_openclaw_session_header(session_id=sid, cwd="/Users/alice/projects/myapp"),
                _make_openclaw_user_message(f"From {agent_name}"),
                _make_openclaw_assistant_message(f"Reply from {agent_name}"),
            ]
            (sessions_dir / f"{sid}.jsonl").write_text(
                "\n".join(json.dumps(l) for l in lines) + "\n"
            )

        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", agents_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["session_count"] == 2


class TestDiscoverClaudeScienceProjects:
    def _disable_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", tmp_path / "no-local-agent")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("clawjournal.parsing.parser.CURSOR_DIR", tmp_path / "no-cursor")
        monkeypatch.setattr("clawjournal.parsing.parser._CURSOR_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.COPILOT_DIR", tmp_path / "no-copilot")
        monkeypatch.setattr("clawjournal.parsing.parser._AIDER_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser._get_aider_project_index", lambda refresh=False: {})

    def _write_db(self, db_path, *, with_execution=False, hidden_frame=False):
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE projects ("
            "id TEXT PRIMARY KEY, name TEXT, description TEXT, context TEXT, "
            "created_at INTEGER, updated_at INTEGER, user_id TEXT, "
            "uploads_frame_id TEXT, memory_enabled INTEGER)"
        )
        conn.execute(
            "CREATE TABLE frames ("
            "id TEXT PRIMARY KEY, parent_frame_id TEXT, root_frame_id TEXT, "
            "agent_name TEXT, status TEXT, input_data TEXT, output_data TEXT, "
            "context_data TEXT, model TEXT, effort TEXT, input_tokens INTEGER, "
            "output_tokens INTEGER, total_cost REAL, created_at INTEGER, "
            "updated_at INTEGER, completed_at INTEGER, project_id TEXT, name TEXT, "
            "conversation_type TEXT, artifact_id TEXT, task_summary TEXT, "
            "mentioned_artifact_ids TEXT, specialists_used TEXT, is_hidden INTEGER, "
            "status_description TEXT, compute_enabled TEXT, delegate_name TEXT, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
            "last_user_message_at INTEGER, last_extract_msg_idx INTEGER, root_seq INTEGER, "
            "aux_input_tokens INTEGER, aux_output_tokens INTEGER, "
            "aux_cache_read_tokens INTEGER, aux_cache_write_tokens INTEGER, "
            "aux_cost REAL, token_class_usage TEXT)"
        )
        conn.execute(
            "CREATE TABLE frame_messages (frame_id TEXT, idx INTEGER, msg_json TEXT, PRIMARY KEY(frame_id, idx))"
        )
        conn.execute(
            "CREATE TABLE execution_log ("
            "id TEXT PRIMARY KEY, frame_id TEXT, cell_index INTEGER, kernel_id TEXT, "
            "conda_env TEXT, language TEXT, source TEXT, stdout TEXT, stderr TEXT, "
            "exit_status TEXT, created_at INTEGER, files_written TEXT, error_lineno INTEGER, "
            "kernel_kind TEXT, origin TEXT, detection TEXT, files_read TEXT)"
        )
        conn.execute(
            "CREATE TABLE host_call_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, execution_log_id TEXT, seq INTEGER, "
            "method TEXT, args_json TEXT, derivable INTEGER, data_inline TEXT, data_ref TEXT, "
            "error TEXT, bytes INTEGER, created_at INTEGER)"
        )
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("proj-1", "Physics Lab", 1782883193508, 1782883193508),
        )
        conn.execute(
            "INSERT INTO frames ("
            "id, parent_frame_id, root_frame_id, agent_name, status, model, effort, "
            "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
            "created_at, updated_at, completed_at, project_id, name, conversation_type, "
            "is_hidden, aux_input_tokens, aux_output_tokens, aux_cache_read_tokens, "
            "aux_cache_write_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "frame-1", None, "frame-1", "Claude Science", "completed",
                "claude-sonnet-4", "high", 100, 50, 10, 5,
                1782883193508, 1782883200000, 1782883205000,
                "proj-1", "Density task", "agent", 0, 3, 2, 1, 1,
            ),
        )
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Measure density"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will run the calculation."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "python",
                        "input": {"code": "print('ok')", "working_dir": "/Users/testuser/project"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "ok",
                    }
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ]
        for idx, msg in enumerate(messages):
            conn.execute(
                "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?, ?, ?)",
                ("frame-1", idx, json.dumps(msg)),
            )
        if hidden_frame:
            conn.execute(
                "INSERT INTO frames ("
                "id, parent_frame_id, root_frame_id, agent_name, status, model, effort, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "created_at, updated_at, completed_at, project_id, name, conversation_type, "
                "is_hidden, aux_input_tokens, aux_output_tokens, aux_cache_read_tokens, "
                "aux_cache_write_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "frame-2", "frame-1", "frame-1", "Claude Science", "completed",
                    "claude-sonnet-4", "high", 20, 10, 0, 0,
                    1782883210000, 1782883215000, 1782883216000,
                    "proj-1", "Hidden specialist", "agent", 1, 0, 0, 0, 0,
                ),
            )
            conn.execute(
                "INSERT INTO frame_messages (frame_id, idx, msg_json) VALUES (?, ?, ?)",
                (
                    "frame-2", 0,
                    json.dumps({"role": "assistant", "content": [{"type": "text", "text": "Specialist note"}]}),
                ),
            )
        if with_execution:
            conn.execute(
                "INSERT INTO execution_log ("
                "id, frame_id, cell_index, kernel_id, conda_env, language, source, "
                "stdout, stderr, exit_status, created_at, files_written, files_read, "
                "kernel_kind, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "exec-1", "frame-1", 0, "kernel-1", "science-env", "python",
                    "print('ok')", "ok\n", "", "ok", 1782883201000,
                    json.dumps(["/Users/testuser/project/result.csv"]),
                    json.dumps(["/Users/testuser/project/input.csv"]),
                    "python", "agent",
                ),
            )
            conn.execute(
                "INSERT INTO host_call_log (execution_log_id, seq, method, args_json, derivable, bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "exec-1", 0, "artifact_path",
                    json.dumps({"path": "/Users/testuser/project/result.csv"}),
                    1, 128, 1782883201500,
                ),
            )
        conn.commit()
        conn.close()

    def test_discover_claude_science_projects(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        root = tmp_path / "claude-science"
        self._write_db(root / "orgs" / "org-1" / "operon-cli.db")
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", root)
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})

        projects = discover_projects(source_filter="claude-science")

        assert len(projects) == 1
        assert projects[0]["source"] == "claude-science"
        assert projects[0]["dir_name"] == "org-1:proj-1"
        assert projects[0]["display_name"] == "claude-science:Physics Lab"
        assert projects[0]["session_count"] == 1

    def test_discover_claude_science_includes_hidden_message_frames(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        root = tmp_path / "claude-science"
        self._write_db(root / "orgs" / "org-1" / "operon-cli.db", hidden_frame=True)
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", root)
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})

        projects = discover_projects(source_filter="claude-science")

        assert len(projects) == 1
        assert projects[0]["session_count"] == 2

    def test_parse_claude_science_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        root = tmp_path / "claude-science"
        self._write_db(root / "orgs" / "org-1" / "operon-cli.db", with_execution=True)
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", root)
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})

        sessions = parse_project_sessions(
            "org-1:proj-1", mock_anonymizer, source="claude-science"
        )

        assert len(sessions) == 1
        session = sessions[0]
        assert session["session_id"] == "claude-science:org-1:frame-1"
        assert session["project"] == "claude-science:Physics Lab"
        assert session["source"] == "claude-science"
        assert session["model"] == "claude-sonnet-4"
        assert session["model_effort"] == "high"
        assert session["segment_title"] == "Density task"
        assert session["end_time"] == "2026-07-01T05:20:05+00:00"
        assert session["stats"]["input_tokens"] == 103
        assert session["stats"]["output_tokens"] == 52
        assert session["stats"]["cache_read_tokens"] == 11
        assert session["stats"]["cache_creation_tokens"] == 6
        assert session["messages"][0]["content"] == "Measure density"
        tool_use = session["messages"][1]["tool_uses"][0]
        assert tool_use["tool"] == "python"
        assert tool_use["output"]["text"] == "ok"
        execution_tool = session["messages"][-1]["tool_uses"][0]
        assert execution_tool["tool"] == "python"
        assert execution_tool["output"]["stdout"] == "ok\n"
        assert execution_tool["output"]["host_calls"][0]["method"] == "artifact_path"
        assert session["client_origin"] == "desktop"
        assert session["runtime_channel"] == "claude-science"
        assert session["raw_source_path"].endswith("operon-cli.db#frame=frame-1")


class TestDiscoverKimiProjects:
    def _disable_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", tmp_path / "no-local-agent")
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", tmp_path / "no-claude-science")
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("clawjournal.parsing.parser.CURSOR_DIR", tmp_path / "no-cursor")
        monkeypatch.setattr("clawjournal.parsing.parser._CURSOR_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.COPILOT_DIR", tmp_path / "no-copilot")
        monkeypatch.setattr("clawjournal.parsing.parser._AIDER_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser._get_aider_project_index", lambda refresh=False: {})

    @staticmethod
    def _write_kimi_context(session_dir):
        (session_dir / "context.jsonl").write_text(
            json.dumps({"role": "user", "content": "Hello"}) + "\n"
            + json.dumps({
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi there"}],
            }) + "\n"
            + json.dumps({"role": "_usage", "token_count": 7}) + "\n"
        )

    def test_discover_kimi_projects(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        cwd = "/Users/alice/projects/myapp"
        project_hash = hashlib.md5(cwd.encode()).hexdigest()
        session_dir = tmp_path / "kimi-sessions" / project_hash / "session-1"
        session_dir.mkdir(parents=True)
        self._write_kimi_context(session_dir)

        config_path = tmp_path / "kimi.json"
        config_path.write_text(json.dumps({"work_dirs": [{"path": cwd}]}))

        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_SESSIONS_DIR", tmp_path / "kimi-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_CONFIG_PATH", config_path)

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["display_name"] == "kimi:myapp"
        assert projects[0]["source"] == "kimi"

    def test_parse_kimi_project_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        cwd = "/Users/alice/projects/myapp"
        project_hash = hashlib.md5(cwd.encode()).hexdigest()
        session_dir = tmp_path / "kimi-sessions" / project_hash / "session-1"
        session_dir.mkdir(parents=True)
        self._write_kimi_context(session_dir)

        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_SESSIONS_DIR", tmp_path / "kimi-sessions")

        sessions = parse_project_sessions(cwd, mock_anonymizer, source="kimi")
        assert len(sessions) == 1
        assert sessions[0]["project"] == "kimi:myapp"
        assert sessions[0]["source"] == "kimi"
        assert sessions[0]["messages"][0]["content"] == "Hello"


class TestDiscoverAiderProjects:
    def test_counts_first_session_header(self, tmp_path, monkeypatch):
        history_file = tmp_path / ".aider.chat.history.md"
        history_file.write_text(
            "# aider chat started at 2026-04-10 14:32:00\n"
            "> First task\n"
            "Assistant reply\n\n"
            "# aider chat started at 2026-04-11 09:10:00\n"
            "> Second task\n"
            "Assistant reply\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "clawjournal.parsing.parser._get_aider_project_index",
            lambda refresh=True: {"/Users/alice/projects/myapp": history_file},
        )

        projects = _discover_aider_projects()

        assert len(projects) == 1
        assert projects[0]["source"] == "aider"
        assert projects[0]["session_count"] == 2


class TestDiscoverCustomProjects:
    def _disable_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-claude")
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", tmp_path / "no-local-agent")
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", tmp_path / "no-claude-science")
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CURSOR_DIR", tmp_path / "no-cursor")
        monkeypatch.setattr("clawjournal.parsing.parser._CURSOR_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.COPILOT_DIR", tmp_path / "no-copilot")
        monkeypatch.setattr("clawjournal.parsing.parser._AIDER_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser._get_aider_project_index", lambda refresh=False: {})

    def _make_valid_session(self, session_id="s1", model="gpt-4", content="hello"):
        return json.dumps({
            "session_id": session_id,
            "model": model,
            "messages": [
                {"role": "user", "content": content},
                {"role": "assistant", "content": "hi there"},
            ],
            "stats": {"user_messages": 1, "assistant_messages": 1, "tool_uses": 0,
                       "input_tokens": 10, "output_tokens": 5},
        })

    def test_discover_custom_projects(self, tmp_path, monkeypatch, mock_anonymizer):
        self._disable_others(tmp_path, monkeypatch)
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "my-project"
        proj.mkdir(parents=True)
        (proj / "sessions.jsonl").write_text(
            self._make_valid_session("s1") + "\n" + self._make_valid_session("s2") + "\n"
        )
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", custom_dir)
        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["display_name"] == "custom:my-project"
        assert projects[0]["session_count"] == 2
        assert projects[0]["source"] == "custom"

    def test_discover_skips_empty_dir(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        custom_dir = tmp_path / "custom"
        (custom_dir / "empty-project").mkdir(parents=True)
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", custom_dir)
        projects = discover_projects()
        assert len(projects) == 0

    def test_discover_missing_dir(self, tmp_path, monkeypatch):
        self._disable_others(tmp_path, monkeypatch)
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", tmp_path / "nonexistent")
        projects = discover_projects()
        assert len(projects) == 0

    def test_parse_valid_sessions(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "test-proj"
        proj.mkdir(parents=True)
        (proj / "data.jsonl").write_text(
            self._make_valid_session("s1") + "\n" + self._make_valid_session("s2", model="o1") + "\n"
        )
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 2
        assert sessions[0]["session_id"] == "s1"
        assert sessions[1]["model"] == "o1"
        assert sessions[0]["project"] == "custom:test-proj"
        assert sessions[0]["source"] == "custom"

    def test_parse_skips_missing_fields(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "test-proj"
        proj.mkdir(parents=True)
        valid = self._make_valid_session("s1")
        no_model = json.dumps({"session_id": "s2", "messages": []})
        no_messages = json.dumps({"session_id": "s3", "model": "m"})
        no_session_id = json.dumps({"model": "m", "messages": []})
        (proj / "data.jsonl").write_text(
            "\n".join([valid, no_model, no_messages, no_session_id]) + "\n"
        )
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"

    def test_parse_skips_invalid_json(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "test-proj"
        proj.mkdir(parents=True)
        valid = self._make_valid_session("s1")
        (proj / "data.jsonl").write_text(valid + "\n" + "not-json\n")
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 1

    def test_parse_multiple_files(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        proj = custom_dir / "test-proj"
        proj.mkdir(parents=True)
        (proj / "a.jsonl").write_text(self._make_valid_session("s1") + "\n")
        (proj / "b.jsonl").write_text(self._make_valid_session("s2") + "\n")
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("test-proj", mock_anonymizer, source="custom")
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert ids == {"s1", "s2"}

    def test_parse_nonexistent_project(self, tmp_path, monkeypatch, mock_anonymizer):
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir(parents=True)
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", custom_dir)
        sessions = parse_project_sessions("nope", mock_anonymizer, source="custom")
        assert sessions == []


# --- Claude Desktop local-agent support ---

def _make_valid_claude_jsonl(
    cwd="/Users/testuser/code/test-project",
    session_id="sess-001",
):
    return (
        '{"type":"user","timestamp":1706000000000,"message":{"content":"Hello"},'
        f'"cwd":"{cwd}","sessionId":"{session_id}"}}\n'
        '{"type":"assistant","timestamp":1706000001000,"message":{"model":"claude-sonnet-4","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
    )


_VALID_CLAUDE_JSONL = _make_valid_claude_jsonl()

_ROOT_UUID = "aaaaaaaa-1111-2222-3333-444444444444"
_WORKSPACE_UUID = "bbbbbbbb-5555-6666-7777-888888888888"


def _make_wrapper_json(
    session_id="local_abc123",
    cli_session_id="sess-001",
    process_name="test-process",
    user_selected_folders=None,
    title="Test session",
    model="claude-sonnet-4",
):
    return json.dumps({
        "sessionId": session_id,
        "cliSessionId": cli_session_id,
        "processName": process_name,
        "cwd": f"/sessions/{process_name}",
        "userSelectedFolders": user_selected_folders or [],
        "createdAt": 1706000000000,
        "lastActivityAt": 1706000001000,
        "model": model,
        "title": title,
        "isArchived": False,
    })


def _setup_local_agent_session(
    la_dir,
    session_id="local_abc123",
    cli_session_id="sess-001",
    process_name="test-process",
    user_selected_folders=None,
    title="Test session",
    jsonl_content=None,
):
    """Create a local-agent session fixture under la_dir."""
    workspace_dir = la_dir / _ROOT_UUID / _WORKSPACE_UUID
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Write wrapper JSON
    wrapper_path = workspace_dir / f"{session_id}.json"
    wrapper_path.write_text(_make_wrapper_json(
        session_id=session_id,
        cli_session_id=cli_session_id,
        process_name=process_name,
        user_selected_folders=user_selected_folders,
        title=title,
    ))

    # Create session directory with nested .claude/projects
    session_dir = workspace_dir / session_id
    nested_project_dir = session_dir / ".claude" / "projects" / f"-sessions-{process_name}"
    nested_project_dir.mkdir(parents=True)

    # Write JSONL transcript
    jsonl_path = nested_project_dir / f"{cli_session_id}.jsonl"
    if jsonl_content is None:
        cwd = "/Users/testuser/code/test-project"
        if user_selected_folders and user_selected_folders[0] and user_selected_folders[0] != "/":
            cwd = user_selected_folders[0].rstrip("/")
        jsonl_content = _make_valid_claude_jsonl(cwd=cwd, session_id=cli_session_id)
    jsonl_path.write_text(jsonl_content)

    return workspace_dir


class TestPathToDirName:
    def test_basic_path(self):
        assert _path_to_dir_name("/Users/alice/projects/app") == "-Users-alice-projects-app"

    def test_path_with_hyphens(self):
        assert _path_to_dir_name("/Users/alice/my-cool-project") == "-Users-alice-my-cool-project"

    def test_root(self):
        assert _path_to_dir_name("/") == "-"


class TestScanLocalAgentSessions:
    def test_basic_scan(self, tmp_path, monkeypatch):
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            user_selected_folders=["/Users/testuser/projects/myapp"],
        )
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        groups = _scan_local_agent_sessions()
        assert len(groups) == 1
        key = "-Users-testuser-projects-myapp"
        assert key in groups
        assert len(groups[key]) == 1
        assert groups[key][0]["cli_session_id"] == "sess-001"
        assert groups[key][0]["outer_session_id"] == "local_abc123"

    def test_empty_user_selected_folders_uses_synthetic_key(self, tmp_path, monkeypatch):
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(la_dir, user_selected_folders=[])
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        groups = _scan_local_agent_sessions()
        assert len(groups) == 1
        key = list(groups.keys())[0]
        assert key.startswith("_cowork_")

    def test_skips_non_uuid_dirs(self, tmp_path, monkeypatch):
        la_dir = tmp_path / "local-agent"
        # Create a skills-plugin dir (should be skipped)
        (la_dir / "skills-plugin" / "something").mkdir(parents=True)
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        groups = _scan_local_agent_sessions()
        assert groups == {}

    def test_skips_no_cli_session_id(self, tmp_path, monkeypatch):
        la_dir = tmp_path / "local-agent"
        workspace_dir = la_dir / _ROOT_UUID / _WORKSPACE_UUID
        workspace_dir.mkdir(parents=True)
        wrapper = workspace_dir / "local_no_cli.json"
        wrapper.write_text(json.dumps({
            "sessionId": "local_no_cli",
            "processName": "test",
            "userSelectedFolders": [],
        }))
        # Create session dir
        (workspace_dir / "local_no_cli" / ".claude" / "projects" / "-sessions-test").mkdir(parents=True)
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        groups = _scan_local_agent_sessions()
        assert groups == {}

    def test_nonexistent_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", tmp_path / "nonexistent")
        assert _scan_local_agent_sessions() == {}


class TestDiscoverClaudeProjectsWithLocalAgent:
    def _disable_other_sources(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.parsing.parser.CLAUDE_SCIENCE_DIR", tmp_path / "no-claude-science")
        monkeypatch.setattr("clawjournal.parsing.parser._CLAUDE_SCIENCE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_SESSIONS_DIR", tmp_path / "no-codex-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CODEX_ARCHIVED_DIR", tmp_path / "no-codex-archived")
        monkeypatch.setattr("clawjournal.parsing.parser._CODEX_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.GEMINI_DIR", tmp_path / "no-gemini")
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCODE_DB_PATH", tmp_path / "no-opencode.db")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCODE_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.OPENCLAW_AGENTS_DIR", tmp_path / "no-openclaw-agents")
        monkeypatch.setattr("clawjournal.parsing.parser._OPENCLAW_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.KIMI_SESSIONS_DIR", tmp_path / "no-kimi-sessions")
        monkeypatch.setattr("clawjournal.parsing.parser.CUSTOM_DIR", tmp_path / "no-custom")
        monkeypatch.setattr("clawjournal.parsing.parser.CURSOR_DIR", tmp_path / "no-cursor")
        monkeypatch.setattr("clawjournal.parsing.parser._CURSOR_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser.COPILOT_DIR", tmp_path / "no-copilot")
        monkeypatch.setattr("clawjournal.parsing.parser._AIDER_PROJECT_INDEX", {})
        monkeypatch.setattr("clawjournal.parsing.parser._get_aider_project_index", lambda refresh=False: {})

    def test_la_only_project_with_host_path(self, tmp_path, monkeypatch):
        """Local-agent session with userSelectedFolders gets proper display name."""
        self._disable_other_sources(tmp_path, monkeypatch)
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-native")

        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            user_selected_folders=["/Users/testuser/projects/myapp"],
        )
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        projects = discover_projects()
        assert len(projects) == 1
        assert projects[0]["source"] == "claude"
        assert projects[0]["display_name"] == "claude:myapp"
        assert projects[0]["session_count"] == 1
        # Should not have sessions- prefix in name
        assert "sessions-" not in projects[0]["display_name"]

    def test_la_only_project_with_hyphenated_path_uses_cwd_basename(
        self, tmp_path, monkeypatch
    ):
        self._disable_other_sources(tmp_path, monkeypatch)
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-native")

        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            session_id="local_hy",
            cli_session_id="sess-hy",
            user_selected_folders=["/Users/testuser/Rayward/Codes/llm-gateway-infra"],
        )
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        projects = discover_projects(source_filter="claude")

        assert len(projects) == 1
        assert projects[0]["display_name"] == "claude:llm-gateway-infra"

    def test_la_merges_with_native_project(self, tmp_path, monkeypatch):
        """Local-agent session with matching host path merges into native project."""
        self._disable_other_sources(tmp_path, monkeypatch)

        # Create native project
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-testuser-projects-myapp"
        proj.mkdir(parents=True)
        (proj / "sess-native.jsonl").write_text(
            _make_valid_claude_jsonl(
                cwd="/Users/testuser/projects/myapp",
                session_id="sess-native",
            )
        )
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)

        # Create local-agent session pointing to same host path
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            session_id="local_xyz",
            cli_session_id="sess-la-only",
            user_selected_folders=["/Users/testuser/projects/myapp"],
        )
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        projects = discover_projects()
        claude_projects = [p for p in projects if p["source"] == "claude"]
        assert len(claude_projects) == 1
        assert claude_projects[0]["display_name"] == "claude:myapp"
        # Native session + one new LA session
        assert claude_projects[0]["session_count"] == 2
        assert len(claude_projects[0]["locator"]["local_agent_sessions"]) == 1

    def test_la_trailing_slash_still_merges(self, tmp_path, monkeypatch):
        """Trailing slash in userSelectedFolders still merges with native project."""
        self._disable_other_sources(tmp_path, monkeypatch)

        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-testuser-projects-myapp"
        proj.mkdir(parents=True)
        (proj / "sess-native.jsonl").write_text(
            _make_valid_claude_jsonl(
                cwd="/Users/testuser/projects/myapp",
                session_id="sess-native",
            )
        )
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)

        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            session_id="local_trail",
            cli_session_id="sess-trail",
            # Trailing slash — should still match native project
            user_selected_folders=["/Users/testuser/projects/myapp/"],
        )
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        projects = discover_projects()
        claude_projects = [p for p in projects if p["source"] == "claude"]
        assert len(claude_projects) == 1
        assert claude_projects[0]["session_count"] == 2

    def test_la_dedupes_same_session_id(self, tmp_path, monkeypatch):
        """When LA session has same session_id as native, count is not inflated."""
        self._disable_other_sources(tmp_path, monkeypatch)

        # Create native project with session "sess-001"
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-testuser-projects-myapp"
        proj.mkdir(parents=True)
        (proj / "sess-001.jsonl").write_text(
            _make_valid_claude_jsonl(cwd="/Users/testuser/projects/myapp")
        )
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)

        # Create local-agent session with same cli_session_id
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            cli_session_id="sess-001",
            user_selected_folders=["/Users/testuser/projects/myapp"],
        )
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        projects = discover_projects()
        claude_projects = [p for p in projects if p["source"] == "claude"]
        assert len(claude_projects) == 1
        # Should NOT double-count
        assert claude_projects[0]["session_count"] == 1

    def test_la_empty_folders_gets_cowork_name(self, tmp_path, monkeypatch):
        """Session with empty userSelectedFolders gets synthetic cowork name."""
        self._disable_other_sources(tmp_path, monkeypatch)
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-native")

        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            user_selected_folders=[],
            title="My cowork task",
        )
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        projects = discover_projects()
        claude_projects = [p for p in projects if p["source"] == "claude"]
        assert len(claude_projects) == 1
        assert claude_projects[0]["display_name"].startswith("claude:cowork/")
        assert "sessions-" not in claude_projects[0]["display_name"]

    def test_no_local_agent_dir(self, tmp_path, monkeypatch):
        """Discovery works fine when LOCAL_AGENT_DIR doesn't exist."""
        self._disable_other_sources(tmp_path, monkeypatch)
        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", tmp_path / "no-native")
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", tmp_path / "nonexistent")

        projects = discover_projects()
        assert projects == []


class TestParseProjectSessionsWithLocator:
    def test_native_preferred_over_la_duplicate(self, tmp_path, monkeypatch, mock_anonymizer):
        """Native session wins when same session_id exists in both."""
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "-Users-testuser-projects-myapp"
        proj.mkdir(parents=True)
        (proj / "sess-001.jsonl").write_text(
            _make_valid_claude_jsonl(cwd="/Users/testuser/projects/myapp")
        )

        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            cli_session_id="sess-001",
            user_selected_folders=["/Users/testuser/projects/myapp"],
        )

        locator = {
            "native_project_dir": proj,
            "local_agent_sessions": [{
                "wrapper_path": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_abc123.json",
                "session_dir": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_abc123",
                "nested_project_dir": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_abc123" / ".claude" / "projects" / "-sessions-test-process",
                "audit_path": None,
                "cli_session_id": "sess-001",
                "outer_session_id": "local_abc123",
                "process_name": "test-process",
                "wrapper_meta": {"title": "Test", "model": "claude-sonnet-4", "createdAt": 1706000000000, "lastActivityAt": 1706000001000},
            }],
        }

        sessions = parse_project_sessions(
            "-Users-testuser-projects-myapp",
            mock_anonymizer,
            locator=locator,
        )
        assert len(sessions) == 1
        # Native session should NOT have desktop provenance
        assert sessions[0].get("client_origin") is None

    def test_la_only_session_has_provenance(self, tmp_path, monkeypatch, mock_anonymizer):
        """LA session not in native gets provenance fields."""
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            session_id="local_xyz",
            cli_session_id="sess-la-only",
            user_selected_folders=["/Users/testuser/projects/myapp"],
        )

        nested_dir = la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_xyz" / ".claude" / "projects" / "-sessions-test-process"

        locator = {
            "native_project_dir": None,
            "local_agent_sessions": [{
                "wrapper_path": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_xyz.json",
                "session_dir": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_xyz",
                "nested_project_dir": nested_dir,
                "audit_path": None,
                "cli_session_id": "sess-la-only",
                "outer_session_id": "local_xyz",
                "process_name": "test-process",
                "wrapper_meta": {"title": "Test", "model": "claude-sonnet-4", "createdAt": 1706000000000, "lastActivityAt": 1706000001000},
            }],
        }

        sessions = parse_project_sessions(
            "-Users-testuser-projects-myapp",
            mock_anonymizer,
            locator=locator,
        )
        assert len(sessions) == 1
        assert sessions[0]["client_origin"] == "desktop"
        assert sessions[0]["runtime_channel"] == "local-agent"
        assert sessions[0]["outer_session_id"] == "local_xyz"
        assert sessions[0]["raw_source_path"] is not None
        assert sessions[0]["source"] == "claude"

    def test_la_only_session_uses_cwd_basename(self, tmp_path, monkeypatch, mock_anonymizer):
        """Local-agent-only projects (no native_project_dir) recover the
        basename from the cwd in the nested transcript, not the hyphenated key."""
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            session_id="local_hy",
            cli_session_id="sess-hy",
            user_selected_folders=["/Users/testuser/Rayward/Codes/llm-gateway-infra"],
            jsonl_content=(
                '{"type":"user","timestamp":1706000000000,"message":{"content":"Hi"},'
                '"cwd":"/Users/testuser/Rayward/Codes/llm-gateway-infra"}\n'
                '{"type":"assistant","timestamp":1706000001000,"message":{"model":"m",'
                '"content":[{"type":"text","text":"Ok"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
            ),
        )
        nested_dir = la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_hy" / ".claude" / "projects" / "-sessions-test-process"

        locator = {
            "native_project_dir": None,
            "local_agent_sessions": [{
                "wrapper_path": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_hy.json",
                "session_dir": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_hy",
                "nested_project_dir": nested_dir,
                "audit_path": None,
                "cli_session_id": "sess-hy",
                "outer_session_id": "local_hy",
                "process_name": "test-process",
                "wrapper_meta": {"title": "Test", "model": "claude-sonnet-4", "createdAt": 1706000000000, "lastActivityAt": 1706000001000},
            }],
        }

        sessions = parse_project_sessions(
            "-Users-testuser-Rayward-Codes-llm-gateway-infra",
            mock_anonymizer,
            locator=locator,
        )
        assert len(sessions) == 1
        assert sessions[0]["project"] == "claude:llm-gateway-infra"

    def test_locator_none_backward_compatible(self, tmp_path, monkeypatch, mock_anonymizer):
        """When locator=None, behaves exactly as before."""
        projects_dir = tmp_path / "projects"
        proj = projects_dir / "test-project"
        proj.mkdir(parents=True)
        (proj / "session1.jsonl").write_text(_VALID_CLAUDE_JSONL)

        monkeypatch.setattr("clawjournal.parsing.parser.PROJECTS_DIR", projects_dir)
        sessions = parse_project_sessions("test-project", mock_anonymizer, locator=None)
        assert len(sessions) == 1
        assert sessions[0]["project"] == "claude:test-project"

    def test_cowork_project_name(self, tmp_path, monkeypatch, mock_anonymizer):
        """Cowork projects get synthetic display name."""
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            session_id="local_cw1",
            cli_session_id="sess-cw1",
            user_selected_folders=[],
            title="Draft email task",
        )

        nested_dir = la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_cw1" / ".claude" / "projects" / "-sessions-test-process"

        locator = {
            "native_project_dir": None,
            "local_agent_sessions": [{
                "wrapper_path": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_cw1.json",
                "session_dir": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_cw1",
                "nested_project_dir": nested_dir,
                "audit_path": None,
                "cli_session_id": "sess-cw1",
                "outer_session_id": "local_cw1",
                "process_name": "test-process",
                "wrapper_meta": {"title": "Draft email task", "model": "claude-sonnet-4", "createdAt": 1706000000000, "lastActivityAt": 1706000001000},
            }],
        }

        sessions = parse_project_sessions(
            "_cowork_local_cw1",
            mock_anonymizer,
            locator=locator,
        )
        assert len(sessions) == 1
        assert sessions[0]["project"].startswith("claude:cowork/")

    def test_la_missing_jsonl_gracefully_skipped(self, tmp_path, monkeypatch, mock_anonymizer):
        """LA session with nested_project_dir but no matching JSONL is skipped."""
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            session_id="local_miss",
            cli_session_id="sess-missing",
            user_selected_folders=["/Users/testuser/projects/myapp"],
        )

        # The JSONL was created as sess-missing.jsonl, but let's point to a wrong cli_session_id
        nested_dir = la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_miss" / ".claude" / "projects" / "-sessions-test-process"

        locator = {
            "native_project_dir": None,
            "local_agent_sessions": [{
                "wrapper_path": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_miss.json",
                "session_dir": la_dir / _ROOT_UUID / _WORKSPACE_UUID / "local_miss",
                "nested_project_dir": nested_dir,
                "audit_path": None,
                "cli_session_id": "sess-wrong-id",  # Does not match any JSONL file
                "outer_session_id": "local_miss",
                "process_name": "test-process",
                "wrapper_meta": {"title": "Test", "model": "claude-sonnet-4", "createdAt": 1706000000000, "lastActivityAt": 1706000001000},
            }],
        }

        sessions = parse_project_sessions(
            "-Users-testuser-projects-myapp",
            mock_anonymizer,
            locator=locator,
        )
        assert sessions == []

    def test_multiple_la_sessions_same_workspace(self, tmp_path, monkeypatch, mock_anonymizer):
        """Two LA sessions under same workspace produce two parsed sessions."""
        la_dir = tmp_path / "local-agent"
        workspace_dir = la_dir / _ROOT_UUID / _WORKSPACE_UUID
        workspace_dir.mkdir(parents=True)

        # Each session needs distinct sessionId in the JSONL to avoid dedup
        jsonl_templates = {
            "sess-la-1": (
                '{"type":"user","timestamp":1706000000000,"message":{"content":"First"},"cwd":"/tmp","sessionId":"sess-la-1"}\n'
                '{"type":"assistant","timestamp":1706000001000,"message":{"model":"claude-sonnet-4","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
            ),
            "sess-la-2": (
                '{"type":"user","timestamp":1706000002000,"message":{"content":"Second"},"cwd":"/tmp","sessionId":"sess-la-2"}\n'
                '{"type":"assistant","timestamp":1706000003000,"message":{"model":"claude-sonnet-4","content":[{"type":"text","text":"Hey"}],"usage":{"input_tokens":1,"output_tokens":1}}}\n'
            ),
        }

        for sid, cli_id in [("local_s1", "sess-la-1"), ("local_s2", "sess-la-2")]:
            wrapper_path = workspace_dir / f"{sid}.json"
            wrapper_path.write_text(_make_wrapper_json(
                session_id=sid,
                cli_session_id=cli_id,
                user_selected_folders=["/Users/testuser/projects/myapp"],
            ))
            session_dir = workspace_dir / sid
            nested = session_dir / ".claude" / "projects" / "-sessions-test-process"
            nested.mkdir(parents=True)
            (nested / f"{cli_id}.jsonl").write_text(jsonl_templates[cli_id])

        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        groups = _scan_local_agent_sessions()
        key = "-Users-testuser-projects-myapp"
        assert key in groups
        assert len(groups[key]) == 2

        # Also test parsing
        locator = {
            "native_project_dir": None,
            "local_agent_sessions": groups[key],
        }
        sessions = parse_project_sessions(
            key, mock_anonymizer, locator=locator,
        )
        assert len(sessions) == 2
        ids = {s["session_id"] for s in sessions}
        assert ids == {"sess-la-1", "sess-la-2"}


class TestBuildCoworkProjectName:
    def test_with_title(self):
        sessions = [{"wrapper_meta": {"title": "My task", "lastActivityAt": 1706000000000}}]
        assert _build_cowork_project_name("_cowork_local_abc", sessions) == "claude:cowork/My task"

    def test_empty_title_falls_back_to_id(self):
        sessions = [{"wrapper_meta": {"title": "", "lastActivityAt": 1706000000000}}]
        result = _build_cowork_project_name("_cowork_local_abc", sessions)
        assert result == "claude:cowork/local_abc"

    def test_no_sessions(self):
        result = _build_cowork_project_name("_cowork_local_abc", [])
        assert result == "claude:cowork/local_abc"

    def test_none_title(self):
        sessions = [{"wrapper_meta": {"lastActivityAt": 1706000000000}}]
        result = _build_cowork_project_name("_cowork_local_abc", sessions)
        assert result == "claude:cowork/local_abc"


class TestScanLocalAgentEdgeCases:
    def test_root_slash_uses_synthetic_key(self, tmp_path, monkeypatch):
        """userSelectedFolders=['/'] should use synthetic cowork key."""
        la_dir = tmp_path / "local-agent"
        _setup_local_agent_session(
            la_dir,
            user_selected_folders=["/"],
        )
        monkeypatch.setattr("clawjournal.parsing.parser.LOCAL_AGENT_DIR", la_dir)

        groups = _scan_local_agent_sessions()
        assert len(groups) == 1
        key = list(groups.keys())[0]
        assert key.startswith("_cowork_")

    def test_estimate_size_audit_fallback(self, tmp_path):
        """_estimate_la_session_size falls back to audit.jsonl size."""
        from clawjournal.parsing.parser import _estimate_la_session_size

        session_dir = tmp_path / "local_test"
        session_dir.mkdir()
        audit_file = session_dir / "audit.jsonl"
        audit_file.write_text('{"type":"user","message":"test"}\n')

        descriptor = {
            "nested_project_dir": None,
            "cli_session_id": "nonexistent",
            "audit_path": audit_file,
        }
        size = _estimate_la_session_size(descriptor)
        assert size == audit_file.stat().st_size
        assert size > 0
