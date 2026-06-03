"""clawshare — the full ClawJournal "Share" wizard, as a keyboard-only CLI.

Faithfully mirrors every step of the `clawjournal serve` web Share wizard,
in order, with no skipping:

    1. QUEUE    pick traces by # from a list
    2. REDACT   scrub PII (deterministic); OPTIONAL preview of scrubbed transcript (skippable)
    3. REVIEW   review redaction summary per trace; must approve before packaging
    4. PACKAGE  create + seal the bundle (writes sessions.jsonl/manifest/trufflehog*)
    5. SUBMIT   accept terms + certify ownership, then upload to hosted research
    6. DONE     receipt; OPTIONAL download of the bundle zip

Exposed two ways (both ship with the package):
    clawshare                 # standalone console script
    clawjournal share-cli     # subcommand alias

Usage:
    clawshare                      # past 24h; original (non-AI) titles, fast
    clawshare --summary            # AI-summarized titles (Haiku)
    clawshare --weekly             # past 7 days (168h) of traces
    clawshare --all                # every trace, any date/status
    clawshare --codex              # only Codex traces
    clawshare --claude --limit 60  # this many Claude traces (default cap 40)
    clawshare --summary-model haiku  # pick the model used for --summary titles
    clawshare --ai-pii-review      # run AI-assisted PII pass during packaging
    clawshare -y 3 7               # non-interactive: traces 3 & 7, assume yes
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
from .workbench.index import (
    apply_share_redactions,
    create_share,
    get_effective_share_settings,
    get_session_detail,
    get_share,
    open_index,
    query_sessions,
    release_gate_blockers,
    session_matches_excluded_projects,
    update_session,
)
from .workbench.daemon import (
    _build_share_zip,
    _prepare_share_export_for_upload,
    confirm_email_verification,
    fetch_hosted_consent,
    hosted_upload_status,
    request_email_verification,
    submit_share_to_hosted,
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


def resolve_title(r: dict, summarized: bool) -> str:
    """Title to display for a trace, by mode:
    - default (summarized=False): exactly what the web Queue step shows —
      the raw ``display_title`` (or "Untitled"), system prompts and all.
    - --summary (summarized=True): the AI-summarized title — scoring's
      ai_display_title, or a clawshare summary ensure_titles placed on the row;
      falls back to the raw web title if no AI summary is available."""
    raw = (r.get("display_title") or "").strip().replace("\n", " ") or "Untitled"
    if summarized:
        return (r.get("ai_display_title") or "").strip() or raw
    return raw


def trace_title(r: dict, width: int = 52) -> str:
    """Truncated title for display. Uses the mode-aware title precomputed onto
    the row (``_clawshare_title``); falls back to the raw/original title."""
    t = r.get("_clawshare_title")
    if t is None:
        t = resolve_title(r, False)
    t = (t or "").replace("\n", " ")
    return (t[: width - 1] + "…") if len(t) > width else t


# ---- redaction bucketing / status (mirrors web `li`, `gi`, `hi`) ------------

def _bucket_for(type_str: str) -> str:
    """Classify a redaction_log entry type into one of the 6 display buckets.
    Mirrors the web frontend `li()` classifier exactly."""
    t = (type_str or "").lower()
    if "email" in t:
        return "emails"
    if "url" in t:
        return "urls"
    if "path" in t or "username" in t or "home" in t:
        return "paths"
    if "time" in t or "date" in t:
        return "timestamps"
    if t.startswith("trufflehog") or any(k in t for k in ("token", "key", "secret", "jwt", "cred", "auth")):
        return "tokens"
    return "other"


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def what_was_redacted(rec: dict) -> list[str]:
    """Per-category redaction breakdown for a trace, using the web's category
    labels (Secrets & credentials / Email addresses / File paths & usernames /
    Timestamps coarsened / URLs) with the TruffleHog subset called out."""
    b = rec["buckets"]
    th = rec.get("th_hits", 0)
    out: list[str] = []
    if b["tokens"]:
        label = "Secrets & credentials"
        if th:
            label += f" (incl. {th} via TruffleHog)"
        out.append(f"{label}: {b['tokens']}")
    if b["emails"]:
        out.append(f"Email addresses: {b['emails']}")
    if b["paths"]:
        out.append(f"File paths & usernames: {b['paths']}")
    if b["urls"]:
        out.append(f"URLs: {b['urls']}")
    if b["timestamps"]:
        out.append(f"Timestamps coarsened: {b['timestamps']}")
    if b["other"]:
        out.append(f"Other: {b['other']}")
    counts: dict[str, int] = {}
    for f in rec.get("ai_findings") or []:
        name = (f.get("entity_type") or "").replace("_", " ").strip() or "pii"
        counts[name] = counts.get(name, 0) + 1
    for name, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        out.append(f"AI-flagged {name}: {c}")
    return out


def trace_status(rec: dict) -> str:
    """'clear' or 'review' — mirrors web `hi()`."""
    cov = rec.get("ai_coverage", "disabled")
    if cov in ("rules_only", "disabled"):
        return "review"
    if any((f.get("confidence", 0) or 0) < 0.85 for f in (rec.get("ai_findings") or [])):
        return "review"
    return "clear"


# ---- time-range filtering (by last message / end_time) ----------------------

def _parse_ts(ts):
    """Parse an ISO timestamp into an aware datetime (mirrors index._parse_score_ts)."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _in_time_range(row: dict, time_range: str) -> bool:
    """True if the trace's LAST turn (end_time) falls in the range. Uses rolling
    windows (not calendar boundaries) so the list doesn't empty out at midnight:
    'today' = the past 24h, 'weekly' = the past 168h (7 days)."""
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


# ---- LLM trace summaries (for titles) ---------------------------------------

def summarize_trace(session_detail: dict, *, backend: str = "auto",
                    model: str | None = None) -> str:
    """Return a short one-line title summarizing the whole trace, via
    clawjournal's agent backend. Returns '' on any failure."""
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
                backend=backend,
                cwd=tmp_path,
                task_prompt=prompt,
                model=model,
                timeout_seconds=60,
                codex_sandbox="read-only",
                openclaw_message=prompt + f"\nRead: {tmp_path / 'transcript.md'}",
            )
    except Exception:  # noqa: BLE001
        return ""
    out = (result.stdout or "").strip()
    if not out:
        return ""
    # Take the last non-empty line (agents sometimes print preamble first).
    title = [ln.strip() for ln in out.splitlines() if ln.strip()][-1]
    return title.strip().strip('"').strip("'")[:80]


# Fast/cheap default model per backend for one-line title summaries.
_LIGHT_SUMMARY_MODEL = {"claude": "haiku"}


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
    """Only runs with --summary. Fills an LLM summary title (in-memory + a local
    clawshare cache, NOT the scoring column) for any listed trace that lacks a
    real title. Summaries run concurrently with live progress and can be skipped
    with Ctrl-C. Uses a light model per backend by default (e.g. claude/haiku)."""
    if not do_summarize:
        return
    # Reuse previously generated clawshare summaries from the local cache.
    cache = _load_title_cache()
    for r in rows:
        if not (r.get("ai_display_title") or "").strip():
            cached = cache.get(r["session_id"])
            if cached:
                r["ai_display_title"] = cached
    need = [r for r in rows if not (r.get("ai_display_title") or "").strip()]
    if not need:
        return

    import shutil
    from .scoring.backends import resolve_backend
    # Summaries are a tiny side task — prefer claude/haiku (fast & cheap) whenever
    # claude is installed, regardless of the main backend, unless an explicit
    # --summary-model is given.
    if not summary_model and shutil.which("claude"):
        backend, model = "claude", "haiku"
    else:
        try:
            backend = resolve_backend("auto")
        except Exception:  # noqa: BLE001
            print(f"  {DIM}(no agent backend available — using raw titles; "
                  f"run `clawjournal score` or pass --no-summarize to silence){RST}")
            return
        model = summary_model or _LIGHT_SUMMARY_MODEL.get(backend)

    label = f"{backend}/{model}" if model else backend
    print(f"  {DIM}Summarizing {len(need)} title(s) with {label} "
          f"— Ctrl-C to skip and keep raw titles…{RST}")
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
        print(f"\n  {YEL}Skipped remaining summaries — keeping raw titles for those.{RST}")
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

def step_queue(conn, settings, args) -> list[dict]:
    step(1, "Queue — select traces")
    rows = query_sessions(
        conn,
        status=args.status,
        source=args.source,
        limit=5000,
        sort="end_time",
        order="desc",
    )
    rows = [
        r for r in rows
        if not session_matches_excluded_projects(r, settings["excluded_projects"])
        and r.get("hold_state") in (None, "auto_redacted", "released")
        and not r.get("shared_at")
        and _in_time_range(r, args.time_range)
    ]
    if not rows:
        ranges = {"today": "the past 24h", "weekly": "the past 7 days", "all": "the index"}
        hint = "" if args.time_range == "all" else "  (try --weekly or --all)"
        die(f"No shareable traces found in {ranges[args.time_range]}.{hint}")

    rows = rows[:args.limit]
    label = {"today": "past 24h", "weekly": "past 7 days", "all": "all time"}[args.time_range]
    print(f"  {DIM}Showing {len(rows)} shareable trace(s) — {label}"
          f" (limit {args.limit}; use --limit to change).{RST}")

    # Optionally summarize titles (--summary). Default is off for speed; without
    # it, system-prompt-looking titles are hidden behind a placeholder instead.
    do_summary = args.summary or bool(args.summary_model)
    ensure_titles(conn, rows, do_summary, summary_model=args.summary_model)
    for r in rows:
        r["_clawshare_title"] = resolve_title(r, do_summary)

    print(f"\n  {'#':>3}  {'Last turn':<11} {'Source':<8} {'Msgs':>5} {'Tokens':>9} "
          f"{'Tools':>5} {'Fail':>4}  Title")
    print("  " + "-" * 108)
    for i, r in enumerate(rows, 1):
        msgs = (r.get("user_messages") or 0) + (r.get("assistant_messages") or 0)
        toks = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
        tools = r.get("tool_uses") or 0
        fv = r.get("ai_failure_value_score")
        fv_str = str(fv) if fv is not None else "—"
        dt = _parse_ts(r.get("end_time"))
        when = dt.astimezone().strftime("%m-%d %H:%M") if dt else "—"
        print(f"  {i:>3}  {when:<11} {r.get('source', ''):<8} "
              f"{msgs:>5} {toks:>9,} {tools:>5} {fv_str:>4}  {trace_title(r)}")
    print()

    if args.indices:
        idxs = args.indices
    else:
        raw = ask(f"{BOLD}Enter trace #(s) to share{RST} (space/comma separated, e.g. 1 3 5): ")
        if not raw:
            die("Nothing selected.")
        try:
            idxs = [int(x) for x in raw.replace(",", " ").split()]
        except ValueError:
            die("Indices must be numbers.")
    chosen = []
    for n in idxs:
        if n < 1 or n > len(rows):
            die(f"Index {n} out of range (1–{len(rows)}).")
        chosen.append(rows[n - 1])
    print(f"{GRN}✓ {len(chosen)} trace(s) queued{RST}")
    return chosen


# ---- step 2: redact (scrub + optional preview) ------------------------------

def render_transcript(redacted_session: dict, max_msgs: int | None = None,
                      max_chars: int = 2000):
    """Print a scrubbed transcript. Empty / thinking-only messages (no textual
    content) are skipped. Newlines are preserved; each message body is capped at
    max_chars so one giant message can't flood the terminal. max_msgs=None shows
    every message; otherwise only the first max_msgs are shown."""
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
        print(f"  {DIM}… {len(msgs) - max_msgs} more message(s) — press [v] for the full transcript{RST}")


def step_redact(conn, settings, chosen: list[dict], assume_yes: bool, ai_pii: bool) -> list[dict]:
    step(2, "Redact — scrub PII")
    ai_fns = None
    if ai_pii:
        try:
            from ..redaction.pii import review_session_pii_with_agent, apply_findings_to_session
            ai_fns = (review_session_pii_with_agent, apply_findings_to_session)
        except Exception:  # noqa: BLE001
            ai_fns = None
        print(f"  {DIM}AI PII review enabled — analysing each trace (may take a moment)…{RST}")

    scrubbed = []
    for r in chosen:
        detail = get_session_detail(conn, r["session_id"])
        if detail is None:
            die(f"Session {r['session_id']} not found.")
        red, count, log = apply_share_redactions(
            conn, detail,
            custom_strings=settings["custom_strings"],
            user_allowlist=settings["allowlist_entries"],
            extra_usernames=settings["extra_usernames"],
            blocked_domains=settings["blocked_domains"],
        )
        buckets = {k: 0 for k in ("tokens", "emails", "paths", "timestamps", "urls", "other")}
        th_hits = 0
        for e in log:
            etype = e.get("type", "")
            buckets[_bucket_for(etype)] += 1
            if str(etype).lower().startswith("trufflehog"):
                th_hits += 1

        ai_findings: list[dict] = []
        ai_coverage = "disabled"
        if ai_pii:
            ai_coverage = "rules_only"
            if ai_fns is not None:
                review_fn, apply_fn = ai_fns
                try:
                    findings = review_fn(red, ignore_errors=False, backend="auto")
                    ai_coverage = "full"
                    if findings:
                        red, ai_count = apply_fn(red, findings)
                        count += ai_count
                        ai_findings = findings
                except Exception:  # noqa: BLE001
                    ai_coverage = "rules_only"

        rec = {"row": r, "redacted": red, "count": count, "buckets": buckets,
               "th_hits": th_hits, "ai_findings": ai_findings, "ai_coverage": ai_coverage}
        rec["status"] = trace_status(rec)
        scrubbed.append(rec)

    print(f"  {DIM}Redacting your traces — categories flagged per trace:{RST}")
    for i, s in enumerate(scrubbed, 1):
        verdict = (f"{GRN}clear{RST}" if s["status"] == "clear"
                   else f"{YEL}needs review{RST}")
        print(f"\n  {BOLD}[{i}]{RST} {verdict} · {s['count']} redaction{_plural(s['count'])}"
              f" · {trace_title(s['row'])}")
        cats = what_was_redacted(s)
        if cats:
            for c in cats:
                print(f"        {DIM}•{RST} {c}")
        else:
            print(f"        {DIM}nothing matched the deterministic rules{RST}")

    # OPTIONAL preview — skippable
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
    return scrubbed


# ---- step 3: review ---------------------------------------------------------

def _show_redaction_detail(rec: dict):
    """Print the 'What was redacted' panel for one trace (mirrors web `gi()`)."""
    items = what_was_redacted(rec)
    print(f"  {BOLD}What was redacted:{RST}")
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


def step_review(conn, scrubbed: list[dict], assume_yes: bool) -> list[dict]:
    step(3, "Review — inspect & approve before packaging")
    print(f"  {'#':>3}  {'Status':<14} Title")
    print("  " + "-" * 80)
    for i, s in enumerate(scrubbed, 1):
        verdict = (f"{GRN}clear{RST}        " if s["status"] == "clear"
                   else f"{YEL}needs review{RST} ")
        print(f"  {i:>3}  {verdict:<23} {trace_title(s['row'])}")

    clear = [s for s in scrubbed if s["status"] == "clear"]
    review = [s for s in scrubbed if s["status"] != "clear"]
    approved_ids: set[str] = set()

    if assume_yes:
        # Non-interactive: include everything.
        approved_ids = {s["row"]["session_id"] for s in scrubbed}
    else:
        # Clean traces may be bulk-included (mirrors "Include all clean").
        if clear and yesno(f"\n{BOLD}Include all {len(clear)} clear trace(s)?{RST}", default_yes=True):
            approved_ids |= {s["row"]["session_id"] for s in clear}

        # Needs-review traces MUST be inspected & decided individually.
        if review:
            print(f"\n  {YEL}{len(review)} trace(s) need review — inspect each before packaging.{RST}")
            for n, s in enumerate(review, 1):
                print(f"\n{CYN}  ── review {n}/{len(review)} ─ {trace_title(s['row'], 60)}{RST}")
                _show_redaction_detail(s)
                # Always show a short preview so the user knows which trace this is,
                # even when nothing was redacted.
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
                        approved_ids.add(s["row"]["session_id"])
                        print(f"  {GRN}✓ included{RST}")
                        break
                    if choice in ("r", "remove", "n", "no", "skip"):
                        print(f"  {DIM}removed from bundle{RST}")
                        break
                    print(f"  {YEL}Please enter v, i, or r.{RST}")

    approved = [s for s in scrubbed if s["row"]["session_id"] in approved_ids]
    if not approved:
        die("No traces included — nothing to package.")

    for s in approved:
        if s["row"].get("review_status") != "approved":
            update_session(conn, s["row"]["session_id"], status="approved", reason="clawshare CLI review")
    conn.commit()
    dropped = len(scrubbed) - len(approved)
    print(f"{GRN}✓ {len(approved)} trace(s) approved"
          + (f", {dropped} dropped" if dropped else "") + RST)
    return approved


# ---- step 4: package --------------------------------------------------------

def step_package(conn, settings, approved: list[dict], ai_pii: bool, args):
    step(4, "Package — create + seal bundle")
    session_ids = [s["row"]["session_id"] for s in approved]

    blockers = release_gate_blockers(conn, session_ids)
    if blockers:
        print(f"{RED}Release gate blocked these traces:{RST}")
        for b in blockers:
            print(f"  • {b.get('session_id', '?')[:12]}  {b.get('reason', b)}")
        die("Resolve hold/embargo state (e.g. `clawjournal release <id>`) and retry.")

    share_id = create_share(conn, session_ids, note=args.note)
    share = get_share(conn, share_id)
    if share is None:
        die("Share row could not be loaded after creation.")

    print(f"  {DIM}sealing… (redact → TruffleHog → PII pass → re-scan){RST}")
    export_dir, manifest, error = _prepare_share_export_for_upload(
        conn, share_id, share, settings,
        reuse_finalized=True, ai_pii_review_enabled=ai_pii,
    )
    if error:
        msg = error.get("error", "Packaging failed.")
        blocked = error.get("blocked_sessions") or []
        if blocked:
            print(f"{RED}Packaging blocked — secrets detected in:{RST}")
            for sid in blocked:
                print(f"  • {sid[:12] if isinstance(sid, str) else sid}")
            die("Remove/redact the flagged trace(s) and retry.")
        die(msg)
    if export_dir is None:
        die("Packaging failed: no bundle produced.")
    if manifest.get("blocked"):
        print(f"{RED}Bundle marked blocked: {manifest.get('blocked_sessions')}{RST}")
        die("Cannot submit a blocked bundle.")

    try:
        zip_size = len(_build_share_zip(export_dir))
    except Exception:
        zip_size = 0
    rsum = manifest.get("redaction_summary", {})
    print(f"{GRN}✓ packaged{RST}")
    print(f"  bundle:   {share_id[:8]}   sessions: {len(manifest.get('sessions', []))}")
    print(f"  path:     {export_dir}")
    print(f"  zip size: {zip_size / 1024:.1f} KiB")
    if rsum:
        print(f"  privacy:  {DIM}{rsum}{RST}")
    return share_id, export_dir


# ---- step 5: submit (accept terms) ------------------------------------------

def ensure_email(assume_yes: bool):
    st = hosted_upload_status()
    if st.get("token_valid"):
        print(f"  {GRN}✓ upload token valid for {st['verified_email']}{RST}")
        return
    print(f"  {YEL}Upload token missing/expired — verify your academic email.{RST}")
    default_email = st.get("verified_email") or ""
    email = ask(f"  Academic email{f' [{default_email}]' if default_email else ''}: ", default_email)
    if not email:
        die("Email required for hosted submission.")
    try:
        request_email_verification(email)
    except Exception as exc:  # noqa: BLE001
        die(f"Could not send verification code: {exc}")
    print(f"  {DIM}A 6-digit code was emailed to {email}.{RST}")
    code = ask("  Enter the code: ")
    if not code:
        die("No code entered.")
    try:
        confirm_email_verification(email, code)
    except Exception as exc:  # noqa: BLE001
        die(f"Verification failed: {exc}")
    if not hosted_upload_status().get("token_valid"):
        die("Verification did not yield a valid token.")
    print(f"  {GRN}✓ email verified{RST}")


def step_submit(conn, settings, share_id: str, ai_pii: bool, assume_yes: bool):
    step(5, "Submit — accept terms & upload")
    try:
        doc = fetch_hosted_consent()
    except Exception as exc:  # noqa: BLE001
        die(f"Could not load consent terms: {exc}")
    cv = doc.get("consent_version") or ""
    rv = doc.get("retention_policy_version") or ""
    if not cv or not rv:
        die("Hosted service did not return consent/retention versions.")

    consent_text = (doc.get("consent_text") or "").strip()
    retention_text = (doc.get("retention_text") or "").strip()
    if not consent_text and not retention_text:
        die("Hosted service did not return the consent terms text.")

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

    if not assume_yes:
        if not yesno(f"  {BOLD}I accept the displayed consent and data-use terms.{RST}"):
            print(f"{YEL}Terms not accepted — bundle packaged but not submitted.{RST}")
            return None
        if not yesno(f"  {BOLD}I certify this bundle is mine to submit and contains no "
                     f"third-party confidential material.{RST}"):
            print(f"{YEL}Ownership not certified — bundle packaged but not submitted.{RST}")
            return None

    ensure_email(assume_yes)
    print(f"  {DIM}uploading to hosted research…{RST}")
    result = submit_share_to_hosted(
        conn, share_id,
        accept_terms=True, ownership_certification=True,
        consent_version=cv, retention_policy_version=rv,
        settings=settings, ai_pii_review_enabled=ai_pii,
    )
    if not result.get("ok"):
        suffix = f"  (status {result['status']})" if result.get("status") else ""
        die(result.get("error", "Upload failed.") + suffix)
    print(f"  {GRN}✓ uploaded{RST}")
    return result


# ---- step 6: done (+ optional download) -------------------------------------

def step_done(share_id: str, export_dir: Path, result, assume_yes: bool):
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

    # OPTIONAL download of the zip — single prompt so a stray "y" can't become
    # the filename: Enter/y = default path, n = skip, anything else = a path.
    fname = f"clawjournal-share-{share_id[:8]}.zip"
    default = Path("~/Downloads").expanduser() / fname
    if assume_yes:
        dest = default
    else:
        ans = ask(f"\n  {BOLD}Download the bundle zip?{RST} "
                  f"{DIM}[Enter/y = {default}, n = skip, or type a path]{RST}\n  > ").strip()
        low = ans.lower()
        if low in ("n", "no", "q", "skip"):
            return
        dest = default if low in ("", "y", "yes") else Path(ans).expanduser()
    if dest.is_dir() or str(dest).endswith(os.sep):
        dest = dest / fname
    try:
        data = _build_share_zip(export_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"  {GRN}✓ saved {len(data) / 1024:.1f} KiB → {dest}{RST}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}Download failed: {exc}{RST}")


# ---- arg wiring (single source of truth) ------------------------------------

def add_share_cli_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Define the clawshare wizard flags. Shared by the `clawshare` console
    script and the `clawjournal share-cli` subcommand."""
    parser.add_argument("indices", nargs="*", type=int,
                        help="trace #(s) from the list (non-interactive)")
    # Time range — default is today (last turn happened today).
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--weekly", dest="time_range", action="store_const", const="weekly",
                      help="traces from the past 7 days (default: past 24h)")
    when.add_argument("--all", dest="time_range", action="store_const", const="all",
                      help="all traces, any date and any status")
    parser.set_defaults(time_range="today")
    parser.add_argument("--status", default=None,
                        choices=["new", "shortlisted", "approved", "blocked"],
                        help="filter by review status (default: any)")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--source", default=None,
                     help="only this source (claude, codex, gemini, …)")
    src.add_argument("--codex", dest="source", action="store_const", const="codex",
                     help="only Codex traces (shortcut for --source codex)")
    src.add_argument("--claude", dest="source", action="store_const", const="claude",
                     help="only Claude traces (shortcut for --source claude)")
    parser.add_argument("--limit", type=int, default=40,
                        help="max traces to list (default 40)")
    parser.add_argument("--summary", action="store_true",
                        help="show AI-summarized titles (Haiku); default shows the "
                             "original, non-AI titles (faster)")
    parser.add_argument("--summary-model", default=None, metavar="MODEL",
                        help="model for --summary titles (default: a light model per "
                             "backend, e.g. claude/haiku; implies --summary)")
    parser.add_argument("--note", default=None, help="optional submission note")
    parser.add_argument("--ai-pii-review", action="store_true",
                        help="run AI-assisted PII pass during packaging")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="assume yes for preview-skip / review / terms / download prompts")
    return parser


def run(args) -> None:
    """Run the full 6-step share wizard against a parsed argparse namespace."""
    config = load_config()
    conn = open_index()
    try:
        settings = get_effective_share_settings(conn, config)
        ai_pii = bool(args.ai_pii_review or settings.get("ai_pii_review_enabled"))
        chosen = step_queue(conn, settings, args)
        scrubbed = step_redact(conn, settings, chosen, args.yes, ai_pii)
        approved = step_review(conn, scrubbed, args.yes)
        share_id, export_dir = step_package(conn, settings, approved, ai_pii, args)
        result = step_submit(conn, settings, share_id, ai_pii, args.yes)
        step_done(share_id, export_dir, result, args.yes)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point for the standalone `clawshare` command."""
    parser = argparse.ArgumentParser(
        prog="clawshare",
        description="Full ClawJournal Share wizard, keyboard-only "
                    "(queue → redact → review → package → submit → done).",
    )
    add_share_cli_args(parser)
    run(parser.parse_args(argv))


if __name__ == "__main__":
    main()
