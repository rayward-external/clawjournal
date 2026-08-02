"""OpenClaw session segmentation.

Splits multi-task OpenClaw sessions into single-task child traces.
Uses a hybrid approach: pre-scans raw JSONL for hints (compaction entries,
model changes), then segments on parsed session dicts using heuristic signals.

Design doc: docs/openclaw-traces-design.md
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APPEND_ONLY_MAX_MESSAGES = 40
APPEND_ONLY_MAX_USER_MESSAGES = 20
APPEND_ONLY_MAX_TEXT_BYTES = 4 * 1024 * 1024
APPEND_ONLY_MAX_RAW_BYTES = 8 * 1024 * 1024


def _message_text_bytes(message: dict[str, Any]) -> int:
    total = 0
    stack: list[Any] = [message]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            total += len(value.encode("utf-8"))
        elif isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    return total


def segment_append_only_session(
    session: dict[str, Any],
    message_end_offsets: list[int],
    *,
    message_start_offsets: list[int] | None = None,
    raw_source_size: int | None = None,
    message_token_snapshots: list[tuple[int, int, int, int]] | None = None,
    max_messages: int = APPEND_ONLY_MAX_MESSAGES,
    max_user_messages: int = APPEND_ONLY_MAX_USER_MESSAGES,
    max_text_bytes: int = APPEND_ONLY_MAX_TEXT_BYTES,
    max_raw_bytes: int = APPEND_ONLY_MAX_RAW_BYTES,
) -> list[dict[str, Any]]:
    """Split a growing Claude/Codex trace at complete assistant turns.

    Boundaries are deterministic: the first complete assistant turn at or
    beyond any limit closes the current segment once the next user message
    confirms the raw boundary. Appending later records can therefore only
    extend the active tail or create new segments; it never changes an
    already-closed segment.

    ``message_token_snapshots`` carries the cumulative
    (input, output, cache_read, cache_creation) token totals as of each
    message; each child's stats get the delta over its message range so the
    file-level totals partition across segments instead of being dropped.
    """

    messages = session.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return [session]
    if len(message_end_offsets) != len(messages):
        return [session]
    if message_start_offsets is None:
        message_start_offsets = [
            0 if index == 0 else message_end_offsets[index - 1]
            for index in range(len(messages))
        ]
    if len(message_start_offsets) != len(messages):
        return [session]
    if any(
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset <= 0
        or (index and offset < message_end_offsets[index - 1])
        for index, offset in enumerate(message_end_offsets)
    ):
        return [session]
    if any(
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or offset > message_end_offsets[index]
        for index, offset in enumerate(message_start_offsets)
    ):
        return [session]

    token_snapshots = _validated_token_snapshots(
        message_token_snapshots, len(messages)
    )

    boundaries: list[int] = []
    segment_start = 0
    raw_start = 0
    user_messages = 0
    text_bytes = 0

    for index, message in enumerate(messages):
        if message.get("role") == "user":
            user_messages += 1
        text_bytes += _message_text_bytes(message)
        raw_bytes = message_end_offsets[index] - raw_start
        over_limit = (
            index + 1 - segment_start >= max_messages
            or user_messages >= max_user_messages
            or text_bytes >= max_text_bytes
            or raw_bytes >= max_raw_bytes
        )
        next_role = (
            messages[index + 1].get("role")
            if index + 1 < len(messages)
            else None
        )
        complete_turn = (
            message.get("role") == "assistant"
            and next_role == "user"
        )
        if over_limit and complete_turn:
            boundary = index + 1
            boundaries.append(boundary)
            segment_start = boundary
            raw_start = message_start_offsets[boundary]
            user_messages = 0
            text_bytes = 0

    interior_boundaries = [boundary for boundary in boundaries if boundary < len(messages)]
    starts = [0, *interior_boundaries]
    ends = [*interior_boundaries, len(messages)]
    if len(starts) <= 1:
        return [session]

    parent_id = str(session.get("session_id") or "")
    if not parent_id:
        return [session]
    closed_boundaries = set(boundaries)
    children: list[dict[str, Any]] = []
    for segment_index, (start, end) in enumerate(zip(starts, ends)):
        segment_messages = messages[start:end]
        segment_stats = _compute_stats(segment_messages)
        if token_snapshots is not None:
            segment_stats.update(_segment_token_deltas(token_snapshots, start, end))
        raw_segment_start = 0 if start == 0 else message_start_offsets[start]
        raw_segment_end = (
            message_start_offsets[end]
            if end < len(messages)
            else int(raw_source_size or message_end_offsets[end - 1])
        )
        sealed = end in closed_boundaries
        child = {
            key: value
            for key, value in session.items()
            if key not in {
                "session_id",
                "messages",
                "stats",
                "_raw_message_end_offsets",
                "_raw_source_fingerprint",
            }
        }
        child.update({
            # Keep the first checkpoint on the source session's established
            # identity. If that trace was shared before it grew large, the
            # hosted receiver sees a normal revision chain instead of a new,
            # overlapping trace. Later checkpoints are independent children.
            "session_id": (
                parent_id
                if segment_index == 0
                else f"{parent_id}_seg-{segment_index:04d}"
            ),
            "segment_index": segment_index,
            "segment_title": _extract_segment_title(segment_messages),
            "segment_reason": "bounded_checkpoint" if sealed else "active_tail",
            "segment_message_range": [start, end - 1],
            "segment_sealed": sealed,
            # Only a closed checkpoint may tolerate bytes appended after its
            # boundary. The active tail deliberately keeps no range so the
            # normal whole-file fingerprint catches a concurrent append.
            "raw_source_start_offset": raw_segment_start if sealed else None,
            "raw_source_end_offset": raw_segment_end if sealed else None,
            "start_time": _first_timestamp(segment_messages),
            "end_time": _last_timestamp(segment_messages),
            "messages": segment_messages,
            "stats": segment_stats,
        })
        if segment_index == 0:
            child.pop("parent_session_id", None)
        else:
            child["parent_session_id"] = parent_id
        children.append(child)
    return children


# ---------------------------------------------------------------------------
# Layer 1: Raw JSONL pre-scan (optional, OpenClaw-specific)
# ---------------------------------------------------------------------------

def pre_scan_openclaw_hints(jsonl_path: str | Path) -> dict[str, Any]:
    """Read raw OpenClaw JSONL and extract signals the parser discards.

    Returns a hints dict with:
        compaction_indices: list of approximate message positions for compaction entries
        compaction_summaries: list of summary strings from compaction entries
        model_changes: list of (entry_index, provider, model_id) tuples
        cwd_from_header: initial working directory from session header
    """
    hints: dict[str, Any] = {
        "compaction_indices": [],
        "compaction_summaries": [],
        "model_changes": [],
        "cwd_from_header": None,
    }
    message_count = 0
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                entry_type = entry.get("type")
                if entry_type == "session":
                    hints["cwd_from_header"] = entry.get("cwd")
                elif entry_type == "compaction":
                    hints["compaction_indices"].append(message_count)
                    hints["compaction_summaries"].append(entry.get("summary", ""))
                elif entry_type == "model_change":
                    provider = entry.get("provider", "")
                    model_id = entry.get("modelId", "")
                    hints["model_changes"].append((message_count, provider, model_id))
                elif entry_type == "message":
                    message_count += 1
    except OSError:
        pass
    return hints


# ---------------------------------------------------------------------------
# Signal detectors — each returns list of boundary message indices
# ---------------------------------------------------------------------------

def _detect_time_gaps(messages: list[dict], threshold_minutes: int = 30) -> list[int]:
    """Find message indices where the time gap from the previous message exceeds threshold."""
    boundaries: list[int] = []
    threshold_seconds = threshold_minutes * 60

    prev_ts: datetime | None = None
    for i, msg in enumerate(messages):
        if msg.get("_compaction"):
            continue
        ts = _parse_ts(msg.get("timestamp"))
        if ts and prev_ts:
            gap = (ts - prev_ts).total_seconds()
            if gap >= threshold_seconds:
                boundaries.append(i)
        if ts:
            prev_ts = ts
    return boundaries


def _detect_compaction_boundaries(messages: list[dict]) -> list[int]:
    """Find message indices right after compaction markers."""
    boundaries: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("_compaction") and i + 1 < len(messages):
            boundaries.append(i + 1)
    return boundaries


def _detect_tool_mode_shifts(messages: list[dict]) -> list[int]:
    """Find boundaries where tool usage density changes significantly.

    Classifies each user→agent exchange:
        Q&A mode: 0 tool calls
        Light tool mode: 1-3 tool calls
        Heavy tool mode: 4+ tool calls

    A transition from Q&A → heavy (or heavy → Q&A) is a boundary candidate.
    """
    exchanges = _build_exchanges(messages)
    if len(exchanges) < 2:
        return []

    boundaries: list[int] = []
    prev_mode = _classify_tool_mode(exchanges[0])

    for ex in exchanges[1:]:
        mode = _classify_tool_mode(ex)
        # Only fire on Q&A↔heavy transitions (skip gradual light transitions)
        if (prev_mode == "qa" and mode == "heavy") or (prev_mode == "heavy" and mode == "qa"):
            boundaries.append(ex["start_index"])
        prev_mode = mode

    return boundaries


def _detect_workspace_switches(messages: list[dict]) -> list[int]:
    """Find boundaries where the working directory or project changes.

    Scans tool calls for:
    1. Explicit cd commands in bash/exec tools
    2. File path prefix shifts in read/edit/write tools
    """
    boundaries: list[int] = []
    current_cwd: str | None = None
    current_prefix: str | None = None

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for tu in msg.get("tool_uses", []):
            tool = tu.get("tool", "")
            inp = tu.get("input", {})

            # Check for cd commands in bash/exec
            if tool in ("bash", "exec"):
                cmd = inp.get("command", "") if isinstance(inp, dict) else str(inp)
                new_cwd = _extract_cd_target(cmd)
                if new_cwd and current_cwd and not _same_project(new_cwd, current_cwd):
                    boundaries.append(i)
                if new_cwd:
                    current_cwd = new_cwd

            # Check for file path prefix shifts
            if tool in ("read", "edit", "write"):
                path = ""
                if isinstance(inp, dict):
                    path = inp.get("file_path", inp.get("path", ""))
                if path:
                    prefix = _project_prefix(path)
                    if prefix and current_prefix and prefix != current_prefix:
                        boundaries.append(i)
                    if prefix:
                        current_prefix = prefix

    return boundaries


# ---------------------------------------------------------------------------
# Boundary processing
# ---------------------------------------------------------------------------

def _snap_to_user_messages(boundaries: list[int], messages: list[dict]) -> list[int]:
    """Snap each boundary index to the nearest preceding user message.

    Drops boundaries that can't snap to a user message (e.g., walk-back
    reaches index 0 with no user message found).
    """
    snapped: list[int] = []
    for b in boundaries:
        idx = b
        while idx > 0 and messages[idx].get("role") != "user":
            idx -= 1
        if idx > 0 and messages[idx].get("role") == "user":
            snapped.append(idx)
        # idx == 0: don't create a boundary at the very start, drop it
    return snapped


def _score_boundaries(
    boundary_sets: dict[str, list[int]],
) -> list[tuple[int, float, str]]:
    """Score each boundary based on which signals agree.

    Returns list of (message_index, confidence, reason) sorted by index.
    """
    SIGNAL_WEIGHTS = {
        "time_gap": 0.9,
        "compaction": 0.8,
        "workspace": 0.6,
        "tool_mode": 0.5,
    }

    # Collect all unique boundary indices
    all_indices: set[int] = set()
    for indices in boundary_sets.values():
        all_indices.update(indices)

    scored: list[tuple[int, float, str]] = []
    for idx in sorted(all_indices):
        signals_at_idx: list[str] = []
        for signal_name, indices in boundary_sets.items():
            # Check if this signal fires at or near (±2 messages) this index
            for bi in indices:
                if abs(bi - idx) <= 2:
                    signals_at_idx.append(signal_name)
                    break

        if not signals_at_idx:
            continue

        if len(signals_at_idx) >= 3:
            confidence = 0.95
        elif len(signals_at_idx) == 2:
            confidence = max(SIGNAL_WEIGHTS.get(s, 0.5) for s in signals_at_idx) + 0.1
        else:
            confidence = SIGNAL_WEIGHTS.get(signals_at_idx[0], 0.5)

        reason = " + ".join(sorted(signals_at_idx))
        scored.append((idx, confidence, reason))

    return scored


def _enforce_minimum_segments(
    boundaries: list[int],
    messages: list[dict],
    min_user_msgs: int = 2,
    min_total_msgs: int = 3,
) -> list[int]:
    """Remove boundaries that would create segments too small.

    Exception: the last segment can be small (a trailing Q&A is fine).
    Segment 0 (before first boundary) is never checked — it's implicitly exempt.
    """
    if not boundaries:
        return boundaries

    total = len(messages)
    # Build segment ranges: [0, b0), [b0, b1), ..., [bN, total)
    all_starts = [0] + boundaries
    all_ends = boundaries + [total]

    keep: list[int] = []
    for i, b in enumerate(boundaries):
        seg_idx = i + 1  # segment created by this boundary (the right side)
        start = all_starts[seg_idx]
        end = all_ends[seg_idx]
        seg_msgs = messages[start:end]

        user_count = sum(1 for m in seg_msgs if m.get("role") == "user")
        total_count = len(seg_msgs)

        is_last = seg_idx == len(boundaries)

        # Allow small last segment (trailing Q&A at end of session)
        if is_last:
            keep.append(b)
            continue

        if user_count >= min_user_msgs or total_count >= min_total_msgs:
            keep.append(b)
        # else: drop this boundary (merge with neighbor)

    return keep


# ---------------------------------------------------------------------------
# Session splitting
# ---------------------------------------------------------------------------

def _split_session(
    session: dict[str, Any],
    boundaries: list[int],
    reasons: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Split a session at boundary indices, producing child trace dicts."""
    messages = session["messages"]
    parent_id = session["session_id"]
    reasons = reasons or {}

    if not boundaries:
        return [session]

    # Build segment ranges
    starts = [0] + boundaries
    ends = boundaries + [len(messages)]

    children: list[dict[str, Any]] = []
    for seg_idx, (start, end) in enumerate(zip(starts, ends)):
        seg_messages = messages[start:end]
        if not seg_messages:
            continue

        # Filter out compaction markers from child messages
        clean_messages = [m for m in seg_messages if not m.get("_compaction")]
        if not clean_messages:
            continue

        stats = _compute_stats(clean_messages)
        start_time = _first_timestamp(clean_messages)
        end_time = _last_timestamp(clean_messages)

        # Title from first user message
        title = _extract_segment_title(clean_messages)

        child: dict[str, Any] = {
            "session_id": f"{parent_id}_seg-{seg_idx:02d}",
            "parent_session_id": parent_id,
            "segment_index": seg_idx,
            "segment_title": title,
            "segment_reason": reasons.get(boundaries[seg_idx - 1], "initial_segment") if seg_idx > 0 else "initial_segment",
            "segment_message_range": [start, end - 1],
            "model": session.get("model"),
            "source": session.get("source", "openclaw"),
            "project": session.get("project"),
            "git_branch": session.get("git_branch"),
            "start_time": start_time,
            "end_time": end_time,
            "messages": clean_messages,
            "stats": stats,
        }
        children.append(child)

    return children


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def segment_openclaw_session(
    session: dict[str, Any],
    hints: dict[str, Any] | None = None,
    threshold_minutes: int = 30,
    confidence_threshold: float = 0.6,
) -> list[dict[str, Any]]:
    """Segment an OpenClaw session into child traces.

    Args:
        session: Parsed session dict with messages, stats, etc.
        hints: Optional pre-scan hints from pre_scan_openclaw_hints().
        threshold_minutes: Time gap threshold for boundary detection.
        confidence_threshold: Minimum confidence to keep a boundary.

    Returns:
        List of child trace dicts. If no segmentation needed, returns a
        single-element list containing the original session.
    """
    messages = session.get("messages", [])
    if len(messages) < 4:
        return [session]

    # Step 1: Collect candidate boundaries from all signals
    boundary_sets: dict[str, list[int]] = {
        "time_gap": _detect_time_gaps(messages, threshold_minutes),
        "compaction": _detect_compaction_boundaries(messages),
        "tool_mode": _detect_tool_mode_shifts(messages),
        "workspace": _detect_workspace_switches(messages),
    }

    # Step 2: Snap to user message boundaries
    for signal_name in boundary_sets:
        boundary_sets[signal_name] = _snap_to_user_messages(
            boundary_sets[signal_name], messages
        )

    # Step 3: Score and filter boundaries
    scored = _score_boundaries(boundary_sets)
    filtered = [(idx, conf, reason) for idx, conf, reason in scored if conf >= confidence_threshold]

    if not filtered:
        return [session]

    # Step 4: Extract boundaries and reasons
    boundaries = [idx for idx, _, _ in filtered]
    reasons = {idx: reason for idx, _, reason in filtered}

    # Deduplicate and sort
    seen: set[int] = set()
    unique_boundaries: list[int] = []
    unique_reasons: dict[int, str] = {}
    for b in boundaries:
        if b not in seen and b > 0:
            seen.add(b)
            unique_boundaries.append(b)
            unique_reasons[b] = reasons[b]
    unique_boundaries.sort()

    # Step 5: Enforce minimum segment size
    final_boundaries = _enforce_minimum_segments(unique_boundaries, messages)
    final_reasons = {b: unique_reasons[b] for b in final_boundaries if b in unique_reasons}

    if not final_boundaries:
        return [session]

    # Step 6: Split
    return _split_session(session, final_boundaries, final_reasons)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts: Any) -> datetime | None:
    """Parse a timestamp string or epoch ms to a datetime."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(ts, str):
        # Handle Z suffix (fromisoformat on Python 3.10 doesn't support Z)
        normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            pass
    return None


def _build_exchanges(messages: list[dict]) -> list[dict[str, Any]]:
    """Group messages into user→agent exchanges.

    Each exchange starts at a user message and includes all subsequent
    assistant messages until the next user message.
    """
    exchanges: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for i, msg in enumerate(messages):
        if msg.get("_compaction"):
            continue
        role = msg.get("role", "")
        if role == "user":
            if current is not None:
                exchanges.append(current)
            current = {"start_index": i, "tool_count": 0, "messages": [msg]}
        elif role == "assistant" and current is not None:
            current["messages"].append(msg)
            current["tool_count"] += len(msg.get("tool_uses", []))

    if current is not None:
        exchanges.append(current)
    return exchanges


def _classify_tool_mode(exchange: dict[str, Any]) -> str:
    """Classify an exchange as qa, light, or heavy tool usage."""
    tc = exchange.get("tool_count", 0)
    if tc == 0:
        return "qa"
    if tc <= 3:
        return "light"
    return "heavy"


_CD_RE = re.compile(r'\bcd\s+([^\s;&|]+)')


def _extract_cd_target(command: str) -> str | None:
    """Extract the target directory from a cd command in a shell string."""
    m = _CD_RE.search(command)
    if m:
        return m.group(1).rstrip("/")
    return None


_SYSTEM_PATH_PREFIXES = ("/usr/", "/opt/", "/etc/", "/var/", "/tmp/", "/proc/", "/sys/")


def _project_prefix(path: str) -> str | None:
    """Extract a project-level prefix from a file path (first 3 components).

    Ignores system paths (e.g., /usr/lib/...) which are shared resources,
    not project directories.
    """
    if any(path.startswith(p) for p in _SYSTEM_PATH_PREFIXES):
        return None
    parts = path.strip("/").split("/")
    if len(parts) >= 3:
        return "/" + "/".join(parts[:3])
    return None


def _same_project(path_a: str, path_b: str) -> bool:
    """Check if two paths are in the same project directory."""
    prefix_a = _project_prefix(path_a)
    prefix_b = _project_prefix(path_b)
    if prefix_a and prefix_b:
        return prefix_a == prefix_b
    return path_a == path_b


_TOKEN_SNAPSHOT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def _validated_token_snapshots(
    snapshots: Any, message_count: int
) -> list[tuple[int, int, int, int]] | None:
    """Return the snapshots only when they align 1:1 with the messages."""
    if not isinstance(snapshots, list) or len(snapshots) != message_count:
        return None
    for snapshot in snapshots:
        if not isinstance(snapshot, (list, tuple)) or len(snapshot) != len(
            _TOKEN_SNAPSHOT_FIELDS
        ):
            return None
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in snapshot
        ):
            return None
    return snapshots


def _segment_token_deltas(
    snapshots: list[tuple[int, int, int, int]], start: int, end: int
) -> dict[str, int]:
    """Token totals accrued across messages[start:end], from cumulative snapshots."""
    previous = snapshots[start - 1] if start else (0, 0, 0, 0)
    last = snapshots[end - 1]
    return {
        field: max(0, int(last[index]) - int(previous[index]))
        for index, field in enumerate(_TOKEN_SNAPSHOT_FIELDS)
    }


def _compute_stats(messages: list[dict]) -> dict[str, int]:
    """Compute stats for a subset of messages."""
    stats = {
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_uses": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    for msg in messages:
        role = msg.get("role")
        if role == "user":
            stats["user_messages"] += 1
        elif role == "assistant":
            stats["assistant_messages"] += 1
            stats["tool_uses"] += len(msg.get("tool_uses", []))
    return stats


def _first_timestamp(messages: list[dict]) -> str | None:
    """Get the first non-None timestamp from messages."""
    for msg in messages:
        ts = msg.get("timestamp")
        if ts:
            return ts
    return None


def _last_timestamp(messages: list[dict]) -> str | None:
    """Get the last non-None timestamp from messages."""
    for msg in reversed(messages):
        ts = msg.get("timestamp")
        if ts:
            return ts
    return None


def _extract_segment_title(messages: list[dict]) -> str:
    """Generate a title from the first user message in the segment."""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            # Strip common metadata prefixes (OpenClaw wraps user messages)
            text = _strip_openclaw_metadata(content)
            if text:
                # Truncate to ~80 chars at word boundary
                if len(text) > 80:
                    text = text[:77].rsplit(" ", 1)[0] + "..."
                return text
    return "Untitled segment"


def _strip_openclaw_metadata(content: str) -> str:
    """Strip OpenClaw sender metadata wrapper from user message content.

    OpenClaw user messages often have one or two metadata blocks:
        ```json { timestamp ... } ```
        Sender (untrusted metadata): ```json { sender ... } ```
        Actual message text
    """
    # Split on ``` fences — metadata blocks appear in pairs
    parts = content.split("```")
    # Skip all json metadata blocks (each block = odd-indexed part)
    # The actual text is after the last closing ```
    if len(parts) >= 3:
        # Find the last part that isn't a json block
        after_all_meta = parts[-1].strip()
        if after_all_meta:
            # Strip leading timestamp like [Sat 2026-03-21 01:18 UTC]
            after_all_meta = re.sub(r'^\[.*?\]\s*', '', after_all_meta)
            # Strip "Sender (untrusted metadata):" prefix if it leaked through
            after_all_meta = re.sub(r'^Sender\s*\(untrusted metadata\):\s*', '', after_all_meta)
            return after_all_meta.strip()
    # No metadata block — return content directly (strip timestamp if present)
    text = re.sub(r'^\[.*?\]\s*', '', content.strip())
    return text
