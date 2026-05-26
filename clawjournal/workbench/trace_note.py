"""Per-session markdown trace notes.

Renders a sessions row + its reviewer_notes into a human-readable and
AI-agent-friendly markdown file at ~/.clawjournal/notes/{session_id}.md.

The file is a materialized view of the DB. Only the `## Notes` block
round-trips back to `sessions.reviewer_notes` via `note sync`. See
docs/db-refactor.md for the full design, especially §File Format,
§Sync Model, and §Canonical normalization.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def _notes_dir() -> Path:
    """Resolve the notes directory from the current `BLOBS_DIR` location.

    `BLOBS_DIR` in `clawjournal.workbench.index` is the canonical "where
    clawjournal keeps per-session state" pointer, and every existing test
    that cares already monkeypatches it to a tmp_path. Deriving notes/
    from it at call time (not module-import time) means test pollution
    is impossible: if a test has redirected `BLOBS_DIR`, the scoring-path
    auto-create hook automatically writes under that redirected root.

    Production: `BLOBS_DIR = ~/.clawjournal/blobs`, so notes/ is
    `~/.clawjournal/notes` — identical to the hardcoded form.
    """
    from .index import BLOBS_DIR
    return Path(BLOBS_DIR).parent / "notes"


# Back-compat shim for tests that explicitly monkeypatched
# `clawjournal.workbench.trace_note.NOTES_DIR`. Reading this attribute
# still returns the correct path for the current BLOBS_DIR; assigning to
# it in a fixture still works but is no longer necessary.
class _NotesDirProxy:
    def __truediv__(self, other):
        return _notes_dir() / other

    def __str__(self):
        return str(_notes_dir())

    def __repr__(self):
        return f"_NotesDirProxy({_notes_dir()!r})"

    def mkdir(self, *args, **kwargs):
        return _notes_dir().mkdir(*args, **kwargs)

    def glob(self, pattern):
        return _notes_dir().glob(pattern)

    def exists(self):
        return _notes_dir().exists()

    def __fspath__(self):
        return str(_notes_dir())


NOTES_DIR = _NotesDirProxy()

_RENDERED_UPDATED_AT_RE = re.compile(
    r"<!--\s*rendered_updated_at:\s*(\S+?)\s*-->"
)
_NOTES_HEADING_RE = re.compile(r"^## Notes\s*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^## \S", re.MULTILINE)


def trace_note_path(session_id: str) -> Path:
    """Deterministic path — no DB lookup, no `trace_note_path` column."""
    return _notes_dir() / f"{session_id}.md"


def _normalize_notes(s: str | None) -> str:
    """Canonical form for `## Notes` ↔ `reviewer_notes` equality checks.

    Rules (see docs/db-refactor.md §Canonical normalization):
    - None → ''                    (UI and render treat NULL and '' identically)
    - CRLF / CR → LF               (cross-platform editor tolerance)
    - rstrip trailing whitespace   (render ends with '\\n'; edits may not)

    Used at every comparison site — never use raw `==` on notes text.
    """
    if s is None:
        return ""
    return s.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def _fmt_duration(seconds: Any) -> str:
    if seconds is None:
        return "—"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if total <= 0:
        return "—"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if minutes and secs and total < 600:
        return f"{minutes}m {secs}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _parse_iso(ts: Any) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_when(start_time: Any, end_time: Any, duration_seconds: Any) -> str:
    start_dt = _parse_iso(start_time)
    end_dt = _parse_iso(end_time)
    dur = _fmt_duration(duration_seconds)
    if start_dt is None:
        return "—"
    start_str = start_dt.strftime("%Y-%m-%d %H:%M")
    if end_dt is not None:
        end_str = end_dt.strftime("%H:%M")
        return f"{start_str} → {end_str} UTC ({dur})"
    return f"{start_str} UTC ({dur})"


def _parse_badges(value: Any) -> list[str]:
    """Accept a list, a JSON-encoded list, or None."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed if v]
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _coalesce(*values: Any) -> Any:
    """Return first non-empty, non-None value (mirrors API's ai_* promotion)."""
    for v in values:
        if v is not None and v != "" and v != []:
            return v
    return None


def _fmt_tokens(input_tokens: Any, output_tokens: Any) -> str:
    try:
        in_n = int(input_tokens or 0)
        out_n = int(output_tokens or 0)
    except (TypeError, ValueError):
        return "—"
    if in_n == 0 and out_n == 0:
        return "—"
    return f"{in_n:,} in / {out_n:,} out"


def _fmt_score(score: Any, effort: Any) -> str:
    if score is None:
        return "—"
    try:
        n = int(score)
    except (TypeError, ValueError):
        return "—"
    if effort is None:
        return f"{n}/5"
    try:
        e = float(effort)
    except (TypeError, ValueError):
        return f"{n}/5"
    return f"{n}/5 (effort {e:g})"


def _fmt_tags(value_badges: Any, risk_badges: Any) -> str:
    tags = _parse_badges(value_badges) + _parse_badges(risk_badges)
    if not tags:
        return "—"
    return ", ".join(tags)


def _fmt_or_dash(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _fmt_source(source: Any, model: Any, model_effort: Any = None) -> str:
    s = str(source or "").strip()
    m = str(model or "").strip()
    e = str(model_effort or "").strip()
    model_part = f"`{m}`" if m else ""
    if m and e:
        model_part = f"`{m}` @ {e}"
    if s and model_part:
        return f"{s} ({model_part})"
    if s:
        return s
    if model_part:
        return model_part
    return "—"


def render_trace_note(
    session: dict[str, Any],
    reviewer_notes: str | None,
) -> str:
    """Render the full trace note from a sessions row dict.

    `session` must carry at least session_id + updated_at. Every other
    field renders as '—' when missing so the metadata bullet list has a
    stable shape across files.

    Title uses `sessions.display_title` only — matches the API's visible
    title path at daemon.py:302-329, which does NOT promote
    `ai_display_title`. Badges/outcome prefer `ai_*` (mirroring the API).
    """
    session_id = session.get("session_id") or ""
    updated_at = session.get("updated_at") or ""

    title = session.get("display_title") or session_id or "untitled"

    project = _fmt_or_dash(session.get("project"))
    source = _fmt_source(
        session.get("source"), session.get("model"), session.get("model_effort")
    )
    when = _fmt_when(
        session.get("start_time"),
        session.get("end_time"),
        session.get("duration_seconds"),
    )
    tokens = _fmt_tokens(session.get("input_tokens"), session.get("output_tokens"))
    score = _fmt_score(
        session.get("ai_quality_score"),
        session.get("ai_effort_estimate"),
    )
    outcome = _fmt_or_dash(
        _coalesce(session.get("ai_outcome_badge"), session.get("outcome_badge"))
    )
    tags = _fmt_tags(
        _coalesce(session.get("ai_value_badges"), session.get("value_badges")),
        _coalesce(session.get("ai_risk_badges"), session.get("risk_badges")),
    )

    summary_body = (session.get("ai_summary") or "").strip()
    notes_body = _normalize_notes(reviewer_notes)
    failure_block = _render_failure_block(session)

    lines = [
        f"<!-- clawjournal-trace-note v{SCHEMA_VERSION} -->",
        f"<!-- session_id: {session_id} -->",
        f"<!-- rendered_updated_at: {updated_at} -->",
        "",
        f"# {title}",
        "",
        f"- **Session:** `{session_id}`",
        f"- **Project:** {project}",
        f"- **Source:** {source}",
        f"- **When:** {when}",
        f"- **Tokens:** {tokens}",
        f"- **Score:** {score}",
        f"- **Outcome:** {outcome}",
        f"- **Tags:** {tags}",
        "",
        "## Summary",
        "",
        summary_body,
    ]
    if failure_block:
        lines.extend(["", "## Failure analysis", "", failure_block])
    lines.extend(["", "## Notes", "", notes_body])
    return "\n".join(lines).rstrip() + "\n"


def _parse_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
    return []


def _render_failure_block(session: dict[str, Any]) -> str:
    """Render the failure-analysis section. Returns "" when no failure data.

    Pulls from dedicated columns (`ai_failure_modes`, `ai_failure_attribution`,
    `ai_recovery_labels`, `ai_learning_summary`) and from the `ai_scoring_detail`
    JSON blob (`ai_failure_evidence`, `ai_meta_labels`). This is the canonical
    snapshot of judge output for later analysis — keep it round-trippable.
    """
    detail: dict[str, Any] = {}
    raw_detail = session.get("ai_scoring_detail")
    if isinstance(raw_detail, str) and raw_detail.strip():
        try:
            parsed = json.loads(raw_detail)
            if isinstance(parsed, dict):
                detail = parsed
        except (json.JSONDecodeError, ValueError):
            detail = {}

    score = session.get("ai_failure_value_score")
    attribution = (session.get("ai_failure_attribution") or "").strip()
    modes = _parse_list(session.get("ai_failure_modes"))
    recovery = _parse_list(session.get("ai_recovery_labels"))
    meta_labels = _parse_list(detail.get("ai_meta_labels"))
    evidence = _parse_list(detail.get("ai_failure_evidence"))
    learning = (session.get("ai_learning_summary") or "").strip()

    has_any = (
        score is not None
        or attribution
        or modes
        or recovery
        or meta_labels
        or evidence
        or learning
    )
    if not has_any:
        return ""

    parts: list[str] = []
    if score is not None:
        parts.append(f"- **Failure value:** {score}/5")
    if attribution:
        parts.append(f"- **Attribution:** {attribution}")
    if modes:
        parts.append(f"- **Modes:** {', '.join(modes)}")
    if recovery:
        parts.append(f"- **Recovery:** {', '.join(recovery)}")
    if meta_labels:
        parts.append(f"- **Meta labels:** {', '.join(meta_labels)}")
    if learning:
        parts.extend(["", f"_{learning}_"])
    if evidence:
        parts.extend(["", "**Evidence:**"])
        parts.extend(f"- {e}" for e in evidence)
    return "\n".join(parts)


def extract_trace_note_notes(text: str) -> str | None:
    """Return the `## Notes` block body (no heading), or None if absent.

    Block ends at the next `## <heading>` or EOF. Empty block returns ''.
    Never returns None for an empty block — that's a valid user choice to
    clear the note. None means the heading itself is missing (malformed
    file), which `note sync` treats as an error.
    """
    m = _NOTES_HEADING_RE.search(text)
    if m is None:
        return None
    body_start = m.end()
    # Skip the newline after the heading line, if present.
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1

    rest = text[body_start:]
    next_m = _NEXT_HEADING_RE.search(rest)
    body = rest[: next_m.start()] if next_m else rest
    # Strip only the leading blank line(s) that follow the heading and the
    # trailing whitespace introduced by the render's terminal newline.
    # Preserve leading spaces / tabs — per §Canonical normalization, leading
    # whitespace inside the notes block is content, not formatting.
    return body.lstrip("\n").rstrip()


def extract_rendered_updated_at(text: str) -> str | None:
    """Parse `<!-- rendered_updated_at: ... -->`, returning the timestamp or None."""
    m = _RENDERED_UPDATED_AT_RE.search(text)
    if m is None:
        return None
    value = m.group(1).strip()
    return value or None


def write_note_atomically(path: Path, text: str) -> None:
    """Write to `path` via tempfile + os.replace.

    Atomic on POSIX; readers of the final path never see partial content.
    Creates parent directories as needed.
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".note-", suffix=".md", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def create_note_if_missing(session: dict[str, Any]) -> Path | None:
    """Render and write a trace note only if one does not already exist.

    Used by the scoring hooks in daemon.py: auto-create once when a session
    gets its first summary; never overwrite existing files (those may have
    unsynced user edits). Returns the path when a new file is written,
    None when one already exists or writing is skipped.

    Caller must have a session dict (e.g. from `get_session_detail` or a
    direct `SELECT * FROM sessions WHERE session_id = ?` query).
    """
    session_id = session.get("session_id")
    if not session_id:
        return None
    path = trace_note_path(session_id)
    if path.exists():
        return None
    text = render_trace_note(session, session.get("reviewer_notes"))
    write_note_atomically(path, text)
    return path
