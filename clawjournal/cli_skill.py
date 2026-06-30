"""``clawjournal skill`` — distill + install the local ``clawjournal-lessons`` skill.

Mode A end to end: select top candidates from the user's own scored sessions ->
one local distill call -> deterministic gate -> preview -> install for Claude Code
and Codex. The core (``generate_skill``) is pure and testable with a fake caller;
``run_skill`` owns IO (preview / confirm / install).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .skill import distill as _distill
from .skill import install as _install
from .skill import render as _render
from .skill import select as _select
from .skill.schema import SkillRule

# A wide window approximates "all history" for the first run.
ALL_HISTORY_DAYS = 3650


@dataclass
class SkillResult:
    rules: list[SkillRule]
    skill_md: str
    region: str
    blocked: list[tuple[SkillRule, list[str]]]
    gate_issues: list[str]
    corpus: _select.SkillCorpus
    meta: dict[str, Any]


def generate_skill(conn, *, window_days: int, backend: str = "auto",
                   model: str | None = None, caller=None, now: datetime | None = None) -> SkillResult:
    """Pure pipeline: select -> distill -> gate -> render. No IO, no install."""
    corpus = _select.select_skill_candidates(conn, window_days=window_days, now=now)
    rules: list[SkillRule] = []
    blocked: list[tuple[SkillRule, list[str]]] = []
    gate_issues: list[str] = []
    skill_md = region = ""
    meta: dict[str, Any] = {
        "generated_at": (now or datetime.now(timezone.utc)).date().isoformat(),
        "window_days": window_days,
        "sources": len(corpus.session_ids),
    }
    if not corpus.is_empty():
        distilled = _distill.distill_skills(corpus, backend=backend, model=model, caller=caller)
        rules, blocked = _render.gate_rules(distilled)
        if rules:
            skill_md = _render.render_skill_md(rules, meta)
            region = _render.render_agents_region(rules, meta)
            gate_issues = _render.gate_rendered(skill_md)
    return SkillResult(rules, skill_md, region, blocked, gate_issues, corpus, meta)


# --- IO / CLI ---------------------------------------------------------------

def _print_preview(res: SkillResult) -> None:
    c = res.corpus
    print(
        f"\nScanned window: {c.total_failures} failure + {c.total_successes} success/recovery "
        f"candidate sessions.\n"
    )
    if not res.rules:
        if c.is_empty():
            print("No scored failure/success sessions found in the window.")
            print("Run `clawjournal scan` and let scoring run first (or widen with --all).")
        else:
            print("The distiller returned no usable rules this run.")
        return
    print(f"Distilled {len(res.rules)} skill(s):\n")
    for i, r in enumerate(res.rules, 1):
        tag = "AVOID" if r.kind == "avoid" else "DO"
        print(f"  {i}. [{tag}] {r.guidance}")
        if r.trigger:
            print(f"       when: {r.trigger}")
        if r.why:
            print(f"       why:  {r.why}")
    if res.blocked:
        print(f"\n  ({len(res.blocked)} rule(s) dropped by the safety hard-deny.)")
    if res.gate_issues:
        print(f"\n  ⚠ render-time gate found: {', '.join(res.gate_issues)} — install blocked.")


def run_skill(args) -> None:
    from .workbench.index import open_index

    window_days = ALL_HISTORY_DAYS if getattr(args, "all", False) else getattr(args, "window_days", 7)
    conn = open_index()
    try:
        res = generate_skill(
            conn, window_days=window_days,
            backend=getattr(args, "backend", "auto"), model=getattr(args, "model", None),
        )
    finally:
        conn.close()

    _print_preview(res)

    if not res.rules or res.gate_issues:
        sys.exit(0 if res.rules else 1)
    if getattr(args, "preview", False):
        print("\n(preview only — not installed; re-run without --preview to install.)")
        return

    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print("\nRe-run with --yes to install (or --preview to just look).")
            return
        ans = input(f"\nInstall these {len(res.rules)} skill(s) for Claude Code + Codex? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("Not installed.")
            return

    targets = getattr(args, "target", None) or ["claude", "codex"]
    installed: list[str] = []
    if "claude" in targets:
        installed.append(str(_install.install_claude(res.skill_md)))
    if "codex" in targets:
        installed.append(str(_install.install_codex(res.region)))
    print("\nInstalled:")
    for p in installed:
        print(f"  - {p}")
    print("\nYour agents will pick up the new lessons on their next session.")
