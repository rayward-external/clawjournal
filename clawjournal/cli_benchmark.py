"""CLI handler for the ``benchmark`` verb.

Kept separate from ``cli.py`` to contain the surface. Generates / lists / shows /
exports the personalized weekly benchmark. The handler takes a parsed argparse
``Namespace``, opens the index, and prints JSON (or markdown for ``--show``).
Hard errors exit non-zero so scripts can react.

Generation makes real backend (LLM) calls via the default scoring backend; the
other verbs are local reads/renders.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .benchmark import render, store
from .benchmark import schema as bm
from .benchmark.render import EXPORT_KINDS
from .workbench.index import open_index


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def _fail(message: str, **extra: Any) -> None:
    _emit({"error": message, **extra})
    sys.exit(1)


def _resolve(conn, target: str) -> dict[str, Any] | None:
    return store.get_latest_benchmark(conn) if target == "latest" else store.get_benchmark(conn, target)


def run_benchmark(args) -> None:
    if getattr(args, "list", False):
        _list(args)
    elif getattr(args, "show", None):
        _show(args)
    elif getattr(args, "export", None):
        _export(args)
    else:
        _generate(args)


def _generate(args) -> None:
    # Imported lazily so `--list/--show/--export` never pull in the backend stack.
    from .benchmark.generate import generate_benchmark

    conn = open_index()
    try:
        try:
            benchmark = generate_benchmark(
                conn,
                window_days=args.window,
                cap=args.cap,
                backend=args.backend,
                progress=lambda msg: print(f"… {msg}", file=sys.stderr),
            )
        except (ValueError, RuntimeError) as exc:
            _fail(str(exc))
        bid = store.save_benchmark(conn, benchmark)
        _emit({
            "benchmark_id": bid,
            "window": [benchmark.window_start, benchmark.window_end],
            "n_tasks": benchmark.n_tasks,
            "total_points": benchmark.total_points,
            "ready": benchmark.ready_count,
            "needs_staging": benchmark.needs_staging_count,
            "themes": len(benchmark.themes),
            "dropped_for_cost": benchmark.dropped_for_cost,
            "backend": benchmark.backend,
        })
    finally:
        conn.close()


def _list(args) -> None:
    conn = open_index()
    try:
        rows = store.list_benchmarks(conn)
        if getattr(args, "json", False):
            _emit({"benchmarks": rows})
            return
        if not rows:
            print("No benchmarks yet. Run `clawjournal benchmark` to generate one.")
            return
        for r in rows:
            print(
                f"{r['benchmark_id']:>12}  "
                f"{(r['window_start'] or '')[:10]}→{(r['window_end'] or '')[:10]}  "
                f"{r['status']:>10}  {r['n_tasks'] or 0} tasks  {r['total_points'] or 0} pts  "
                f"(ready {r['ready_count'] or 0}, staging {r['needs_staging_count'] or 0})"
            )
    finally:
        conn.close()


def _show(args) -> None:
    conn = open_index()
    try:
        got = _resolve(conn, args.show)
        if got is None:
            _fail("benchmark not found", id=args.show)
        print(render.render_markdown(got))
    finally:
        conn.close()


def _export(args) -> None:
    kind = getattr(args, "kind", None) or "authoring_md"
    if kind not in EXPORT_KINDS:
        _fail(f"unknown export kind {kind!r}", kinds=list(EXPORT_KINDS))
    conn = open_index()
    try:
        got = _resolve(conn, args.export)
        if got is None:
            _fail("benchmark not found", id=args.export)
        content = render.render(got, kind)
        # Best-effort local PII scan as a recorded receipt (full deterministic
        # export redaction lands in the privacy-guard phase).
        pii_hits = len(bm.find_pii(content))
        ext = "json" if kind.endswith("json") else "md"
        out = getattr(args, "output", None) or f"benchmark-{got['benchmark_id']}-{kind}.{ext}"
        Path(out).write_text(content, encoding="utf-8")
        summary = {"kind": kind, "pii_scan_hits": pii_hits, "deterministic_redaction": "deferred"}
        store.record_export(conn, got["benchmark_id"], kind=kind, path=str(out), redaction_summary=summary)
        _emit({"exported": str(out), "kind": kind, "benchmark_id": got["benchmark_id"],
               "pii_scan_hits": pii_hits})
    finally:
        conn.close()
