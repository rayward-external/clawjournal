"""Structured scoring pipeline for agentic traces.

Implements: Format -> Judge -> Store
See docs/scoring-algorithm.md for the full specification.

All scoring judgment lives in the rubric
(`clawjournal/prompts/agents/scoring/rubric.md`). Python code handles
formatting, calling the judge, and storing results. Zero scoring logic.
"""

from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backends import (
    AgentResult,
    BACKEND_CHOICES,
    BACKEND_COMMANDS,
    BACKEND_COMMAND_ALIASES,
    BACKEND_ENV_MARKERS,
    PROMPTS_DIR,
    SUPPORTED_BACKENDS,
    detect_current_agent,
    resolve_backend,
    resolve_model_for_backend,
    run_default_agent_task,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """A single tool-call cycle within a segment."""
    plan: str              # assistant text before tool call
    action_tool: str       # tool name
    action_input: str      # first arg / summary of input
    result_output: str     # tool output (may be truncated)
    result_status: str     # "success", "error", "failure", ""
    reflect: str           # assistant text after result (may be empty)


@dataclass
class Segment:
    """A block of agent work bounded by user messages."""
    user_message: str
    steps: list[Step]
    user_response: str | None = None   # next user message, or None
    judge_result: dict | None = None


@dataclass
class ScoringResult:
    """Final scoring output for one session."""
    segments: list[Segment]
    quality: int                 # 1-5 productivity score, from judge
    reason: str                  # judge's reasoning
    display_title: str = ""              # LLM-generated concise title
    summary: str = ""                    # 1-3 sentence session summary
    task_type: str = "unknown"           # LLM-classified task type
    outcome_label: str = ""              # resolution label; "" means judge gave no valid label
    value_labels: list[str] = field(default_factory=list)  # session tags
    risk_level: list[str] = field(default_factory=list)     # privacy flags
    effort_estimate: float = 0.0         # 0.0-1.0 effort estimate
    project_areas: list[str] = field(default_factory=list)  # directory paths touched
    taste_signals: list[dict] = field(default_factory=list)  # kept for backward compat
    detail_json: str = "{}"
    failure_value_score: int | None = None
    recovery_labels: list[str] = field(default_factory=list)
    failure_attribution: str = ""
    failure_modes: list[str] = field(default_factory=list)
    learning_summary: str = ""
    scorer_backend: str = ""
    scorer_model: str = ""
    rubric_git_sha: str = ""
    scored_at: str = ""


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------

def get_message_text(msg: dict) -> str:
    """Extract text content from a message dict."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                return block
            if isinstance(block, dict) and block.get("text"):
                return block["text"]
    return ""


def extract_tool_uses(msg: dict) -> list[dict]:
    """Extract tool uses from a message, handling both parsed and raw formats."""
    tool_uses = msg.get("tool_uses", [])
    if tool_uses:
        return tool_uses
    content = msg.get("content")
    if isinstance(content, list):
        uses = []
        for block in content:
            if isinstance(block, dict) and block.get("tool"):
                inp = block.get("input", {})
                first_arg = ""
                if isinstance(inp, dict):
                    for v in inp.values():
                        if isinstance(v, str) and v.strip():
                            first_arg = v.strip()
                            break
                uses.append({
                    "tool": block["tool"],
                    "input": inp,
                    "output": block.get("output", ""),
                    "status": block.get("status", ""),
                    "first_arg": first_arg,
                })
        return uses
    return []


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"


def _first_input_value(inp: dict | Any) -> str:
    """Return the first string value from a tool input dict."""
    if isinstance(inp, dict):
        for v in inp.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(inp, str):
        return inp.strip()
    return ""


# ---------------------------------------------------------------------------
# Format: parse messages into turns and build the judge prompt
# ---------------------------------------------------------------------------

def segment_session(messages: list[dict]) -> list[Segment]:
    """Split a message list into Segments bounded by user messages.

    Each user message starts a new segment. Within a segment, each tool_use
    in an assistant message becomes a Step. This is purely structural formatting.
    """
    if not messages:
        return []

    segments: list[Segment] = []
    current_user_msg = ""
    current_steps: list[Step] = []
    pending_plan = ""

    def _flush_segment() -> None:
        nonlocal current_user_msg, current_steps, pending_plan
        if current_user_msg or current_steps:
            segments.append(Segment(
                user_message=current_user_msg,
                steps=current_steps,
            ))
        current_user_msg = ""
        current_steps = []
        pending_plan = ""

    for msg in messages:
        role = msg.get("role", "")

        if role == "user":
            text = get_message_text(msg)
            _flush_segment()
            if segments:
                segments[-1].user_response = text
            current_user_msg = text

        elif role == "assistant":
            text = get_message_text(msg)
            tool_uses = extract_tool_uses(msg)

            if not tool_uses:
                if current_steps:
                    current_steps[-1].reflect = text
                else:
                    pending_plan = text
            else:
                for i, tu in enumerate(tool_uses):
                    plan = text if i == 0 else ""
                    if i == 0 and pending_plan and not text:
                        plan = pending_plan
                        pending_plan = ""

                    output = tu.get("output", "")
                    if isinstance(output, dict):
                        output = json.dumps(output)[:500]
                    elif not isinstance(output, str):
                        output = str(output)[:500] if output else ""

                    current_steps.append(Step(
                        plan=plan,
                        action_tool=tu.get("tool", ""),
                        action_input=_first_input_value(tu.get("input", {})),
                        result_output=output,
                        result_status=tu.get("status", ""),
                        reflect="",
                    ))
                pending_plan = ""

    _flush_segment()

    if not segments and messages:
        segments.append(Segment(user_message="", steps=[]))

    return segments


def compute_heuristic_effort(
    duration_seconds: int | float | None,
    tool_calls: int,
    total_tokens: int,
    files_touched: int,
) -> float:
    """Compute a 0.0-1.0 effort estimate from session metrics.

    Formula weights duration and tool calls (active work signals) more heavily
    than token count (inflates with verbose output) and file count.
    Each factor is capped at 1.0 so no single metric dominates.
    """
    duration_minutes = (duration_seconds or 0) / 60.0
    raw = (
        0.3 * min(duration_minutes / 60.0, 1.0)
        + 0.3 * min(tool_calls / 50.0, 1.0)
        + 0.2 * min(total_tokens / 100_000.0, 1.0)
        + 0.2 * min(files_touched / 20.0, 1.0)
    )
    return max(0.0, min(1.0, raw))


def compute_basic_metrics(segments: list[Segment], detail: dict) -> dict:
    """Compute simple stats for the judge prompt. No scoring judgment."""
    total_steps = sum(len(s.steps) for s in segments)
    tool_failures = sum(
        1 for s in segments for step in s.steps
        if step.result_status in ("failure", "error")
    )
    files_touched = detail.get("files_touched", []) or []
    if isinstance(files_touched, str):
        try:
            files_touched = json.loads(files_touched)
        except (json.JSONDecodeError, ValueError):
            files_touched = []

    input_tokens = detail.get("input_tokens", 0) or 0
    output_tokens = detail.get("output_tokens", 0) or 0
    duration_seconds = detail.get("duration_seconds")
    files_count = len(files_touched)

    heuristic_effort = compute_heuristic_effort(
        duration_seconds=duration_seconds,
        tool_calls=total_steps,
        total_tokens=input_tokens + output_tokens,
        files_touched=files_count,
    )

    return {
        "total_steps": total_steps,
        "segments": len(segments),
        "tool_failures": tool_failures,
        "user_messages": detail.get("user_messages", 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_seconds": duration_seconds,
        "files_touched": files_count,
        "outcome_badge": detail.get("outcome_badge"),
        "heuristic_effort": round(heuristic_effort, 3),
    }


def _extract_task_context(messages: list[dict]) -> str:
    """Extract the user's task from the first user message + refinements."""
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") == "user":
            text = get_message_text(msg)
            if text:
                parts.append(text)
            if len(parts) >= 3:
                break
    return "\n".join(parts) if parts else "(no user message)"


def _format_metrics_line(metrics: dict) -> str:
    """Format basic metrics as a compact one-liner."""
    parts = []
    parts.append(f"Steps: {metrics.get('total_steps', 0)}")
    failures = metrics.get("tool_failures", 0)
    if failures:
        parts.append(f"Tool failures: {failures}")
    in_tok = metrics.get("input_tokens", 0)
    out_tok = metrics.get("output_tokens", 0)
    if in_tok or out_tok:
        parts.append(f"Tokens: {in_tok} in / {out_tok} out")
    dur = metrics.get("duration_seconds")
    if dur and isinstance(dur, (int, float)):
        minutes = int(dur) // 60
        parts.append(f"Duration: {minutes}m" if minutes else f"Duration: {int(dur)}s")
    files = metrics.get("files_touched", 0)
    if files:
        parts.append(f"Files: {files}")
    badge = metrics.get("outcome_badge")
    if badge:
        parts.append(f"Outcome: {badge}")
    effort = metrics.get("heuristic_effort")
    if effort is not None:
        parts.append(f"Heuristic effort: {effort:.2f}")
    return " | ".join(parts)


def format_session_for_judge(
    segments: list[Segment],
    task_context: str,
    metrics: dict | None = None,
) -> str:
    """Format the full session for a single judge call."""
    lines: list[str] = []

    lines.append("## User's Task")
    lines.append(task_context)
    lines.append("")

    if metrics:
        lines.append("## Session Metrics")
        lines.append(_format_metrics_line(metrics))
        lines.append("")

    if len(segments) == 1:
        seg = segments[0]
        lines.append(f"## Agent Work ({len(seg.steps)} steps)")
        for i, step in enumerate(seg.steps, 1):
            plan_text = _truncate(step.plan, 200) if step.plan else ""
            if plan_text:
                lines.append(f"Step {i}: {plan_text}")
            else:
                lines.append(f"Step {i}:")
            input_text = _truncate(step.action_input, 150)
            lines.append(f" → {step.action_tool}({input_text})")
            result_text = _truncate(step.result_output, 300)
            lines.append(f" → {step.result_status}: {result_text}")
            reflect_text = _truncate(step.reflect, 300) if step.reflect else ""
            if reflect_text:
                lines.append(f" → assistant reflection: {reflect_text}")
        lines.append("")

        lines.append("## User Response After Agent Work")
        if seg.user_response:
            lines.append(f'"{_truncate(seg.user_response, 500)}"')
        else:
            lines.append("No response — session ended")
        lines.append("")
    else:
        for idx, seg in enumerate(segments):
            lines.append(f"## Turn {idx + 1}: User")
            lines.append(_truncate(seg.user_message, 300))
            lines.append("")

            if seg.steps:
                lines.append(f"## Turn {idx + 1}: Agent Work ({len(seg.steps)} steps)")
                for i, step in enumerate(seg.steps, 1):
                    plan_text = _truncate(step.plan, 200) if step.plan else ""
                    if plan_text:
                        lines.append(f"Step {i}: {plan_text}")
                    else:
                        lines.append(f"Step {i}:")
                    input_text = _truncate(step.action_input, 150)
                    lines.append(f" → {step.action_tool}({input_text})")
                    result_text = _truncate(step.result_output, 300)
                    lines.append(f" → {step.result_status}: {result_text}")
                    reflect_text = _truncate(step.reflect, 300) if step.reflect else ""
                    if reflect_text:
                        lines.append(f" → assistant reflection: {reflect_text}")
                lines.append("")

            if seg.user_response:
                lines.append(f"## Turn {idx + 1}: User Response")
                lines.append(f'"{_truncate(seg.user_response, 500)}"')
                lines.append("")

        # Show final state
        last_seg = segments[-1]
        if not last_seg.user_response:
            lines.append("## Session End")
            lines.append("No final user response — session ended")
            lines.append("")

    lines.append("## Respond with JSON:")
    lines.append('{"substance": N, "ai_quality_score": N, "ai_failure_value_score": N, "ai_recovery_labels": [...], "ai_failure_attribution": "...", "ai_failure_modes": [...], "ai_meta_labels": [...], "ai_failure_evidence": [...], "ai_learning_summary": "...", "reasoning": "...", "resolution": "resolved|partial|failed|abandoned|exploratory|trivial", "display_title": "...", "summary": "...", "effort_estimate": 0.0-1.0, "task_type": "...", "session_tags": [...], "privacy_flags": [...], "project_areas": [...]}')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Judge: call LLM with rubric
# ---------------------------------------------------------------------------

_RUBRIC_SEARCH_PATHS = [
    PROMPTS_DIR / "scoring" / "rubric.md",
    # Legacy fallback — kept until all deployments have the new layout.
    Path(__file__).parent.parent / "skills" / "clawjournal-score" / "RUBRIC.md",
]

_FALLBACK_RUBRIC = """\
Score this coding agent session for productivity and failure value. \
Return JSON with substance, ai_quality_score, ai_failure_value_score, \
ai_recovery_labels, ai_failure_attribution, ai_failure_modes, ai_meta_labels, \
ai_failure_evidence, ai_learning_summary, reasoning, display_title, summary, \
resolution, effort_estimate, task_type, session_tags, privacy_flags, and \
project_areas fields."""


def _looks_like_rubric_redirect_stub(text: str) -> bool:
    """Return True for short redirect stubs that point at the canonical rubric."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("<!-- Canonical location:"):
        return True
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) <= 4 and any(
        path in line
        for line in lines
        for path in (
            "clawjournal/prompts/agents/scoring/rubric.md",
            "prompts/agents/scoring/rubric.md",
        )
    ):
        return True
    return False


def load_scoring_rubric() -> str:
    """Load the scoring rubric from the canonical prompt copy."""
    for path in _RUBRIC_SEARCH_PATHS:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text.startswith("---"):
                try:
                    end = text.index("---", 3)
                    text = text[end + 3:].strip()
                except ValueError:
                    pass
            if _looks_like_rubric_redirect_stub(text):
                continue
            return text
    return _FALLBACK_RUBRIC


JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "substance": {"type": "integer", "minimum": 1, "maximum": 5},
        "ai_quality_score": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "Legacy productivity/substance score, kept for compatibility.",
        },
        "ai_failure_value_score": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "Value of this trace for understanding frontier-agent failure behavior.",
        },
        "ai_recovery_labels": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["self_recovered", "user_corrected_recovery", "unrecovered", "blocked"],
            },
        },
        "ai_failure_attribution": {
            "type": "string",
            "enum": ["agent_caused", "environment", "preexisting_problem", "user_redirect", "unclear", ""],
        },
        "ai_failure_modes": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "task_framing",
                    "method_selection",
                    "context_handling",
                    "execution_error",
                    "reasoning_fabrication",
                    "revision_failure",
                    "verification_skipped",
                    "deliverable_defect",
                    "communication_error",
                    "collaboration_error",
                    "safety_security",
                    "efficiency_waste",
                ],
            },
        },
        "ai_meta_labels": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["evaluation_measurement"],
            },
            "description": "Optional failures in the measurement system itself (not the agent). Usually empty.",
        },
        "ai_failure_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short evidence snippets or paraphrases supporting the failure-value label.",
        },
        "ai_learning_summary": {
            "type": "string",
            "description": "One concise sentence naming the lesson this failure trace teaches.",
        },
        "reasoning": {"type": "string"},
        "display_title": {
            "type": "string",
            "description": (
                "A concise human-readable title (under 60 chars) summarizing "
                "what the session accomplished. Use imperative mood "
                "(e.g. 'Fix auth tests', 'Add pagination to /users'). "
                "For trivial sessions use a short description like "
                "'Slash command with no task'."
            ),
        },
        "summary": {
            "type": "string",
            "description": (
                "1-3 sentence summary of what happened and the outcome. "
                "Focus on what was done and what resulted."
            ),
        },
        "resolution": {
            "type": "string",
            "description": (
                "One of: resolved, partial, failed, abandoned, exploratory, trivial"
            ),
        },
        "effort_estimate": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": (
                "Override the heuristic effort estimate (0.0-1.0) only if misleading. "
                "Otherwise return the heuristic value from metadata."
            ),
        },
        "task_type": {
            "type": "string",
            "description": "A short snake_case label for the primary task type",
        },
        "session_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Zero or more snake_case tags for organizing and searching",
        },
        "privacy_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Zero or more snake_case privacy/sensitivity flags",
        },
        "project_areas": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Zero or more directory paths that were the focus of work",
        },
    },
    "required": [
        "substance", "ai_quality_score", "ai_failure_value_score",
        "ai_recovery_labels", "ai_failure_attribution", "ai_failure_modes",
        "ai_meta_labels", "ai_failure_evidence", "ai_learning_summary",
        "reasoning", "display_title", "summary", "resolution", "effort_estimate",
        "task_type", "session_tags", "privacy_flags", "project_areas",
    ],
}

# Backward compat: old schema used "quality" key — _validate_judge_result handles both


_SCORER_PROMPT_FILE = PROMPTS_DIR / "scoring" / "system.md"

# Backward-compat aliases — cli.py imports SCORING_BACKEND_CHOICES
SUPPORTED_SCORING_BACKENDS = SUPPORTED_BACKENDS
SCORING_BACKEND_CHOICES = BACKEND_CHOICES
SCORING_BACKEND_COMMANDS = BACKEND_COMMANDS
SCORING_BACKEND_ENV_MARKERS = BACKEND_ENV_MARKERS
SCORING_BACKEND_COMMAND_ALIASES = BACKEND_COMMAND_ALIASES


_SCORE_TASK_PROMPT = (
    "Score the coding agent session in the current directory for trace management. "
    "Read judge_input.md for the condensed transcript, session.json for compact session metadata, "
    "metadata.json for derived metrics, and RUBRIC.md for the rubric. "
    "Write scoring.json with your assessment (substance, resolution, summary, etc.)."
)

_SCORE_TASK_PROMPT_CODEX = (
    "Score the coding agent session in the current directory for trace management. "
    "Read judge_input.md for the condensed transcript, session.json for compact session metadata, "
    "metadata.json for derived metrics, and RUBRIC.md for the rubric. "
    "Return only a JSON object matching the provided schema."
)


def _truncate_command_list(raw: Any, *, limit: int = 20, max_chars: int = 240) -> list[str]:
    """Normalize a command list to a compact bounded list of strings."""
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        result.append(_truncate(text, max_chars))
        if len(result) >= limit:
            break
    return result


def _truncate_path_list(raw: Any, *, limit: int = 20, max_chars: int = 160) -> list[str]:
    """Normalize a path list to a compact bounded list of strings."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        result.append(_truncate(text, max_chars))
        if len(result) >= limit:
            break
    return result


def _build_session_payload_for_judge(session_data: dict[str, Any]) -> dict[str, Any]:
    """Keep only compact metadata for the judge-side session.json file.

    The transcript content already lives in judge_input.md. Duplicating the full
    message blob here makes large sessions much slower to score and can push the
    Codex judge over its timeout budget.
    """
    payload: dict[str, Any] = {}
    keep_keys = (
        "session_id",
        "project",
        "source",
        "model",
        "display_title",
        "task_type",
        "start_time",
        "end_time",
        "duration_seconds",
        "git_branch",
        "user_messages",
        "assistant_messages",
        "tool_uses",
        "input_tokens",
        "output_tokens",
        "review_status",
        "estimated_cost_usd",
        "outcome_label",
        "value_labels",
        "risk_level",
        "client_origin",
        "runtime_channel",
        "outer_session_id",
        "tool_counts",
    )
    for key in keep_keys:
        value = session_data.get(key)
        if value is None:
            continue
        payload[key] = value

    files_touched = _truncate_path_list(session_data.get("files_touched"))
    if files_touched:
        payload["files_touched"] = files_touched

    commands_run = _truncate_command_list(session_data.get("commands_run"))
    if commands_run:
        payload["commands_run"] = commands_run

    return payload


def _write_agent_inputs(
    tmp_path: Path,
    *,
    prompt_text: str,
    session_data: dict[str, Any],
    metadata: dict[str, Any],
    rubric: str,
) -> None:
    """Write the judge inputs that backend CLIs can inspect."""
    (tmp_path / "judge_input.md").write_text(prompt_text, encoding="utf-8")
    (tmp_path / "session.json").write_text(
        json.dumps(_build_session_payload_for_judge(session_data), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "RUBRIC.md").write_text(rubric, encoding="utf-8")


def _rubric_revision(rubric: str) -> str:
    """Return the git commit for the active rubric, or a content hash fallback."""
    rubric_path = PROMPTS_DIR / "scoring" / "rubric.md"
    repo_root = Path(__file__).resolve().parents[2]
    try:
        rubric_path_arg = str(rubric_path.relative_to(repo_root))
    except ValueError:
        rubric_path_arg = str(rubric_path)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-n", "1", "--format=%H", "--", rubric_path_arg],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        proc = None
    if proc and proc.returncode == 0:
        sha = proc.stdout.strip()
        if sha:
            return sha
    return "sha256:" + hashlib.sha256(rubric.encode("utf-8")).hexdigest()


def _attach_scorer_metadata(
    result: dict[str, Any],
    *,
    backend: str,
    model: str | None,
    rubric: str,
) -> dict[str, Any]:
    """Attach provenance fields after validation."""
    result["_scorer_backend"] = backend
    result["_scorer_model"] = model or ""
    result["_rubric_git_sha"] = _rubric_revision(rubric)
    result["_scored_at"] = datetime.now(timezone.utc).isoformat()
    return result


def _extract_json_candidate_strings(value: Any) -> list[str]:
    """Collect string candidates that may contain a JSON judge result."""
    candidates: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if text:
            candidates.append(text)
            # Stdout-only backends (OpenClaw / Hermes) may wrap the JSON in
            # markdown fences or surround it with prose despite instructions
            # not to. Fall back to the outermost {...} span so the parse
            # survives that.
            start, end = text.find("{"), text.rfind("}")
            if 0 <= start < end:
                span = text[start:end + 1]
                if span != text:
                    candidates.append(span)
    elif isinstance(value, dict):
        priority_keys = (
            "text", "message", "result", "reply", "output", "content",
            "assistant", "response",
        )
        for key in priority_keys:
            if key in value:
                candidates.extend(_extract_json_candidate_strings(value[key]))
        for nested in value.values():
            candidates.extend(_extract_json_candidate_strings(nested))
    elif isinstance(value, list):
        for item in value:
            candidates.extend(_extract_json_candidate_strings(item))
    return candidates


def _looks_like_judge_result(d: dict) -> bool:
    """Check if a dict looks like a judge result (new or old schema).

    Requires 'reasoning' plus the primary score key, plus at least one
    classification key to reduce false positives from session data that
    happens to contain scoring-like keys.
    """
    if not isinstance(d, dict) or "reasoning" not in d:
        return False
    has_score = "substance" in d or "quality" in d or "ai_quality_score" in d
    has_classification = "task_type" in d or "display_title" in d
    return has_score and has_classification


_REQUIRED_FAILURE_FIELDS = (
    "ai_failure_value_score",
    "ai_recovery_labels",
    "ai_failure_attribution",
    "ai_failure_modes",
    "ai_failure_evidence",
    "ai_learning_summary",
)


def _validate_backend_judge_result(result: dict[str, Any]) -> dict[str, Any]:
    """Validate a live backend result and require the failure-value schema."""
    validated = _validate_judge_result(result)
    missing = [field for field in _REQUIRED_FAILURE_FIELDS if field not in result]
    if missing or validated["ai_failure_value_score"] is None:
        details = ", ".join(missing or ["ai_failure_value_score"])
        raise RuntimeError(
            "Judge result missing required failure-value fields: "
            f"{details}. Re-run with the current scoring rubric."
        )
    return validated


def _extract_judge_result_from_value(
    value: Any,
    *,
    require_failure_fields: bool = False,
) -> dict[str, Any]:
    """Find and validate a judge result inside a backend response payload."""
    if isinstance(value, dict) and _looks_like_judge_result(value):
        return (
            _validate_backend_judge_result(value)
            if require_failure_fields
            else _validate_judge_result(value)
        )

    for candidate in _extract_json_candidate_strings(value):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and _looks_like_judge_result(parsed):
            return (
                _validate_backend_judge_result(parsed)
                if require_failure_fields
                else _validate_judge_result(parsed)
            )

    raise RuntimeError("Backend response did not contain a valid JSON judge result")


def _read_scoring_output(result: AgentResult, backend: str) -> dict:
    """Read and validate judge output from an AgentResult."""
    scoring_path = result.cwd / "scoring.json"

    # Claude / Codex: read from file
    if scoring_path.exists():
        try:
            parsed = json.loads(scoring_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise RuntimeError("scoring.json is not valid JSON")
        if isinstance(parsed, dict):
            return _validate_backend_judge_result(parsed)
        raise RuntimeError("scoring.json does not contain a JSON object")

    # OpenClaw / Hermes: parse from stdout
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"{backend} did not produce scoring output")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return _extract_judge_result_from_value(stdout, require_failure_fields=True)

    return _extract_judge_result_from_value(payload, require_failure_fields=True)


def call_judge(
    prompt_text: str,
    model: str | None = None,
    *,
    session_data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    backend: str = "auto",
) -> dict:
    """Call the resolved scoring backend and return a validated judge result."""
    rubric = load_scoring_rubric()
    resolved = resolve_backend(backend)
    effective_model = resolve_model_for_backend(resolved, model)
    session_payload = session_data or {}
    metadata_payload = metadata or {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_agent_inputs(
            tmp_path,
            prompt_text=prompt_text,
            session_data=session_payload,
            metadata=metadata_payload,
            rubric=rubric,
        )

        # Build backend-specific messages for agents that do not consume
        # Claude-style system prompts or Codex structured-output files.
        file_based_msg = None
        if resolved in ("openclaw", "hermes"):
            file_based_msg = (
                "Score the coding agent session using the files below.\n\n"
                f"Read these absolute paths:\n"
                f"- {tmp_path / 'judge_input.md'}\n"
                f"- {tmp_path / 'session.json'}\n"
                f"- {tmp_path / 'metadata.json'}\n"
                f"- {tmp_path / 'RUBRIC.md'}\n\n"
                "Return only a JSON object matching the scoring schema used in the rubric. "
                "Do not wrap it in markdown fences."
            )

        # Codex uses structured output; Hermes/OpenClaw get absolute paths.
        if resolved == "codex":
            task_prompt = _SCORE_TASK_PROMPT_CODEX
        elif resolved == "hermes" and file_based_msg is not None:
            task_prompt = file_based_msg
        else:
            task_prompt = _SCORE_TASK_PROMPT

        result = run_default_agent_task(
            backend=resolved,
            cwd=tmp_path,
            system_prompt_file=_SCORER_PROMPT_FILE,
            task_prompt=task_prompt,
            model=effective_model,
            timeout_seconds=120,
            codex_sandbox="read-only",
            codex_output_schema=JUDGE_SCHEMA,
            codex_output_file="scoring.json",
            openclaw_message=file_based_msg if resolved == "openclaw" else None,
        )

        parsed = _read_scoring_output(result, resolved)
        return _attach_scorer_metadata(
            parsed,
            backend=resolved,
            model=effective_model,
            rubric=rubric,
        )


def _normalize_snake_case(s: str) -> str:
    """Normalize a string to snake_case."""
    return s.strip().lower().replace(" ", "_").replace("-", "_")


def _validate_snake_list(raw: Any) -> list[str]:
    """Validate and normalize a list of snake_case strings."""
    if not isinstance(raw, list):
        return []
    return [
        _normalize_snake_case(v)
        for v in raw
        if isinstance(v, str) and v.strip()
    ]


def _validate_bounded_string_list(raw: Any, *, limit: int = 8, max_chars: int = 220) -> list[str]:
    """Return a short list of clean strings for display-only judge fields."""
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if not text:
            continue
        values.append(text[:max_chars])
        if len(values) >= limit:
            break
    return values


_VALID_RECOVERY_LABELS = {
    "self_recovered",
    "user_corrected_recovery",
    "unrecovered",
    "blocked",
}

_VALID_FAILURE_ATTRIBUTIONS = {
    "agent_caused",
    "environment",
    "preexisting_problem",
    "user_redirect",
    "unclear",
}

_VALID_FAILURE_MODES = {
    "task_framing",
    "method_selection",
    "context_handling",
    "execution_error",
    "reasoning_fabrication",
    "revision_failure",
    "verification_skipped",
    "deliverable_defect",
    "communication_error",
    "collaboration_error",
    "safety_security",
    "efficiency_waste",
}

_VALID_META_LABELS = {
    "evaluation_measurement",
}


def _validate_enum_list(raw: Any, valid_values: set[str]) -> list[str]:
    """Validate and normalize a judge-emitted enum list, preserving order."""
    values: list[str] = []
    seen: set[str] = set()
    for item in _validate_snake_list(raw):
        if item in valid_values and item not in seen:
            values.append(item)
            seen.add(item)
    return values


_VALID_RESOLUTIONS = {"resolved", "partial", "failed", "abandoned", "exploratory", "trivial"}

# Old-schema outcome_label values that predate the six-value resolution set.
# Judges (and their prompts) from before the schema migration returned
# tool-output-derived badges. Translate them into the nearest
# new-schema resolution so backward-compatible judge outputs don't
# silently map to "" and lose their signal. Mirrors the SQL
# normalization in workbench/index.py._OUTCOME_NORMALIZE_SQL.
_LEGACY_RESOLUTION_MAP = {
    "tests_passed": "resolved",
    "completed": "resolved",
    "tests_failed": "failed",
    "build_failed": "failed",
    "errored": "failed",
    "analysis_only": "exploratory",
}


def _validate_judge_result(result: dict) -> dict:
    """Parse judge result safely. Handles both new (substance) and old (quality) schemas."""
    # Support the new explicit ai_quality_score, current "substance", and
    # older "quality" key. Keep returning substance for existing callers.
    if "ai_quality_score" in result:
        substance = result.get("ai_quality_score")
    elif "substance" in result:
        substance = result.get("substance")
    else:
        substance = result.get("quality")
    if not isinstance(substance, int) or not (1 <= substance <= 5):
        substance = 3  # safety net: invalid defaults to middle

    failure_value = result.get("ai_failure_value_score")
    if isinstance(failure_value, int) and 1 <= failure_value <= 5:
        failure_value_score = failure_value
    else:
        failure_value_score = None

    recovery_labels = _validate_enum_list(
        result.get("ai_recovery_labels", []),
        _VALID_RECOVERY_LABELS,
    )

    failure_attribution = result.get("ai_failure_attribution", "")
    if isinstance(failure_attribution, str):
        failure_attribution = _normalize_snake_case(failure_attribution)
    else:
        failure_attribution = ""
    if failure_attribution not in _VALID_FAILURE_ATTRIBUTIONS:
        failure_attribution = ""

    failure_modes = _validate_enum_list(
        result.get("ai_failure_modes", []),
        _VALID_FAILURE_MODES,
    )
    meta_labels = _validate_enum_list(
        result.get("ai_meta_labels", []),
        _VALID_META_LABELS,
    )
    failure_evidence = _validate_bounded_string_list(result.get("ai_failure_evidence", []))
    if failure_value_score is not None and failure_value_score >= 4 and not failure_evidence:
        failure_value_score = 3

    learning_summary = result.get("ai_learning_summary", "")
    if not isinstance(learning_summary, str):
        learning_summary = ""
    learning_summary = " ".join(learning_summary.split())[:500]

    # Resolution (new) or fall back from old outcome_label. The judge
    # sometimes returns values outside _VALID_RESOLUTIONS (historically
    # `unknown`, typos, or old-schema labels like `completed`). Those
    # used to be stored verbatim and leaked onto the dashboard as a
    # separate bucket. Now: anything outside the valid set is coerced
    # to an empty string, which the persist path treats as "no label"
    # so the sessions fall back to the heuristic badge until rescored.
    resolution = result.get("resolution") if "resolution" in result else result.get("outcome_label", "")
    if not isinstance(resolution, str) or not resolution.strip():
        resolution = ""
    else:
        resolution = _normalize_snake_case(resolution)
        if resolution not in _VALID_RESOLUTIONS:
            # Translate old-schema tool-output labels; drop anything else.
            resolution = _LEGACY_RESOLUTION_MAP.get(resolution, "")

    # Summary (new field, may be absent in old schema)
    summary = result.get("summary", "")
    if not isinstance(summary, str):
        summary = ""

    # Effort estimate (new field, may be absent). Use None sentinel so
    # score_session can fall back to the heuristic when the judge omits it.
    effort_estimate = result.get("effort_estimate")
    if effort_estimate is not None and isinstance(effort_estimate, (int, float)):
        effort_estimate = max(0.0, min(1.0, float(effort_estimate)))
    else:
        effort_estimate = None

    # Classification fields — normalize to snake_case strings
    task_type = result.get("task_type", "unknown")
    if not isinstance(task_type, str) or not task_type.strip():
        task_type = "unknown"
    task_type = _normalize_snake_case(task_type)

    # Session tags (new) or value_labels (old)
    raw_tags = result.get("session_tags") if "session_tags" in result else result.get("value_labels", [])
    session_tags = _validate_snake_list(raw_tags)

    # Privacy flags (new) or risk_level (old)
    raw_flags = result.get("privacy_flags") if "privacy_flags" in result else result.get("risk_level", [])
    privacy_flags = _validate_snake_list(raw_flags)

    # Project areas (new, may be absent)
    project_areas = result.get("project_areas", [])
    if not isinstance(project_areas, list):
        project_areas = []
    project_areas = [
        a.strip() for a in project_areas
        if isinstance(a, str) and a.strip()
    ]

    display_title = result.get("display_title", "")
    if not isinstance(display_title, str):
        display_title = ""
    display_title = display_title.strip()[:80]

    return {
        "substance": substance,
        "ai_quality_score": substance,
        "ai_failure_value_score": failure_value_score,
        "ai_recovery_labels": recovery_labels,
        "ai_failure_attribution": failure_attribution,
        "ai_failure_modes": failure_modes,
        "ai_meta_labels": meta_labels,
        "ai_failure_evidence": failure_evidence,
        "ai_learning_summary": learning_summary,
        "reasoning": str(result.get("reasoning", "")),
        "display_title": display_title,
        "summary": summary,
        "resolution": resolution,
        "effort_estimate": effort_estimate,
        "task_type": task_type,
        "session_tags": session_tags,
        "privacy_flags": privacy_flags,
        "project_areas": project_areas,
        "_scorer_backend": result.get("_scorer_backend", ""),
        "_scorer_model": result.get("_scorer_model", ""),
        "_rubric_git_sha": result.get("_rubric_git_sha", ""),
        "_scored_at": result.get("_scored_at", ""),
    }


# ---------------------------------------------------------------------------
# Top-level: score_session
# ---------------------------------------------------------------------------

def _redact_blocked_domains(text: str, blocked_domains: list[str]) -> str:
    """Apply the same simple domain-blocking semantics used by share/export."""
    if not text or not blocked_domains:
        return text
    try:
        from ..workbench.index import _compile_blocked_domain_pattern
    except Exception:
        return text
    redacted = text
    for domain in blocked_domains:
        pattern = _compile_blocked_domain_pattern(domain)
        if pattern is not None:
            redacted = pattern.sub("[REDACTED_DOMAIN]", redacted)
    return redacted


def _redact_custom_for_scoring(
    value: Any,
    *,
    custom_strings: list[str],
    blocked_domains: list[str],
) -> Any:
    if isinstance(value, str):
        from ..redaction.secrets import redact_custom_strings
        redacted, _count = redact_custom_strings(value, custom_strings)
        return _redact_blocked_domains(redacted, blocked_domains)
    if isinstance(value, list):
        return [
            _redact_custom_for_scoring(
                v, custom_strings=custom_strings, blocked_domains=blocked_domains)
            for v in value
        ]
    if isinstance(value, dict):
        return {
            k: _redact_custom_for_scoring(
                v, custom_strings=custom_strings, blocked_domains=blocked_domains)
            for k, v in value.items()
        }
    return value


def _anonymize_for_scoring(
    detail: dict[str, Any], messages: list[dict[str, Any]],
    redaction_settings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Scrub locally configured sensitive strings before sending to the judge.

    Home paths/usernames are always anonymized. Callers that have a DB handle can
    pass the same effective share/export settings (config + workbench policies)
    so custom redactions and blocked-domain policies apply to scoring egress too.
    Returns fresh objects so callers can't mutate the DB-backed copy.
    """
    from ..config import load_config
    from ..redaction.anonymizer import Anonymizer

    if redaction_settings is None:
        try:
            config = load_config()
        except Exception:
            config = {}
        if not isinstance(config, dict):
            config = {}
        extra = list(config.get("redact_usernames", []) or [])
        custom_strings = list(config.get("redact_strings", []) or [])
        blocked_domains: list[str] = []
    else:
        extra = list(redaction_settings.get("extra_usernames", []) or [])
        custom_strings = list(redaction_settings.get("custom_strings", []) or [])
        blocked_domains = list(redaction_settings.get("blocked_domains", []) or [])

    anonymizer = Anonymizer(extra_usernames=extra)

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            anonymized = anonymizer.text(value)
            return _redact_custom_for_scoring(
                anonymized,
                custom_strings=custom_strings,
                blocked_domains=blocked_domains,
            )
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        return value

    new_detail = dict(detail)
    for field in ("display_title", "project", "git_branch"):
        val = new_detail.get(field)
        if isinstance(val, str):
            new_detail[field] = scrub(val)

    new_messages: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            new_messages.append(msg)
            continue
        m = dict(msg)
        for text_field in ("content", "thinking"):
            v = m.get(text_field)
            if isinstance(v, str):
                m[text_field] = scrub(v)
        if isinstance(m.get("tool_uses"), list):
            m["tool_uses"] = [
                {**tu, **{f: scrub(tu.get(f)) for f in ("input", "output") if f in tu}}
                if isinstance(tu, dict)
                else tu
                for tu in m["tool_uses"]
            ]
        new_messages.append(m)

    new_detail["messages"] = new_messages
    return new_detail, new_messages


def score_session(
    conn: Any,
    session_id: str,
    *,
    model: str | None = None,
    backend: str = "auto",
    redaction_settings: dict[str, Any] | None = None,
) -> ScoringResult:
    """Score a session: format → judge → store. No aggregation formulas."""
    from ..workbench.index import BLOBS_DIR, get_session_detail

    detail = get_session_detail(conn, session_id)
    if not detail:
        return ScoringResult(
            segments=[],
            quality=1,
            reason="Session not found",
            failure_value_score=1,
            learning_summary="Session not found.",
        )

    messages = detail.get("messages", [])
    blob_path_str = detail.get("blob_path")
    blob_path = Path(blob_path_str) if isinstance(blob_path_str, str) and blob_path_str else None
    if blob_path and not blob_path.exists():
        fallback = BLOBS_DIR / f"{session_id}.json"
        if fallback.exists():
            blob_path = fallback

    if blob_path is None or not blob_path.exists():
        raise RuntimeError(
            "Session transcript is unavailable. Re-run `clawjournal scan` to rebuild the index."
        )

    # Distinguish legitimately empty sessions from missing/corrupt blobs so we
    # do not persist a false 1/5 score for broken index state.
    if not messages:
        try:
            blob_data = json.loads(blob_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(
                "Session transcript is unreadable. Re-run `clawjournal scan` to rebuild the index."
            ) from exc
        raw_messages = blob_data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise RuntimeError(
                "Session transcript is invalid. Re-run `clawjournal scan` to rebuild the index."
            )
        messages = raw_messages
        detail["messages"] = messages

    # Blobs hold raw content since the security refactor. Anonymize
    # home-dir paths and usernames before handing anything to the judge —
    # the judge may be a cloud backend (Anthropic API / Codex / etc.).
    detail, messages = _anonymize_for_scoring(detail, messages, redaction_settings)

    # Format: parse into turns
    segments = segment_session(messages)
    if not segments:
        return ScoringResult(
            segments=[],
            quality=1,
            reason="No scorable content",
            failure_value_score=1,
            learning_summary="No scorable content.",
        )

    metrics = compute_basic_metrics(segments, detail)
    total_steps = metrics["total_steps"]

    if total_steps == 0:
        return ScoringResult(
            segments=segments,
            quality=1,
            reason="No tool usage",
            failure_value_score=1,
            learning_summary="No useful failure signal: no tool usage.",
        )

    # Judge: LLM scores holistically
    task_context = _extract_task_context(messages)
    prompt = format_session_for_judge(segments, task_context, metrics)

    result = call_judge(
        prompt,
        model,
        session_data=detail,
        metadata=metrics,
        backend=backend,
    )

    # Effort: use judge override if provided, otherwise heuristic
    effort_estimate = result["effort_estimate"]
    if effort_estimate is None:
        effort_estimate = metrics.get("heuristic_effort", 0.0)

    # Store: pass through judge result, no formulas
    detail_data = {
        "substance": result["substance"],
        "resolution": result["resolution"],
        "reasoning": result["reasoning"],
        "display_title": result["display_title"],
        "summary": result.get("summary", ""),
        "effort_estimate": effort_estimate,
        "metrics": metrics,
        "task_type": result["task_type"],
        "session_tags": result["session_tags"],
        "privacy_flags": result["privacy_flags"],
        "project_areas": result.get("project_areas", []),
        "ai_failure_value_score": result.get("ai_failure_value_score"),
        "ai_recovery_labels": result.get("ai_recovery_labels", []),
        "ai_failure_attribution": result.get("ai_failure_attribution", ""),
        "ai_failure_modes": result.get("ai_failure_modes", []),
        "ai_meta_labels": result.get("ai_meta_labels", []),
        "ai_failure_evidence": result.get("ai_failure_evidence", []),
        "ai_learning_summary": result.get("ai_learning_summary", ""),
        "ai_scorer_backend": result.get("_scorer_backend", ""),
        "ai_scorer_model": result.get("_scorer_model", ""),
        "ai_rubric_git_sha": result.get("_rubric_git_sha", ""),
        "ai_scored_at": result.get("_scored_at", ""),
    }

    return ScoringResult(
        segments=segments,
        quality=result["substance"],
        reason=result["reasoning"],
        display_title=result["display_title"],
        summary=result.get("summary", ""),
        task_type=result["task_type"],
        outcome_label=result["resolution"],
        value_labels=result["session_tags"],
        risk_level=result["privacy_flags"],
        effort_estimate=effort_estimate,
        project_areas=result.get("project_areas", []),
        detail_json=json.dumps(detail_data),
        failure_value_score=result.get("ai_failure_value_score"),
        recovery_labels=result.get("ai_recovery_labels", []),
        failure_attribution=result.get("ai_failure_attribution", ""),
        failure_modes=result.get("ai_failure_modes", []),
        learning_summary=result.get("ai_learning_summary", ""),
        scorer_backend=result.get("_scorer_backend", ""),
        scorer_model=result.get("_scorer_model", ""),
        rubric_git_sha=result.get("_rubric_git_sha", ""),
        scored_at=result.get("_scored_at", ""),
    )
