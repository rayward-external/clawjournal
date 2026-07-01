"""``clawjournal skill`` — distill + install the local ``clawjournal-lessons`` skill.

Mode A end to end (§7): preflight -> scan/index -> score the window -> select
top candidates -> one local distill call -> merge into the durable set (replace
the weakest, skip rejected) -> deterministic gate -> preview the diff -> install
for Claude Code + Codex. The core (``generate_skill``) is pure/testable with a
fake caller and is read-only on the store; ``run_skill`` owns scan/score/IO.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .skill import distill as _distill
from .skill import install as _install
from .skill import render as _render
from .skill import select as _select
from .skill import store as _store
from .skill.schema import MAX_RULES, SkillRule
from .workbench.index import FAILURE_VALUE_SOURCE_SCOPE

# A wide window approximates "all history" for the first run.
ALL_HISTORY_DAYS = 3650
DEFAULT_SCORE_LIMIT = 25


@dataclass
class SkillResult:
    rules: list[SkillRule]          # the merged top-<=5 to install
    skill_md: str
    region: str
    blocked: list[tuple[SkillRule, list[str]]]
    gate_issues: list[str]
    corpus: _select.SkillCorpus
    meta: dict[str, Any]
    added_fps: set[str] = field(default_factory=set)
    dropped: list[SkillRule] = field(default_factory=list)
    # week-over-week: {failure_mode: (prev_rate_or_None, current_rate)} for targeted modes
    trend: dict[str, tuple[float | None, float]] = field(default_factory=dict)


def merge_rules(existing: list[SkillRule], new: list[SkillRule], rejected: set[str]) -> list[SkillRule]:
    """Merge existing + newly-distilled rules -> top-<=5 (replace the weakest).

    Deduped by fingerprint; rejected fingerprints dropped; ranked by support then
    recurred-this-run. **Interleaved across kinds** so the installed set keeps a
    good+bad mix (D2) — otherwise 'do' rules (whose support signal is weaker than
    'avoid' mode-recurrence) would be structurally crowded out over weeks.
    """
    new_fps = {_store.fingerprint(r) for r in new}
    pool: dict[str, SkillRule] = {}
    for r in list(existing) + list(new):  # new last -> refreshes support on ties
        fp = _store.fingerprint(r)
        if fp in rejected:
            continue
        if fp not in pool or r.support >= pool[fp].support:
            pool[fp] = r

    def _rank(rules: list[SkillRule]) -> list[SkillRule]:
        return sorted(rules, key=lambda r: (r.support, _store.fingerprint(r) in new_fps), reverse=True)

    avoid = _rank([r for r in pool.values() if r.kind == "avoid"])
    do = _rank([r for r in pool.values() if r.kind == "do"])
    out: list[SkillRule] = []
    ai = di = 0
    while len(out) < MAX_RULES and (ai < len(avoid) or di < len(do)):
        if ai < len(avoid):
            out.append(avoid[ai]); ai += 1
        if len(out) < MAX_RULES and di < len(do):
            out.append(do[di]); di += 1
    return out


def _config_sources() -> list[str] | None:
    """The corpus source scope the user confirmed (§4.4). Falls back to the
    coding-agent scope for 'all'/'both'/'auto'."""
    from .config import load_config
    src = load_config().get("source")
    if not src or src in ("all", "both", "auto"):
        return list(FAILURE_VALUE_SOURCE_SCOPE)
    return [src]


def _scan_source_filter() -> str | None:
    from .config import load_config
    src = load_config().get("source")
    return None if (not src or src in ("all", "both", "auto")) else src


def generate_skill(conn, *, window_days: int, backend: str = "auto",
                   model: str | None = None, caller=None, now: datetime | None = None,
                   sources: list[str] | None = None) -> SkillResult:
    """Pure pipeline: select -> distill -> merge(store) -> gate -> render. Read-only.

    ``sources`` is the confirmed source scope (``run_skill`` passes
    ``_config_sources()``); defaults to the coding-agent scope so the core stays
    independent of the on-disk config for tests.
    """
    corpus = _select.select_skill_candidates(
        conn, window_days=window_days, now=now,
        sources=sources if sources is not None else list(FAILURE_VALUE_SOURCE_SCOPE))
    meta: dict[str, Any] = {
        "generated_at": (now or datetime.now(timezone.utc)).date().isoformat(),
        "window_days": window_days,
        "sources": len(corpus.session_ids),
    }
    rules: list[SkillRule] = []
    blocked: list[tuple[SkillRule, list[str]]] = []
    gate_issues: list[str] = []
    skill_md = region = ""
    added_fps: set[str] = set()
    dropped: list[SkillRule] = []

    distilled: list[SkillRule] = []
    if not corpus.is_empty():
        distilled = _distill.distill_skills(corpus, backend=backend, model=model, caller=caller)
    fresh, blocked = _render.gate_rules(distilled)

    # merge with durable state (skip rejected, replace weakest)
    rejected = _store.rejected_fingerprints(conn)
    existing = _store.load_kept(conn)
    prev_installed = _store.installed_fingerprints(conn)
    merged = merge_rules(existing, fresh, rejected)
    # re-apply the external/exec hard-deny to the FULL install set (incl. store rules)
    rules, merged_blocked = _render.gate_rules(merged)
    blocked = blocked + merged_blocked

    merged_fps = {_store.fingerprint(r) for r in rules}
    added_fps = merged_fps - prev_installed
    dropped = [r for r in existing if _store.fingerprint(r) in (prev_installed - merged_fps)]

    if rules:
        skill_md = _render.render_skill_md(rules, meta)
        region = _render.render_agents_region(rules, meta)
        gate_issues = _render.gate_rendered(skill_md)

    # week-over-week recurrence signal (§9/D9): current vs last snapshot, for the
    # failure modes the current "avoid" rules target. Directional, not powered.
    cur_rates = corpus.mode_rates()
    last = _store.last_mode_snapshot(conn)
    prev_rates = last[2] if last else {}
    targeted = {r.taxonomy for r in rules if r.kind == "avoid" and r.taxonomy}
    trend = {m: (prev_rates.get(m), cur_rates.get(m, 0.0)) for m in sorted(targeted)}

    return SkillResult(rules, skill_md, region, blocked, gate_issues, corpus, meta,
                       added_fps, dropped, trend)


# --- scan + score (§7.1, §7.2) ---------------------------------------------

def _ensure_corpus(window_days: int, *, do_scan: bool, do_score: bool,
                   score_limit: int, backend: str, model: str | None) -> None:
    if do_scan:
        print("Indexing sessions…")
        from .cli import _run_scan
        _run_scan(source_filter=_scan_source_filter())
    if not do_score:
        return
    from .cli import _score_single_session
    from .workbench.index import open_index, query_unscored_sessions
    since = (None if window_days >= ALL_HISTORY_DAYS
             else (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat())
    conn = open_index()
    try:
        unscored = query_unscored_sessions(
            conn, limit=score_limit, source=_config_sources(), since=since)
        if unscored:
            print(f"Scoring {len(unscored)} unscored session(s) in the window "
                  f"(this uses your agent CLI)…")
            for i, s in enumerate(unscored, 1):
                sid = s["session_id"]
                print(f"  [{i}/{len(unscored)}] {sid}", flush=True)
                res = _score_single_session(conn, sid, backend=backend, model=model)
                if isinstance(res, dict) and res.get("error"):
                    print(f"      (skipped: {res['error']})")
            if len(unscored) >= score_limit:
                print(f"  scored {score_limit} (the per-run cap); re-run to score more "
                      f"or raise --score-limit.")
    finally:
        conn.close()


# --- IO / CLI ---------------------------------------------------------------

def _print_preview(res: SkillResult) -> None:
    c = res.corpus
    print(f"\nWindow: {c.total_failures} failure + {c.total_successes} success/recovery "
          f"candidate sessions.\n")
    if not res.rules:
        if c.is_empty():
            print("No scored failure/success sessions in the window.")
            print("Try `clawjournal skill --all` for the first run.")
        else:
            print("No usable rules this run.")
        return
    print(f"Proposed skill set ({len(res.rules)} rule(s)):\n")
    for i, r in enumerate(res.rules, 1):
        fp = _store.fingerprint(r)
        state = "NEW " if fp in res.added_fps else "KEPT"
        tag = "AVOID" if r.kind == "avoid" else "DO"
        print(f"  {i}. [{state}] [{tag}] {r.display_title()}   ({fp})")
        if r.guidance and r.guidance.strip() != r.display_title():
            print(f"        rule: {r.guidance}")
        if r.trigger:
            print(f"        when: {r.trigger}")
        if r.why:
            print(f"        why:  {r.why}")
    if res.dropped:
        print(f"\n  Dropping {len(res.dropped)} previously-installed rule(s) outranked this run:")
        for r in res.dropped:
            print(f"    - {r.guidance}  ({_store.fingerprint(r)})")
    if res.trend:
        n = res.corpus.eligible_scored
        print(f"\n  Recurrence of targeted failure modes (rate over {n} scored session(s) "
              f"— directional, not a powered metric):")
        for mode, (prev, cur) in res.trend.items():
            if n < 10:
                print(f"    - {mode}: {cur:.0%}  (insufficient data — n={n})")
            elif prev is None:
                print(f"    - {mode}: {cur:.0%}  (baseline; re-run next week to see the trend)")
            else:
                arrow = "↓ improving" if cur < prev - 1e-9 else ("↑ worsening" if cur > prev + 1e-9 else "→ flat")
                print(f"    - {mode}: {prev:.0%} → {cur:.0%}  ({arrow})")
    if res.blocked:
        print(f"\n  ({len(res.blocked)} rule(s) dropped by the safety hard-deny.)")
    if res.gate_issues:
        print(f"\n  ⚠ render-time gate found: {', '.join(res.gate_issues)} — install blocked.")


def run_skill(args) -> None:
    from .workbench.index import open_index

    backend = getattr(args, "backend", "auto")
    model = getattr(args, "model", None)
    score_limit = getattr(args, "score_limit", DEFAULT_SCORE_LIMIT)
    if score_limit < 0:
        print("--score-limit must be >= 0")
        sys.exit(2)
    if not getattr(args, "all", False) and getattr(args, "window_days", 7) < 1:
        print("--window-days must be >= 1")
        sys.exit(2)

    # --reject <fingerprint>: mark rejected so it is never re-proposed, then stop.
    if getattr(args, "reject", None):
        conn = open_index()
        try:
            hit = _store.reject(conn, args.reject)
        finally:
            conn.close()
        print(f"Rejected {args.reject}; it will drop out on the next `clawjournal skill` run."
              if hit else f"No rule with fingerprint {args.reject}.")
        return

    # 0. preflight (§7.0)
    if not getattr(args, "skip_preflight", False):
        from .skill.preflight import preflight
        problems = preflight(backend=backend)
        if problems:
            print("Cannot generate skills yet:")
            for p in problems:
                print(f"  - {p}")
            sys.exit(1)

    window_days = ALL_HISTORY_DAYS if getattr(args, "all", False) else getattr(args, "window_days", 7)

    # 1-2. scan + score the window (§7.1, §7.2)
    _ensure_corpus(
        window_days,
        do_scan=not getattr(args, "no_scan", False),
        do_score=not getattr(args, "no_score", False),
        score_limit=score_limit,
        backend=backend, model=model,
    )

    # 3-6. select -> distill -> merge -> gate -> render
    conn = open_index()
    try:
        res = generate_skill(conn, window_days=window_days, backend=backend, model=model,
                             sources=_config_sources())
        for rule in res.rules:
            _store.upsert_seen(conn, rule)
        _print_preview(res)
        if res.gate_issues:
            conn.close()
            sys.exit(1)
        if not res.rules:
            conn.close()
            sys.exit(1)
        if getattr(args, "preview", False):
            print("\n(preview only — not installed; re-run without --preview to install.)")
            return
        if not getattr(args, "yes", False):
            if not sys.stdin.isatty():
                print("\nRe-run with --yes to install (or --preview to just look).")
                return
            ans = input(f"\nInstall these {len(res.rules)} rule(s) for Claude Code + Codex? [y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                print("Not installed.")
                return

        # 7. install + persist state
        targets = getattr(args, "target", None) or ["claude", "codex"]
        installed: list[str] = []
        if "claude" in targets:
            installed.append(str(_install.install_claude(res.skill_md)))
        if "codex" in targets:
            installed.append(str(_install.install_codex(res.region)))
        _store.mark_installed(conn, res.rules)
        # record this run's per-mode rates for next week's trend (§9/D9)
        _store.save_mode_snapshot(conn, res.corpus.mode_rates(), res.corpus.eligible_scored)
    finally:
        conn.close()

    print("\nInstalled:")
    for p in installed:
        print(f"  - {p}")
    print("\nNote: these lessons reach your model provider when your agent loads them "
          "(that's how any skill/CLAUDE.md works) — nothing is uploaded to us.")
    print("Re-run weekly (`clawjournal skill`) to keep them fresh.")
