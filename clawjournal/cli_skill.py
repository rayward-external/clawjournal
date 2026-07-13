"""``clawjournal skill`` — distill + install the local ``clawjournal-lessons`` skill.

Mode A end to end (§7): preflight -> scan/index -> score the window -> select
top candidates -> one local distill call -> merge into the durable set (replace
the weakest, skip rejected) -> deterministic gate -> preview the diff -> install
for Claude Code + Codex. The core (``generate_skill``) is pure/testable with a
fake caller and is read-only on the store; ``run_skill`` owns scan/score/IO.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .skill import distill as _distill
from .skill import install as _install
from .skill import render as _render
from .skill import select as _select
from .skill import store as _store
from .skill import turns as _turns
from .skill.schema import MAX_INSTALLED_RULES, SkillRule
from .workbench.index import FAILURE_VALUE_SOURCE_SCOPE

# A wide window approximates "all history" for the first run.
ALL_HISTORY_DAYS = 3650
DEFAULT_SCORE_LIMIT = 25


@dataclass
class SkillResult:
    rules: list[SkillRule]          # the merged active top-<=MAX_INSTALLED_RULES to install
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
    # run-over-run for OBJECTIVE signals: {signal: (prev_rate_or_None, current_rate)}
    objective_trend: dict[str, tuple[float | None, float]] = field(default_factory=dict)


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


# --- semantic dedup (paraphrase collapse) -----------------------------------
# Fingerprint dedup keys on kind + normalized guidance, so a REWORDING of an existing
# lesson (same meaning, different words -> different fingerprint) survives as a separate
# rule. Left unchecked, the distiller re-emits paraphrases of installed lessons every run
# (esp. now that distill runs bare and can't see the installed set), wasting the 5-rule
# budget on duplicates and dropping distinct lessons. Collapse them here.

_DUP_STOPWORDS = frozenset(
    "the a an to of in on and or for with is are be that this it its your you not never "
    "always before after when so than then them they into from by at as if do dont use "
    "using ensure make sure any all each every rather instead only".split()
)


def _stem(w: str) -> str:
    """Crude suffix fold so rewrites match ('flagging'/'flag', 'waited'/'wait').

    Deliberately conservative: 'ing' only off longer words so 'string'/'timing'
    survive; the trailing double consonant collapses so 'flagg' -> 'flag'.
    """
    if w.endswith("s") and len(w) > 3:
        w = w[:-1]
    if w.endswith("ing") and len(w) > 6:
        w = w[:-3]
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    return w


def _guidance_keywords(text: str) -> set[str]:
    """Significant word stems in a rule's guidance."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {_stem(w) for w in words if len(w) > 2 and w not in _DUP_STOPWORDS}


def _guidance_overlap(a: str, b: str) -> float:
    ka, kb = _guidance_keywords(a), _guidance_keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


def _same_lesson(a: SkillRule, b: SkillRule) -> bool:
    """True if two rules teach the SAME lesson (a paraphrase fingerprint dedup misses).

    Same failure MODE (taxonomy) => same lesson, full stop: for a 5-rule budget, covering
    5 DISTINCT modes beats two rules on one mode, and LLM paraphrases of one lesson share
    little vocabulary (word-overlap alone reliably MISSES them — e.g. 'answer the actual
    question' vs 'close with a recommendation' overlap ~0.1 yet are the same
    communication_error lesson), so taxonomy is the trustworthy signal. Rules without a
    shared mode (e.g. 'do' rules, which carry no taxonomy) fall back to word overlap at a
    lowered threshold so real rewrites still collapse.
    """
    # An identical title => the same lesson even ACROSS kinds — the distiller often emits
    # one lesson as both 'do X' and 'avoid not-X' (e.g. "Verify Beyond Green Tests" as
    # both), which a within-kind check never compares.
    ta, tb = a.title.strip().lower(), b.title.strip().lower()
    if ta and ta == tb:
        return True
    # One title EXTENDING the other is the same lesson too: the distiller decorates a
    # carried title instead of reusing it verbatim ("Fix Root Cause" came back as
    # "Fix Root Cause Durably" [do] beside the carried [avoid], guidance overlap only
    # 0.25). Require >=2 shared keywords so one-word titles can't match everything.
    tka, tkb = _guidance_keywords(ta), _guidance_keywords(tb)
    if len(tka & tkb) >= 2 and (tka <= tkb or tkb <= tka):
        return True
    if a.kind == b.kind:
        if a.taxonomy and a.taxonomy == b.taxonomy:  # same failure mode -> same lesson
            return True
        return _guidance_overlap(a.guidance, b.guidance) >= 0.3
    # cross-kind (do vs avoid) with different titles: require strong word overlap.
    # 0.40 (not higher): the distiller restates a carried avoid as a fresh 'do' with
    # ~0.4 overlap ("Pair Flags With Fixes" -> "Propose Fixes Not Flags"); genuinely
    # distinct cross-kind lessons measure far lower (<0.2).
    return _guidance_overlap(a.guidance, b.guidance) >= 0.40


def _semantic_dedup(ranked: list[SkillRule], new_fps: set[str]) -> list[SkillRule]:
    """Collapse paraphrase clusters in a rank-ordered list, keeping ONE lesson each.

    Within a cluster, prefer the already-installed (carried) rule over a fresh
    paraphrase — stability beats re-wording the user's skill file every run.
    """
    kept: list[SkillRule] = []
    for r in ranked:
        dup = next((i for i, k in enumerate(kept) if _same_lesson(r, k)), None)
        if dup is None:
            kept.append(r)
            continue
        kept_rule = kept[dup]
        kept_fresh = _store.fingerprint(kept_rule) in new_fps
        r_fresh = _store.fingerprint(r) in new_fps
        if kept_fresh and not r_fresh:
            # Replace a fresh paraphrase with the carried original (no churn), but
            # mark it seen-this-run and preserve the fresh support signal.
            r.last_seen = ""
            r.support = max(r.support, kept_rule.support)
            kept[dup] = r
        elif (not kept_fresh) and r_fresh:
            # The carried original ranked before the fresh paraphrase; keep its
            # stable wording but refresh its recency/support.
            kept_rule.last_seen = ""
            kept_rule.support = max(kept_rule.support, r.support)
    return kept


def merge_rules(existing: list[SkillRule], new: list[SkillRule], rejected: set[str],
                *, now: datetime | None = None) -> list[SkillRule]:
    """Merge existing + newly-distilled rules -> active top-<=5 installed (replace the weakest).

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

    # rank the FULL pool, then collapse paraphrase clusters ACROSS kinds (so a lesson
    # framed as both a 'do' and an 'avoid' can't install twice), then split for the
    # good+bad interleave — the dedup preserves rank order, so each list stays ranked.
    deduped = _semantic_dedup(_rank(list(pool.values())), new_fps)
    avoid = [r for r in deduped if r.kind == "avoid"]
    do = [r for r in deduped if r.kind == "do"]
    out: list[SkillRule] = []
    ai = di = 0
    while len(out) < MAX_INSTALLED_RULES and (ai < len(avoid) or di < len(do)):
        if ai < len(avoid):
            out.append(avoid[ai]); ai += 1
        if len(out) < MAX_INSTALLED_RULES and di < len(do):
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


def _config_excluded_projects(cfg: dict | None = None, conn=None) -> list[str]:
    """The EFFECTIVE excluded-project set — the SAME egress gate as export/share.

    That means config ``--exclude`` PLUS workbench DB ``exclude_project`` policies
    (stored by the Policies UI, not config.json), merged via
    ``get_effective_share_settings``. Without a DB connection, fall back to config-only.
    """
    from .config import load_config, normalize_excluded_project_names
    cfg = cfg if cfg is not None else load_config()
    if conn is not None:
        from .workbench.index import get_effective_share_settings
        return list(get_effective_share_settings(conn, cfg).get("excluded_projects") or [])
    return list(normalize_excluded_project_names(cfg.get("excluded_projects") or []))


def _effective_share_settings(conn, cfg: dict | None = None) -> dict[str, Any]:
    from .config import load_config
    from .workbench.index import get_effective_share_settings
    return get_effective_share_settings(conn, cfg if cfg is not None else load_config())


def generate_skill(conn, *, window_days: int, backend: str = "auto",
                   model: str | None = None, effort: str | None = None,
                   caller=None, now: datetime | None = None,
                   sources: list[str] | None = None,
                   excluded_projects: list[str] | None = None,
                   cfg: dict | None = None) -> SkillResult:
    """Pure pipeline: select -> distill -> merge(store) -> gate -> render. Read-only.

    ``sources`` is the confirmed source scope (``run_skill`` passes
    ``_config_sources()``); defaults to the coding-agent scope so the core stays
    independent of the on-disk config for tests. ``excluded_projects`` mirrors the
    export egress gate.
    """
    redaction_settings = _effective_share_settings(conn, cfg)
    corpus = _select.select_skill_candidates(
        conn, window_days=window_days, now=now,
        sources=sources if sources is not None else list(FAILURE_VALUE_SOURCE_SCOPE),
        excluded_projects=excluded_projects,
        # captured user-corrections and error->recovery pairs are a SELECTION signal:
        # they boost the session's rank into the pool and ride along to the distill
        # prompt (scrubbed at prompt-format time like every other field)
        excerpt_loader=lambda sid: _turns.excerpts_for_session(conn, sid))
    # count the REAL source sessions before appending synthetic env/rejection candidates
    # (whose ids are placeholders, not sessions) so the "sources=N" footer isn't inflated
    n_source_sessions = len({c.session_id for c in corpus.candidates})
    # objective environment feedback: a tool-error signature recurring across the
    # window's (gated) sessions becomes an avoid-candidate whose support_count is
    # the REAL session count — evidence the judge can't fabricate
    _turns.add_env_candidates(conn, corpus, now=now)
    # human-rejection feedback: reject-button hits + permission/classifier denials
    # become one avoid-candidate (support = distinct sessions with a rejection)
    _turns.add_rejection_candidate(conn, corpus, now=now)
    meta: dict[str, Any] = {
        "generated_at": (now or datetime.now(timezone.utc)).date().isoformat(),
        "window_days": window_days,
        "sources": n_source_sessions,
    }
    rules: list[SkillRule] = []
    blocked: list[tuple[SkillRule, list[str]]] = []
    gate_issues: list[str] = []
    skill_md = region = ""
    added_fps: set[str] = set()
    dropped: list[SkillRule] = []

    distilled: list[SkillRule] = []
    if not corpus.is_empty():
        distilled = _distill.distill_skills(
            corpus,
            backend=backend,
            model=model,
            effort=effort,
            caller=caller,
            cfg=cfg,
            redaction_settings=redaction_settings,
        )
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
        skill_md, region = _render.render_targets(rules, meta)  # body built once
        gate_issues = _render.gate_rendered(skill_md)   # whole-doc, incl. TruffleHog (1 call)
        if gate_issues:
            # A scanner INFRA failure (missing binary, timeout, crash, not-installed)
            # must fail closed AS-IS — never run the per-rule pinpoint, which would
            # misattribute the same error to every rule and silently drop the whole set.
            # Classification lives with the message producer (render.gate_rendered).
            scanner_error = _render.gate_has_scanner_error(gate_issues)
            if not scanner_error:
                # Real content finding TruffleHog caught that the fast per-rule regex
                # missed: attribute it to a rule and drop that rule.
                rules, tf_blocked = _render.gate_secret_pii_per_rule(rules, run_trufflehog=True)
                if tf_blocked:
                    blocked = blocked + tf_blocked
                    skill_md, region = _render.render_targets(rules, meta) if rules else ("", "")
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

    # OBJECTIVE-signal trend: judge-free ground truth (tool-error signatures, rejections).
    # Union of what recurs now with what was tracked last run, so a signal that fell
    # below the teach bar still shows as improved rather than silently vanishing.
    cur_obj = corpus.objective_rates()
    last_obj = _store.last_objective_snapshot(conn)
    prev_obj = last_obj[2] if last_obj else {}
    obj_keys = set(cur_obj) | set(prev_obj)
    objective_trend = {k: (prev_obj.get(k), cur_obj.get(k, 0.0)) for k in sorted(obj_keys)}

    return SkillResult(rules, skill_md, region, blocked, gate_issues, corpus, meta,
                       added_fps, dropped, trend, objective_trend)


# --- scan + score (§7.1, §7.2) ---------------------------------------------

def _ensure_corpus(window_days: int, *, do_scan: bool, do_score: bool,
                   score_limit: int, score_all: bool = False, cfg: dict | None = None) -> None:
    from .config import load_config
    cfg = cfg if cfg is not None else load_config()  # thread the scope/scorer lookups
    if do_scan:
        print("Indexing sessions…")
        from .cli import _run_scan
        _run_scan(source_filter=_scan_source_filter(cfg))
    if not do_score:
        return
    from .cli import _score_single_session
    from .workbench.index import SCORE_SETTLE_SECONDS, open_index, query_unscored_sessions
    now = datetime.now(timezone.utc)
    # Widen the SQL lower bound by 2 days (> the 14h max UTC-offset skew) so a
    # mixed-offset start_time string can't exclude an in-window session — the same
    # reason select.py widens. Scoring a few just-out-of-window sessions is harmless;
    # the corpus is filtered on the precise instant there.
    since = (None if window_days >= ALL_HISTORY_DAYS
             else (now - timedelta(days=window_days + 2)).isoformat())
    conn = open_index()
    try:
        redaction_settings = _effective_share_settings(conn, cfg)
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
        excluded_projects = _config_excluded_projects(cfg, conn)
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

        # `--all` (score_all) scores the WHOLE history in one run, as the README/design
        # promise — no per-run cap. A windowed run over-fetches by the skip count PLUS one
        # so `len(shareable) > score_limit` can detect that more remain and warn.
        if score_all:
            fetched = query_unscored_sessions(
                conn, limit=10**9, source=_config_sources(cfg), since=since,
                include_stale_scored=True, settle_seconds=SCORE_SETTLE_SECONDS, now=now)
        else:
            fetched = query_unscored_sessions(
                conn, limit=score_limit + len(skip) + 1, source=_config_sources(cfg), since=since,
                include_stale_scored=True, settle_seconds=SCORE_SETTLE_SECONDS, now=now)
        shareable = [s for s in fetched if s["session_id"] not in skip]
        unscored = shareable if score_all else shareable[:score_limit]
        # Scoring uses the CONFIGURED scorer backend, independent of the distill
        # `--backend` — that flag tunes distillation only and must not silently
        # re-route the scoring fleet.
        scorer_backend = cfg.get("scorer_backend") or "auto"
        if unscored:
            where = ("in your whole history (first run — this can take a while)"
                     if score_all else "in the window")
            print(f"Scoring {len(unscored)} unscored session(s) {where} (this uses your agent CLI)…")
            for i, s in enumerate(unscored, 1):
                sid = s["session_id"]
                print(f"  [{i}/{len(unscored)}] {sid}", flush=True)
                try:
                    res = _score_single_session(
                        conn,
                        sid,
                        backend=scorer_backend,
                        redaction_settings=redaction_settings,
                    )
                except Exception as exc:  # e.g. sqlite 'database is locked' vs the daemon
                    print(f"      (skipped: {exc.__class__.__name__}: {exc})")
                    continue
                if isinstance(res, dict) and res.get("error"):
                    print(f"      (skipped: {res['error']})")
        if not score_all and len(shareable) > score_limit:
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


_INSTALL_TARGET_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "workbuddy": "WorkBuddy",
}


def _format_install_targets(targets: list[str]) -> str:
    """Return a human-readable label for current and future install targets."""
    labels = [
        _INSTALL_TARGET_LABELS.get(target, target.replace("-", " ").title())
        for target in dict.fromkeys(targets)
    ]
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return " + ".join(labels)
    return ", ".join(labels[:-1]) + f" + {labels[-1]}"


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
    if res.objective_trend:
        n = res.corpus.eligible_scored
        print(_ascii_safe("\n  Objective feedback recurrence vs your last run "
                          "(tool errors + rejections per scored session — verifiable, not judge-inferred):"))
        # most-recurrent first; cap so a long tail of rare signatures doesn't flood output
        ordered = sorted(res.objective_trend.items(),
                         key=lambda kv: -max(kv[1][0] or 0.0, kv[1][1]))[:6]
        for sig, (prev, cur) in ordered:
            if n < 10:
                print(_ascii_safe(f"    - {sig}: {cur:.0%}  (insufficient data — n={n})"))
            elif prev is None:
                print(_ascii_safe(f"    - {sig}: {cur:.0%}  (baseline; re-run later to see the trend)"))
            else:
                arrow = "↓ improving" if cur < prev - 1e-9 else ("↑ worsening" if cur > prev + 1e-9 else "→ flat")
                print(_ascii_safe(f"    - {sig}: {prev:.0%} → {cur:.0%}  ({arrow})"))
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
    effort = getattr(args, "effort", None)
    score_limit = getattr(args, "score_limit", DEFAULT_SCORE_LIMIT)
    if score_limit < 0:
        print("--score-limit must be >= 0")
        sys.exit(2)
    if not getattr(args, "all", False) and getattr(args, "window_days", 7) < 1:
        print("--window-days must be >= 1")
        sys.exit(2)
    if effort:
        from .scoring.backends import resolve_backend, validate_effort_for_backend
        try:
            validate_effort_for_backend(resolve_backend(backend), effort)
        except RuntimeError as exc:
            print(str(exc))
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
        score_all=getattr(args, "all", False),  # --all scores the whole history, uncapped
        cfg=cfg,
    )

    # 3-6. select -> distill -> merge -> gate -> render
    conn = open_index()
    try:
        res = generate_skill(conn, window_days=window_days, backend=backend, model=model, effort=effort,
                             sources=_config_sources(cfg),
                             excluded_projects=_config_excluded_projects(cfg, conn), cfg=cfg)
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
        targets = getattr(args, "target", None) or ["claude", "codex"]
        if not getattr(args, "yes", False):
            if not sys.stdin.isatty():
                _persist_seen()
                print("\nRe-run with --yes to install (or --preview to just look).")
                return
            try:
                target_label = _format_install_targets(targets)
                ans = input(f"\nInstall these {len(res.rules)} rule(s) for {target_label}? [y/N] ")
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
                    _store.save_objective_snapshot(conn, res.corpus.objective_rates(),
                                                   res.corpus.eligible_scored)
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
