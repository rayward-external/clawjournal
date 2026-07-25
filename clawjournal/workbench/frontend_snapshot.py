"""Immutable frontend assets captured before a background self-update starts."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


DEFAULT_FRONTEND_DIST = (
    Path(__file__).resolve().parent.parent / "web" / "frontend" / "dist"
)


@dataclass(frozen=True)
class FrontendSnapshot:
    """A process-local, immutable workbench build."""

    revision: str | None
    files: Mapping[str, bytes]

    def read(self, relative_path: str) -> bytes | None:
        return self.files.get(relative_path.replace("\\", "/").lstrip("/"))


def _tree_signature(root: Path) -> tuple[tuple[str, int, int], ...] | None:
    try:
        return tuple(
            sorted(
                (
                    path.relative_to(root).as_posix(),
                    path.stat().st_size,
                    path.stat().st_mtime_ns,
                )
                for path in root.rglob("*")
                if path.is_file()
            )
        )
    except OSError:
        return None


def capture_frontend_snapshot(
    root: Path = DEFAULT_FRONTEND_DIST,
    *,
    revision: str | None = None,
    attempts: int = 2,
    retry_delay: float = 0.05,
) -> FrontendSnapshot | None:
    """Capture one internally consistent frontend tree, or ``None``.

    The CLI calls this before starting the detached updater. A running daemon
    therefore keeps serving assets compatible with its imported Python even
    while an installer rebuilds ``dist/`` for the next process.

    ``None`` means "nothing worth pinning" — no ``dist/`` yet, a tree still
    being rewritten, or a file that went away mid-read — and puts the caller
    back on the historical disk-backed serving. Pinning an *empty* build
    instead would be worse than not pinning at all: the daemon has no reason
    to re-read disk, so it would serve the placeholder page for its entire
    lifetime even after ``dist/`` is healthy again. That is a live risk rather
    than a theoretical one, because Vite empties ``dist/`` before it starts
    bundling — every installer rerun leaves a seconds-wide window in which a
    concurrently starting daemon sees no frontend at all.
    """

    if not root.is_dir():
        return None  # no build to pin; nothing to wait for either

    for attempt in range(max(1, attempts)):
        if attempt and retry_delay > 0:
            # Without a gap the retry re-samples the same instant and only ever
            # catches a read that lost a race by microseconds.
            time.sleep(retry_delay)
        before = _tree_signature(root)
        if not before:
            continue  # unreadable, or emptied by a build already in flight
        files: dict[str, bytes] = {}
        try:
            for relative_path, _, _ in before:
                files[relative_path] = (root / relative_path).read_bytes()
        except OSError:
            continue
        if "index.html" not in files:
            continue  # partial tree — no SPA entry point to fall back to
        if before == _tree_signature(root):
            return FrontendSnapshot(
                revision=revision,
                files=MappingProxyType(files),
            )
    return None
