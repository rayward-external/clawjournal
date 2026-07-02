"""Install the rendered skill into the agents' GLOBAL surfaces (off the git tree).

  - Claude Code: ``~/.claude/skills/clawjournal-lessons/SKILL.md`` (a full skill,
    overwritten each run).
  - Codex: a delimited managed region in ``~/.codex/AGENTS.md`` (always-read),
    spliced in/out without touching the rest of the user's file.

Writes are atomic-overwrite (temp in the same dir + ``os.replace``) so a weekly
re-run can't leave a half-written skill. We never write into a repo ``cwd``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..paths import atomic_write_text
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
    atomic_write_text(path, text, parents=True)


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
INTEGRITY_PREFIX = "<!-- clawjournal-integrity sha256:"


def _with_integrity(skill_md: str) -> str:
    """Append a trailing integrity comment so the file is self-verifying (one write)."""
    body = skill_md.rstrip("\n") + "\n"
    return f"{body}{INTEGRITY_PREFIX}{_sha256_text(body)} -->\n"


def _verify_integrity(existing: str) -> bool | None:
    """Whether *existing* still matches its embedded integrity hash.

    Returns True (untouched ours), False (a body edit or trailing append — the hash
    no longer matches / content follows the comment), or None (no integrity line at
    all, i.e. a pre-integrity file). Because the hash lives INSIDE the single SKILL.md
    write, there is no second sidecar file to desync — so this both detects mid-body
    edits (which a separate .sha256 could too) AND can never brick on a partial write.
    """
    marker = existing.rfind(INTEGRITY_PREFIX)
    if marker == -1:
        return None
    close = existing.find("-->", marker)
    if close == -1 or existing[close + 3:].strip() != "":
        return False  # malformed, or content appended after the integrity comment
    line_start = existing.rfind("\n", 0, marker) + 1
    body = existing[:line_start]
    recorded = existing[marker + len(INTEGRITY_PREFIX):close].strip()
    return _sha256_text(body) == recorded


def install_claude(skill_md: str) -> Path:
    path = claude_skill_path()
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        integ = _verify_integrity(existing)
        if integ is False:
            # Ours, but changed externally. This file is a weekly-regenerated artifact,
            # so a benign touch (an editor's final-newline, git EOL normalization, a
            # manual tweak) must NOT permanently block the refresh. Preserve the user's
            # copy and regenerate — never hard-refuse our own managed file.
            backup = path.with_name(path.name + ".local.bak")
            _atomic_write(backup, existing)
            print(f"note: {path.name} was modified externally; saved your copy to "
                  f"{backup.name} and regenerated it.")
        elif integ is None and PROVENANCE_MARK not in existing:
            raise RuntimeError(f"Refusing to overwrite non-ClawJournal Claude skill: {path}")
        # integ True, or pre-integrity file with our marker -> overwrite in place.
    _atomic_write(path, _with_integrity(skill_md))
    # Integrity now lives in the file; retire any legacy .sha256 sidecar.
    sidecar = claude_skill_hash_path(path)
    try:
        sidecar.unlink()
    except OSError:
        pass
    return path


def install_codex(region_body: str) -> Path:
    path = codex_agents_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _atomic_write(path, upsert_region(existing, region_body))
    return path
