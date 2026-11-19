"""Tests for Antigravity CLI (`agy`) session discovery and parsing.

Every fixture here is synthetic and built to the layout of the CLI's data
directory; none is copied from a real conversation.
"""

import json
import sqlite3

import pytest

from clawjournal.parsing import parser
from clawjournal.parsing.parser import (
    _antigravity_file_uri_path,
    discover_projects,
    parse_project_sessions,
)

CWD = "/workspace/dev/agy-app"
CONVERSATION = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
MODEL = "Gemini 3.5 Flash (Medium)"

# The table as the CLI creates it.
_SUMMARIES_SCHEMA = (
    "CREATE TABLE conversation_summaries (conversation_id text PRIMARY KEY, title text, "
    "preview text, step_count integer, last_modified_time datetime NOT NULL, "
    "workspace_uris text NOT NULL, status text, source text, project_id text, "
    "agent_name text, parent_conversation_id text, nesting_depth integer, battle_id text, "
    "winning_conversation_id text, not_fully_idle numeric, killed numeric, "
    "last_user_input_time datetime NOT NULL, last_user_input_step_index integer, "
    "app_data_dir text, raw_summary blob, group_id text)"
)


@pytest.fixture
def agy(tmp_path, monkeypatch):
    """Point the Antigravity CLI data directory at an empty tmp tree."""
    home = tmp_path / "gemini" / "antigravity-cli"
    monkeypatch.setattr(parser, "ANTIGRAVITY_DIR", home)
    monkeypatch.setattr(parser, "_ANTIGRAVITY_PROJECT_INDEX", {})
    return home


def _step(index, source, kind, created_at="2026-11-07T15:53:32Z", status="DONE", **fields):
    return {
        "step_index": index,
        "source": source,
        "type": kind,
        "status": status,
        "created_at": created_at,
        **fields,
    }


def _prompt(index, text, created_at="2026-11-07T15:53:32Z", model=MODEL):
    content = (
        f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\n"
        "The current local time is: 2026-11-07T10:53:32-05:00.\n"
        "</ADDITIONAL_METADATA>"
    )
    if model:
        content += (
            "\n<USER_SETTINGS_CHANGE>\n"
            f"The user changed setting `Model Selection` from None to {model}. "
            "No need to comment on this change if the user doesn't ask about it.\n"
            "</USER_SETTINGS_CHANGE>"
        )
    return _step(index, "USER_EXPLICIT", "USER_INPUT", created_at, content=content)


def _response(index, created_at="2026-11-07T15:53:33Z", **fields):
    return _step(index, "MODEL", "PLANNER_RESPONSE", created_at, **fields)


def _result(index, body, created_at="2026-11-07T15:53:34Z", kind="GENERIC", **fields):
    header = (
        "Created At: 2026-11-07T10:53:34-05:00\n"
        "Completed At: 2026-11-07T10:53:34-05:00\n"
    )
    return _step(index, "MODEL", kind, created_at, content=header + body, **fields)


def _system_message(index, line, created_at="2026-11-07T15:54:00Z", source="SYSTEM"):
    content = (
        "The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided "
        "by the system as important information to pay attention to.\n\n"
        f"<SYSTEM_MESSAGE>\n{line}\n</SYSTEM_MESSAGE>"
    )
    return _step(index, source, "SYSTEM_MESSAGE", created_at, content=content)


def _write_transcript(home, steps, conversation=CONVERSATION, name="transcript_full.jsonl"):
    path = home / "brain" / conversation / ".system_generated" / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(step) + "\n" for step in steps), encoding="utf-8")
    return path


def _write_summaries(home, rows, schema=_SUMMARIES_SCHEMA):
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "conversation_summaries.db")
    conn.execute(schema)
    for row in rows:
        row = {
            "last_modified_time": "2026-11-07 15:54:00.000000000+00:00",
            "last_user_input_time": "2026-11-07 15:53:32.000000000+00:00",
            **row,
        }
        if "workspace_uris" in row and not isinstance(row["workspace_uris"], str):
            row["workspace_uris"] = json.dumps(row["workspace_uris"])
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO conversation_summaries ({columns}) VALUES ({marks})",
            list(row.values()),
        )
    conn.commit()
    conn.close()


def _summary(conversation=CONVERSATION, cwd=CWD, **fields):
    return {
        "conversation_id": conversation,
        "workspace_uris": [f"file://{cwd}"] if cwd else [],
        **fields,
    }


def _hello(home, conversation=CONVERSATION, **kwargs):
    return _write_transcript(
        home,
        [_prompt(0, "hi"), _response(1, content="hello")],
        conversation,
        **kwargs,
    )


def _only_session(anonymizer, cwd=CWD, **kwargs):
    projects = discover_projects(source_filter="antigravity")
    assert [p["dir_name"] for p in projects] == [cwd]
    sessions = parse_project_sessions(cwd, anonymizer, source="antigravity", **kwargs)
    assert len(sessions) == 1
    return sessions[0]


def _tool_uses(session):
    return [tool_use for m in session["messages"] for tool_use in m.get("tool_uses", [])]


class TestAntigravityDiscovery:
    def test_nothing_installed(self, agy, mock_anonymizer):
        assert discover_projects(source_filter="antigravity") == []
        assert parse_project_sessions(CWD, mock_anonymizer, source="antigravity") == []

    def test_included_when_scanning_every_source(self, agy, monkeypatch):
        for name in dir(parser):
            if (
                name.startswith("_discover_")
                and name.endswith("_projects")
                and "antigravity" not in name
            ):
                monkeypatch.setattr(parser, name, lambda: [])
        transcript = _hello(agy)
        _write_summaries(agy, [_summary()])

        assert discover_projects() == [{
            "dir_name": CWD,
            "display_name": "antigravity:agy-app",
            "session_count": 1,
            "total_size_bytes": transcript.stat().st_size,
            "source": "antigravity",
        }]

    def test_groups_conversations_by_recorded_workspace(self, agy, mock_anonymizer):
        _hello(agy)
        _hello(agy, OTHER)
        _hello(agy, "cccccccc-0000-4000-8000-000000000001")
        _write_summaries(agy, [
            _summary(),
            _summary(OTHER),
            # Percent-encoded, with a trailing slash and a second folder.
            {
                "conversation_id": "cccccccc-0000-4000-8000-000000000001",
                "workspace_uris": ["file:///workspace/my%20notes/", "file:///workspace/extra"],
            },
        ])

        projects = discover_projects(source_filter="antigravity")

        assert [(p["dir_name"], p["display_name"], p["session_count"]) for p in projects] == [
            (CWD, "antigravity:agy-app", 2),
            ("/workspace/my notes", "antigravity:my notes", 1),
        ]
        sessions = parse_project_sessions(CWD, mock_anonymizer, source="antigravity")
        assert sorted(s["session_id"] for s in sessions) == sorted([CONVERSATION, OTHER])
        assert {s["project"] for s in sessions} == {"antigravity:agy-app"}

    def test_workspace_falls_back_to_the_resume_files(self, agy):
        """Without a summary row, history.jsonl and last_conversations.json say where."""
        in_history = "cccccccc-0000-4000-8000-000000000001"
        in_cache = "cccccccc-0000-4000-8000-000000000002"
        nowhere = "cccccccc-0000-4000-8000-000000000003"
        for conversation in (in_history, in_cache, nowhere):
            _hello(agy, conversation)
        (agy / "history.jsonl").write_text(
            # A fresh launch's first prompt has no conversation ID.
            json.dumps({"display": "hi", "timestamp": 1, "workspace": "/workspace/ignored"})
            + "\nnot json\n"
            + json.dumps({
                "display": "hi", "timestamp": 2,
                "workspace": "/workspace/from-history", "conversationId": in_history,
            })
            + "\n",
            encoding="utf-8",
        )
        (agy / "cache").mkdir()
        (agy / "cache" / "last_conversations.json").write_text(
            json.dumps({"/workspace/from-cache": in_cache}), encoding="utf-8"
        )

        projects = discover_projects(source_filter="antigravity")

        assert [(p["dir_name"], p["display_name"]) for p in projects] == [
            ("/workspace/from-cache", "antigravity:from-cache"),
            ("/workspace/from-history", "antigravity:from-history"),
            ("<unknown-cwd>", "antigravity:unknown"),
        ]

    def test_subagent_without_a_workspace_uses_its_parents(self, agy):
        _hello(agy)
        _hello(agy, OTHER)
        _write_summaries(agy, [
            _summary(),
            _summary(OTHER, cwd=None, parent_conversation_id=CONVERSATION),
        ])

        projects = discover_projects(source_filter="antigravity")

        assert [(p["dir_name"], p["session_count"]) for p in projects] == [(CWD, 2)]

    def test_skips_conversations_without_a_step_log(self, agy):
        # /clear makes the folder at once and the log only with the next prompt.
        (agy / "brain" / OTHER).mkdir(parents=True)
        (agy / "brain" / "not-a-conversation.txt").write_text("x", encoding="utf-8")
        _write_transcript(agy, [], "cccccccc-0000-4000-8000-000000000001")
        _hello(agy)
        _write_summaries(agy, [_summary(), _summary(OTHER)])

        projects = discover_projects(source_filter="antigravity")

        assert [(p["dir_name"], p["session_count"]) for p in projects] == [(CWD, 1)]

    def test_reads_the_full_transcript_when_both_exist(self, agy, mock_anonymizer):
        full = _write_transcript(agy, [_prompt(0, "hi"), _response(1, content="from full")])
        _write_transcript(
            agy, [_prompt(0, "hi"), _response(1, content="from compact")], name="transcript.jsonl"
        )
        _write_summaries(agy, [_summary()])

        session = _only_session(mock_anonymizer)

        assert session["raw_source_path"] == str(full)
        assert session["messages"][1]["content"] == "from full"

    def test_reads_the_compact_transcript_when_the_full_one_is_empty(self, agy, mock_anonymizer):
        _write_transcript(agy, [])
        compact = _write_transcript(
            agy, [_prompt(0, "hi"), _response(1, content="from compact")], name="transcript.jsonl"
        )
        _write_summaries(agy, [_summary()])

        session = _only_session(mock_anonymizer)

        assert session["raw_source_path"] == str(compact)
        assert session["messages"][1]["content"] == "from compact"

    @pytest.mark.parametrize("schema", [
        # Builds that predate the workspace, title and parent columns.
        "CREATE TABLE conversation_summaries (conversation_id text PRIMARY KEY, "
        "last_modified_time datetime, last_user_input_time datetime)",
        "CREATE TABLE something_else (conversation_id text, "
        "last_modified_time datetime, last_user_input_time datetime)",
    ])
    def test_summaries_from_another_schema(self, agy, mock_anonymizer, schema):
        _hello(agy)
        table = schema.split()[2]
        conn = sqlite3.connect(agy / "conversation_summaries.db")
        conn.execute(schema)
        conn.execute(f"INSERT INTO {table} (conversation_id) VALUES (?)", (CONVERSATION,))
        conn.commit()
        conn.close()

        session = _only_session(mock_anonymizer, cwd="<unknown-cwd>")

        assert session["project"] == "antigravity:unknown"
        assert "segment_title" not in session

    def test_unreadable_summaries_database(self, agy, mock_anonymizer):
        _hello(agy)
        (agy / "conversation_summaries.db").write_text("not a database", encoding="utf-8")

        session = _only_session(mock_anonymizer, cwd="<unknown-cwd>")

        assert [m["content"] for m in session["messages"]] == ["hi", "hello"]


class TestAntigravityTranscript:
    def _session(self, agy, anonymizer, steps, *, summary=None, name="transcript_full.jsonl", **kwargs):
        path = _write_transcript(agy, steps, name=name)
        _write_summaries(agy, [summary or _summary(title="Fix  the\nparser")])
        session = _only_session(anonymizer, **kwargs)
        assert session["raw_source_path"] == str(path)
        return session

    def test_steps_become_user_and_assistant_messages(self, agy, mock_anonymizer):
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "Fix the parser\nand run the tests", "2026-11-07T15:53:32Z"),
            _step(1, "SYSTEM", "CONVERSATION_HISTORY", "2026-11-07T15:53:32Z"),
            _response(
                2, "2026-11-07T15:53:33Z",
                thinking="Read the file first.",
                content="Reading the file.",
                tool_calls=[{"name": "read_file", "args": {
                    "Path": "/Users/testuser/app/main.py", "StartLine": 1, "EndLine": 40,
                }}],
                input_tokens=7000, cache_read_tokens=0, output_tokens=40,
            ),
            _result(3, "File Path: `file:///Users/testuser/app/main.py`\n1: print('hi')",
                    "2026-11-07T15:53:34Z"),
            _response(
                4, "2026-11-07T15:53:35Z",
                tool_calls=[{"name": "run_command", "args": {
                    "CommandLine": "pytest -q", "Cwd": "/Users/testuser/app",
                    "WaitMsBeforeAsync": 5000, "toolSummary": "Run the tests",
                }}],
                input_tokens=2200, cache_read_tokens=5000, output_tokens=60,
            ),
            _result(5, "Command exited with code 0.\n\nCommand output:\n3 passed\r\n",
                    "2026-11-07T15:53:40Z"),
            _response(6, "2026-11-07T15:53:41Z", content="Fixed.",
                      input_tokens=2300, cache_read_tokens=5300, output_tokens=10),
        ])

        assert session["session_id"] == CONVERSATION
        assert session["source"] == "antigravity"
        assert session["project"] == "antigravity:agy-app"
        assert session["client_origin"] == "cli"
        assert session["model"] == MODEL
        assert session["segment_title"] == "Fix the parser"
        assert "parent_session_id" not in session
        assert session["start_time"] == "2026-11-07T15:53:32+00:00"
        assert session["end_time"] == "2026-11-07T15:53:41+00:00"
        assert session["stats"] == {
            "user_messages": 1,
            "assistant_messages": 3,
            "tool_uses": 2,
            "input_tokens": 11500,
            "output_tokens": 110,
            "cache_read_tokens": 10300,
            "cache_creation_tokens": 0,
            "user_interrupts": 0,
        }
        header = (
            "Created At: 2026-11-07T10:53:34-05:00\n"
            "Completed At: 2026-11-07T10:53:34-05:00\n"
        )
        assert session["messages"] == [
            {
                "role": "user",
                "content": "Fix the parser\nand run the tests",
                "timestamp": "2026-11-07T15:53:32+00:00",
            },
            {
                "role": "assistant",
                "timestamp": "2026-11-07T15:53:33+00:00",
                "content": "Reading the file.",
                "thinking": "Read the file first.",
                "tool_uses": [{
                    "tool": "read_file",
                    "input": {"Path": "[REDACTED_PATH]", "StartLine": 1, "EndLine": 40},
                    "output": {
                        "text": header + "File Path: `file://[REDACTED_PATH]`\n1: print('hi')",
                    },
                    "status": "success",
                }],
            },
            {
                "role": "assistant",
                "timestamp": "2026-11-07T15:53:35+00:00",
                "tool_uses": [{
                    "tool": "run_command",
                    "input": {
                        "CommandLine": "pytest -q",
                        "Cwd": "[REDACTED_PATH]",
                        "WaitMsBeforeAsync": 5000,
                        "toolSummary": "Run the tests",
                    },
                    "output": {
                        "text": header + "Command exited with code 0.\n\nCommand output:\n3 passed",
                    },
                    "status": "success",
                }],
            },
            {"role": "assistant", "timestamp": "2026-11-07T15:53:41+00:00", "content": "Fixed."},
        ]

    def test_results_answer_calls_by_step_index(self, agy, mock_anonymizer):
        """The nth call of the response at step N is answered at step N + n."""
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "Do three things"),
            _response(1, tool_calls=[
                {"name": "read_file", "args": {"Path": "a.txt"}},
                {"name": "read_file", "args": {"Path": "b.txt"}},
                {"name": "read_file", "args": {"Path": "c.txt"}},
            ]),
            # Lines land as steps settle: the third result before the first.
            _result(4, "c"),
            _result(2, "a"),
            # Step 3 never got a line: the second call has no result.
            # A compaction is appended late, after steps that follow it.
            _response(8, content="Done."),
            _result(6, "Command exited with code 0.\n\nCommand output:\nfile"),
            _response(5, tool_calls=[{"name": "run_command", "args": {"CommandLine": "ls"}}]),
            _step(7, "SYSTEM", "CHECKPOINT", content="# Resuming from a compaction\n<summary>…</summary>"),
            # A result no call is waiting for is left out.
            _result(9, "stray"),
        ])

        outputs = [
            (tool_use["input"], tool_use.get("output", {}).get("text", "").rsplit("\n", 1)[-1])
            for tool_use in _tool_uses(session)
        ]
        assert outputs == [
            ({"Path": "a.txt"}, "a"),
            ({"Path": "b.txt"}, ""),
            ({"Path": "c.txt"}, "c"),
            ({"CommandLine": "ls"}, "file"),
        ]
        assert "output" not in _tool_uses(session)[1]
        assert [m["role"] for m in session["messages"]] == ["user", "assistant", "assistant", "assistant"]
        assert session["messages"][-1]["content"] == "Done."
        assert session["stats"]["tool_uses"] == 4

    @pytest.mark.parametrize("result, status", [
        (_result(2, "Command exited with code 0.\n\nCommand output:\nok"), "success"),
        (_result(2, "Command exited with code 1.\n\nCommand output:\nboom"), "error"),
        # Earlier builds: another wording, a typed step and an exit_code field.
        (_result(2, "\n\t\t\t\tThe command exited with code 2.\n\t\t\t\tOutput:\n\t\t\t\tboom",
                 kind="RUN_COMMAND"), "error"),
        (_result(2, "The command failed.", kind="RUN_COMMAND", exit_code=3), "error"),
        (_result(2, "The command completed successfully.", kind="RUN_COMMAND", exit_code=0),
         "success"),
        # The words in a command's own output are not its exit status.
        (_result(2, "Command exited with code 0.\n\nCommand output:\n"
                    "Command exited with code 1."), "success"),
        (_result(2, "Encountered error in step execution: file does not exist",
                 error="file does not exist"), "error"),
        (_result(2, "permission check failed: user denied permission", status="ERROR",
                 error="user denied permission"), "error"),
        # Sent to the background: the log never says how the step itself ended.
        (_result(2, f"Tool is running as a background task with task id: {CONVERSATION}/task-2",
                 status="RUNNING"), None),
    ])
    def test_tool_result_status(self, agy, mock_anonymizer, result, status):
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "Run it"),
            _response(1, tool_calls=[{"name": "run_command", "args": {"CommandLine": "make"}}]),
            result,
        ])

        (tool_use,) = _tool_uses(session)
        assert tool_use.get("status") == status
        assert tool_use["output"]["text"].startswith("Created At: ")

    def test_error_text_stands_in_for_a_result_without_content(self, agy, mock_anonymizer):
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "Read it"),
            _response(1, tool_calls=[{"name": "read_file", "args": {"Path": "x"}}]),
            _step(2, "MODEL", "GENERIC", status="ERROR", error="file does not exist"),
        ])

        assert _tool_uses(session) == [{
            "tool": "read_file",
            "input": {"Path": "x"},
            "output": {"text": "file does not exist"},
            "status": "error",
        }]

    def test_compact_transcript_arguments_are_json_strings(self, agy, mock_anonymizer):
        """transcript.jsonl encodes arguments as strings and cuts long ones short."""
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "Write two files"),
            _response(1, tool_calls=[
                {"name": "write_file", "args": json.dumps({
                    "Path": "/Users/testuser/app/a.txt", "Content": "alpha", "Overwrite": True,
                })},
                {"name": "write_file",
                 "args": '{"Content":"beta beta beta\n<truncated 4096 bytes>\n'},
                {"name": "list_permissions"},
            ], truncated_fields=["tool_calls"]),
            _result(2, "Created file."),
            _result(3, "Created file."),
            _result(4, "[]"),
        ], name="transcript.jsonl")

        assert [(t["tool"], t["input"], t["status"]) for t in _tool_uses(session)] == [
            ("write_file", {"Path": "[REDACTED_PATH]", "Content": "alpha", "Overwrite": True},
             "success"),
            ("write_file", {"raw": '{"Content":"beta beta beta\n<truncated 4096 bytes>\n'},
             "success"),
            ("list_permissions", {}, "success"),
        ]

    def test_tool_arguments_are_anonymized_and_commands_redacted(self, agy, mock_anonymizer):
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "Delegate it"),
            _response(1, tool_calls=[
                {"name": "run_command", "args": {
                    "CommandLine": f"GITHUB_TOKEN={secret} gh api user",
                    "Cwd": "/Users/testuser/app",
                }},
                {"name": "invoke_subagent", "args": {"Subagents": [{
                    "Model": "inherit",
                    "Prompt": "Look in /Users/testuser/app/src",
                    "Role": "explorer",
                    "Workspace": "/Users/testuser/app",
                }]}},
            ]),
        ])

        command, subagent = (t["input"] for t in _tool_uses(session))
        assert secret not in command["CommandLine"]
        assert command["CommandLine"].endswith(" gh api user")
        assert command["Cwd"] == "[REDACTED_PATH]"
        assert subagent == {"Subagents": [{
            "Model": "inherit",
            "Prompt": "Look in [REDACTED_PATH]",
            "Role": "explorer",
            "Workspace": "[REDACTED_PATH]",
        }]}

    def test_thinking_can_be_left_out(self, agy, mock_anonymizer):
        session = self._session(
            agy,
            mock_anonymizer,
            [
                _prompt(0, "Think"),
                # A response that only reasoned is not a message without it.
                _response(1, thinking="Hmm.", input_tokens=10, output_tokens=5),
                _response(2, thinking="So.", content="Answer.", input_tokens=20, output_tokens=7),
            ],
            include_thinking=False,
        )

        assert session["messages"][1:] == [
            {"role": "assistant", "timestamp": "2026-11-07T15:53:33+00:00", "content": "Answer."},
        ]
        assert session["stats"]["assistant_messages"] == 1
        assert session["stats"]["input_tokens"] == 30
        assert session["stats"]["output_tokens"] == 12

    def test_task_results_and_messages_reach_the_model_as_user_turns(self, agy, mock_anonymizer):
        task = f"{CONVERSATION}/task-2"
        finished = (
            f"[Message] timestamp=2026-11-07T15:54:00Z sender={task} "
            f'priority=MESSAGE_PRIORITY_HIGH content=Task id "{task}" finished with result:\n'
            "built /Users/testuser/app/dist"
        )
        report = (
            f"[Message] timestamp=2026-11-07T15:55:00Z sender={OTHER} "
            "priority=MESSAGE_PRIORITY_HIGH content=All tests pass."
        )
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "Build it"),
            _response(1, tool_calls=[{"name": "run_command", "args": {"CommandLine": "make"}}]),
            _result(2, f"Tool is running as a background task with task id: {task}",
                    status="RUNNING"),
            _response(3, content="Waiting for the build."),
            _system_message(4, finished, "2026-11-07T15:54:00Z"),
            _response(5, "2026-11-07T15:54:01Z", content="Built."),
            _system_message(6, report, "2026-11-07T15:55:00Z"),
            _response(7, "2026-11-07T15:55:01Z", content="The subagent confirms it."),
        ])

        assert [(m["role"], m["content"]) for m in session["messages"][3:]] == [
            ("user", finished.replace("/Users/testuser/app/dist", "[REDACTED_PATH]")),
            ("assistant", "Built."),
            ("user", report),
            ("assistant", "The subagent confirms it."),
        ]
        assert session["stats"]["user_messages"] == 3
        # A message from another conversation does not make this one a subagent.
        assert "parent_session_id" not in session
        assert session["segment_title"] == "Fix the parser"

    def test_the_clis_own_notices_and_reminders_are_left_out(self, agy, mock_anonymizer):
        restart = (
            "[Message] timestamp=2026-11-07T15:54:00Z sender=system "
            "priority=MESSAGE_PRIORITY_LOW content=[Notice] All your subagents and background "
            "tasks have been stopped due to server restart."
        )
        failure = (
            "[Message] timestamp=2026-11-07T15:54:02Z sender=system "
            f'priority=MESSAGE_PRIORITY_HIGH content=The subagent explorer (general) with id '
            f'"{OTHER}" encountered an error and has either stopped or failed to start execution: '
            "quota"
        )
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "Go on"),
            # Added after the first prompt each time a conversation is resumed.
            _system_message(1, restart),
            _system_message(2, "This is a periodic system reminder.", source="SYSTEM_SDK"),
            _response(3, content="Going on."),
            _system_message(4, failure, "2026-11-07T15:54:02Z"),
            _step(5, "SYSTEM", "ERROR_MESSAGE", content="Error: The stream was interrupted.",
                  error="The stream was interrupted."),
            _response(6, "2026-11-07T15:54:03Z", content="The subagent failed."),
        ])

        assert [(m["role"], m["content"]) for m in session["messages"]] == [
            ("user", "Go on"),
            ("assistant", "Going on."),
            ("user", failure),
            ("assistant", "The subagent failed."),
        ]
        assert session["stats"]["user_messages"] == 2

    def test_subagent_conversation_starts_with_its_parents_message(self, agy, mock_anonymizer):
        """A subagent has no prompt, title or model of its own."""
        assignment = (
            f"[Message] timestamp=2026-11-07T15:53:32Z sender={OTHER}\n"
            "Run `echo hi` in /Users/testuser/app\nand report the exact output."
        )
        session = self._session(
            agy,
            mock_anonymizer,
            [
                _system_message(0, "This is a periodic system reminder.", source="SYSTEM_SDK"),
                _system_message(1, assignment, "2026-11-07T15:53:32Z"),
                _response(2, tool_calls=[{"name": "run_command", "args": {"CommandLine": "echo hi"}}]),
                _result(3, "Command exited with code 0.\n\nCommand output:\nhi"),
                _response(4, "2026-11-07T15:53:36Z", tool_calls=[
                    {"name": "send_message", "args": {"Message": "hi", "Recipient": OTHER}},
                ]),
                _result(5, "Message sent.", "2026-11-07T15:53:37Z"),
            ],
            summary=_summary(),
        )

        assert session["parent_session_id"] == OTHER
        assert session["segment_title"] == "Run `echo hi` in [REDACTED_PATH]"
        assert session["model"] == "antigravity-unknown"
        assert session["messages"][0] == {
            "role": "user",
            "content": assignment.replace("/Users/testuser/app", "[REDACTED_PATH]"),
            "timestamp": "2026-11-07T15:53:32+00:00",
        }
        assert session["end_time"] == "2026-11-07T15:53:37+00:00"

    def test_recorded_parent_and_title_win_over_the_first_message(self, agy, mock_anonymizer):
        sibling = "cccccccc-0000-4000-8000-000000000001"
        session = self._session(
            agy,
            mock_anonymizer,
            [
                # A swarm worker can hear from a sibling before its coordinator.
                _system_message(
                    0, f"[Message] timestamp=2026-11-07T15:53:32Z sender={sibling} "
                       "priority=MESSAGE_PRIORITY_LOW content=[swarm] new board message"),
                _response(1, content="Noted."),
            ],
            summary=_summary(title="Explore the repo", parent_conversation_id=OTHER),
        )

        assert session["parent_session_id"] == OTHER
        assert session["segment_title"] == "Explore the repo"

    def test_prompt_without_the_request_wrapper(self, agy, mock_anonymizer):
        session = self._session(agy, mock_anonymizer, [
            _step(0, "USER_EXPLICIT", "USER_INPUT", content=(
                "  plain prompt  \n<ADDITIONAL_METADATA>\nThe current local time is: now.\n"
                "</ADDITIONAL_METADATA>"
            )),
            # A prompt that is only the CLI's own blocks is not a message.
            _step(1, "USER_EXPLICIT", "USER_INPUT", content=(
                "<ADDITIONAL_METADATA>\nThe current local time is: now.\n</ADDITIONAL_METADATA>"
            )),
            _response(2, content="ok"),
        ])

        assert [(m["role"], m["content"]) for m in session["messages"]] == [
            ("user", "plain prompt"),
            ("assistant", "ok"),
        ]
        assert session["model"] == "antigravity-unknown"

    def test_model_comes_from_the_first_settings_change(self, agy, mock_anonymizer):
        change = (
            "<USER_REQUEST>\nagain\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\n"
            f"The user changed setting `Model Selection` from {MODEL} to Claude Sonnet 4.6 "
            "(Thinking). No need to comment on this change if the user doesn't ask about it.\n"
            "</USER_SETTINGS_CHANGE>"
        )
        session = self._session(agy, mock_anonymizer, [
            _prompt(0, "hi", model=None),
            _response(1, content="hello"),
            _step(2, "USER_EXPLICIT", "USER_INPUT", content=change),
            _response(3, content="hello again"),
        ])

        assert session["model"] == "Claude Sonnet 4.6 (Thinking)"
        assert [m["content"] for m in session["messages"]] == ["hi", "hello", "again", "hello again"]

    def test_tolerates_lines_it_cannot_use(self, agy, mock_anonymizer):
        path = _write_transcript(agy, [])
        lines = [
            json.dumps(_prompt(0, "hi")),
            "{not json",
            json.dumps(["a", "list"]),
            json.dumps(_step(1, "MODEL", "PLANNER_RESPONSE", content=7, tool_calls="nope")),
            json.dumps(_step(2, "MODEL", "PLANNER_RESPONSE", content="hello", tool_calls=[
                "not a call", {"args": {"x": 1}}, {"name": "read_file", "args": None},
            ])),
            json.dumps(_step(3, "USER_EXPLICIT", "SOMETHING_NEW", content="ignored")),
            # No step index: the line stays where the file has it.
            json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "bye",
                        "created_at": "not a time"}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _write_summaries(agy, [_summary()])

        session = _only_session(mock_anonymizer)

        assert [(m["role"], m.get("content")) for m in session["messages"]] == [
            ("user", "hi"),
            ("assistant", "hello"),
            ("assistant", "bye"),
        ]
        assert _tool_uses(session) == [{"tool": "read_file", "input": {}}]

    def test_conversation_without_messages_is_not_a_session(self, agy, mock_anonymizer):
        _write_transcript(agy, [
            _step(0, "SYSTEM", "CONVERSATION_HISTORY"),
            _step(1, "SYSTEM", "CHECKPOINT", content="{{ CHECKPOINT 0 }}"),
        ])
        _write_summaries(agy, [_summary()])

        assert [p["session_count"] for p in discover_projects(source_filter="antigravity")] == [1]
        assert parse_project_sessions(CWD, mock_anonymizer, source="antigravity") == []


@pytest.mark.parametrize("uri, expected", [
    ("file:///workspace/dev/agy-app", "/workspace/dev/agy-app"),
    ("file:///workspace/dev/agy-app/", "/workspace/dev/agy-app"),
    ("file://localhost/workspace/my%20notes", "/workspace/my notes"),
    ("file:///C:/Users/dev/agy-app", "C:/Users/dev/agy-app"),
    ("file:///", "/"),
    ("file://fileserver/share/agy-app", None),
    ("vscode-remote://ssh-remote+box/workspace", None),
    ("/workspace/dev/agy-app", None),
    ("file://[bad", None),
    ("", None),
    (None, None),
    (7, None),
])
def test_antigravity_file_uri_path(uri, expected):
    assert _antigravity_file_uri_path(uri) == expected
