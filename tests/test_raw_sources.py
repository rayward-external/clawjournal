from __future__ import annotations

from pathlib import Path

from clawjournal.raw_sources import fingerprint_raw_source, read_raw_source_snapshot


def test_file_fingerprint_is_bound_to_content(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    source.write_text('{"first":1}\n', encoding="utf-8")
    before = fingerprint_raw_source(source)

    source.write_text('{"first":1}\n{"second":2}\n', encoding="utf-8")

    assert fingerprint_raw_source(source) != before


def test_subagent_fingerprint_detects_existing_member_append(tmp_path: Path) -> None:
    session_dir = tmp_path / "session-id"
    subagents = session_dir / "subagents"
    subagents.mkdir(parents=True)
    member = subagents / "agent-1.jsonl"
    member.write_text('{"first":1}\n', encoding="utf-8")
    directory_stat = session_dir.stat()
    before = fingerprint_raw_source(session_dir)

    with member.open("a", encoding="utf-8") as handle:
        handle.write('{"second":2}\n')

    # Appending an existing nested file does not reliably touch the parent;
    # the composite fingerprint must nevertheless observe its content.
    after_directory_stat = session_dir.stat()
    assert (
        directory_stat.st_size,
        directory_stat.st_mtime_ns,
    ) == (
        after_directory_stat.st_size,
        after_directory_stat.st_mtime_ns,
    )
    assert fingerprint_raw_source(session_dir) != before


def test_subagent_fingerprint_tracks_parser_members_only(tmp_path: Path) -> None:
    session_dir = tmp_path / "session-id"
    subagents = session_dir / "subagents"
    subagents.mkdir(parents=True)
    first = subagents / "agent-1.jsonl"
    first.write_text('{"first":1}\n', encoding="utf-8")
    baseline = fingerprint_raw_source(session_dir)

    unrelated = subagents / "notes.txt"
    unrelated.write_text("not a parser input", encoding="utf-8")
    assert fingerprint_raw_source(session_dir) == baseline

    second = subagents / "agent-2.jsonl"
    second.write_text('{"second":2}\n', encoding="utf-8")
    with_second = fingerprint_raw_source(session_dir)
    assert with_second != baseline

    renamed = subagents / "agent-renamed.jsonl"
    second.rename(renamed)
    assert fingerprint_raw_source(session_dir) != with_second

    renamed.unlink()
    assert fingerprint_raw_source(session_dir) == baseline

    snapshots, _fingerprint = read_raw_source_snapshot(session_dir)
    assert [path.name for path, _data in snapshots] == ["agent-1.jsonl"]
