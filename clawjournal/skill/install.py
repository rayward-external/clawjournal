"""Install the rendered skill into the agents' GLOBAL surfaces (off the git tree).

  - Claude Code: ``~/.claude/skills/clawjournal-lessons/SKILL.md`` (a full skill,
    overwritten each run).
  - Codex: a delimited managed region in ``~/.codex/AGENTS.md`` (always-read),
    spliced in/out without touching the rest of the user's file.

Writes are atomic-overwrite (temp in the same dir + ``os.replace``) so a weekly
re-run can't leave a half-written skill. We never write into a repo ``cwd``.
"""

from __future__ import annotations

import os
import tempfile
import hashlib
from pathlib import Path

from .render import SKILL_NAME

BEGIN_MARKER = "<!-- BEGIN clawjournal-lessons (managed by `clawjournal skill`) -->"
END_MARKER = "<!-- END clawjournal-lessons -->"


def claude_skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / SKILL_NAME / "SKILL.md"


def codex_agents_path() -> Path:
    return Path.home() / ".codex" / "AGENTS.md"


def claude_skill_hash_path(path: Path | None = None) -> Path:
    return (path or claude_skill_path()).with_name("SKILL.md.sha256")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upsert_region(existing: str, region_body: str) -> str:
    """Return *existing* with the managed region replaced (or appended)."""
    block = f"{BEGIN_MARKER}\n{region_body.rstrip()}\n{END_MARKER}\n"
    start = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if start != -1 and end != -1 and end > start:
        end_full = end + len(END_MARKER)
        # consume a single trailing newline so re-runs don't accumulate blanks
        if end_full < len(existing) and existing[end_full] == "\n":
            end_full += 1
        return existing[:start] + block + existing[end_full:]
    sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return f"{existing}{sep}{block}" if existing.strip() else block


PROVENANCE_MARK = "<!-- clawjournal-lessons:"


def _is_unmodified_managed(existing: str) -> bool:
    """True if *existing* is our own generated skill with nothing appended.

    Our SKILL.md always ends with the ``<!-- clawjournal-lessons: … -->`` provenance
    comment. If the file still ends there, it is our untouched output — even when the
    .sha256 sidecar is missing or stale (its write can be interrupted separately from
    the SKILL.md write). This lets a re-run regenerate instead of bricking on a false
    "hand-edited" error, while text appended after the comment still reads as edited.
    """
    idx = existing.rfind(PROVENANCE_MARK)
    if idx == -1:
        return False
    tail = existing[idx:]
    close = tail.find("-->")
    return close != -1 and tail[close + 3:].strip() == ""


def install_claude(skill_md: str) -> Path:
    path = claude_skill_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        hash_path = claude_skill_hash_path(path)
        recorded = hash_path.read_text(encoding="utf-8").strip() if hash_path.exists() else None
        verified = recorded is not None and recorded == _sha256_text(existing)
        if verified or _is_unmodified_managed(existing):
            pass  # our own file (hash-verified, or unmodified despite a stale/missing sidecar)
        elif PROVENANCE_MARK in existing:
            raise RuntimeError(f"Refusing to overwrite hand-edited Claude skill: {path}")
        else:
            raise RuntimeError(f"Refusing to overwrite non-ClawJournal Claude skill: {path}")
    _atomic_write(path, skill_md)
    _atomic_write(claude_skill_hash_path(path), _sha256_text(skill_md) + "\n")
    return path


def install_codex(region_body: str) -> Path:
    path = codex_agents_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _atomic_write(path, upsert_region(existing, region_body))
    return path
