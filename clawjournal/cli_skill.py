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
    # run-over-run: {failure_mode: (prev_rate_or_None, current_rate)} for targeted modes
    trend: dict[str, tuple[float | None, float]] = field(default_factory=dict)


_SUPPORT_HALFLIFE_DAYS = 30.0  # a rule's effective support halves every 30 idle days


def _decayed_support(rule: SkillRule, now: datetime) -> float:
    """Support weighted by recency so a once-frequent, now-idle rule actually decays.

    A rule seen THIS run (``last_seen`` empty, or freshly re-proposed) keeps its full
    support; a stored rule not seen in weeks decays toward 0 and can be outranked by
    currently-relevant lessons. Without this, ``support = MAX(support, …)`` is a
    monotonic peak and the set never decays despite the README's promise.
    """
    if not rule.last_seen:
        return float(rule.support)
    try:
        seen = datetime.fromisoformat(rule.last_seen)
    except ValueError:
        return float(rule.support)
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - seen).total_seconds() / 86400)
    return rule.support * (0.5 ** (age_days / _SUPPORT_HALFLIFE_DAYS))


def merge_rules(existing: list[SkillRule], new: list[SkillRule], rejected: set[str],
                *, now: datetime | None = None) -> list[SkillRule]:
    """Merge existing + newly-distilled rules -> top-<=5 (replace the weakest).

    Deduped by fingerprint; rejected fingerprints dropped; ranked by RECENCY-WEIGHTED
    support (so stale peaks decay) then recurred-this-run. **Interleaved across kinds**
    so the installed set keeps a good+bad mix (D2) — otherwise 'do' rules (whose
    support signal is weaker than 'avoid' mode-recurrence) would be crowded out.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:  # a naive caller must not crash the aware-vs-naive subtraction
        clock = clock.replace(tzinfo=timezone.utc)
    new_fps = {_store.fingerprint(r) for r in new}
    pool: dict[str, SkillRule] = {}
    for r in existing:
        fp = _store.fingerprint(r)
        if fp not in rejected:
            pool[fp] = r
    for r in new:  # a re-distilled rule wins: keep its FRESH why/trigger/title/evidence
        fp = _store.fingerprint(r)
        if fp in rejected:
            continue
        if fp in pool:
            r.support = max(r.support, pool[fp].support)  # but carry the peak support forward
        pool[fp] = r

    def _rank(rules: list[SkillRule]) -> list[SkillRule]:
        def key(r: SkillRule):
            fresh = _store.fingerprint(r) in new_fps  # re-proposed this run -> no decay
            weight = float(r.support) if fresh else _decayed_support(r, clock)
            return (weight, fresh)
        return sorted(rules, key=key, reverse=True)

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


def _config_sources(cfg: dict | None = None) -> list[str] | None:
    """The corpus source scope the user confirmed (§4.4).

    Delegates to the canonical ``config.source_scope_sources`` so legacy ``both``
    stays Claude+Codex (it must NOT widen to opencode/openclaw, which were never
    confirmed). ``all``/``auto``/unset map to None there → the coding-agent scope
    the skill targets. Pass ``cfg`` to reuse an already-loaded config.
    """
    from .config import load_config, source_scope_sources
    cfg = cfg if cfg is not None else load_config()
    scope = source_scope_sources(cfg.get("source"))
    if scope is None:                       # all / auto / unset
        return list(FAILURE_VALUE_SOURCE_SCOPE)
    return list(scope)                      # 'both' -> claude+codex; else the single source


def _scan_source_filter(cfg: dict | None = None) -> str | None:
    # Reuse the canonical mapper so this can't drift from _config_sources. The scan
    # API takes ONE source filter, so a single-source scope maps cleanly; 'both'/'all'
    # index broadly (None) and scoring/selection still narrow via _config_sources().
    from .config import load_config, source_scope_sources
    cfg = cfg if cfg is not None else load_config()
    scope = source_scope_sources(cfg.get("source"))
    return scope[0] if scope is not None and len(scope) == 1 else None


def _config_excluded_projects(cfg: dict | None = None) -> list[str]:
    """Projects the user has --exclude'd (same egress gate as export/share)."""
    from .config import load_config
    cfg = cfg if cfg is not None else load_config()
    return list(cfg.get("excluded_projects") or [])


def generate_skill(conn, *, window_days: int, backend: str = "auto",
                   model: str | None = None, caller=None, now: datetime | None = None,
                   sources: list[str] | None = None,
                   excluded_projects: list[str] | None = None,
                   cfg: dict | None = None) -> SkillResult:
    """Pure pipeline: select -> distill -> merge(store) -> gate -> render. Read-only.

    ``sources`` is the confirmed source scope (``run_skill`` passes
    ``_config_sources()``); defaults to the coding-agent scope so the core stays
    independent of the on-disk config for tests. ``excluded_projects`` mirrors the
    export egress gate.
    """
    corpus = _select.select_skill_candidates(
        conn, window_days=window_days, now=now,
        sources=sources if sources is not None else list(FAILURE_VALUE_SOURCE_SCOPE),
        excluded_projects=excluded_projects)
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
        distilled = _distill.distill_skills(corpus, backend=backend, model=model, caller=caller, cfg=cfg)
    fresh, blocked = _render.gate_rules(distilled)

    # merge with durable state (skip rejected, replace weakest)
    rejected = _store.rejected_fingerprints(conn)
    existing = _store.load_kept(conn)
    prev_installed = _store.installed_fingerprints(conn)
    merged = merge_rules(existing, fresh, rejected, now=now or datetime.now(timezone.utc))
    # re-apply the external/exec hard-deny to the FULL install set (incl. store rules)
    rules, merged_blocked = _render.gate_rules(merged)
    # then drop any single rule the secret/PII/TruffleHog scan flags, PER RULE — so a
    # dirty rule can't dead-end the whole install with no rejectable fingerprint.
    rules, secret_blocked = _render.gate_secret_pii_per_rule(rules)
    blocked = blocked + merged_blocked + secret_blocked

    if rules:
        skill_md = _render.render_skill_md(rules, meta)
        region = _render.render_agents_region(rules, meta)
        gate_issues = _render.gate_rendered(skill_md)   # whole-doc, incl. TruffleHog (1 call)
        if gate_issues:
            # A scanner INFRA failure (missing binary, timeout, crash, not-installed)
            # must fail closed AS-IS — never run the per-rule pinpoint, which would
            # misattribute the same error to every rule and silently drop the whole set.
            # A CONTENT finding is always formatted "... N match(es)" / "... N finding(s)";
            # anything else is an infra error, regardless of the specific reason string.
            scanner_error = any(("match(es)" not in i) and ("finding(s)" not in i)
                                for i in gate_issues)
            if not scanner_error:
                # Real content finding TruffleHog caught that the fast per-rule regex
                # missed: attribute it to a rule and drop that rule.
                rules, tf_blocked = _render.gate_secret_pii_per_rule(rules, run_trufflehog=True)
                if tf_blocked:
                    blocked = blocked + tf_blocked
                    skill_md = _render.render_skill_md(rules, meta) if rules else ""
                    region = _render.render_agents_region(rules, meta) if rules else ""
                    # Re-run the FULL gate incl. TruffleHog: a residual detector-only
                    # finding in the reduced document that is NOT attributable to a single
                    # surviving rule must still fail closed — never write flagged content.
                    gate_issues = _render.gate_rendered(skill_md) if rules else []
                # else: a real finding NOT attributable to any single rule (spans rule
                # boundaries / rendered structure). FAIL CLOSED — keep gate_issues so the
                # install is blocked; NEVER silently write flagged content to the skill
                # files. ('any TruffleHog finding blocks' is a hard invariant.)

    merged_fps = {_store.fingerprint(r) for r in rules}
    added_fps = merged_fps - prev_installed
    dropped = [r for r in existing if _store.fingerprint(r) in (prev_installed - merged_fps)]

    # run-over-run recurrence signal (§9/D9): current vs the LAST saved snapshot (which
    # is per-run, not calendar-weekly), for the failure modes the current "avoid" rules
    # target. Directional, not powered — labeled "vs your last run", not "week-over-week".
    cur_rates = corpus.mode_rates()
    last = _store.last_mode_snapshot(conn)
    prev_rates = last[2] if last else {}
    targeted = {r.taxonomy for r in rules if r.kind == "avoid" and r.taxonomy}
    trend = {m: (prev_rates.get(m), cur_rates.get(m, 0.0)) for m in sorted(targeted)}

    return SkillResult(rules, skill_md, region, blocked, gate_issues, corpus, meta,
                       added_fps, dropped, trend)


# --- scan + score (§7.1, §7.2) ---------------------------------------------

def _ensure_corpus(window_days: int, *, do_scan: bool, do_score: bool,
                   score_limit: int, cfg: dict | None = None) -> None:
    from .config import load_config
    cfg = cfg if cfg is not None else load_config()  # thread the scope/scorer lookups
    if do_scan:
        print("Indexing sessions…")
        from .cli import _run_scan
        _run_scan(source_filter=_scan_source_filter(cfg))
    if not do_score:
        return
    from .cli import _score_single_session
    from .workbench.index import open_index, query_unscored_sessions
    now = datetime.now(timezone.utc)
    # Widen the SQL lower bound by 2 days (> the 14h max UTC-offset skew) so a
    # mixed-offset start_time string can't exclude an in-window session — the same
    # reason select.py widens. Scoring a few just-out-of-window sessions is harmless;
    # the corpus is filtered on the precise instant there.
    since = (None if window_days >= ALL_HISTORY_DAYS
             else (now - timedelta(days=window_days + 2)).isoformat())
    conn = open_index()
    try:
        # Scoring uses the configured scoring backend's own default model, NOT the
        # distill `--model` — a frontier distill model must not re-price the fleet.
        #
        # Determine the window's egress-blocked (explicitly held/embargoed) sessions
        # FIRST and cap AFTER filtering: a page of held rows must not starve older
        # shareable ones, and a held session must never be scored (=> egress). Rows
        # default to the shareable 'auto_redacted', so this set is small;
        # _release_blocked_ids chunks the gate query (SQLite variable limit) and
        # resolves expired embargoes back to shareable.
        held_where = "review_status != 'segmented' AND hold_state NOT IN ('auto_redacted','released')"
        held_params: list = []
        if since:
            held_where += " AND start_time >= ?"
            held_params.append(since)
        held_candidates = [r["session_id"] for r in conn.execute(
            f"SELECT session_id FROM sessions WHERE {held_where}", held_params).fetchall()]
        blocked = _select._release_blocked_ids(conn, held_candidates, now=now) if held_candidates else set()

        # Excluded projects must never be scored (egress) either — mirror the export
        # gate. Treat them like held: gather their ids in the window and skip them,
        # over-fetching so they can't starve shareable rows. (Guarded: usually empty.)
        excluded_projects = _config_excluded_projects(cfg)
        excluded_ids: set[str] = set()
        if excluded_projects:
            from .workbench.index import session_matches_excluded_projects
            win_where = "review_status != 'segmented'" + (" AND start_time >= ?" if since else "")
            for r in conn.execute(
                    f"SELECT session_id, project, source FROM sessions WHERE {win_where}",
                    ([since] if since else [])).fetchall():
                if session_matches_excluded_projects(
                        {"project": r["project"], "source": r["source"]}, excluded_projects):
                    excluded_ids.add(r["session_id"])
        skip = blocked | excluded_ids

        # Over-fetch by the skip count (so >= score_limit shareable rows survive the
        # filter) PLUS one, so `len(shareable) > score_limit` can actually detect that
        # more unscored sessions remain and warn the user to re-run / raise the cap.
        fetched = query_unscored_sessions(
            conn, limit=score_limit + len(skip) + 1, source=_config_sources(cfg), since=since)
        shareable = [s for s in fetched if s["session_id"] not in skip]
        unscored = shareable[:score_limit]
        # Scoring uses the CONFIGURED scorer backend, independent of the distill
        # `--backend` — that flag tunes distillation only and must not silently
        # re-route the scoring fleet.
        scorer_backend = cfg.get("scorer_backend") or "auto"
        if unscored:
            print(f"Scoring {len(unscored)} unscored session(s) in the window "
                  f"(this uses your agent CLI)…")
            for i, s in enumerate(unscored, 1):
                sid = s["session_id"]
                print(f"  [{i}/{len(unscored)}] {sid}", flush=True)
                try:
                    res = _score_single_session(conn, sid, backend=scorer_backend)
                except Exception as exc:  # e.g. sqlite 'database is locked' vs the daemon
                    print(f"      (skipped: {exc.__class__.__name__}: {exc})")
                    continue
                if isinstance(res, dict) and res.get("error"):
                    print(f"      (skipped: {res['error']})")
        if len(shareable) > score_limit:
            print(f"  hit the per-run score cap ({score_limit}); re-run or raise "
                  f"--score-limit to score more.")
    finally:
        conn.close()


# --- IO / CLI ---------------------------------------------------------------

def _ascii_safe(text: str) -> str:
    """Downgrade the glyphs we print when the console can't encode them (e.g. cp1252)."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        for uni, asc in (("↓", "v"), ("↑", "^"), ("→", "->"), ("⚠", "!"), ("—", "-")):
            text = text.replace(uni, asc)
        return text


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
        print(_ascii_safe(f"\n  Recurrence of targeted failure modes vs your last run "
                          f"(rate over {n} scored session(s) — directional, not a powered metric):"))
        for mode, (prev, cur) in res.trend.items():
            if n < 10:
                print(_ascii_safe(f"    - {mode}: {cur:.0%}  (insufficient data — n={n})"))
            elif prev is None:
                print(f"    - {mode}: {cur:.0%}  (baseline; re-run later to see the trend)")
            else:
                arrow = "↓ improving" if cur < prev - 1e-9 else ("↑ worsening" if cur > prev + 1e-9 else "→ flat")
                print(_ascii_safe(f"    - {mode}: {prev:.0%} → {cur:.0%}  ({arrow})"))
    if res.blocked:
        print(f"\n  {len(res.blocked)} rule(s) dropped by the safety gate:")
        for r, reasons in res.blocked:
            print(f"    - {r.display_title()}  ({', '.join(reasons)})")
    if res.gate_issues:
        print(_ascii_safe(f"\n  ⚠ render-time secret/PII gate blocked install: {', '.join(res.gate_issues)}"))
        print("    (fail-closed — nothing was written. If the scanner itself errored, re-run; "
              "otherwise the flagged lesson spans rules — inspect the source sessions.)")


def run_skill(args) -> None:
    from .workbench.index import open_index

    # Never let a non-ASCII glyph (rule text, trend arrows) crash the preview on a
    # non-UTF-8 console; _ascii_safe handles the ones we emit, this covers the rest.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass

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

    from .config import load_config
    cfg = load_config()  # read the config ONCE and thread it through preflight/scan/select

    # 0. preflight (§7.0)
    if not getattr(args, "skip_preflight", False):
        from .skill.preflight import preflight
        problems = preflight(backend=backend, check_scorer=not getattr(args, "no_score", False), cfg=cfg)
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
        cfg=cfg,
    )

    # 3-6. select -> distill -> merge -> gate -> render
    conn = open_index()
    try:
        res = generate_skill(conn, window_days=window_days, backend=backend, model=model,
                             sources=_config_sources(cfg),
                             excluded_projects=_config_excluded_projects(cfg), cfg=cfg)
        _print_preview(res)
        # Persist NOTHING when the gate fails: a rule the render-time gate flags would
        # otherwise be stored as 'proposed', reloaded by load_kept every run, and
        # re-block the install forever.
        if res.gate_issues:
            sys.exit(1)
        if not res.rules:
            sys.exit(1)

        def _persist_seen() -> None:
            for rule in res.rules:
                _store.upsert_seen(conn, rule)

        if getattr(args, "preview", False):
            _persist_seen()
            print("\n(preview only — not installed; re-run without --preview to install.)")
            return
        if not getattr(args, "yes", False):
            if not sys.stdin.isatty():
                _persist_seen()
                print("\nRe-run with --yes to install (or --preview to just look).")
                return
            try:
                ans = input(f"\nInstall these {len(res.rules)} rule(s) for Claude Code + Codex? [y/N] ")
            except EOFError:  # Ctrl-D / stdin closed -> treat as a graceful decline
                ans = ""
            if ans.strip().lower() not in ("y", "yes"):
                _persist_seen()
                print("Not installed.")
                return

        # 7. install each target independently, then persist state for whatever
        # actually landed on disk. If one target fails after another succeeded, the
        # rules ARE installed for the survivor, so the store MUST record them — else
        # the next run mislabels every rule [NEW] and the trend snapshot is lost.
        targets = getattr(args, "target", None) or ["claude", "codex"]
        installed: list[str] = []
        failures: list[str] = []
        for name, fn, payload in (("claude", _install.install_claude, res.skill_md),
                                  ("codex", _install.install_codex, res.region)):
            if name not in targets:
                continue
            try:
                installed.append(str(fn(payload)))
            except (RuntimeError, OSError, UnicodeDecodeError) as exc:
                failures.append(f"{name}: {exc}")
        if installed:
            # The skill is ALREADY on disk; a durable-store write failure (e.g. sqlite
            # lock vs the daemon) must NOT crash with a traceback — warn and continue.
            # Worst case the next run re-labels these rules [NEW]; the install is safe.
            try:
                _store.mark_installed(conn, res.rules)  # upserts + marks 'kept'
                # Only snapshot when the window actually had scored sessions — an idle
                # run (empty corpus but kept rules re-proposed) must NOT overwrite the
                # last real snapshot with an empty one (would reset the run-over-run trend).
                if res.corpus.eligible_scored > 0:
                    _store.save_mode_snapshot(conn, res.corpus.mode_rates(), res.corpus.eligible_scored)
            except Exception as exc:
                print(f"note: installed to disk, but failed to record state "
                      f"({exc.__class__.__name__}); the next run may re-propose these rules.")
        else:
            _persist_seen()  # nothing landed -> at least keep 'seen' state for next run
    finally:
        conn.close()

    if installed:
        print("\nInstalled:")
        for p in installed:
            print(f"  - {p}")
        print("\nNote: these lessons reach your model provider when your agent loads them "
              "(that's how any skill/CLAUDE.md works) — nothing is uploaded to us.")
        print("Re-run weekly (`clawjournal skill`) to keep them fresh.")
    if failures:
        print("\nInstall problems (fix and re-run):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
