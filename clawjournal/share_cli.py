"""Interactive terminal Share wizard for ClawJournal.

A keyboard-driven MVP of the `clawjournal serve` web Share flow, for sharing
traces without a browser:

    1. QUEUE    pick traces from a time-scoped, filterable list
    2. REDACT   scrub PII (rules; optional AI pass) + per-category breakdown
    3. REVIEW   inspect & include traces (share-local; does NOT touch triage)
    4. PACKAGE  seal the bundle (with blocked-trace removal/retry)
    5. SUBMIT   accept consent explicitly, then upload — or fall back to download
    6. DONE     receipt and/or bundle-zip download

This is a terminal front-end only: all share logic lives in `share_flow`
(shared with the daemon); this module handles presentation and prompts.

Primary entry point:  `clawjournal share --interactive`
Thin alias:           `clawshare`

It does NOT yet reach full parity with the web wizard (richer queue
add/remove/reorder, the complete AI interaction model). See the PR notes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_config
from .scoring.backends import default_model_for_backend, resolve_backend
from .workbench.index import (
    SHAREABLE_HOLD_STATES,
    effective_hold_state,
    get_effective_share_settings,
    get_session_detail,
    open_index,
    query_sessions,
    query_unscored_sessions,
    SCORE_SETTLE_SECONDS,
    session_matches_excluded_projects,
)
from . import share_flow
from .share_flow import (
    build_redaction_record,
    build_zip,
    category_breakdown,
    effective_ai_pii,
    gate_blockers,
    hosted_destination,
)

# ---- tty helpers ------------------------------------------------------------

BOLD = "\033[1m"; DIM = "\033[2m"; GRN = "\033[32m"; YEL = "\033[33m"; RED = "\033[31m"
CYN = "\033[36m"; RST = "\033[0m"
_ANSI_RE = re.compile(r"(\033\[[0-9;]*m)")
NSTEPS = 6


def _rl(prompt: str) -> str:
    """Wrap ANSI escapes in readline ignore-markers so cursor width stays correct."""
    return _ANSI_RE.sub("\001\\1\002", prompt)


def ask(prompt: str, default: str = "") -> str:
    try:
        val = input(_rl(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(130)
    return val or default


def yesno(prompt: str, default_yes: bool = False) -> bool:
    val = ask(prompt + (" [Y/n] " if default_yes else " [y/N] ")).lower()
    return default_yes if not val else val in {"y", "yes"}


def step(n: int, title: str):
    print(f"\n{CYN}{BOLD}── Step {n}/{NSTEPS}: {title} {'─' * max(0, 46 - len(title))}{RST}")


def die(msg: str, code: int = 1):
    print(f"{RED}✗ {msg}{RST}")
    sys.exit(code)


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


# ---- titles -----------------------------------------------------------------

_SYS_PROMPT_PREFIXES = (
    "you are", "you're", "your task", "act as", "system:",
    "base directory for this skill",
)


def _looks_like_system_prompt(text: str) -> bool:
    s = (text or "").strip().lower()
    return s.startswith(_SYS_PROMPT_PREFIXES) or "you are an ai" in s[:40]


def user_prompt_title(conn, row: dict) -> str | None:
    """Pull a meaningful title from the first user message, skipping a leading
    system-prompt / instruction preamble."""
    detail = get_session_detail(conn, row["session_id"])
    for m in (detail or {}).get("messages") or []:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if not isinstance(c, str) or not c.strip():
            continue
        text = c.strip()
        head, sep, tail = text.partition("\n\n")
        if sep and _looks_like_system_prompt(head) and tail.strip():
            text = tail.strip()
        return text.replace("\n", " ")
    return None


def resolve_title(r: dict, summarized: bool) -> str:
    raw = (r.get("display_title") or "").strip().replace("\n", " ") or "Untitled"
    if summarized:
        return (r.get("ai_display_title") or "").strip() or raw
    return raw


def trace_title(r: dict, width: int = 52) -> str:
    t = r.get("_clawshare_title")
    if t is None:
        t = resolve_title(r, False)
    t = (t or "").replace("\n", " ")
    return (t[: width - 1] + "…") if len(t) > width else t


def _coverage_label(rec: dict) -> str:
    return {"full": f"{GRN}AI-reviewed{RST}",
            "rules_only": f"{YEL}AI unavailable · rules-only{RST}",
            "disabled": f"{DIM}rules-only{RST}"}.get(rec.get("ai_coverage", "disabled"), "")


# ---- time-range filtering ---------------------------------------------------

def _parse_ts(ts):
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _in_time_range(row: dict, time_range: str) -> bool:
    """Rolling windows (not calendar): 'today' = past 24h, 'weekly' = past 168h."""
    if time_range == "all":
        return True
    dt = _parse_ts(row.get("end_time")) or _parse_ts(row.get("start_time"))
    if dt is None:
        return False
    now = datetime.now(timezone.utc)
    if time_range == "today":
        return dt >= now - timedelta(hours=24)
    if time_range == "weekly":
        return dt >= now - timedelta(hours=168)
    return True


# ---- LLM trace summaries (for --summary titles) -----------------------------

def summarize_trace(session_detail: dict, *, backend: str = "auto",
                    model: str | None = None) -> str:
    import tempfile
    from .scoring.backends import run_default_agent_task
    from .scoring.scoring import _anonymize_for_scoring, get_message_text

    messages = session_detail.get("messages", []) or []
    try:
        _detail, messages = _anonymize_for_scoring(session_detail, messages)
    except Exception:  # noqa: BLE001
        pass

    lines = []
    for m in messages[:60]:
        try:
            text = get_message_text(m).strip()
        except Exception:  # noqa: BLE001
            text = ""
        if not text or text == "None":
            continue
        lines.append(f"{m.get('role', '')}: {text[:600]}")
    transcript = "\n".join(lines)
    if not transcript:
        return ""

    prompt = (
        "Read transcript.md in the current directory — a coding-agent session. "
        "Reply with ONE short line (under 60 characters), imperative mood, "
        "summarizing what the user was actually trying to do. Ignore boilerplate "
        "system/skill preamble. Output only the title — no quotes, no extra text."
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "transcript.md").write_text(transcript, encoding="utf-8")
            result = run_default_agent_task(
                backend=backend, cwd=tmp_path, task_prompt=prompt, model=model,
                timeout_seconds=60, codex_sandbox="read-only",
                openclaw_message=prompt + f"\nRead: {tmp_path / 'transcript.md'}",
            )
    except Exception:  # noqa: BLE001
        return ""
    out = (result.stdout or "").strip()
    if not out:
        return ""
    title = [ln.strip() for ln in out.splitlines() if ln.strip()][-1]
    return title.strip().strip('"').strip("'")[:80]


def _title_cache_path() -> Path:
    from .config import CONFIG_DIR
    return Path(CONFIG_DIR) / "clawshare_titles.json"


def _load_title_cache() -> dict:
    try:
        return json.loads(_title_cache_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_title_cache(cache: dict) -> None:
    try:
        _title_cache_path().write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def ensure_titles(conn, rows: list[dict], do_summarize: bool, summary_model: str | None = None):
    """--summary only. Fills an LLM summary title (in-memory + a local clawshare
    cache, NOT the scoring column). Concurrent with live progress; Ctrl-C skips."""
    if not do_summarize:
        return
    cache = _load_title_cache()
    for r in rows:
        if not (r.get("ai_display_title") or "").strip():
            cached = cache.get(r["session_id"])
            if cached:
                r["ai_display_title"] = cached
    need = [r for r in rows if not (r.get("ai_display_title") or "").strip()]
    if not need:
        return

    try:
        backend = resolve_backend("auto")
    except Exception:  # noqa: BLE001
        print(f"  {DIM}(no agent backend available — using raw titles){RST}")
        return
    model = summary_model or default_model_for_backend(backend)

    label = f"{backend}/{model}" if model else backend
    print(f"  {DIM}Summarizing {len(need)} title(s) with {label} — Ctrl-C to skip…{RST}")
    details = {r["session_id"]: get_session_detail(conn, r["session_id"]) for r in need}

    from concurrent.futures import as_completed
    titles: dict[str, str] = {}
    pool = ThreadPoolExecutor(max_workers=min(8, len(need)))
    futures = {
        pool.submit(summarize_trace, details[r["session_id"]], backend=backend, model=model):
            r["session_id"]
        for r in need if details.get(r["session_id"])
    }
    done = 0
    try:
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                titles[sid] = fut.result()
            except Exception:  # noqa: BLE001
                titles[sid] = ""
            done += 1
            print(f"\r  {DIM}  …summarized {done}/{len(futures)}{RST}", end="", flush=True)
        print()
    except KeyboardInterrupt:
        print(f"\n  {YEL}Skipped remaining summaries.{RST}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    changed = False
    for r in need:
        title = titles.get(r["session_id"]) or ""
        if title:
            r["ai_display_title"] = title
            cache[r["session_id"]] = title
            changed = True
    if changed:
        _save_title_cache(cache)


# ---- step 1: queue ----------------------------------------------------------

def _parse_selection(raw: str, count: int) -> list[int]:
    """Parse the queue selection input into 1-based indices. 'all' (or 'a'/'*')
    selects every listed trace; otherwise space/comma-separated numbers."""
    low = raw.strip().lower()
    if low in ("all", "a", "*"):
        return list(range(1, count + 1))
    return [int(x) for x in raw.replace(",", " ").split()]  # raises ValueError if not numbers


def _rank_key(r: dict):
    """Rank by failure value desc, unscored (None) last — the web Queue order."""
    return (r.get("ai_failure_value_score") is None, -(r.get("ai_failure_value_score") or 0))


def score_traces(conn, rows: list[dict], *, backend: str = "auto",
                 model: str | None = None, cap: int | None = None, workers: int = 6) -> int:
    """Score unscored rows for failure value, IN PARALLEL.

    The slow AI-judge step runs in worker threads (each with its own read
    connection via share_flow.score_compute), while DB writes happen serially on
    `conn` (single writer; WAL handles the readers) — so N traces take about one
    judge call, not N. Only `workers` judge calls are submitted at a time; on
    Ctrl-C we stop queueing new calls, cancel any submitted calls that have not
    started, and return what's done (in-flight calls may still finish). Mutates
    rows in place and persists.
    """
    from concurrent.futures import FIRST_COMPLETED, wait

    unscored = [r for r in rows if r.get("ai_failure_value_score") is None]
    if cap is not None:
        unscored = unscored[:cap]
    if not unscored:
        return 0
    total = len(unscored)
    n_workers = max(1, min(workers, total))
    print(f"  {DIM}Scoring {total} unscored trace(s) for failure value "
          f"({n_workers} in parallel, AI judge — Ctrl-C to skip the rest)…{RST}")
    done = 0
    pool = ThreadPoolExecutor(max_workers=n_workers)
    rows_iter = iter(unscored)
    futures = {}

    def submit_next() -> bool:
        try:
            r = next(rows_iter)
        except StopIteration:
            return False
        sid = r["session_id"]
        futures[pool.submit(share_flow.score_compute, sid, backend=backend, model=model)] = r
        return True

    for _ in range(n_workers):
        submit_next()

    try:
        while futures:
            completed, _pending = wait(futures, timeout=0.1, return_when=FIRST_COMPLETED)
            if not completed:
                continue
            for fut in completed:
                r = futures.pop(fut)
                sid = r["session_id"]
                res = fut.result()
                if not res.get("ok"):
                    print(f"\r  {DIM}scored {done}/{total}…{RST}", end="", flush=True)
                    submit_next()
                    continue
                share_flow.persist_score(conn, sid, res["fields"])  # serial write on main conn
                r["ai_failure_value_score"] = res.get("failure_value")
                if res.get("display_title"):
                    r["ai_display_title"] = res["display_title"]
                done += 1
                print(f"\r  {DIM}scored {done}/{total}…{RST}", end="", flush=True)
                submit_next()
        print()
    except KeyboardInterrupt:
        for fut in futures:
            fut.cancel()
        print(f"\n  {YEL}Skipped remaining scoring ({done}/{total} done).{RST}")
        return done
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return done


def select_queue_rows(rows: list[dict], settings: dict, args, *, limit: bool = True) -> list[dict]:
    """Filter to shareable, in-window traces (+ optional search/project/min-fv),
    then rank by failure value descending with unscored (None) last — the web
    Queue ordering. With limit=False, returns the full ranked set (no cap)."""
    out = [
        r for r in rows
        if not session_matches_excluded_projects(r, settings["excluded_projects"])
        # Use the same effective hold-state as the web/release gate so EXPIRED
        # embargoes are shareable (raw hold_state would wrongly drop them).
        and effective_hold_state(r.get("hold_state"), r.get("embargo_until")) in SHAREABLE_HOLD_STATES
        and not r.get("shared_at")
        and _in_time_range(r, args.time_range)
    ]
    if args.project:
        out = [r for r in out if args.project.lower() in (r.get("project") or "").lower()]
    if args.min_failure_value is not None:
        out = [r for r in out if (r.get("ai_failure_value_score") or 0) >= args.min_failure_value]
    if args.search:
        q = args.search.lower()
        out = [r for r in out if q in (
            (r.get("display_title") or "") + " " + (r.get("ai_display_title") or "")
            + " " + (r.get("project") or "")).lower()]
    out.sort(key=_rank_key)
    return out[:args.limit] if limit else out


def step_queue(conn, settings, args) -> list[dict]:
    step(1, "Queue — select traces")
    # Fetch recent candidates (so time windows are accurate), then rank by value.
    candidates = query_sessions(conn, status=args.status, source=args.source,
                                limit=5000, sort="end_time", order="desc")

    # By default (like the web), score the in-window unscored traces on open so
    # the queue shows real failure values instead of "—". Skip with --no-score.
    # (Scored before the --min-failure-value filter so freshly-scored ones can
    # still qualify; only runs if an agent backend is available.)
    if not getattr(args, "no_score", False):
        try:
            resolve_backend("auto")
            backend_ok = True
        except Exception:  # noqa: BLE001
            backend_ok = False
        if backend_ok:
            # Scoring uses the backend's fast default model (Claude → haiku,
            # Codex → gpt-5.4-mini) via score_session; --score-model overrides.
            score_model = getattr(args, "score_model", None)
            from types import SimpleNamespace
            pool_args = SimpleNamespace(**{**vars(args), "min_failure_value": None})
            pool = select_queue_rows(candidates, settings, pool_args, limit=False)
            ready_ids = {
                r["session_id"] for r in query_unscored_sessions(
                    conn,
                    limit=5000,
                    source=args.source,
                    settle_seconds=SCORE_SETTLE_SECONDS,
                )
            }
            pool = [r for r in pool if r["session_id"] in ready_ids]
            score_traces(conn, pool, model=score_model, cap=args.limit)

    rows = select_queue_rows(candidates, settings, args)
    if not rows:
        ranges = {"today": "the past 24h", "weekly": "the past 7 days", "all": "the index"}
        hint = "" if args.time_range == "all" else "  (try --weekly or --all)"
        die(f"No shareable traces found in {ranges[args.time_range]}.{hint}")

    label = {"today": "past 24h", "weekly": "past 7 days", "all": "all time"}[args.time_range]
    unscored = sum(1 for r in rows if r.get("ai_failure_value_score") is None)
    print(f"  {DIM}Showing {len(rows)} shareable trace(s) — {label}, ranked by failure "
          f"value (limit {args.limit}).{RST}")
    if unscored:
        print(f"  {DIM}{unscored} still unscored (no agent backend, scoring skipped via "
              f"--no-score, trace still active/recent, or it failed) — run "
              f"`clawjournal score`; listed last.{RST}")

    do_summary = args.summary or bool(args.summary_model)
    ensure_titles(conn, rows, do_summary, summary_model=args.summary_model)
    for r in rows:
        title = resolve_title(r, do_summary)
        if not do_summary and _looks_like_system_prompt(title):
            title = user_prompt_title(conn, r) or title
        r["_clawshare_title"] = title

    print(f"\n  {'#':>3}  {'Fail':>4} {'Last turn':<11} {'Source':<8} {'Msgs':>5} "
          f"{'Tokens':>9} {'Tools':>5}  Title")
    print("  " + "-" * 108)
    for i, r in enumerate(rows, 1):
        msgs = (r.get("user_messages") or 0) + (r.get("assistant_messages") or 0)
        toks = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
        tools = r.get("tool_uses") or 0
        fv = r.get("ai_failure_value_score")
        fv_str = str(fv) if fv is not None else "—"
        dt = _parse_ts(r.get("end_time"))
        when = dt.astimezone().strftime("%m-%d %H:%M") if dt else "—"
        print(f"  {i:>3}  {fv_str:>4} {when:<11} {r.get('source', ''):<8} "
              f"{msgs:>5} {toks:>9,} {tools:>5}  {trace_title(r)}")
    print()
    print(f"  {DIM}Tip: filter with --search/--project/--min-failure-value, widen with "
          f"--weekly/--all, more with --limit.{RST}")

    if args.indices:
        idxs = args.indices
    else:
        raw = ask(f"{BOLD}Enter trace #(s) to share{RST} (space/comma separated, "
                  f"or {BOLD}all{RST}): ")
        if not raw:
            die("Nothing selected.")
        try:
            idxs = _parse_selection(raw, len(rows))
        except ValueError:
            die("Enter trace numbers, or 'all'.")
    chosen = []
    for n in idxs:
        if n < 1 or n > len(rows):
            die(f"Index {n} out of range (1–{len(rows)}).")
        chosen.append(rows[n - 1])
    print(f"{GRN}✓ {len(chosen)} trace(s) queued{RST}")
    return chosen


# ---- step 2: redact ---------------------------------------------------------

def render_transcript(redacted_session: dict, max_msgs: int | None = None, max_chars: int = 2000):
    all_msgs = redacted_session.get("messages") or []
    msgs, hidden = [], 0
    for m in all_msgs:
        c = m.get("content")
        if not isinstance(c, str) or not c.strip() or c.strip() == "None":
            hidden += 1
            continue
        msgs.append(m)
    shown = msgs if max_msgs is None else msgs[:max_msgs]
    head = f"all {len(msgs)}" if max_msgs is None else f"first {len(shown)} of {len(msgs)}"
    note = f"({head} content message{_plural(len(msgs))}"
    if hidden:
        note += f", {hidden} empty/thinking hidden"
    note += " — already scrubbed)"
    print(f"  {DIM}{note}{RST}")
    for m in shown:
        role = m.get("role", "?")
        content = m["content"]
        color = CYN if role == "user" else (YEL if role == "assistant" else DIM)
        body = content
        if len(body) > max_chars:
            body = body[:max_chars] + f"  …(+{len(content) - max_chars} more chars)"
        lines = body.split("\n")
        print(f"  {color}{role:>9}{RST}  {lines[0] if lines else ''}")
        for ln in lines[1:]:
            print(f"             {ln}")
    if max_msgs is not None and len(msgs) > max_msgs:
        print(f"  {DIM}… {len(msgs) - max_msgs} more — press [v] for the full transcript{RST}")


def _build_records(conn, settings, chosen, ai_pii):
    recs = []
    for r in chosen:
        detail = get_session_detail(conn, r["session_id"])
        if detail is None:
            die(f"Session {r['session_id']} not found.")
        rec = build_redaction_record(conn, detail, settings, ai_pii)
        rec["row"] = r
        recs.append(rec)
    return recs


def step_redact(conn, settings, chosen, assume_yes, ai_pii_requested) -> tuple[list[dict], bool]:
    step(2, "Redact — scrub PII")
    # AI on/off disclosure (#3)
    if ai_pii_requested:
        print(f"  {BOLD}AI PII review: ON{RST} {DIM}— each trace is analysed by an agent "
              f"(may take a moment).{RST}")
    else:
        print(f"  {BOLD}AI PII review: OFF{RST} {DIM}— deterministic rules only "
              f"(enable with --ai-pii-review).{RST}")

    scrubbed = _build_records(conn, settings, chosen, ai_pii_requested)
    package_ai, uniform = effective_ai_pii(scrubbed, ai_pii_requested)

    # Keep preview == shipped: if AI was requested but couldn't run everywhere,
    # let the user retry or degrade ALL traces to rules-only before continuing (#2,#3).
    while ai_pii_requested and not uniform:
        n_missing = sum(1 for s in scrubbed if s.get("ai_coverage") != "full")
        print(f"  {YEL}AI PII review was unavailable for {n_missing}/{len(scrubbed)} "
              f"trace(s).{RST}")
        choice = "p" if assume_yes else ask(
            f"  {BOLD}[r]{RST}etry AI  {BOLD}[p]{RST}roceed rules-only "
            f"(what you'll review is what ships)  {BOLD}[a]{RST}bort: ").lower()
        if choice in ("r", "retry"):
            scrubbed = _build_records(conn, settings, chosen, True)
            package_ai, uniform = effective_ai_pii(scrubbed, True)
        elif choice in ("a", "abort"):
            die("Aborted before packaging.")
        else:  # proceed rules-only — rebuild uniformly so the preview matches
            scrubbed = _build_records(conn, settings, chosen, False)
            package_ai = False
            print(f"  {DIM}Proceeding rules-only.{RST}")
            break

    print(f"\n  {DIM}Redacting your traces — categories flagged per trace:{RST}")
    for i, s in enumerate(scrubbed, 1):
        verdict = (f"{GRN}clear{RST}" if s["status"] == "clear" else f"{YEL}needs review{RST}")
        print(f"\n  {BOLD}[{i}]{RST} {verdict} · {_coverage_label(s)} · "
              f"{s['count']} redaction{_plural(s['count'])} · {trace_title(s['row'])}")
        cats = category_breakdown(s)
        if cats:
            for c in cats:
                print(f"        {DIM}•{RST} {c}")
        else:
            print(f"        {DIM}nothing matched the deterministic rules{RST}")

    if not assume_yes:
        while True:
            raw = ask(f"\n{BOLD}Preview a scrubbed transcript?{RST} "
                      f"Enter trace # ({DIM}blank = skip{RST}): ")
            if not raw:
                break
            try:
                n = int(raw)
                assert 1 <= n <= len(scrubbed)
            except (ValueError, AssertionError):
                print(f"{YEL}Enter a # between 1 and {len(scrubbed)}, or blank to skip.{RST}")
                continue
            print()
            render_transcript(scrubbed[n - 1]["redacted"])
    print(f"{GRN}✓ scrubbed {len(scrubbed)} trace(s){RST}")
    return scrubbed, package_ai


# ---- step 3: review (share-local; does NOT mutate review_status) ------------

def _show_redaction_detail(rec: dict):
    items = category_breakdown(rec)
    print(f"  {BOLD}What was redacted{RST} ({_coverage_label(rec)}):")
    if items:
        for it in items:
            print(f"    • {it}")
    else:
        print(f"    {DIM}Nothing matched the deterministic rules.{RST}")
        cov = rec.get("ai_coverage", "disabled")
        if cov == "disabled":
            print(f"    {DIM}AI review off.{RST}")
        elif cov == "rules_only":
            print(f"    {DIM}AI review unavailable.{RST}")


def step_review(conn, scrubbed: list[dict], assume_yes: bool,
                include_needs_review: bool = False) -> list[dict]:
    step(3, "Review — inspect & include before packaging")
    print(f"  {DIM}Including a trace here affects this bundle only — it does not change "
          f"its local review status.{RST}")
    print(f"\n  {'#':>3}  {'Status':<14} Title")
    print("  " + "-" * 80)
    for i, s in enumerate(scrubbed, 1):
        verdict = (f"{GRN}clear{RST}        " if s["status"] == "clear"
                   else f"{YEL}needs review{RST} ")
        print(f"  {i:>3}  {verdict:<23} {trace_title(s['row'])}")

    clear = [s for s in scrubbed if s["status"] == "clear"]
    review = [s for s in scrubbed if s["status"] != "clear"]
    included_ids: set[str] = set()

    if assume_yes:
        # Non-interactive: include clear traces. Needs-review traces are NOT
        # silently included (we can't show their redacted preview here) — they
        # require the explicit --include-needs-review opt-in.
        included_ids = {s["row"]["session_id"] for s in clear}
        if review:
            if include_needs_review:
                included_ids |= {s["row"]["session_id"] for s in review}
                print(f"  {YEL}Including {len(review)} needs-review trace(s) unreviewed "
                      f"(--include-needs-review).{RST}")
            else:
                die(f"{len(review)} trace(s) need review and --yes can't show their redacted "
                    f"preview. Re-run interactively to inspect them, or pass "
                    f"--include-needs-review to include them unreviewed.")
    else:
        if clear and yesno(f"\n{BOLD}Include all {len(clear)} clear trace(s)?{RST}", default_yes=True):
            included_ids |= {s["row"]["session_id"] for s in clear}
        if review:
            print(f"\n  {YEL}{len(review)} trace(s) need review — inspect each before packaging.{RST}")
            for n, s in enumerate(review, 1):
                print(f"\n{CYN}  ── review {n}/{len(review)} ─ {trace_title(s['row'], 60)}{RST}")
                _show_redaction_detail(s)
                print(f"  {BOLD}Preview:{RST}")
                render_transcript(s["redacted"], max_msgs=6, max_chars=700)
                while True:
                    choice = ask(f"  {BOLD}[v]{RST}iew full transcript  "
                                 f"{BOLD}[i]{RST}nclude  {BOLD}[r]{RST}emove: ").lower()
                    if choice in ("v", "view"):
                        print()
                        render_transcript(s["redacted"])
                        continue
                    if choice in ("i", "include", "y", "yes"):
                        included_ids.add(s["row"]["session_id"])
                        print(f"  {GRN}✓ included{RST}")
                        break
                    if choice in ("r", "remove", "n", "no", "skip"):
                        print(f"  {DIM}removed from bundle{RST}")
                        break
                    print(f"  {YEL}Please enter v, i, or r.{RST}")

    included = [s for s in scrubbed if s["row"]["session_id"] in included_ids]
    if not included:
        die("No traces included — nothing to package.")
    dropped = len(scrubbed) - len(included)
    print(f"{GRN}✓ {len(included)} trace(s) included"
          + (f", {dropped} dropped" if dropped else "") + RST)
    return included


# ---- step 4: package (with blocked-trace removal/retry, #7) -----------------

def step_package(conn, settings, included: list[dict], package_ai: bool, args):
    step(4, "Package — create + seal bundle")
    recs = list(included)
    while True:
        session_ids = [s["row"]["session_id"] for s in recs]
        blockers = gate_blockers(conn, session_ids)
        if blockers:
            print(f"{RED}Release gate blocked these traces (hold/embargo):{RST}")
            for b in blockers:
                print(f"  • {b.get('session_id', '?')[:12]}  {b.get('reason', '')}")
            die("Resolve hold/embargo state (e.g. `clawjournal release <id>`) and retry.")

        print(f"  {DIM}sealing… (redact → TruffleHog → PII pass → re-scan){RST}")
        res = share_flow.package(conn, session_ids, settings, ai_pii=package_ai, note=args.note)
        if res["ok"]:
            export_dir, manifest, share_id = res["export_dir"], res["manifest"], res["share_id"]
            try:
                zip_size = len(build_zip(export_dir))
            except Exception:  # noqa: BLE001
                zip_size = 0
            # #2: refuse to ship a sealed bundle whose AI coverage differs from
            # the preview the user reviewed (seal can silently fall back to rules-only).
            ok, why = share_flow.verify_coverage(manifest, package_ai)
            if not ok:
                die(f"AI coverage mismatch — {why}. Refusing to share a bundle that differs "
                    f"from what you reviewed. Re-run (the seal will retry AI), or run without "
                    f"--ai-pii-review to review and ship rules-only consistently.")
            print(f"{GRN}✓ packaged{RST}")
            print(f"  bundle:   {share_id[:8]}   sessions: {len(manifest.get('sessions', []))}")
            print(f"  path:     {export_dir}")
            print(f"  zip size: {zip_size / 1024:.1f} KiB")
            rsum = manifest.get("redaction_summary", {})
            if rsum:
                print(f"  privacy:  {DIM}{rsum}{RST}")
            return share_id, export_dir

        blocked = res.get("blocked_sessions") or []
        if blocked:
            blocked_ids = {b if isinstance(b, str) else b.get("session_id") for b in blocked}
            reason = res.get("error") or "TruffleHog blocked the share."
            print(f"{RED}Packaging blocked by the final secret scan:{RST}")
            print(f"  {DIM}{reason}{RST}")
            # Same as the web: a blocked trace can't be submitted, so it is removed
            # (not retained) and the rest are retried.
            print(f"  {RED}Removed blocked trace(s):{RST}")
            for s in recs:
                if s["row"]["session_id"] in blocked_ids:
                    print(f"  • {s['row']['session_id'][:12]}  {trace_title(s['row'], 50)}")
            remaining = [s for s in recs if s["row"]["session_id"] not in blocked_ids]
            if not remaining:
                die("All included traces were blocked by the final secret scan — nothing to "
                    "package. To keep one, go back and add a redaction rule (allowlist / custom "
                    "string) so the secret is scrubbed, then retry.")
            print(f"  {DIM}Retrying with the remaining {len(remaining)}…{RST}")
            recs = remaining
            continue
        die(res.get("error", "Packaging failed."))


# ---- step 5: submit (explicit consent #6; hosted fallback #8) ---------------

def _ensure_email(assume_yes: bool) -> bool:
    st = share_flow.upload_status()
    if st.get("token_valid"):
        print(f"  {GRN}✓ upload token valid for {st['verified_email']}{RST}")
        return True
    if assume_yes:
        print(f"  {YEL}No valid upload token (email verification is interactive). "
              f"Skipping submit.{RST}")
        return False
    print(f"  {YEL}Upload token missing/expired — verify your academic email.{RST}")
    default_email = st.get("verified_email") or ""
    email = ask(f"  Academic email{f' [{default_email}]' if default_email else ''}: ", default_email)
    if not email:
        print(f"  {YEL}No email — skipping submit.{RST}")
        return False
    try:
        share_flow.request_email_verification(email)
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}Could not send verification code: {exc}{RST}")
        return False
    print(f"  {DIM}A 6-digit code was emailed to {email}.{RST}")
    code = ask("  Enter the code: ")
    if not code:
        return False
    try:
        share_flow.confirm_email_verification(email, code)
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}Verification failed: {exc}{RST}")
        return False
    return bool(share_flow.upload_status().get("token_valid"))


def step_submit(conn, settings, share_id: str, package_ai: bool, args):
    step(5, "Submit — consent & upload")

    # #8: check destination; fall back to download-only when hosted isn't available.
    dest = hosted_destination()
    if not dest["can_submit"]:
        print(f"  {YEL}{dest.get('message') or 'Hosted submission is unavailable.'}{RST}")
        if dest.get("support_contact"):
            print(f"  {DIM}Support: {dest['support_contact']}{RST}")
        print(f"  {DIM}→ Bundle is packaged; you can still download it below.{RST}")
        return None

    try:
        doc = share_flow.consent()
    except Exception as exc:  # noqa: BLE001
        print(f"  {YEL}Could not load consent terms ({exc}) — skipping submit; "
              f"download still available.{RST}")
        return None
    cv = doc.get("consent_version") or ""
    rv = doc.get("retention_policy_version") or ""
    consent_text = (doc.get("consent_text") or "").strip()
    retention_text = (doc.get("retention_text") or "").strip()
    if not cv or not rv or not (consent_text or retention_text):
        print(f"  {YEL}Hosted service did not return consent terms — skipping submit.{RST}")
        return None

    print(f"\n  {BOLD}Consent and retention{RST}  {DIM}(consent {cv} · retention {rv}){RST}")
    print(f"  {DIM}{'─' * 60}{RST}")
    for para in (consent_text, retention_text):
        if not para:
            continue
        for ln in para.split("\n"):
            for wrapped in (textwrap.wrap(ln, 78) or [""]):
                print(f"  {wrapped}")
        print()
    print(f"  {DIM}{'─' * 60}{RST}")

    # #6: consent must be explicit. -y/--yes does NOT auto-accept.
    if args.accept_terms and args.certify_ownership:
        print(f"  {DIM}Consent provided via --accept-terms --certify-ownership.{RST}")
    elif args.yes:
        print(f"  {YEL}Consent is required and is never auto-accepted by --yes. "
              f"Re-run with --accept-terms --certify-ownership, or run interactively. "
              f"Bundle packaged but not submitted.{RST}")
        return None
    else:
        if not yesno(f"  {BOLD}I accept the displayed consent and data-use terms.{RST}"):
            print(f"{YEL}Terms not accepted — bundle packaged but not submitted.{RST}")
            return None
        if not yesno(f"  {BOLD}I certify this bundle is mine to submit and contains no "
                     f"third-party confidential material.{RST}"):
            print(f"{YEL}Ownership not certified — bundle packaged but not submitted.{RST}")
            return None

    if not _ensure_email(args.yes):
        return None

    print(f"  {DIM}uploading to hosted research…{RST}")
    result = share_flow.submit(
        conn, share_id, accept_terms=True, ownership_certification=True,
        consent_version=cv, retention_policy_version=rv, settings=settings, ai_pii=package_ai,
    )
    if not result.get("ok"):
        suffix = f"  (status {result['status']})" if result.get("status") else ""
        print(f"  {RED}Upload failed: {result.get('error', 'unknown')}{suffix}{RST}")
        print(f"  {DIM}→ Bundle is packaged; you can still download it below.{RST}")
        return None
    print(f"  {GRN}✓ uploaded{RST}")
    return result


# ---- step 6: done (+ optional download) -------------------------------------

def _resolve_download_dest(ans: str, default: Path, fname: str):
    """Resolve the download prompt answer to a destination Path, or None to skip.
    Enter/y/yes = default; n/no/skip = None; anything else = a path (a directory
    gets the filename appended). Guards against a stray 'y' becoming the filename."""
    low = ans.strip().lower()
    if low in ("n", "no", "q", "skip"):
        return None
    dest = default if low in ("", "y", "yes") else Path(ans.strip()).expanduser()
    if dest.is_dir() or str(dest).endswith(os.sep):
        dest = dest / fname
    return dest


def step_done(share_id: str, export_dir: Path, result, assume_yes: bool, download_default: bool):
    step(6, "Done")
    if result:
        print(f"  {GRN}{BOLD}✓ Shared to hosted research!{RST}")
        print(f"  receipt:    {result.get('receipt_id')}")
        if result.get("hosted_submission_url"):
            print(f"  submission: {result['hosted_submission_url']}")
        print(f"  sessions:   {result.get('session_count')}")
    else:
        print(f"  {YEL}Bundle packaged but not submitted.{RST}")
    print(f"  bundle dir: {export_dir}")

    fname = f"clawjournal-share-{share_id[:8]}.zip"
    default = Path("~/Downloads").expanduser() / fname
    if assume_yes:
        if not download_default:
            return
        dest = default
    else:
        ans = ask(f"\n  {BOLD}Download the bundle zip?{RST} "
                  f"{DIM}[Enter/y = {default}, n = skip, or type a path]{RST}\n  > ")
        dest = _resolve_download_dest(ans, default, fname)
        if dest is None:
            return
    try:
        data = build_zip(export_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"  {GRN}✓ saved {len(data) / 1024:.1f} KiB → {dest}{RST}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}Download failed: {exc}{RST}")


# ---- arg wiring -------------------------------------------------------------

def add_interactive_flags(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Interactive-wizard options (no positional, no --status/--note/--ai-pii-review,
    which the `share` subcommand already defines). Reused by both entry points."""
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--weekly", dest="time_range", action="store_const", const="weekly",
                      help="traces from the past 7 days (default: past 24h)")
    when.add_argument("--all", dest="time_range", action="store_const", const="all",
                      help="all traces, any date")
    parser.set_defaults(time_range="today")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--source", default=None, help="only this source (claude, codex, …)")
    src.add_argument("--codex", dest="source", action="store_const", const="codex",
                     help="only Codex traces")
    src.add_argument("--claude", dest="source", action="store_const", const="claude",
                     help="only Claude traces")
    parser.add_argument("--search", default=None, metavar="TEXT",
                        help="only traces whose title/project contains TEXT")
    parser.add_argument("--project", default=None, help="only traces in this project")
    parser.add_argument("--min-failure-value", type=int, default=None, metavar="N",
                        help="only traces with failure value >= N (1-5)")
    parser.add_argument("--limit", type=int, default=40, help="max traces to list (default 40)")
    parser.add_argument("--summary", action="store_true",
                        help="show AI-summarized titles using the selected backend default; default shows original titles")
    parser.add_argument("--summary-model", default=None, metavar="MODEL",
                        help="model for --summary titles (implies --summary)")
    parser.add_argument("--no-score", action="store_true",
                        help="don't score unscored traces on open (default: score them so the "
                             "queue shows real failure values, like the web)")
    parser.add_argument("--score-model", default=None, metavar="MODEL",
                        help="model for on-open scoring (default: backend default)")
    parser.add_argument("--accept-terms", action="store_true",
                        help="non-interactively accept the hosted consent/data-use terms")
    parser.add_argument("--certify-ownership", action="store_true",
                        help="non-interactively certify the bundle is yours to submit")
    parser.add_argument("--download", action="store_true",
                        help="with --yes, also write the bundle zip to ~/Downloads")
    parser.add_argument("--no-refresh", action="store_true",
                        help="skip the startup index scan (use the existing index as-is)")
    parser.add_argument("--include-needs-review", action="store_true",
                        help="with --yes, include needs-review traces unreviewed (default: refuse)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="assume yes for preview/review/download prompts (NOT consent)")
    return parser


# Interactive-only option dests + the flag spelling to show when misused outside
# `--interactive`. Used to reject silently-ignored flags on non-interactive share.
_INTERACTIVE_ONLY_CHECKS = (
    ("time_range", lambda v: v not in (None, "today"), "--weekly/--all"),
    ("source", lambda v: bool(v), "--source/--codex/--claude"),
    ("search", lambda v: bool(v), "--search"),
    ("project", lambda v: bool(v), "--project"),
    ("min_failure_value", lambda v: v is not None, "--min-failure-value"),
    ("limit", lambda v: v not in (None, 40), "--limit"),
    ("summary", lambda v: bool(v), "--summary"),
    ("summary_model", lambda v: bool(v), "--summary-model"),
    ("no_score", lambda v: bool(v), "--no-score"),
    ("score_model", lambda v: bool(v), "--score-model"),
    ("yes", lambda v: bool(v), "--yes"),
    ("accept_terms", lambda v: bool(v), "--accept-terms"),
    ("certify_ownership", lambda v: bool(v), "--certify-ownership"),
    ("download", lambda v: bool(v), "--download"),
    ("no_refresh", lambda v: bool(v), "--no-refresh"),
    ("include_needs_review", lambda v: bool(v), "--include-needs-review"),
)


def noninteractive_share_rejections(args) -> list[str]:
    """Flags/values that are only meaningful in the interactive wizard and would
    be silently ignored by non-interactive `clawjournal share`. Returns the list
    of offending flag descriptions (empty = fine)."""
    bad = [label for dest, is_set, label in _INTERACTIVE_ONLY_CHECKS
           if is_set(getattr(args, dest, None))]
    # #5: non-interactive one-step share must not auto-package new/blocked sessions.
    if getattr(args, "status", None) in ("new", "blocked"):
        bad.append("--status new/blocked (interactive only; non-interactive supports "
                   "approved/shortlisted)")
    return bad


def add_share_cli_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Full flag set for the standalone `clawshare` alias."""
    parser.add_argument("indices", nargs="*", type=int,
                        help="trace #(s) from the list (non-interactive)")
    parser.add_argument("--status", default=None,
                        choices=["new", "shortlisted", "approved", "blocked"],
                        help="filter by review status (default: any)")
    parser.add_argument("--note", default=None, help="optional submission note")
    parser.add_argument("--ai-pii-review", action="store_true",
                        help="run an AI-assisted PII pass (preview + packaging stay consistent)")
    add_interactive_flags(parser)
    return parser


def refresh_index(source_filter: str | None = None) -> int:
    """One-shot incremental scan (same as `clawjournal scan` / the daemon's
    initial scan), so the wizard sees fresh traces without a running
    `clawjournal serve`. Returns the number of newly indexed sessions."""
    from .workbench.daemon import Scanner
    results = Scanner(source_filter=source_filter).scan_once()
    return sum(results.values())


def _normalize_indices(args):
    """Accept indices either as the wizard's int positional or as `share`'s
    string `session_ids` positional (interpreted as list #s)."""
    if getattr(args, "indices", None):
        return
    raw = getattr(args, "session_ids", None) or []
    try:
        args.indices = [int(x) for x in raw]
    except (TypeError, ValueError):
        die("Interactive selectors must be the trace #s shown in the list.")


def run(args) -> None:
    """Run the interactive share wizard against a parsed argparse namespace."""
    _normalize_indices(args)
    # Refresh the index first so the wizard reflects recent traces even when
    # `clawjournal serve` isn't running (its background scanner is the only other
    # thing that ingests). Incremental, so it's cheap on repeat.
    if not getattr(args, "no_refresh", False):
        print(f"{DIM}Refreshing trace index…{RST}")
        try:
            n = refresh_index(args.source)
            print(f"{DIM}{('Indexed %d new session(s).' % n) if n else 'Index already up to date.'}"
                  f"{RST}")
        except Exception as exc:  # noqa: BLE001
            print(f"{YEL}Index refresh skipped: {exc}{RST}")
    config = load_config()
    conn = open_index()
    try:
        settings = get_effective_share_settings(conn, config)
        ai_pii_requested = bool(args.ai_pii_review or settings.get("ai_pii_review_enabled"))
        chosen = step_queue(conn, settings, args)
        scrubbed, package_ai = step_redact(conn, settings, chosen, args.yes, ai_pii_requested)
        included = step_review(conn, scrubbed, args.yes,
                               include_needs_review=getattr(args, "include_needs_review", False))
        share_id, export_dir = step_package(conn, settings, included, package_ai, args)
        result = step_submit(conn, settings, share_id, package_ai, args)
        step_done(share_id, export_dir, result, args.yes,
                  download_default=getattr(args, "download", False) or result is None)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point for the thin `clawshare` alias."""
    parser = argparse.ArgumentParser(
        prog="clawshare",
        description="Interactive ClawJournal Share wizard "
                    "(alias for `clawjournal share --interactive`).",
    )
    add_share_cli_args(parser)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
