import json
from unittest.mock import patch

import pytest

from clawjournal.capture import discovery
from clawjournal.parsing import parser


@pytest.fixture
def isolated_homedir(tmp_path, monkeypatch):
    """Monkeypatch every parser path the capture adapter looks at so a test
    exercising Claude doesn't pick up the developer's real Codex history.
    Tests populate whichever subdirectory they care about."""
    monkeypatch.setattr(parser, "PROJECTS_DIR", tmp_path / "claude" / "projects")
    monkeypatch.setattr(parser, "CODEX_SESSIONS_DIR", tmp_path / "codex" / "sessions")
    monkeypatch.setattr(
        parser, "CODEX_ARCHIVED_DIR", tmp_path / "codex" / "archived_sessions"
    )
    monkeypatch.setattr(parser, "LOCAL_AGENT_DIR", tmp_path / "local_agent")
    monkeypatch.setattr(parser, "OPENCLAW_AGENTS_DIR", tmp_path / "openclaw" / "agents")
    return tmp_path


def _write_codex_session(path, cwd):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n"
    )


def _write_local_agent_wrapper(
    workspace_dir,
    name,
    *,
    cli_session_id,
    session_id,
    process_name,
    user_folder=None,
):
    wrapper = workspace_dir / f"local_{name}.json"
    payload = {
        "cliSessionId": cli_session_id,
        "sessionId": session_id,
        "processName": process_name,
    }
    if user_folder is not None:
        payload["userSelectedFolders"] = [user_folder]
    wrapper.write_text(json.dumps(payload))
    session_dir = wrapper.with_suffix("")
    session_dir.mkdir()
    return wrapper, session_dir


def _make_workspace_dirs(isolated_homedir):
    root_uuid = isolated_homedir / "local_agent" / "11111111-2222-3333-4444-555555555555"
    workspace_uuid = root_uuid / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    workspace_uuid.mkdir(parents=True)
    return workspace_uuid


# ---------- Claude native ----------


def test_claude_native_discovery_yields_root_jsonls_and_subagent_only_sessions(
    isolated_homedir,
):
    """Root `<uuid>.jsonl` files are always yielded. Subagent streams are
    yielded only for UUID-named dirs that have NO sibling `<uuid>.jsonl`
    — mirrors parser._find_subagent_only_sessions. Rooted sessions'
    subagents are consumed as part of the root transcript, so the
    adapter must skip them to avoid step-2 double-ingestion."""
    proj = isolated_homedir / "claude" / "projects" / "myproject"
    proj.mkdir(parents=True)
    # Rooted session: root jsonl plus subagents — only the root file is
    # yielded, because the parser treats the subagents as part of the root.
    (proj / "rooted.jsonl").write_text("{}\n")
    rooted_sub = proj / "rooted" / "subagents"
    rooted_sub.mkdir(parents=True)
    (rooted_sub / "agent-r.jsonl").write_text("{}\n")  # skipped
    # Subagent-only session: no root jsonl; its subagents ARE yielded.
    sub_only = proj / "subagent-only-uuid" / "subagents"
    sub_only.mkdir(parents=True)
    (sub_only / "agent-s.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    names = sorted(f.path.name for f in files)
    assert names == ["agent-s.jsonl", "rooted.jsonl"]
    assert all(f.client == "claude" for f in files)
    assert all(f.project_dir_name == "myproject" for f in files)


def test_claude_native_subagent_of_rooted_session_is_skipped(isolated_homedir):
    proj = isolated_homedir / "claude" / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "abc.jsonl").write_text("{}\n")
    subs = proj / "abc" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-1.jsonl").write_text("{}\n")
    (subs / "agent-2.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    names = sorted(f.path.name for f in files)
    assert names == ["abc.jsonl"]  # no agent-*.jsonl


def test_claude_native_subagent_only_session_is_included(isolated_homedir):
    proj = isolated_homedir / "claude" / "projects" / "p"
    proj.mkdir(parents=True)
    subs = proj / "only-uuid" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-1.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    names = sorted(f.path.name for f in files)
    assert names == ["agent-1.jsonl"]


# ---------- Claude Desktop local-agent (step 1b) ----------


def test_local_agent_yields_only_cli_session_id_transcript(isolated_homedir):
    """parser.py:862 reads only `{nested_project_dir}/{cli_session_id}.jsonl`
    per wrapper. Other `.jsonl` files in the same dir, subagent streams,
    and audit.jsonl are deliberately skipped so step-2 Scanner parity
    holds."""
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "abc",
        cli_session_id="cli-42",
        session_id="sess-42",
        process_name="myproc",
        user_folder="/Users/me/ws-one",
    )
    proj = session_dir / ".claude" / "projects" / "-sessions-myproc"
    proj.mkdir(parents=True)
    (proj / "cli-42.jsonl").write_text("{}\n")          # matches cliSessionId
    (proj / "stale-session.jsonl").write_text("{}\n")   # parser ignores
    subagents = proj / "cli-42" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-1.jsonl").write_text("{}\n")    # parser ignores
    (session_dir / "audit.jsonl").write_text("{}\n")    # metadata, not a transcript

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert [f.path.name for f in files] == ["cli-42.jsonl"]
    assert files[0].project_dir_name == "-Users-me-ws-one"
    assert files[0].client == "claude"


def test_local_agent_falls_back_to_cowork_key_without_user_folder(isolated_homedir):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "def",
        cli_session_id="cli-2",
        session_id="sess-2",
        process_name="otherproc",
    )
    proj = session_dir / ".claude" / "projects" / "-sessions-otherproc"
    proj.mkdir(parents=True)
    (proj / "cli-2.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert len(files) == 1
    assert files[0].project_dir_name == "_cowork_sess-2"
    assert files[0].path.name == "cli-2.jsonl"


def test_local_agent_skips_workspace_without_nested_project_dir(isolated_homedir):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "noproj",
        cli_session_id="cli-nodir",
        session_id="sess-nodir",
        process_name="x",
    )
    (session_dir / "audit.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert files == []


def test_local_agent_skips_when_cli_session_transcript_missing(isolated_homedir):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "nomatch",
        cli_session_id="cli-expected",
        session_id="sess-nomatch",
        process_name="myproc",
        user_folder="/Users/me/ws",
    )
    proj = session_dir / ".claude" / "projects" / "-sessions-myproc"
    proj.mkdir(parents=True)
    (proj / "cli-different.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert files == []


def test_local_agent_skips_non_uuid_directories(isolated_homedir):
    root = isolated_homedir / "local_agent"
    root.mkdir()
    (root / "not-a-uuid").mkdir()
    (root / "not-a-uuid" / "stray.json").write_text("{}")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert files == []


def test_local_agent_missing_directory_is_a_no_op(isolated_homedir):
    files = list(discovery.iter_source_files(source_filter="claude"))
    assert files == []


# ---------- wrapper validity gates (mirror parser.py:220-227) ----------


def test_local_agent_skips_wrapper_with_malformed_json(isolated_homedir):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    wrapper = workspace_uuid / "local_bad.json"
    wrapper.write_text("{not json")
    session_dir = wrapper.with_suffix("")
    session_dir.mkdir()
    proj_dir = session_dir / ".claude" / "projects" / "-sessions-x"
    proj_dir.mkdir(parents=True)
    (proj_dir / "anything.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert files == []


def test_local_agent_skips_wrapper_that_is_not_a_dict(isolated_homedir):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    wrapper = workspace_uuid / "local_list.json"
    wrapper.write_text(json.dumps([1, 2, 3]))
    session_dir = wrapper.with_suffix("")
    session_dir.mkdir()
    proj_dir = session_dir / ".claude" / "projects" / "-sessions-x"
    proj_dir.mkdir(parents=True)
    (proj_dir / "anything.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert files == []


def test_local_agent_skips_wrapper_without_cli_session_id(isolated_homedir):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    wrapper = workspace_uuid / "local_missing.json"
    wrapper.write_text(json.dumps({"sessionId": "sess-3", "processName": "x"}))
    session_dir = wrapper.with_suffix("")
    session_dir.mkdir()
    proj_dir = session_dir / ".claude" / "projects" / "-sessions-x"
    proj_dir.mkdir(parents=True)
    (proj_dir / "anything.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert files == []


# ---------- nested project dir selection (mirror parser.py:247-253) ----------


def test_local_agent_prefers_sessions_processname_dir_over_others(isolated_homedir):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "pref",
        cli_session_id="cli-5",
        session_id="sess-5",
        process_name="myproc",
        user_folder="/Users/me/ws",
    )
    expected = session_dir / ".claude" / "projects" / "-sessions-myproc"
    expected.mkdir(parents=True)
    (expected / "cli-5.jsonl").write_text("{}\n")
    stray = session_dir / ".claude" / "projects" / "stray"
    stray.mkdir(parents=True)
    (stray / "cli-5.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert len(files) == 1
    assert files[0].path == expected / "cli-5.jsonl"


def test_local_agent_falls_back_to_a_single_nested_dir_when_processname_missing(
    isolated_homedir,
):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "fb",
        cli_session_id="cli-6",
        session_id="sess-6",
        process_name="unexpected",
        user_folder="/Users/me/ws",
    )
    alt = session_dir / ".claude" / "projects" / "-sessions-fallback"
    alt.mkdir(parents=True)
    (alt / "cli-6.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    assert len(files) == 1
    assert files[0].path == alt / "cli-6.jsonl"


def test_local_agent_fallback_uses_raw_iterdir_order_when_processname_missing(
    isolated_homedir,
):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "raworder",
        cli_session_id="cli-raw",
        session_id="sess-raw",
        process_name="unexpected",
        user_folder="/Users/me/ws",
    )
    nested_root = session_dir / ".claude" / "projects"
    alt = nested_root / "-sessions-fallback"
    alt.mkdir(parents=True)
    (alt / "cli-raw.jsonl").write_text("{}\n")
    extra = nested_root / "-sessions-other"
    extra.mkdir(parents=True)
    (extra / "cli-raw.jsonl").write_text("{}\n")

    original_iterdir = discovery.Path.iterdir

    def fake_iterdir(self):
        if self == nested_root:
            return iter([extra, alt])
        return original_iterdir(self)

    with patch.object(discovery.Path, "iterdir", fake_iterdir):
        files = list(discovery.iter_source_files(source_filter="claude"))

    assert len(files) == 1
    assert files[0].path == extra / "cli-raw.jsonl"


# ---------- duplicate preservation / parser-level fallback ----------


def test_local_agent_matching_native_session_is_still_surfaced(isolated_homedir):
    """Discovery must preserve both physical sources when native and
    local-agent layouts share a session UUID.

    The current parser only suppresses the local-agent copy after the
    native transcript actually parses. If discovery dropped the LA file
    based on filenames alone, an empty or malformed native transcript
    would block the LA fallback entirely.
    """
    native = isolated_homedir / "claude" / "projects" / "-Users-me-shared"
    native.mkdir(parents=True)
    (native / "dup.jsonl").write_text("")

    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "dup",
        cli_session_id="dup",
        session_id="sess-dup",
        process_name="proc",
        user_folder="/Users/me/shared",
    )
    proj = session_dir / ".claude" / "projects" / "-sessions-proc"
    proj.mkdir(parents=True)
    (proj / "dup.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    paths = sorted(str(f.path.relative_to(isolated_homedir)) for f in files)
    assert paths == [
        "claude/projects/-Users-me-shared/dup.jsonl",
        "local_agent/11111111-2222-3333-4444-555555555555/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/local_dup/.claude/projects/-sessions-proc/dup.jsonl",
    ]


def test_local_agent_included_when_session_id_differs_from_native(isolated_homedir):
    native = isolated_homedir / "claude" / "projects" / "-Users-me-shared"
    native.mkdir(parents=True)
    (native / "native-only.jsonl").write_text("{}\n")

    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "la",
        cli_session_id="la-only",  # different UUID
        session_id="sess-la",
        process_name="proc",
        user_folder="/Users/me/shared",
    )
    proj = session_dir / ".claude" / "projects" / "-sessions-proc"
    proj.mkdir(parents=True)
    (proj / "la-only.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    names = sorted(f.path.name for f in files)
    assert names == ["la-only.jsonl", "native-only.jsonl"]


def test_duplicate_local_agent_wrappers_same_cli_session_id_are_both_surfaced(
    isolated_homedir,
):
    """Capture stays at the physical source-file layer.

    Multiple wrappers can point at the same logical session UUID. The
    parser handles session-level dedupe and fallback; discovery should
    surface both raw transcripts rather than picking one prematurely.
    """
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_one = _write_local_agent_wrapper(
        workspace_uuid,
        "one",
        cli_session_id="dup-cli",
        session_id="sess-one",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_one = session_one / ".claude" / "projects" / "-sessions-proc"
    proj_one.mkdir(parents=True)
    (proj_one / "dup-cli.jsonl").write_text("{}\n")

    _, session_two = _write_local_agent_wrapper(
        workspace_uuid,
        "two",
        cli_session_id="dup-cli",
        session_id="sess-two",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_two = session_two / ".claude" / "projects" / "-sessions-proc"
    proj_two.mkdir(parents=True)
    (proj_two / "dup-cli.jsonl").write_text("{}\n")

    files = list(discovery.iter_source_files(source_filter="claude"))
    paths = sorted(str(f.path.relative_to(isolated_homedir)) for f in files)
    assert paths == [
        "local_agent/11111111-2222-3333-4444-555555555555/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/local_one/.claude/projects/-sessions-proc/dup-cli.jsonl",
        "local_agent/11111111-2222-3333-4444-555555555555/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/local_two/.claude/projects/-sessions-proc/dup-cli.jsonl",
    ]


# ---------- parser-facing logical inputs ----------


def test_parse_inputs_collapse_native_subagent_only_session(isolated_homedir):
    proj = isolated_homedir / "claude" / "projects" / "p"
    proj.mkdir(parents=True)
    session_dir = proj / "only-uuid"
    subagents = session_dir / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-1.jsonl").write_text("{}\n")
    (subagents / "agent-2.jsonl").write_text("{}\n")

    inputs = list(discovery.iter_parse_inputs(source_filter="claude"))
    assert len(inputs) == 1
    parse_input = inputs[0]
    assert parse_input.session_key == "claude:p:only-uuid"
    assert parse_input.parse_kind == "claude_subagent_session"
    assert parse_input.priority == 0
    assert parse_input.parse_path == session_dir
    assert tuple(path.name for path in parse_input.source_paths) == (
        "agent-1.jsonl",
        "agent-2.jsonl",
    )


def test_parse_inputs_encode_native_then_local_agent_fallback_order(
    isolated_homedir,
):
    native = isolated_homedir / "claude" / "projects" / "-Users-me-shared"
    native.mkdir(parents=True)
    (native / "dup.jsonl").write_text("")

    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "dup",
        cli_session_id="dup",
        session_id="sess-dup",
        process_name="proc",
        user_folder="/Users/me/shared",
    )
    proj = session_dir / ".claude" / "projects" / "-sessions-proc"
    proj.mkdir(parents=True)
    (proj / "dup.jsonl").write_text("{}\n")

    inputs = list(discovery.iter_parse_inputs(source_filter="claude"))
    assert len(inputs) == 2
    assert [parse_input.priority for parse_input in inputs] == [0, 1]
    assert inputs[0].session_key == inputs[1].session_key == "claude:-Users-me-shared:dup"
    assert inputs[0].parse_path == native / "dup.jsonl"
    assert inputs[1].parse_path == proj / "dup.jsonl"


def test_parse_inputs_expand_changed_local_agent_duplicate_to_full_fallback_set(
    isolated_homedir,
):
    native = isolated_homedir / "claude" / "projects" / "-Users-me-shared"
    native.mkdir(parents=True)
    (native / "dup.jsonl").write_text("{}\n")

    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_dir = _write_local_agent_wrapper(
        workspace_uuid,
        "dup",
        cli_session_id="dup",
        session_id="sess-dup",
        process_name="proc",
        user_folder="/Users/me/shared",
    )
    proj = session_dir / ".claude" / "projects" / "-sessions-proc"
    proj.mkdir(parents=True)
    la_file = proj / "dup.jsonl"
    la_file.write_text("{}\n")

    all_files = list(discovery.iter_source_files(source_filter="claude"))
    changed_only = [source for source in all_files if source.path == la_file]

    inputs = list(discovery.iter_parse_inputs(source_files=changed_only))
    assert len(inputs) == 2
    assert [parse_input.priority for parse_input in inputs] == [0, 1]
    assert inputs[0].parse_path == native / "dup.jsonl"
    assert inputs[1].parse_path == la_file


def test_parse_inputs_expand_changed_subagent_file_to_full_session(
    isolated_homedir,
):
    proj = isolated_homedir / "claude" / "projects" / "p"
    proj.mkdir(parents=True)
    session_dir = proj / "only-uuid"
    subagents = session_dir / "subagents"
    subagents.mkdir(parents=True)
    agent_one = subagents / "agent-1.jsonl"
    agent_two = subagents / "agent-2.jsonl"
    agent_one.write_text("{}\n")
    agent_two.write_text("{}\n")

    all_files = list(discovery.iter_source_files(source_filter="claude"))
    changed_only = [source for source in all_files if source.path == agent_one]

    inputs = list(discovery.iter_parse_inputs(source_files=changed_only))
    assert len(inputs) == 1
    assert inputs[0].parse_path == session_dir
    assert tuple(path.name for path in inputs[0].source_paths) == (
        "agent-1.jsonl",
        "agent-2.jsonl",
    )


def test_parse_inputs_preserve_duplicate_local_agent_candidates_stably(
    isolated_homedir,
):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_one = _write_local_agent_wrapper(
        workspace_uuid,
        "one",
        cli_session_id="dup-cli",
        session_id="sess-one",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_one = session_one / ".claude" / "projects" / "-sessions-proc"
    proj_one.mkdir(parents=True)
    (proj_one / "dup-cli.jsonl").write_text("{}\n")

    _, session_two = _write_local_agent_wrapper(
        workspace_uuid,
        "two",
        cli_session_id="dup-cli",
        session_id="sess-two",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_two = session_two / ".claude" / "projects" / "-sessions-proc"
    proj_two.mkdir(parents=True)
    (proj_two / "dup-cli.jsonl").write_text("{}\n")

    inputs = list(discovery.iter_parse_inputs(source_filter="claude"))
    assert len(inputs) == 2
    assert inputs[0].session_key == inputs[1].session_key == "claude:-Users-me-ws:dup-cli"
    assert [parse_input.priority for parse_input in inputs] == [1, 1]
    # Both physical candidates are surfaced. Filesystem iterdir order is
    # OS-dependent — macOS happens to return entries alphabetically, but
    # Linux ext4 uses hash order. That's deliberate parser parity; the
    # sibling test_..._preserve_raw_wrapper_order_... pins ordering via a
    # patched iterdir. Here we only check presence.
    assert {parse_input.parse_path for parse_input in inputs} == {
        proj_one / "dup-cli.jsonl",
        proj_two / "dup-cli.jsonl",
    }
    assert all(len(parse_input.source_paths) == 1 for parse_input in inputs)


def test_parse_inputs_preserve_raw_wrapper_order_for_duplicate_local_agent_candidates(
    isolated_homedir,
):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    wrapper_one, session_one = _write_local_agent_wrapper(
        workspace_uuid,
        "one",
        cli_session_id="dup-cli",
        session_id="sess-one",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_one = session_one / ".claude" / "projects" / "-sessions-proc"
    proj_one.mkdir(parents=True)
    file_one = proj_one / "dup-cli.jsonl"
    file_one.write_text("{}\n")

    wrapper_two, session_two = _write_local_agent_wrapper(
        workspace_uuid,
        "two",
        cli_session_id="dup-cli",
        session_id="sess-two",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_two = session_two / ".claude" / "projects" / "-sessions-proc"
    proj_two.mkdir(parents=True)
    file_two = proj_two / "dup-cli.jsonl"
    file_two.write_text("{}\n")

    original_iterdir = discovery.Path.iterdir

    def fake_iterdir(self):
        if self == workspace_uuid:
            return iter([wrapper_two, session_two, wrapper_one, session_one])
        return original_iterdir(self)

    with patch.object(discovery.Path, "iterdir", fake_iterdir):
        inputs = list(discovery.iter_parse_inputs(source_filter="claude"))

    assert len(inputs) == 2
    assert [parse_input.priority for parse_input in inputs] == [1, 1]
    assert [parse_input.parse_path for parse_input in inputs] == [file_two, file_one]


def test_parse_inputs_preserve_raw_wrapper_order_for_distinct_local_agent_sessions(
    isolated_homedir,
):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    wrapper_a, session_a = _write_local_agent_wrapper(
        workspace_uuid,
        "a",
        cli_session_id="a",
        session_id="sess-a",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_a = session_a / ".claude" / "projects" / "-sessions-proc"
    proj_a.mkdir(parents=True)
    file_a = proj_a / "a.jsonl"
    file_a.write_text("{}\n")

    wrapper_z, session_z = _write_local_agent_wrapper(
        workspace_uuid,
        "z",
        cli_session_id="z",
        session_id="sess-z",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_z = session_z / ".claude" / "projects" / "-sessions-proc"
    proj_z.mkdir(parents=True)
    file_z = proj_z / "z.jsonl"
    file_z.write_text("{}\n")

    original_iterdir = discovery.Path.iterdir

    def fake_iterdir(self):
        if self == workspace_uuid:
            return iter([wrapper_z, session_z, wrapper_a, session_a])
        return original_iterdir(self)

    with patch.object(discovery.Path, "iterdir", fake_iterdir):
        inputs = list(discovery.iter_parse_inputs(source_filter="claude"))

    assert len(inputs) == 2
    assert [parse_input.priority for parse_input in inputs] == [1, 1]
    assert [parse_input.parse_path for parse_input in inputs] == [file_z, file_a]


def test_parse_inputs_expand_changed_local_agent_candidate_to_all_duplicate_wrappers(
    isolated_homedir,
):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    _, session_one = _write_local_agent_wrapper(
        workspace_uuid,
        "one",
        cli_session_id="dup-cli",
        session_id="sess-one",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_one = session_one / ".claude" / "projects" / "-sessions-proc"
    proj_one.mkdir(parents=True)
    file_one = proj_one / "dup-cli.jsonl"
    file_one.write_text("{}\n")

    _, session_two = _write_local_agent_wrapper(
        workspace_uuid,
        "two",
        cli_session_id="dup-cli",
        session_id="sess-two",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_two = session_two / ".claude" / "projects" / "-sessions-proc"
    proj_two.mkdir(parents=True)
    file_two = proj_two / "dup-cli.jsonl"
    file_two.write_text("{}\n")

    all_files = list(discovery.iter_source_files(source_filter="claude"))
    changed_only = [source for source in all_files if source.path == file_one]

    inputs = list(discovery.iter_parse_inputs(source_files=changed_only))
    assert len(inputs) == 2
    assert [parse_input.priority for parse_input in inputs] == [1, 1]
    assert {parse_input.parse_path for parse_input in inputs} == {file_one, file_two}


def test_parse_inputs_expand_changed_duplicate_local_agent_in_raw_wrapper_order(
    isolated_homedir,
):
    workspace_uuid = _make_workspace_dirs(isolated_homedir)
    wrapper_one, session_one = _write_local_agent_wrapper(
        workspace_uuid,
        "one",
        cli_session_id="dup-cli",
        session_id="sess-one",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_one = session_one / ".claude" / "projects" / "-sessions-proc"
    proj_one.mkdir(parents=True)
    file_one = proj_one / "dup-cli.jsonl"
    file_one.write_text("{}\n")

    wrapper_two, session_two = _write_local_agent_wrapper(
        workspace_uuid,
        "two",
        cli_session_id="dup-cli",
        session_id="sess-two",
        process_name="proc",
        user_folder="/Users/me/ws",
    )
    proj_two = session_two / ".claude" / "projects" / "-sessions-proc"
    proj_two.mkdir(parents=True)
    file_two = proj_two / "dup-cli.jsonl"
    file_two.write_text("{}\n")

    original_iterdir = discovery.Path.iterdir

    def fake_iterdir(self):
        if self == workspace_uuid:
            return iter([wrapper_two, session_two, wrapper_one, session_one])
        return original_iterdir(self)

    with patch.object(discovery.Path, "iterdir", fake_iterdir):
        all_files = list(discovery.iter_source_files(source_filter="claude"))
        changed_only = [source for source in all_files if source.path == file_one]
        inputs = list(discovery.iter_parse_inputs(source_files=changed_only))

    assert len(inputs) == 2
    assert [parse_input.priority for parse_input in inputs] == [1, 1]
    assert [parse_input.parse_path for parse_input in inputs] == [file_two, file_one]


# ---------- mixed-source ordering ----------


def test_parse_inputs_preserve_mixed_source_order_when_expanding_claude_families(
    isolated_homedir,
):
    native = isolated_homedir / "claude" / "projects" / "p"
    native.mkdir(parents=True)
    claude_file = native / "c.jsonl"
    claude_file.write_text("{}\n")

    codex = isolated_homedir / "codex" / "sessions" / "2026" / "04" / "19"
    _write_codex_session(codex / "rollout-k.jsonl", "/cwd")

    files = list(discovery.iter_source_files())
    assert [(source.client, source.path.name) for source in files] == [
        ("claude", "c.jsonl"),
        ("codex", "rollout-k.jsonl"),
    ]

    inputs = list(discovery.iter_parse_inputs(source_files=files))
    assert [(parse_input.client, parse_input.parse_path.name) for parse_input in inputs] == [
        ("claude", "c.jsonl"),
        ("codex", "rollout-k.jsonl"),
    ]


# ---------- Codex ----------


def test_codex_discovery_uses_extracted_cwd(isolated_homedir):
    sessions = isolated_homedir / "codex" / "sessions" / "2026" / "04" / "19"
    _write_codex_session(sessions / "rollout-a.jsonl", "/Users/me/proj-active")
    archived = isolated_homedir / "codex" / "archived_sessions"
    _write_codex_session(archived / "rollout-old.jsonl", "/Users/me/proj-old")

    files = list(discovery.iter_source_files(source_filter="codex"))
    by_name = {f.path.name: f for f in files}
    assert set(by_name) == {"rollout-a.jsonl", "rollout-old.jsonl"}
    assert by_name["rollout-a.jsonl"].project_dir_name == "/Users/me/proj-active"
    assert by_name["rollout-old.jsonl"].project_dir_name == "/Users/me/proj-old"
    assert all(f.client == "codex" for f in files)


def test_codex_discovery_falls_back_to_unknown_cwd_when_missing_metadata(
    isolated_homedir,
):
    archived = isolated_homedir / "codex" / "archived_sessions"
    archived.mkdir(parents=True)
    (archived / "rollout-nometa.jsonl").write_text(
        json.dumps({"type": "turn_start"}) + "\n"
    )

    files = list(discovery.iter_source_files(source_filter="codex"))
    assert len(files) == 1
    assert files[0].project_dir_name == parser.UNKNOWN_CODEX_CWD


# ---------- OpenClaw ----------


def _write_openclaw_session(path, cwd):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session", "cwd": cwd}) + "\n")


def test_openclaw_discovery_groups_by_header_cwd(isolated_homedir):
    agents = isolated_homedir / "openclaw" / "agents"
    _write_openclaw_session(agents / "coder" / "sessions" / "s1.jsonl", "/Users/me/projA")
    _write_openclaw_session(agents / "coder" / "sessions" / "s2.jsonl", "/Users/me/projB")
    _write_openclaw_session(agents / "reviewer" / "sessions" / "s3.jsonl", "/Users/me/projA")

    files = list(discovery.iter_source_files(source_filter="openclaw"))
    by_name = {f.path.name: f for f in files}
    assert set(by_name) == {"s1.jsonl", "s2.jsonl", "s3.jsonl"}
    assert by_name["s1.jsonl"].project_dir_name == "/Users/me/projA"
    assert by_name["s2.jsonl"].project_dir_name == "/Users/me/projB"
    assert by_name["s3.jsonl"].project_dir_name == "/Users/me/projA"
    assert all(f.client == "openclaw" for f in files)


def test_openclaw_discovery_falls_back_to_unknown_cwd(isolated_homedir):
    """Files without a first-line `type: session` header, or with no cwd,
    group under UNKNOWN_OPENCLAW_CWD — mirrors parser._extract_openclaw_cwd."""
    sessions = isolated_homedir / "openclaw" / "agents" / "a" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "no-header.jsonl").write_text(json.dumps({"type": "message"}) + "\n")
    (sessions / "empty.jsonl").write_text("")
    (sessions / "bad-json.jsonl").write_text("{not valid json\n")

    files = list(discovery.iter_source_files(source_filter="openclaw"))
    assert {f.path.name for f in files} == {
        "no-header.jsonl",
        "empty.jsonl",
        "bad-json.jsonl",
    }
    assert all(f.project_dir_name == parser.UNKNOWN_OPENCLAW_CWD for f in files)


def test_openclaw_discovery_skips_agent_dirs_without_sessions_subdir(isolated_homedir):
    agents = isolated_homedir / "openclaw" / "agents"
    # Agent dir with the required sessions/ subdir.
    _write_openclaw_session(agents / "with" / "sessions" / "s.jsonl", "/cwd")
    # Sibling agent dir missing the sessions/ subdir — must be ignored.
    (agents / "without").mkdir(parents=True)
    (agents / "without" / "notes.txt").write_text("stray file")

    files = list(discovery.iter_source_files(source_filter="openclaw"))
    assert [f.path.name for f in files] == ["s.jsonl"]


# ---------- auto and unknown filters ----------


def test_auto_filter_yields_all_supported_clients(isolated_homedir):
    native_proj = isolated_homedir / "claude" / "projects" / "p1"
    native_proj.mkdir(parents=True)
    (native_proj / "s.jsonl").write_text("{}\n")
    codex = isolated_homedir / "codex" / "sessions"
    _write_codex_session(codex / "r.jsonl", "/cwd")
    _write_openclaw_session(
        isolated_homedir / "openclaw" / "agents" / "a" / "sessions" / "oc.jsonl",
        "/cwd",
    )

    files = list(discovery.iter_source_files())
    clients = {f.client for f in files}
    assert clients == {"claude", "codex", "openclaw"}


def test_unknown_source_returns_nothing(isolated_homedir):
    files = list(discovery.iter_source_files(source_filter="gemini"))
    assert files == []


def test_size_bytes_matches_file_size(isolated_homedir):
    proj = isolated_homedir / "claude" / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text("{}\n")
    files = list(discovery.iter_source_files(source_filter="claude"))
    assert all(f.size_bytes == 3 for f in files)
