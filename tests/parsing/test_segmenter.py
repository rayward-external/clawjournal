"""Tests for clawjournal.parsing.segmenter — OpenClaw session segmentation."""

import json
import tempfile

import pytest

from clawjournal.parsing.segmenter import (
    _build_exchanges,
    _classify_tool_mode,
    _detect_compaction_boundaries,
    _detect_time_gaps,
    _detect_tool_mode_shifts,
    _detect_workspace_switches,
    _enforce_minimum_segments,
    _extract_cd_target,
    _extract_segment_title,
    _parse_ts,
    _score_boundaries,
    _snap_to_user_messages,
    _split_session,
    _strip_openclaw_metadata,
    pre_scan_openclaw_hints,
    segment_append_only_session,
    segment_openclaw_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(role, content="", ts=None, tool_uses=None, **kwargs):
    """Build a test message dict."""
    m = {"role": role, "content": content}
    if ts:
        m["timestamp"] = ts
    if tool_uses:
        m["tool_uses"] = tool_uses
    m.update(kwargs)
    return m


def _user(content, ts=None):
    return _msg("user", content, ts)


def _assistant(content="", ts=None, tool_uses=None):
    return _msg("assistant", content, ts, tool_uses)


def _tool_use(tool, inp=None, status="success"):
    return {"tool": tool, "input": inp or {}, "output": {}, "status": status}


def _session(messages, session_id="test-session-001", source="openclaw"):
    return {
        "session_id": session_id,
        "model": "test-model",
        "source": source,
        "project": "openclaw:test",
        "git_branch": None,
        "start_time": messages[0].get("timestamp") if messages else None,
        "end_time": messages[-1].get("timestamp") if messages else None,
        "messages": messages,
        "stats": {
            "user_messages": sum(1 for m in messages if m.get("role") == "user"),
            "assistant_messages": sum(1 for m in messages if m.get("role") == "assistant"),
            "tool_uses": sum(len(m.get("tool_uses", [])) for m in messages),
            "input_tokens": 0,
            "output_tokens": 0,
        },
    }


class TestAppendOnlySegmentation:
    def test_eof_turn_remains_active_until_next_user_confirms_boundary(self):
        messages = [
            _user("question one"),
            _assistant("answer one"),
            _user("question two"),
            _assistant("answer two"),
        ]
        session = _session(messages, session_id="growing", source="codex")

        result = segment_append_only_session(
            session,
            [10, 20, 30, 40],
            message_start_offsets=[0, 10, 20, 30],
            raw_source_size=40,
            max_messages=4,
            max_user_messages=99,
            max_text_bytes=999_999,
            max_raw_bytes=999_999,
        )

        assert result == [session]

    def test_next_user_seals_prior_turn_and_leaves_tail_unranged(self):
        messages = [
            _user("question one"),
            _assistant("answer one"),
            _user("question two"),
            _assistant("answer two"),
            _user("question three"),
        ]
        session = _session(messages, session_id="growing", source="codex")

        result = segment_append_only_session(
            session,
            [10, 20, 30, 40, 55],
            message_start_offsets=[0, 10, 20, 30, 50],
            raw_source_size=55,
            max_messages=4,
            max_user_messages=99,
            max_text_bytes=999_999,
            max_raw_bytes=999_999,
        )

        assert [item["session_id"] for item in result] == [
            "growing",
            "growing_seg-0001",
        ]
        assert result[0]["segment_sealed"] is True
        assert result[0]["raw_source_end_offset"] == 50
        assert result[1]["segment_sealed"] is False
        assert result[1]["raw_source_start_offset"] is None
        assert result[1]["raw_source_end_offset"] is None


# ---------------------------------------------------------------------------
# Time gap detection
# ---------------------------------------------------------------------------

class TestDetectTimeGaps:
    def test_no_gap(self):
        msgs = [
            _user("hello", "2026-03-21T09:00:00Z"),
            _assistant("hi", "2026-03-21T09:01:00Z"),
            _user("question", "2026-03-21T09:05:00Z"),
        ]
        assert _detect_time_gaps(msgs, threshold_minutes=30) == []

    def test_gap_detected(self):
        msgs = [
            _user("hello", "2026-03-21T09:00:00Z"),
            _assistant("hi", "2026-03-21T09:01:00Z"),
            _user("back", "2026-03-21T10:00:00Z"),  # 59 min gap
        ]
        assert _detect_time_gaps(msgs, threshold_minutes=30) == [2]

    def test_custom_threshold(self):
        msgs = [
            _user("hello", "2026-03-21T09:00:00Z"),
            _user("back", "2026-03-21T09:20:00Z"),  # 20 min gap
        ]
        assert _detect_time_gaps(msgs, threshold_minutes=15) == [1]
        assert _detect_time_gaps(msgs, threshold_minutes=30) == []

    def test_skips_compaction_markers(self):
        msgs = [
            _user("hello", "2026-03-21T09:00:00Z"),
            _msg("system", "[compaction]", "2026-03-21T10:00:00Z", _compaction=True),
            _user("back", "2026-03-21T10:01:00Z"),
        ]
        # Gap is 61 min (09:00 → 10:01), compaction marker skipped
        assert _detect_time_gaps(msgs, threshold_minutes=30) == [2]


# ---------------------------------------------------------------------------
# Compaction boundary detection
# ---------------------------------------------------------------------------

class TestDetectCompactionBoundaries:
    def test_compaction_before_message(self):
        msgs = [
            _user("hello"),
            _assistant("hi"),
            _msg("system", "[compaction]", _compaction=True),
            _user("new topic"),
        ]
        assert _detect_compaction_boundaries(msgs) == [3]

    def test_no_compaction(self):
        msgs = [_user("hello"), _assistant("hi")]
        assert _detect_compaction_boundaries(msgs) == []

    def test_compaction_at_end_ignored(self):
        msgs = [
            _user("hello"),
            _msg("system", "[compaction]", _compaction=True),
        ]
        # No message after compaction → no boundary
        assert _detect_compaction_boundaries(msgs) == []


# ---------------------------------------------------------------------------
# Tool mode shift detection
# ---------------------------------------------------------------------------

class TestDetectToolModeShifts:
    def test_qa_to_heavy(self):
        msgs = [
            _user("what's the weather?"),
            _assistant("23°C"),
            _user("fix the tests"),
            _assistant("", tool_uses=[
                _tool_use("read"), _tool_use("edit"),
                _tool_use("bash"), _tool_use("bash"),
            ]),
        ]
        assert _detect_tool_mode_shifts(msgs) == [2]

    def test_heavy_to_qa(self):
        msgs = [
            _user("fix tests"),
            _assistant("", tool_uses=[
                _tool_use("read"), _tool_use("edit"),
                _tool_use("bash"), _tool_use("bash"),
            ]),
            _user("what's the weather?"),
            _assistant("23°C"),
        ]
        assert _detect_tool_mode_shifts(msgs) == [2]

    def test_no_shift_qa_to_qa(self):
        msgs = [
            _user("weather?"),
            _assistant("23°C"),
            _user("gold price?"),
            _assistant("$2847"),
        ]
        assert _detect_tool_mode_shifts(msgs) == []

    def test_no_shift_heavy_to_heavy(self):
        msgs = [
            _user("fix tests"),
            _assistant("", tool_uses=[_tool_use("bash")] * 5),
            _user("now fix lint"),
            _assistant("", tool_uses=[_tool_use("edit")] * 4),
        ]
        assert _detect_tool_mode_shifts(msgs) == []

    def test_light_mode_not_triggering(self):
        """Q&A → light (1-3 tools) should not trigger a boundary."""
        msgs = [
            _user("what file is this?"),
            _assistant(""),
            _user("read it"),
            _assistant("", tool_uses=[_tool_use("read")]),
        ]
        assert _detect_tool_mode_shifts(msgs) == []


# ---------------------------------------------------------------------------
# Workspace switch detection
# ---------------------------------------------------------------------------

class TestDetectWorkspaceSwitches:
    def test_cd_command(self):
        msgs = [
            _user("work on clawjournal"),
            _assistant("", tool_uses=[
                _tool_use("bash", {"command": "cd /Users/x/clawjournal && ls"}),
            ]),
            _user("now dataclaw"),
            _assistant("", tool_uses=[
                _tool_use("bash", {"command": "cd /Users/x/dataclaw && ls"}),
            ]),
        ]
        assert _detect_workspace_switches(msgs) == [3]

    def test_file_path_prefix_shift(self):
        msgs = [
            _user("read file"),
            _assistant("", tool_uses=[
                _tool_use("read", {"file_path": "/Users/x/clawjournal/src/main.py"}),
            ]),
            _user("read other file"),
            _assistant("", tool_uses=[
                _tool_use("read", {"file_path": "/Users/x/dataclaw/app.py"}),
            ]),
        ]
        assert _detect_workspace_switches(msgs) == [3]

    def test_no_switch_same_project(self):
        msgs = [
            _user("read file"),
            _assistant("", tool_uses=[
                _tool_use("bash", {"command": "cd /Users/x/clawjournal && ls"}),
            ]),
            _user("read other file"),
            _assistant("", tool_uses=[
                _tool_use("read", {"file_path": "/Users/x/clawjournal/tests/test.py"}),
            ]),
        ]
        assert _detect_workspace_switches(msgs) == []


# ---------------------------------------------------------------------------
# Boundary processing
# ---------------------------------------------------------------------------

class TestSnapToUserMessages:
    def test_snaps_assistant_to_preceding_user(self):
        msgs = [_user("a"), _assistant("b"), _user("c"), _assistant("d")]
        assert _snap_to_user_messages([3], msgs) == [2]

    def test_user_message_stays(self):
        msgs = [_user("a"), _assistant("b"), _user("c")]
        assert _snap_to_user_messages([2], msgs) == [2]

    def test_no_snap_to_index_zero(self):
        """Boundary at msg 0 should not create a split at the very start."""
        msgs = [_user("a"), _assistant("b")]
        assert _snap_to_user_messages([0], msgs) == []

    def test_drops_boundary_with_no_preceding_user(self):
        """When walk-back reaches 0 and it's not a user msg, drop the boundary."""
        msgs = [_assistant("system init"), _assistant("ready"), _user("hello")]
        assert _snap_to_user_messages([1], msgs) == []


class TestScoreBoundaries:
    def test_single_signal(self):
        scored = _score_boundaries({"time_gap": [5], "workspace": []})
        assert len(scored) == 1
        assert scored[0][0] == 5
        assert scored[0][1] == 0.9  # time_gap weight

    def test_two_signals_agree(self):
        scored = _score_boundaries({"time_gap": [5], "tool_mode": [5]})
        assert len(scored) == 1
        assert scored[0][1] == 1.0  # max(0.9, 0.5) + 0.1

    def test_nearby_signals_merge(self):
        """Signals within ±2 messages should count as agreeing."""
        scored = _score_boundaries({"time_gap": [5], "workspace": [6]})
        assert len(scored) == 2  # Both appear (at 5 and 6)
        # The one at 5 should have both signals (6 is within ±2)
        idx5 = [s for s in scored if s[0] == 5][0]
        assert idx5[1] == 1.0  # max(0.9, 0.6) + 0.1

    def test_three_signals(self):
        scored = _score_boundaries({
            "time_gap": [5], "workspace": [5], "tool_mode": [5],
        })
        assert scored[0][1] == 0.95


class TestEnforceMinimumSegments:
    def test_allows_normal_segments(self):
        msgs = [_user("a"), _assistant("b")] * 5  # 10 messages
        boundaries = [4, 8]
        result = _enforce_minimum_segments(boundaries, msgs)
        assert result == [4, 8]

    def test_allows_small_first_segment(self):
        msgs = [_user("a"), _assistant("b"), _user("c")] + [_assistant("d")] * 5
        boundaries = [2]
        result = _enforce_minimum_segments(boundaries, msgs)
        assert result == [2]

    def test_drops_tiny_middle_segment(self):
        # 10 messages: [u,a, u,a, u, u,a, u,a, u,a]
        msgs = [_user("a"), _assistant("b")] * 2 + [_user("tiny")] + [_user("c"), _assistant("d")] * 3
        # boundary at 4 creates [0-3](4 msgs) and [4-4](1 msg) and [5-end](6 msgs)
        boundaries = [4, 5]
        result = _enforce_minimum_segments(boundaries, msgs)
        # Middle segment [4-4] has 1 user msg and 1 total msg — too small, dropped
        # Last segment [5-end] is exempt (last segment exception)
        assert 4 not in result
        assert 5 in result


# ---------------------------------------------------------------------------
# Session splitting
# ---------------------------------------------------------------------------

class TestSplitSession:
    def test_basic_split(self):
        msgs = [
            _user("weather?", "2026-03-21T09:00:00Z"),
            _assistant("23°C", "2026-03-21T09:01:00Z"),
            _user("fix tests", "2026-03-21T10:00:00Z"),
            _assistant("done", "2026-03-21T10:05:00Z"),
        ]
        session = _session(msgs)
        children = _split_session(session, [2])

        assert len(children) == 2
        assert children[0]["session_id"] == "test-session-001_seg-00"
        assert children[1]["session_id"] == "test-session-001_seg-01"
        assert children[0]["parent_session_id"] == "test-session-001"
        assert children[0]["segment_index"] == 0
        assert children[1]["segment_index"] == 1
        assert len(children[0]["messages"]) == 2
        assert len(children[1]["messages"]) == 2
        assert children[0]["stats"]["user_messages"] == 1
        assert children[1]["stats"]["user_messages"] == 1

    def test_preserves_metadata(self):
        msgs = [_user("a"), _assistant("b"), _user("c"), _assistant("d")]
        session = _session(msgs)
        session["project"] = "openclaw:myproject"
        children = _split_session(session, [2])

        for child in children:
            assert child["source"] == "openclaw"
            assert child["project"] == "openclaw:myproject"
            assert child["model"] == "test-model"

    def test_filters_compaction_markers(self):
        msgs = [
            _user("hello"),
            _assistant("hi"),
            _msg("system", "[compaction]", _compaction=True),
            _user("new topic"),
            _assistant("ok"),
        ]
        session = _session(msgs)
        children = _split_session(session, [3])

        # Second child should not contain the compaction marker
        for child in children:
            for m in child["messages"]:
                assert not m.get("_compaction")

    def test_no_boundaries_returns_original(self):
        msgs = [_user("hello"), _assistant("world")]
        session = _session(msgs)
        result = _split_session(session, [])
        assert len(result) == 1
        assert result[0] is session


# ---------------------------------------------------------------------------
# Full segmentation pipeline
# ---------------------------------------------------------------------------

class TestSegmentOpenclawSession:
    def test_multi_task_session_splits(self):
        """The morning session example from the design doc."""
        msgs = [
            _user("What's the weather?", "2026-03-21T09:00:00Z"),
            _assistant("23°C", "2026-03-21T09:00:30Z"),
            _user("What's gold at?", "2026-03-21T09:02:00Z"),
            _assistant("$2847", "2026-03-21T09:02:30Z"),
            _user("Go to clawjournal, check failing tests", "2026-03-21T09:15:00Z"),
            _assistant("", "2026-03-21T09:15:30Z", tool_uses=[
                _tool_use("bash", {"command": "cd /home/user/clawjournal && pytest"}),
                _tool_use("read", {"file_path": "/home/user/clawjournal/tests/test_parser.py"}),
                _tool_use("edit", {"file_path": "/home/user/clawjournal/tests/test_parser.py"}),
                _tool_use("bash", {"command": "pytest"}),
            ]),
            _user("Fix them", "2026-03-21T09:16:00Z"),
            _assistant("All passing", "2026-03-21T09:16:30Z", tool_uses=[
                _tool_use("read"), _tool_use("edit"),
                _tool_use("bash"), _tool_use("bash"),
            ]),
            _user("Now go to dataclaw and install clawjournal", "2026-03-21T09:25:00Z"),
            _assistant("Installed", "2026-03-21T09:25:30Z", tool_uses=[
                _tool_use("bash", {"command": "cd /home/user/dataclaw && pip install clawjournal"}),
            ]),
            _user("What's the weather in Moscow?", "2026-03-21T10:00:00Z"),
            _assistant("-2°C, snow", "2026-03-21T10:00:30Z"),
        ]
        session = _session(msgs)
        children = segment_openclaw_session(session, threshold_minutes=30)

        # Should split into multiple segments
        assert len(children) >= 2
        # All children should have parent reference
        for child in children:
            assert child["parent_session_id"] == "test-session-001"
            assert "segment_index" in child
            assert "segment_reason" in child

    def test_single_task_session_not_split(self):
        """The onboarding example — one coherent task should stay intact."""
        msgs = [
            _user("Wake up!", "2026-03-21T01:18:00Z"),
            _assistant("Hey, who am I?", "2026-03-21T01:18:30Z"),
            _user("You're my assistant, very technical", "2026-03-21T01:22:00Z"),
            _assistant("", "2026-03-21T01:22:30Z", tool_uses=[
                _tool_use("edit", {"file_path": "/home/user/.openclaw/workspace/IDENTITY.md"}),
                _tool_use("edit", {"file_path": "/home/user/.openclaw/workspace/USER.md"}),
            ]),
            _user("Call yourself Bean", "2026-03-21T01:24:00Z"),
            _assistant("I'm Bean now", "2026-03-21T01:24:30Z", tool_uses=[
                _tool_use("edit", {"file_path": "/home/user/.openclaw/workspace/IDENTITY.md"}),
            ]),
            _user("Let's setup Telegram", "2026-03-21T01:25:00Z"),
            _assistant("", "2026-03-21T01:25:30Z", tool_uses=[
                _tool_use("read", {"file_path": "/home/user/.openclaw/workspace/docs/telegram.md"}),
                _tool_use("exec", {"command": "openclaw channels add --channel telegram"}),
            ]),
            _user("Here's the bot token", "2026-03-21T01:29:00Z"),
            _assistant("Connected!", "2026-03-21T01:29:30Z", tool_uses=[
                _tool_use("exec", {"command": "openclaw pairing approve telegram"}),
            ]),
        ]
        session = _session(msgs)
        children = segment_openclaw_session(session, threshold_minutes=30)

        # Should NOT split — one coherent onboarding task
        assert len(children) == 1

    def test_short_session_not_split(self):
        """Sessions with <4 messages should not be segmented."""
        msgs = [
            _user("hello"),
            _assistant("hi"),
            _user("bye"),
        ]
        session = _session(msgs)
        result = segment_openclaw_session(session)
        assert len(result) == 1

    def test_time_gap_split(self):
        """A session with a clear time gap should split there."""
        msgs = [
            _user("task 1", "2026-03-21T09:00:00Z"),
            _assistant("done 1", "2026-03-21T09:05:00Z"),
            _user("task 2", "2026-03-21T11:00:00Z"),  # 2 hour gap
            _assistant("done 2", "2026-03-21T11:05:00Z"),
        ]
        session = _session(msgs)
        children = segment_openclaw_session(session, threshold_minutes=30)

        assert len(children) == 2
        assert children[0]["messages"][0]["content"] == "task 1"
        assert children[1]["messages"][0]["content"] == "task 2"

    def test_compaction_split(self):
        """Compaction markers should trigger segmentation."""
        msgs = [
            _user("task 1", "2026-03-21T09:00:00Z"),
            _assistant("done 1", "2026-03-21T09:05:00Z"),
            _msg("system", "[compaction]", "2026-03-21T09:10:00Z",
                 _compaction=True, _compaction_summary="Discussed task 1"),
            _user("task 2", "2026-03-21T09:10:30Z"),
            _assistant("done 2", "2026-03-21T09:15:00Z"),
        ]
        session = _session(msgs)
        children = segment_openclaw_session(session)

        assert len(children) == 2


# ---------------------------------------------------------------------------
# Pre-scan hints
# ---------------------------------------------------------------------------

class TestPreScanOpenclawHints:
    def test_scans_compaction_and_model_changes(self):
        lines = [
            json.dumps({"type": "session", "id": "s1", "cwd": "/home/user/project"}),
            json.dumps({"type": "message", "message": {"role": "user", "content": "hi"}}),
            json.dumps({"type": "message", "message": {"role": "assistant", "content": "hello"}}),
            json.dumps({"type": "compaction", "summary": "Discussed greetings"}),
            json.dumps({"type": "model_change", "provider": "anthropic", "modelId": "opus"}),
            json.dumps({"type": "message", "message": {"role": "user", "content": "new task"}}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            path = f.name

        hints = pre_scan_openclaw_hints(path)

        assert hints["cwd_from_header"] == "/home/user/project"
        assert hints["compaction_indices"] == [2]  # After 2 messages
        assert hints["compaction_summaries"] == ["Discussed greetings"]
        assert len(hints["model_changes"]) == 1
        assert hints["model_changes"][0][1] == "anthropic"

    def test_handles_missing_file(self):
        hints = pre_scan_openclaw_hints("/nonexistent/path.jsonl")
        assert hints["compaction_indices"] == []
        assert hints["cwd_from_header"] is None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_extract_cd_target(self):
        assert _extract_cd_target("cd /home/user/project && ls") == "/home/user/project"
        assert _extract_cd_target("ls -la") is None
        assert _extract_cd_target("cd ~/dataclaw") == "~/dataclaw"

    def test_classify_tool_mode(self):
        assert _classify_tool_mode({"tool_count": 0}) == "qa"
        assert _classify_tool_mode({"tool_count": 2}) == "light"
        assert _classify_tool_mode({"tool_count": 5}) == "heavy"

    def test_strip_openclaw_metadata(self):
        content = 'Sender (untrusted): ```json\n{"label": "test"}\n```\n[Sat 2026-03-21 01:18 UTC] Hello world'
        assert _strip_openclaw_metadata(content) == "Hello world"

    def test_strip_metadata_no_wrapper(self):
        assert _strip_openclaw_metadata("just plain text") == "just plain text"

    def test_extract_segment_title(self):
        msgs = [_user("Fix the authentication bug"), _assistant("On it")]
        assert _extract_segment_title(msgs) == "Fix the authentication bug"

    def test_extract_segment_title_truncates(self):
        long_msg = "A" * 200
        msgs = [_user(long_msg)]
        title = _extract_segment_title(msgs)
        assert len(title) <= 83  # 77 + "..."

    def test_build_exchanges(self):
        msgs = [
            _user("q1"), _assistant("a1"),
            _user("q2"), _assistant("a2", tool_uses=[_tool_use("bash")]),
        ]
        exchanges = _build_exchanges(msgs)
        assert len(exchanges) == 2
        assert exchanges[0]["tool_count"] == 0
        assert exchanges[1]["tool_count"] == 1

    def test_parse_ts_z_suffix(self):
        dt = _parse_ts("2026-03-21T09:00:00Z")
        assert dt is not None
        assert dt.hour == 9

    def test_parse_ts_offset(self):
        dt = _parse_ts("2026-03-21T09:00:00+00:00")
        assert dt is not None
        assert dt.hour == 9

    def test_parse_ts_fractional(self):
        dt = _parse_ts("2026-03-21T09:00:00.123Z")
        assert dt is not None

    def test_parse_ts_epoch_ms(self):
        dt = _parse_ts(1711004400000)  # epoch ms
        assert dt is not None

    def test_parse_ts_none(self):
        assert _parse_ts(None) is None
        assert _parse_ts("not-a-date") is None
