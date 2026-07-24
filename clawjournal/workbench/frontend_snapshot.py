"""Immutable frontend assets captured before a background self-update starts."""

from __future__ import annotations

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
) -> FrontendSnapshot:
    """Capture one internally consistent frontend tree.

    The CLI calls this before starting the detached updater. A running daemon
    therefore keeps serving assets compatible with its imported Python even
    while an installer rebuilds ``dist/`` for the next process.
    """

    for _ in range(max(1, attempts)):
        before = _tree_signature(root)
        if before is None:
            continue
        files: dict[str, bytes] = {}
        try:
            for relative_path, _, _ in before:
                files[relative_path] = (root / relative_path).read_bytes()
        except OSError:
            continue
        if before == _tree_signature(root):
            return FrontendSnapshot(
                revision=revision,
                files=MappingProxyType(files),
            )
    return FrontendSnapshot(revision=revision, files=MappingProxyType({}))
