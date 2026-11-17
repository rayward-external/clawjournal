"""Tests for Kiro session discovery and parsing.

Kiro keeps sessions in four stores. Every fixture here is synthetic and built
to the layout of the matching store; none is copied from a real session.
"""

import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from clawjournal.parsing import parser
from clawjournal.parsing.parser import (
    _kiro_timestamp,
    discover_projects,
    parse_project_sessions,
)

CWD = "/workspace/dev/kiro-app"


@pytest.fixture
def kiro(tmp_path, monkeypatch):
    """Point every Kiro location at an empty tmp tree and return the paths."""
    locations = {
        "sessions": tmp_path / "kiro" / "sessions",
        "db": tmp_path / "kiro-cli" / "data.sqlite3",
        "ide": tmp_path / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent",
    }
    monkeypatch.setattr(parser, "KIRO_SESSIONS_DIR", locations["sessions"])
    monkeypatch.setattr(parser, "KIRO_CLI_DB_PATH", locations["db"])
    monkeypatch.setattr(parser, "KIRO_IDE_DIR", locations["ide"])
    monkeypatch.setattr(parser, "_KIRO_PROJECT_INDEX", {})
    return locations


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _write_event_log(sessions_dir, session_id="sess_abc123", *, events, meta=None, workspace="ws-hash"):
    session_dir = sessions_dir / workspace / session_id
    _write_jsonl(session_dir / "messages.jsonl", events)
    if meta is not None:
        (session_dir / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    return session_dir / "messages.jsonl"


def _event(kind, timestamp="2026-06-01T10:00:00Z", **payload):
    return {"id": f"evt-{kind}", "timestamp": timestamp, "payload": {"type": kind, **payload}}


def _write_cli_db(db_path, *, v2=(), legacy=()):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE conversations_v2 ("
        "key TEXT NOT NULL, conversation_id TEXT NOT NULL, value TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
        "PRIMARY KEY (key, conversation_id))"
    )
    conn.execute("CREATE TABLE conversations (key TEXT PRIMARY KEY, value TEXT)")
    for key, state, created_at, updated_at in v2:
        conn.execute(
            "INSERT INTO conversations_v2 VALUES (?, ?, ?, ?, ?)",
            (key, state["conversation_id"], json.dumps(state), created_at, updated_at),
        )
    for key, state in legacy:
        conn.execute("INSERT INTO conversations VALUES (?, ?)", (key, json.dumps(state)))
    conn.commit()
    conn.close()


def _turn(user_content, assistant, *, timestamp="2026-05-17T10:00:00Z", stream_end_ms=None):
    turn = {
        "user": {
            "env_context": {"env_state": {"current_working_directory": CWD}},
            "content": user_content,
            "timestamp": timestamp,
        },
        "assistant": assistant,
    }
    if stream_end_ms is not None:
        turn["request_metadata"] = {"stream_end_timestamp_ms": stream_end_ms}
    return turn


def _cli_state(conversation_id="cli-db-session", history=None):
    if history is None:
        history = [
            _turn(
                {"Prompt": {"prompt": "List the files"}},
                {"ToolUse": {
                    "message_id": "a1",
                    "content": "Looking.",
                    "tool_uses": [{"id": "tool-1", "name": "execute_bash", "args": {"command": "ls"}}],
                }},
                stream_end_ms=1779012010000,
            ),
            _turn(
                {"ToolUseResults": {"tool_use_results": [{
                    "tool_use_id": "tool-1",
                    "content": [{"Json": {"exit_status": "0", "stdout": "main.py\n"}}],
                    "status": "Success",
                }]}},
                {"Response": {"message_id": "a2", "content": "There is one file."}},
                timestamp="2026-05-17T10:00:20Z",
                stream_end_ms=1779012030000,
            ),
        ]
    return {
        "conversation_id": conversation_id,
        "history": history,
        "model_info": {"model_id": "claude-sonnet-4.5"},
    }


def _write_ide_session(ide_dir, session, *, cwd=CWD, listing=True):
    encoded = base64.urlsafe_b64encode(cwd.encode()).decode().rstrip("=")
    workspace_dir = ide_dir / "workspace-sessions" / encoded
    workspace_dir.mkdir(parents=True, exist_ok=True)
    path = workspace_dir / f"{session['sessionId']}.json"
    path.write_text(json.dumps(session), encoding="utf-8")
    if listing:
        (workspace_dir / "sessions.json").write_text(json.dumps([{
            "sessionId": session["sessionId"],
            "title": session.get("title", ""),
            "dateCreated": "1779012000000",
            "workspaceDirectory": cwd,
        }]), encoding="utf-8")
    return path


def _write_ide_execution(ide_dir, execution_id, actions, *, cwd=CWD):
    bucket = ide_dir / hashlib.sha256(cwd.encode()).hexdigest()[:32] / "bucket"
    bucket.mkdir(parents=True, exist_ok=True)
    (bucket / f"log-{execution_id}").write_text(
        json.dumps({"executionId": execution_id, "actions": actions}), encoding="utf-8"
    )


def _only_session(anonymizer, cwd=CWD, **kwargs):
    projects = discover_projects(source_filter="kiro")
    assert [p["dir_name"] for p in projects] == [cwd]
    sessions = parse_project_sessions(cwd, anonymizer, source="kiro", **kwargs)
    assert len(sessions) == 1
    return sessions[0]


class TestKiroDiscovery:
    def test_nothing_installed(self, kiro, mock_anonymizer):
        assert discover_projects(source_filter="kiro") == []
        assert parse_project_sessions(CWD, mock_anonymizer, source="kiro") == []

    def test_included_when_scanning_every_source(self, kiro, monkeypatch):
        for name in dir(parser):
            if name.startswith("_discover_") and name.endswith("_projects") and "kiro" not in name:
                monkeypatch.setattr(parser, name, lambda: [])
        _write_event_log(
            kiro["sessions"],
            events=[_event("user", content="hi"), _event("assistant", content="hello")],
            meta={"workspacePaths": [CWD]},
        )

        projects = discover_projects()

        assert projects == [{
            "dir_name": CWD,
            "display_name": "kiro:kiro-app",
            "session_count": 1,
            "total_size_bytes": projects[0]["total_size_bytes"],
            "source": "kiro",
        }]
        assert projects[0]["total_size_bytes"] > 0

    def test_groups_sessions_from_every_store_by_working_directory(self, kiro, mock_anonymizer):
        turn = [_event("user", content="hi"), _event("assistant", content="hello")]
        _write_event_log(kiro["sessions"], "sess_one", events=turn, meta={"workspacePaths": [CWD]})
        _write_event_log(
            kiro["sessions"], "sess_two", events=turn, meta={"workspacePaths": ["/workspace/other"]}
        )
        _write_cli_db(kiro["db"], v2=[(CWD, _cli_state(), 1779012000000, 1779012030000)])
        _write_jsonl(kiro["sessions"] / "cli" / "jsonl-session.jsonl", [
            {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "hi"}]}},
            {"kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "hello"}]}},
        ])
        (kiro["sessions"] / "cli" / "jsonl-session.json").write_text(
            json.dumps({"session_id": "jsonl-session", "cwd": CWD}), encoding="utf-8"
        )
        _write_ide_session(kiro["ide"], {
            "sessionId": "ide-session",
            "history": [
                {"message": {"role": "user", "content": "hi"}},
                {"message": {"role": "assistant", "content": "hello"}},
            ],
        })

        projects = {p["display_name"]: p["session_count"] for p in discover_projects("kiro")}
        sessions = parse_project_sessions(CWD, mock_anonymizer, source="kiro")

        assert projects == {"kiro:kiro-app": 4, "kiro:other": 1}
        assert [(s["session_id"], s.get("client_origin")) for s in sessions] == [
            ("sess_one", None),
            ("cli-db-session", "cli"),
            ("jsonl-session", "cli"),
            ("ide-session", "desktop"),
        ]
        assert {s["project"] for s in sessions} == {"kiro:kiro-app"}
        assert {s["source"] for s in sessions} == {"kiro"}

    def test_session_in_two_stores_is_read_from_the_newer_one(self, kiro, mock_anonymizer):
        _write_cli_db(kiro["db"], v2=[(CWD, _cli_state("shared-id"), 1779012000000, 1779012030000)])
        _write_jsonl(kiro["sessions"] / "cli" / "shared-id.jsonl", [
            {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "stale copy"}]}},
            {"kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "stale"}]}},
        ])
        (kiro["sessions"] / "cli" / "shared-id.json").write_text(
            json.dumps({"session_id": "shared-id", "cwd": CWD}), encoding="utf-8"
        )

        session = _only_session(mock_anonymizer)

        assert session["messages"][0]["content"] == "List the files"
        assert session["raw_source_path"] == f"{kiro['db']}#conversation=shared-id"

    def test_session_without_a_working_directory_lands_in_unknown(self, kiro, mock_anonymizer):
        _write_event_log(
            kiro["sessions"],
            events=[_event("user", content="hi"), _event("assistant", content="hello")],
        )

        session = _only_session(mock_anonymizer, cwd=parser.UNKNOWN_KIRO_CWD)

        assert session["project"] == "kiro:unknown"
        assert session["model"] == "kiro-unknown"


class TestKiroEventLog:
    def _session(self, kiro, anonymizer, events, **kwargs):
        path = _write_event_log(kiro["sessions"], events=events, meta={
            "title": "Fix  the\nparser",
            "modelId": "claude-sonnet-4.5",
            "workspacePaths": [CWD],
            "createdAt": "2026-06-01T09:59:00Z",
            "lastModifiedAt": "2026-06-01T10:05:00Z",
        })
        session = _only_session(anonymizer, **kwargs)
        assert session["raw_source_path"] == str(path)
        return session

    def test_folds_events_into_one_message_per_model_response(self, kiro, mock_anonymizer):
        session = self._session(kiro, mock_anonymizer, [
            _event("user", "2026-06-01T10:00:00Z", content="Fix the parser"),
            _event("turn_start", executionId="exec-1"),
            _event("assistant", "2026-06-01T10:00:01Z", content="Plan it.", operationType="Reasoning"),
            _event("assistant", "2026-06-01T10:00:02Z", content="Reading the file."),
            _event("tool_call", "2026-06-01T10:00:03Z", toolName="fs_read", toolCallId="call-1",
                   args={"path": "/Users/testuser/app/main.py"}),
            _event("tool_result", "2026-06-01T10:00:04Z", toolCallId="call-1", content="print('hi')"),
            _event("tool_call", "2026-06-01T10:00:05Z", toolName="execute_bash", toolCallId="call-2",
                   args='{"command": "pytest"}'),
            _event("tool_result", "2026-06-01T10:00:06Z", toolCallId="call-2", content={"exit": 1}),
            _event("assistant", "2026-06-01T10:00:07Z", content="Fixed."),
            _event("usage_summary", promptTurnSummaries=[{"usage": 1.5}]),
            _event("turn_end"),
        ])

        assert session["session_id"] == "sess_abc123"
        assert session["model"] == "claude-sonnet-4.5"
        assert session["segment_title"] == "Fix the parser"
        assert session["start_time"] == "2026-06-01T09:59:00+00:00"
        assert session["end_time"] == "2026-06-01T10:00:07+00:00"
        assert session["stats"]["user_messages"] == 1
        assert session["stats"]["assistant_messages"] == 3
        assert session["stats"]["tool_uses"] == 2
        assert session["messages"] == [
            {"role": "user", "content": "Fix the parser", "timestamp": "2026-06-01T10:00:00+00:00"},
            {
                "role": "assistant",
                "timestamp": "2026-06-01T10:00:01+00:00",
                "thinking": "Plan it.",
                "content": "Reading the file.",
                "tool_uses": [{
                    "tool": "fs_read",
                    "input": {"path": "[REDACTED_PATH]"},
                    "output": {"text": "print('hi')"},
                }],
            },
            {
                "role": "assistant",
                "timestamp": "2026-06-01T10:00:05+00:00",
                "tool_uses": [{
                    "tool": "execute_bash",
                    "input": {"command": "pytest"},
                    "output": {"text": '{"exit": 1}'},
                }],
            },
            {"role": "assistant", "timestamp": "2026-06-01T10:00:07+00:00", "content": "Fixed."},
        ]

    def test_thinking_is_dropped_when_not_requested(self, kiro, mock_anonymizer):
        session = self._session(kiro, mock_anonymizer, [
            _event("user", content="hi"),
            _event("assistant", content="Consider.", operationType="Reasoning"),
            _event("assistant", content="hello"),
        ], include_thinking=False)

        assert [m.get("thinking") for m in session["messages"]] == [None, None]
        assert session["messages"][1]["content"] == "hello"

    def test_subagent_spawn_becomes_a_tool_call_with_its_result(self, kiro, mock_anonymizer):
        session = self._session(kiro, mock_anonymizer, [
            _event("user", content="Review this"),
            _event("sub_agent_start", subSessionId="sub-1", subAgentName="reviewer",
                   explanation="Review the diff", prompt="Check main.py"),
            _event("sub_agent_complete", subSessionId="sub-1", status="error",
                   response="", errorMessage="reviewer crashed"),
        ])

        assert session["messages"][1]["tool_uses"] == [{
            "tool": "invoke_subagent",
            "input": {
                "subagent_type": "reviewer",
                "description": "Review the diff",
                "prompt": "Check main.py",
            },
            "output": {"text": "reviewer crashed"},
            "status": "error",
        }]

    def test_model_falls_back_to_the_first_assistant_event(self, kiro, mock_anonymizer):
        _write_event_log(kiro["sessions"], events=[
            _event("user", content="hi"),
            _event("assistant", content="hello", reasoningModelId="auto"),
        ], meta={"workspacePaths": [CWD]})

        assert _only_session(mock_anonymizer)["model"] == "kiro-auto"

    def test_session_directly_under_sessions_dir_and_malformed_lines(self, kiro, mock_anonymizer):
        session_dir = kiro["sessions"] / "sess_flat"
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(
            json.dumps({"workspacePaths": [CWD]}), encoding="utf-8"
        )
        (session_dir / "messages.jsonl").write_text(
            "not json\n[1, 2]\n"
            + json.dumps(_event("user", 1780308000000, content="hi")) + "\n"
            + json.dumps({"payload": "not an object"}) + "\n"
            + json.dumps(_event("assistant", 1780308001000, content="hello")) + "\n",
            encoding="utf-8",
        )

        session = _only_session(mock_anonymizer)

        assert session["session_id"] == "sess_flat"
        assert [m["timestamp"] for m in session["messages"]] == [
            "2026-06-01T10:00:00+00:00",
            "2026-06-01T10:00:01+00:00",
        ]

    def test_session_without_messages_is_skipped(self, kiro, mock_anonymizer):
        _write_event_log(kiro["sessions"], events=[_event("turn_start")], meta={"workspacePaths": [CWD]})

        assert [p["session_count"] for p in discover_projects("kiro")] == [1]
        assert parse_project_sessions(CWD, mock_anonymizer, source="kiro") == []


class TestKiroCliDatabase:
    def test_parses_turns_and_attaches_tool_results(self, kiro, mock_anonymizer):
        _write_cli_db(kiro["db"], v2=[(CWD, _cli_state(), 1779012000000, 1779012030000)])

        session = _only_session(mock_anonymizer)

        assert session["session_id"] == "cli-db-session"
        assert session["model"] == "claude-sonnet-4.5"
        assert session["client_origin"] == "cli"
        assert session["raw_source_path"] == f"{kiro['db']}#conversation=cli-db-session"
        assert session["start_time"] == "2026-05-17T10:00:00+00:00"
        assert session["end_time"] == "2026-05-17T10:00:30+00:00"
        assert session["stats"]["tool_uses"] == 1
        assert session["messages"] == [
            {"role": "user", "content": "List the files", "timestamp": "2026-05-17T10:00:00+00:00"},
            {
                "role": "assistant",
                "timestamp": "2026-05-17T10:00:10+00:00",
                "content": "Looking.",
                "tool_uses": [{
                    "tool": "execute_bash",
                    "input": {"command": "ls"},
                    "output": {"text": '{"exit_status": "0", "stdout": "main.py\\n"}'},
                    "status": "success",
                }],
            },
            {
                "role": "assistant",
                "timestamp": "2026-05-17T10:00:30+00:00",
                "content": "There is one file.",
            },
        ]

    def test_consecutive_tool_turns_stay_separate_messages(self, kiro, mock_anonymizer):
        def tool_turn(user_content, tool_id):
            return _turn(user_content, {"ToolUse": {
                "content": "",
                "tool_uses": [{"id": tool_id, "name": "fs_read", "args": {"path": "a.py"}}],
            }})

        _write_cli_db(kiro["db"], v2=[(CWD, _cli_state(history=[
            tool_turn({"Prompt": {"prompt": "Read both"}}, "t1"),
            tool_turn({"ToolUseResults": {"tool_use_results": [
                {"tool_use_id": "t1", "content": [{"Text": "first"}], "status": "Error"},
            ]}}, "t2"),
            _turn(
                {"CancelledToolUses": {"prompt": "Stop", "tool_use_results": [
                    {"tool_use_id": "t2", "content": [{"Text": "cancelled"}], "status": "Error"},
                ]}},
                {"Response": {"content": "Stopped."}},
            ),
        ]), 1779012000000, 1779012030000)])

        messages = _only_session(mock_anonymizer)["messages"]

        assert [m["role"] for m in messages] == ["user", "assistant", "assistant", "user", "assistant"]
        assert messages[1]["tool_uses"][0]["output"] == {"text": "first"}
        assert messages[1]["tool_uses"][0]["status"] == "error"
        assert messages[2]["tool_uses"][0]["output"] == {"text": "cancelled"}
        assert messages[3]["content"] == "Stop"

    def test_turn_without_user_text_still_starts_a_new_message(self, kiro, mock_anonymizer):
        _write_cli_db(kiro["db"], v2=[(CWD, _cli_state(history=[
            _turn({"Prompt": {"prompt": "Go"}}, {"Response": {"content": "First reply."}}),
            _turn({"Prompt": {"prompt": ""}}, {"Response": {"content": "Second reply."}}),
        ]), 1779012000000, 1779012030000)])

        messages = _only_session(mock_anonymizer)["messages"]

        assert [m["content"] for m in messages] == ["Go", "First reply.", "Second reply."]

    def test_newest_row_wins_when_a_conversation_has_two_directories(self, kiro, mock_anonymizer):
        old = _cli_state("moved", history=[
            _turn({"Prompt": {"prompt": "old copy"}}, {"Response": {"content": "old"}}),
        ])
        new = _cli_state("moved", history=[
            _turn({"Prompt": {"prompt": "new copy"}}, {"Response": {"content": "new"}}),
        ])
        _write_cli_db(kiro["db"], v2=[
            ("/workspace/old-dir", old, 1779012000000, 1779012010000),
            (CWD, new, 1779012000000, 1779012030000),
        ])

        assert _only_session(mock_anonymizer)["messages"][0]["content"] == "new copy"

    def test_reads_the_kiro_cli_1x_table_and_paired_history(self, kiro, mock_anonymizer):
        state = _cli_state("legacy-session")
        state["history"] = [[turn["user"], turn["assistant"]] for turn in state["history"]]
        del state["model_info"]
        state["model"] = "auto"
        _write_cli_db(kiro["db"], legacy=[(CWD, state)])

        session = _only_session(mock_anonymizer)

        assert session["session_id"] == "legacy-session"
        assert session["model"] == "kiro-auto"
        assert session["start_time"] == "2026-05-17T10:00:00+00:00"
        assert [m["role"] for m in session["messages"]] == ["user", "assistant", "assistant"]
        assert session["messages"][1]["tool_uses"][0]["status"] == "success"

    def test_nested_tool_input_paths_are_anonymized(self, kiro, mock_anonymizer):
        _write_cli_db(kiro["db"], v2=[(CWD, _cli_state(history=[_turn(
            {"Prompt": {"prompt": "Read /Users/testuser/app/main.py"}},
            {"ToolUse": {"content": "", "tool_uses": [{
                "id": "t1",
                "name": "fs_read",
                "args": {"operations": [{"mode": "Line", "path": "/Users/testuser/app/main.py"}]},
            }]}},
        )]), 1779012000000, 1779012030000)])

        messages = _only_session(mock_anonymizer)["messages"]

        assert messages[0]["content"] == "Read [REDACTED_PATH]"
        assert messages[1]["tool_uses"][0]["input"] == {
            "operations": [{"mode": "Line", "path": "[REDACTED_PATH]"}],
        }

    def test_unreadable_database_and_payload_are_skipped(self, kiro, mock_anonymizer):
        kiro["db"].parent.mkdir(parents=True)
        kiro["db"].write_text("this is not a sqlite database", encoding="utf-8")
        assert discover_projects(source_filter="kiro") == []

        kiro["db"].unlink()
        conn = sqlite3.connect(kiro["db"])
        conn.execute(
            "CREATE TABLE conversations_v2 (key TEXT, conversation_id TEXT, value TEXT, "
            "created_at INTEGER, updated_at INTEGER)"
        )
        conn.execute("INSERT INTO conversations_v2 VALUES (?, 'broken', '{not json', 1, 2)", (CWD,))
        conn.commit()
        conn.close()

        assert [p["session_count"] for p in discover_projects("kiro")] == [1]
        assert parse_project_sessions(CWD, mock_anonymizer, source="kiro") == []


class TestKiroCliJsonl:
    def test_parses_records_with_companion_metadata(self, kiro, mock_anonymizer):
        transcript = kiro["sessions"] / "cli" / "file-name.jsonl"
        _write_jsonl(transcript, [
            {"version": "v1", "kind": "Prompt", "data": {
                "content": [{"kind": "text", "data": "Run the tests"}],
                "meta": {"timestamp": 1780308000},
            }},
            {"version": "v1", "kind": "AssistantMessage", "data": {"content": [
                {"kind": "text", "data": "Running."},
                {"kind": "toolUse", "data": {
                    "toolUseId": "tu-1", "name": "execute_bash", "input": {"command": "pytest"},
                }},
            ]}},
            {"version": "v1", "kind": "ToolResults", "data": {"content": [
                {"kind": "toolResult", "data": {
                    "toolUseId": "tu-1",
                    "content": [{"kind": "text", "data": "2 passed"}, {"kind": "json", "data": {"code": 0}}],
                    "status": "success",
                }},
            ]}},
            {"version": "v1", "kind": "Clear", "data": {}},
            {"version": "v1", "kind": "AssistantMessage", "data": {
                "content": [{"kind": "text", "data": "All green."}],
            }},
        ])
        transcript.with_suffix(".json").write_text(json.dumps({
            "session_id": "jsonl-session",
            "cwd": CWD,
            "title": "Run the tests",
            "created_at": "2026-06-01T10:00:00Z",
            "updated_at": "2026-06-01T10:01:00Z",
            "session_state": {"rts_model_state": {"model_info": {"model_id": "claude-haiku-4.5"}}},
        }), encoding="utf-8")

        session = _only_session(mock_anonymizer)

        assert session["session_id"] == "jsonl-session"
        assert session["model"] == "claude-haiku-4.5"
        assert session["segment_title"] == "Run the tests"
        assert session["client_origin"] == "cli"
        assert session["raw_source_path"] == str(transcript)
        assert session["start_time"] == "2026-06-01T10:00:00+00:00"
        assert session["messages"] == [
            {"role": "user", "content": "Run the tests", "timestamp": "2026-06-01T10:00:00+00:00"},
            {
                "role": "assistant",
                "timestamp": None,
                "content": "Running.",
                "tool_uses": [{
                    "tool": "execute_bash",
                    "input": {"command": "pytest"},
                    "output": {"text": '2 passed\n\n{"code": 0}'},
                    "status": "success",
                }],
            },
            {"role": "assistant", "timestamp": None, "content": "All green."},
        ]

    def test_transcript_without_metadata_uses_its_file_name(self, kiro, mock_anonymizer):
        _write_jsonl(kiro["sessions"] / "bare-session.jsonl", [
            {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "hi"}]}},
            {"kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "hello"}]}},
        ])

        session = _only_session(mock_anonymizer, cwd=parser.UNKNOWN_KIRO_CWD)

        assert session["session_id"] == "bare-session"
        assert session["start_time"] is None


class TestKiroIdeSession:
    def _history(self, assistant):
        return [{"message": {"role": "user", "content": [{"type": "text", "text": "Rename it"}]}}, assistant]

    def test_replays_the_execution_log_behind_an_assistant_turn(self, kiro, mock_anonymizer):
        path = _write_ide_session(kiro["ide"], {
            "sessionId": "ide-session",
            "title": "Rename the loader",
            "workspaceDirectory": CWD,
            "history": self._history(
                {"message": {"role": "assistant", "content": "On it."}, "executionId": "exec-1"}
            ),
        })
        _write_ide_execution(kiro["ide"], "exec-1", [
            {"actionId": "a1", "actionType": "say", "output": {"message": "Renaming the loader."}},
            {"actionId": "a2", "actionType": "replace", "input": {
                "file": "config.py",
                "originalContent": "def load():\n    pass\n",
                "modifiedContent": "def load_config():\n    pass\n",
            }},
            {"actionId": "a3", "actionType": "create", "input": {
                "file": "notes.md", "modifiedContent": "renamed\n",
            }},
            {"actionId": "a4", "actionType": "say", "output": {"message": "Done."}},
        ])

        session = _only_session(mock_anonymizer)

        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        assert session["client_origin"] == "desktop"
        assert session["raw_source_path"] == str(path)
        assert session["segment_title"] == "Rename the loader"
        assert session["model"] == "kiro-unknown"
        assert session["start_time"] == "2026-05-17T10:00:00+00:00"
        assert session["end_time"] == modified
        assert session["messages"] == [
            {"role": "user", "content": "Rename it", "timestamp": None},
            {
                "role": "assistant",
                "timestamp": None,
                "content": "Renaming the loader.",
                "tool_uses": [
                    {"tool": "replace", "input": {
                        "file": "config.py",
                        "diff": (
                            "--- a/config.py\n+++ b/config.py\n@@ -1,2 +1,2 @@\n"
                            "-def load():\n+def load_config():\n     pass"
                        ),
                    }},
                    {"tool": "create", "input": {"file": "notes.md", "content": "renamed\n"}},
                ],
            },
            {"role": "assistant", "timestamp": None, "content": "Done."},
        ]

    def test_falls_back_to_the_recorded_reply_then_the_raw_completions(self, kiro, mock_anonymizer):
        _write_ide_session(kiro["ide"], {
            "sessionId": "ide-session",
            "history": [
                {"message": {"role": "user", "content": "one"}},
                {"message": {"role": "assistant", "content": "Recorded."}, "executionId": "missing"},
                {"message": {"role": "user", "content": "two"}},
                {
                    "message": {"role": "assistant", "content": ""},
                    "promptLogs": [{"completion": "First."}, {"completion": "Second."}],
                },
                {"message": {"role": "tool", "content": "ignored"}},
            ],
        })

        messages = _only_session(mock_anonymizer)["messages"]

        assert [m["content"] for m in messages] == ["one", "Recorded.", "two", "First.\n\nSecond."]

    def test_working_directory_comes_from_the_folder_name_or_the_session(self, kiro, mock_anonymizer):
        _write_ide_session(kiro["ide"], {
            "sessionId": "from-folder",
            "history": self._history({"message": {"role": "assistant", "content": "ok"}}),
        }, listing=False)
        opaque = kiro["ide"] / "workspace-sessions" / "not-base64!"
        opaque.mkdir(parents=True)
        (opaque / "from-session.json").write_text(json.dumps({
            "sessionId": "from-session",
            "workspaceDirectory": "/workspace/elsewhere",
            "history": self._history({"message": {"role": "assistant", "content": "ok"}}),
        }), encoding="utf-8")

        projects = {p["dir_name"]: p["session_count"] for p in discover_projects("kiro")}

        assert projects == {CWD: 1, "/workspace/elsewhere": 1}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-05-17T10:00:00Z", "2026-05-17T10:00:00+00:00"),
        ("2026-05-17T06:00:00.123456789-04:00", "2026-05-17T10:00:00.123456+00:00"),
        (1779012000000, "2026-05-17T10:00:00+00:00"),
        (1779012000, "2026-05-17T10:00:00+00:00"),
        ("1779012000000", "2026-05-17T10:00:00+00:00"),
        ("2026-05-17T10:00:00", "2026-05-17T10:00:00"),
        ("yesterday", "yesterday"),
        ("", None),
        (None, None),
        (True, None),
    ],
)
def test_kiro_timestamp(value, expected):
    assert _kiro_timestamp(value) == expected
