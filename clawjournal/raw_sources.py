"""Stable snapshots for raw parser inputs used by recurring-share gates."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TypeAlias


RawFingerprint: TypeAlias = tuple[int, int, int, int, str]


class RawSourceChanged(OSError):
    """Raised when a raw source cannot be read as one stable snapshot."""


def _read_file_snapshot(path: Path) -> tuple[bytes, RawFingerprint]:
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            data = handle.read()
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise RawSourceChanged("raw source is unavailable") from exc
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(data) != after.st_size
    ):
        raise RawSourceChanged("raw source changed while it was read")
    return data, (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        hashlib.sha256(data).hexdigest(),
    )


def _subagent_members(path: Path) -> list[Path]:
    subagents = path / "subagents"
    if not subagents.is_dir():
        return []
    return sorted(subagents.glob("agent-*.jsonl"), key=lambda item: item.name)


def read_raw_source_snapshot(
    raw_path: str | Path,
) -> tuple[list[tuple[Path, bytes]], RawFingerprint]:
    """Read the exact parser inputs and return a content-bound fingerprint.

    A normal Claude/Codex row maps to one JSONL file.  Claude subagent-only
    rows map to a session directory whose parser input is exactly the sorted
    ``subagents/agent-*.jsonl`` set.  Unrelated directory files are ignored.
    """

    path = Path(raw_path)
    if path.is_file():
        data, fingerprint = _read_file_snapshot(path)
        return [(path, data)], fingerprint
    if not path.is_dir():
        raise RawSourceChanged("raw source is neither a file nor a directory")

    try:
        directory_stat = path.stat()
    except OSError as exc:
        raise RawSourceChanged("raw source directory is unavailable") from exc
    members_before = _subagent_members(path)
    if not members_before:
        raise RawSourceChanged("raw source directory has no parser inputs")

    snapshots: list[tuple[Path, bytes]] = []
    member_records: list[dict[str, object]] = []
    total_size = 0
    # Derive the fingerprint's mtime from the tracked member files only, never
    # from the directory's own st_mtime_ns: the directory mtime changes whenever
    # any unrelated top-level entry is created or removed, which would spuriously
    # trip the raw-source-changed gate even though every parser input byte is
    # identical. (members_before is non-empty, guaranteed above.)
    latest_mtime_ns = 0
    for member in members_before:
        data, fingerprint = _read_file_snapshot(member)
        snapshots.append((member, data))
        total_size += fingerprint[2]
        latest_mtime_ns = max(latest_mtime_ns, fingerprint[3])
        member_records.append(
            {
                "name": member.name,
                "device": fingerprint[0],
                "inode": fingerprint[1],
                "size": fingerprint[2],
                "mtime_ns": fingerprint[3],
                "sha256": fingerprint[4],
            }
        )

    members_after = _subagent_members(path)
    if [item.name for item in members_after] != [item.name for item in members_before]:
        raise RawSourceChanged("raw source members changed while they were read")
    try:
        final_directory_stat = path.stat()
    except OSError as exc:
        raise RawSourceChanged("raw source directory became unavailable") from exc
    if (
        directory_stat.st_dev != final_directory_stat.st_dev
        or directory_stat.st_ino != final_directory_stat.st_ino
    ):
        raise RawSourceChanged("raw source directory identity changed")

    encoded = json.dumps(
        member_records, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return snapshots, (
        int(final_directory_stat.st_dev),
        int(final_directory_stat.st_ino),
        int(total_size),
        int(latest_mtime_ns),
        hashlib.sha256(encoded).hexdigest(),
    )


def fingerprint_raw_source(raw_path: str | Path) -> RawFingerprint:
    """Return a stable, content-bound fingerprint for a parser input."""

    _snapshots, fingerprint = read_raw_source_snapshot(raw_path)
    return fingerprint


def stat_raw_source(raw_path: str | Path) -> tuple:
    """Cheap change-detection signature for a parser input — stat metadata only.

    Never reads or hashes file contents, so it is size-independent.  The value
    is opaque and only meaningful compared against another ``stat_raw_source``
    result for the *same* path: it changes whenever a tracked file is appended
    to or replaced, or (for a subagent directory) a member is added, removed,
    appended to, or replaced.  Callers use it to detect a raw change that
    happens while they wait to acquire a lock, without paying the
    size-unbounded re-hash of :func:`fingerprint_raw_source`.

    Raises :class:`RawSourceChanged` if the source is missing or unreadable, so
    a vanished input is treated as a change rather than silently matching.
    """

    path = Path(raw_path)
    if path.is_file():
        try:
            info = path.stat()
        except OSError as exc:
            raise RawSourceChanged("raw source is unavailable") from exc
        return (
            "file",
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
        )
    if not path.is_dir():
        raise RawSourceChanged("raw source is neither a file nor a directory")
    try:
        directory_stat = path.stat()
    except OSError as exc:
        raise RawSourceChanged("raw source directory is unavailable") from exc
    members = _subagent_members(path)
    if not members:
        raise RawSourceChanged("raw source directory has no parser inputs")
    member_signatures: list[tuple[str, int, int, int, int]] = []
    for member in members:
        try:
            member_stat = member.stat()
        except OSError as exc:
            raise RawSourceChanged("raw source member is unavailable") from exc
        member_signatures.append(
            (
                member.name,
                int(member_stat.st_dev),
                int(member_stat.st_ino),
                int(member_stat.st_size),
                int(member_stat.st_mtime_ns),
            )
        )
    return (
        "dir",
        int(directory_stat.st_dev),
        int(directory_stat.st_ino),
        tuple(member_signatures),
    )
