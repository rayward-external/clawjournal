"""Render a benchmark to the export kinds (authoring markdown + agent/grader packets).

Works on the **payload dict** (``schema.benchmark_to_dict`` / what ``store`` stores
and serves), so the CLI/API can render straight from storage without rebuilding
dataclasses. The agent-packet renderers go through the same field whitelist as
``schema.to_agent_packet`` so the answer key (trap, criteria, grounding) can never
leak into an agent-facing export.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from . import schema as bm

EXPORT_KINDS = (
    "authoring_md",
    "agent_packet_md",
    "grader_packet_md",
    "agent_packet_json",
    "grader_packet_json",
)


def _as_payload(benchmark: Any) -> dict[str, Any]:
    if isinstance(benchmark, bm.Benchmark):
        return bm.benchmark_to_dict(benchmark)
    if dataclasses.is_dataclass(benchmark):
        return dataclasses.asdict(benchmark)
    return dict(benchmark)


def _agent_task(task: dict[str, Any]) -> dict[str, Any]:
    """Whitelist agent-facing fields — mirrors schema.to_agent_packet, dict-side."""
    return {k: task.get(k) for k in bm.AGENT_PACKET_FIELDS}


def _meta_line(b: dict[str, Any]) -> str:
    return (
        f"window {b.get('window_start','?')} → {b.get('window_end','?')} · "
        f"generated {b.get('generated_at','?')} · backend {b.get('backend','?')} · "
        f"{b.get('n_tasks', len(b.get('tasks', [])))} tasks · {b.get('total_points','?')} pts"
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def render_markdown(benchmark: Any) -> str:
    """Full authoring document (overview + themes + tasks w/ both packets + scoring)."""
    b = _as_payload(benchmark)
    tasks = b.get("tasks", []) or []
    themes = b.get("themes", []) or []
    out: list[str] = []
    out.append("# Personalized benchmark")
    out.append("")
    out.append(_meta_line(b))
    if b.get("dropped_for_cost"):
        out.append(f"_coverage: {b.get('source_count', len(b.get('source_session_ids', [])))} sessions "
                   f"deep-read, {b['dropped_for_cost']} dropped for cost._")
    out.append("")
    out.append("> Stored locally · never shared. Tasks may pin private artifacts (see readiness).")
    out.append("")

    if themes:
        out.append("## Failure themes")
        out.append("")
        out.append("| Theme | Freq | Taxonomy | Lesson |")
        out.append("|---|---|---|---|")
        for t in themes:
            out.append(
                f"| {t.get('name','')} | {t.get('frequency','')} | "
                f"{', '.join(t.get('taxonomy', []) or [])} | {t.get('lesson','')} |"
            )
        out.append("")

    out.append("## Tasks")
    out.append("")
    by_theme: dict[str, list[dict]] = {}
    for task in tasks:
        by_theme.setdefault(task.get("theme", "(untyped)"), []).append(task)
    for theme, group in by_theme.items():
        out.append(f"### Theme: {theme}")
        out.append("")
        for task in group:
            out.append(f"#### {task.get('id','?')} — {task.get('title','')}")
            out.append("")
            out.append(
                f"`{'/'.join(task.get('domains', []) or ['—'])} · {task.get('difficulty','?')} · "
                f"{task.get('points','?')} pts · {task.get('grading','?')} · "
                f"readiness: {task.get('readiness','?')} · "
                f"agents: {', '.join(task.get('source_agents', []) or ['—'])} · "
                f"grounded: {', '.join(task.get('grounded_session_ids', []) or ['—'])}`"
            )
            out.append("")
            out.append("**Scenario.** " + (task.get("scenario") or ""))
            if task.get("seed_inputs"):
                out.append("")
                out.append("**Seed inputs.** " + task["seed_inputs"])
            out.append("")
            out.append("**The trap.** " + (task.get("the_trap") or ""))
            if task.get("ideal_trajectory"):
                out.append("")
                out.append("**Ideal trajectory.**")
                for i, step in enumerate(task["ideal_trajectory"], 1):
                    out.append(f"{i}. {step}")
            if task.get("pass_criteria"):
                out.append("")
                out.append("**Pass criteria.**")
                for c in task["pass_criteria"]:
                    out.append(f"- [ ] {c}")
            if task.get("fail_signals"):
                out.append("")
                out.append("**Fail signals.** " + "; ".join(task["fail_signals"]))
            crit = task.get("critique") or {}
            if crit.get("staging_notes"):
                out.append("")
                out.append(f"> ⚠️ staging: {crit['staging_notes']}")
            out.append("")

    out.append("## Scoring")
    out.append("")
    out.append("| Task | Theme | Readiness | Points |")
    out.append("|---|---|---|---|")
    for task in tasks:
        out.append(
            f"| {task.get('id','?')} | {task.get('theme','')} | "
            f"{task.get('readiness','?')} | {task.get('points','?')} |"
        )
    out.append(f"| **Total** | | | **{b.get('total_points','?')}** |")
    out.append("")
    return "\n".join(out)


def render_agent_packet_markdown(benchmark: Any) -> str:
    """Runnable prompts only — scenario + seed inputs, answer withheld."""
    b = _as_payload(benchmark)
    out = ["# Benchmark — agent packets", "", _meta_line(b),
           "", "> Run each agent against the scenario + seed inputs only. Grader material withheld.", ""]
    for task in b.get("tasks", []) or []:
        ap = _agent_task(task)
        out.append(f"## {ap.get('id','?')} — {ap.get('title','')}")
        out.append("")
        out.append("**Scenario.** " + (ap.get("scenario") or ""))
        if ap.get("seed_inputs"):
            out.append("")
            out.append("**Seed inputs.** " + ap["seed_inputs"])
        out.append("")
    return "\n".join(out)


def render_grader_packet_markdown(benchmark: Any) -> str:
    """The answer key — identical to the authoring doc by design (it already carries
    the full trap/criteria/grounding), so MD and JSON grader packets stay in sync."""
    return render_markdown(benchmark)


# ---------------------------------------------------------------------------
# JSON packets
# ---------------------------------------------------------------------------
def agent_packet_dict(benchmark: Any) -> dict[str, Any]:
    b = _as_payload(benchmark)
    return {
        "window_start": b.get("window_start"),
        "window_end": b.get("window_end"),
        "generated_at": b.get("generated_at"),
        "tasks": [_agent_task(t) for t in b.get("tasks", []) or []],
    }


def grader_packet_dict(benchmark: Any) -> dict[str, Any]:
    return _as_payload(benchmark)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def render(benchmark: Any, kind: str) -> str:
    """Render ``benchmark`` to the export ``kind`` (returns a string)."""
    if kind == "authoring_md":
        return render_markdown(benchmark)
    if kind == "agent_packet_md":
        return render_agent_packet_markdown(benchmark)
    if kind == "grader_packet_md":
        return render_grader_packet_markdown(benchmark)
    if kind == "agent_packet_json":
        return json.dumps(agent_packet_dict(benchmark), indent=2)
    if kind == "grader_packet_json":
        return json.dumps(grader_packet_dict(benchmark), indent=2)
    raise ValueError(f"unknown export kind {kind!r}; expected one of {EXPORT_KINDS}")
