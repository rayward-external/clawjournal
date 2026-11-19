"""Project exclusions at the public CLI boundary, using a synthetic Antigravity home."""

import json
import os
from pathlib import Path
import subprocess
import sys

from parsing.test_antigravity import (
    _hello,
    _summary,
    _system_message,
    _write_summaries,
    _write_transcript,
)

PUBLIC = "11111111-0000-4000-8000-000000000001"
MIXED = "22222222-0000-4000-8000-000000000002"
PARENT = "33333333-0000-4000-8000-000000000003"
CHILD = "44444444-0000-4000-8000-000000000004"


def _cli(home, *args, check=True):
    return subprocess.run(
        [sys.executable, "-m", "clawjournal.cli", *args],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CLAWJOURNAL_HOME": str(home / "state"),
            "CLAWJOURNAL_NO_AUTO_UPDATE": "1",
        },
        capture_output=True,
        text=True,
        check=check,
    )


def test_cli_exports_only_sessions_outside_excluded_workspaces(tmp_path):
    agy = tmp_path / ".gemini" / "antigravity-cli"
    for session_id in (PUBLIC, MIXED, PARENT):
        _hello(agy, session_id)
    _write_transcript(agy, [
        _system_message(0, f"[Message] timestamp=2026-11-07T15:53:32Z sender={PARENT}\nCheck the build."),
    ], CHILD)
    _write_summaries(agy, [
        _summary(PUBLIC, cwd="/src/public"),
        _summary(MIXED, workspace_uris=["file:///src/public", "file:///src/private"]),
        _summary(PARENT, cwd="/src/private"),
    ])
    _cli(tmp_path, "config", "--source", "antigravity", "--exclude", "antigravity:private")

    projects = json.loads(_cli(tmp_path, "list").stdout)
    public = next(row for row in projects if row["name"] == "antigravity:public")
    assert public["additional_projects"] == [{"name": "antigravity:private", "excluded": True}]

    output = tmp_path / "export.jsonl"
    _cli(tmp_path, "export", "--output", str(output))
    exported = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["session_id"] for row in exported] == [PUBLIC]
    assert "project_memberships" not in exported[0]

    markdown = tmp_path / "markdown"
    _cli(tmp_path, "export", "--format", "md", "--output", str(markdown))
    assert {f.name for f in markdown.glob("*.md")} == {"README.md", f"{PUBLIC}.md"}

    _cli(tmp_path, "export", "--all-projects", "--output", str(output))
    all_ids = [json.loads(line)["session_id"] for line in output.read_text().splitlines()]
    assert sorted(all_ids) == sorted([PUBLIC, MIXED, PARENT, CHILD])


def test_cli_does_not_export_reclassified_sessions_during_summaries_failure(tmp_path):
    agy = tmp_path / ".gemini" / "antigravity-cli"
    _hello(agy, PUBLIC)
    _hello(agy, PARENT)
    _write_summaries(agy, [_summary(PUBLIC, cwd="/src/public"), _summary(PARENT, cwd="/src/private")])
    _cli(tmp_path, "config", "--source", "antigravity", "--exclude", "antigravity:private")

    output = tmp_path / "initial.jsonl"
    _cli(tmp_path, "export", "--output", str(output))
    assert [json.loads(line)["session_id"] for line in output.read_text().splitlines()] == [PUBLIC]

    (agy / "conversation_summaries.db").write_bytes(b"unreadable sqlite database")
    retry_output = tmp_path / "retry.jsonl"
    result = _cli(tmp_path, "export", "--output", str(retry_output), check=False)
    assert result.returncode != 0
    assert not retry_output.exists()
