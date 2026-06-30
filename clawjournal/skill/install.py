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
from pathlib import Path

from .render import SKILL_NAME

BEGIN_MARKER = "<!-- BEGIN clawjournal-lessons (managed by `clawjournal skill`) -->"
END_MARKER = "<!-- END clawjournal-lessons -->"


def claude_skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / SKILL_NAME / "SKILL.md"


def codex_agents_path() -> Path:
    return Path.home() / ".codex" / "AGENTS.md"


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


def install_claude(skill_md: str) -> Path:
    path = claude_skill_path()
    _atomic_write(path, skill_md)
    return path


def install_codex(region_body: str) -> Path:
    path = codex_agents_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _atomic_write(path, upsert_region(existing, region_body))
    return path
