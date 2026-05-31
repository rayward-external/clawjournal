"""Persistence for personalized benchmarks.

Hybrid model: the run's full payload lives in ``benchmarks.payload_json`` (for
faithful rendering/export), while the queryable per-task fields are
denormalized into ``benchmark_tasks`` so the tab can filter by
readiness/theme/risk cheaply. Export receipts land in ``benchmark_exports``.

All writes commit; reads return plain dicts (JSON columns parsed). ``uuid`` is
used for task/export ids so callers don't have to supply them.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from . import schema as bm


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _isoweek_id(window_end: str) -> str:
    """``2026-W22`` from a window-end ISO timestamp (falls back to ``"benchmark"``).

    Unreachable on validated save paths — ``validate_benchmark`` rejects an empty
    window before this is called.
    """
    try:
        dt = datetime.fromisoformat(str(window_end).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "benchmark"
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _unique_benchmark_id(conn: sqlite3.Connection, base: str) -> str:
    """Append-only history: ``2026-W22``, then ``2026-W22-2``, ``-3``, …"""
    rows = conn.execute(
        "SELECT benchmark_id FROM benchmarks WHERE benchmark_id = ? OR benchmark_id LIKE ?",
        (base, f"{base}-%"),
    ).fetchall()
    taken = {r[0] for r in rows}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
_RUN_COLS = (
    "benchmark_id", "window_start", "window_end", "generated_at", "status",
    "stage", "backend", "rubric_git_sha", "n_tasks", "total_points",
    "source_count", "dropped_for_cost", "ready_count", "needs_staging_count",
    "payload_json", "error",
)


def _run_values(benchmark: bm.Benchmark, *, status: str, payload: str) -> tuple:
    return (
        benchmark.benchmark_id,
        benchmark.window_start,
        benchmark.window_end,
        benchmark.generated_at,
        status,
        None,  # stage
        benchmark.backend,
        benchmark.rubric_git_sha,
        benchmark.n_tasks,
        benchmark.total_points,
        benchmark.source_count,
        benchmark.dropped_for_cost,
        benchmark.ready_count,
        benchmark.needs_staging_count,
        payload,
        None,  # error
    )


def _replace_tasks(conn: sqlite3.Connection, benchmark_id: str, tasks: list[bm.BenchmarkTask]) -> None:
    conn.execute("DELETE FROM benchmark_tasks WHERE benchmark_id = ?", (benchmark_id,))
    for task in tasks:
        conn.execute(
            "INSERT INTO benchmark_tasks (task_id, benchmark_id, title, theme, "
            "domains_json, source_agents_json, difficulty, points, grading, "
            "readiness, leakage_risk, privacy_risk, grounded_session_ids_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{benchmark_id}:{task.id}",
                benchmark_id,
                task.title,
                task.theme,
                json.dumps(task.domains),
                json.dumps(task.source_agents),
                task.difficulty,
                task.points,
                task.grading,
                task.readiness,
                task.leakage_risk,
                task.privacy_risk,
                json.dumps(task.grounded_session_ids),
            ),
        )


def save_benchmark(conn: sqlite3.Connection, benchmark: bm.Benchmark, *, status: str = "ready") -> str:
    """Persist a completed benchmark (run row + denormalized task rows).

    Assigns a unique ``benchmark_id`` (ISO-week, append-only) if absent.
    Validates before writing. Returns the id.
    """
    bm.validate_or_raise(benchmark)
    if not benchmark.benchmark_id:
        benchmark.benchmark_id = _unique_benchmark_id(conn, _isoweek_id(benchmark.window_end))
    payload = json.dumps(bm.benchmark_to_dict(benchmark))
    exists = conn.execute(
        "SELECT 1 FROM benchmarks WHERE benchmark_id = ?", (benchmark.benchmark_id,)
    ).fetchone()
    if exists:
        # Non-destructive re-save: UPDATE in place. INSERT OR REPLACE would
        # DELETE+INSERT the row and cascade-wipe its benchmark_exports receipts.
        conn.execute(
            "UPDATE benchmarks SET window_start = ?, window_end = ?, generated_at = ?, "
            "status = ?, stage = NULL, backend = ?, rubric_git_sha = ?, n_tasks = ?, "
            "total_points = ?, source_count = ?, dropped_for_cost = ?, ready_count = ?, "
            "needs_staging_count = ?, payload_json = ?, error = NULL WHERE benchmark_id = ?",
            (
                benchmark.window_start, benchmark.window_end, benchmark.generated_at, status,
                benchmark.backend, benchmark.rubric_git_sha, benchmark.n_tasks,
                benchmark.total_points, benchmark.source_count, benchmark.dropped_for_cost,
                benchmark.ready_count, benchmark.needs_staging_count, payload, benchmark.benchmark_id,
            ),
        )
    else:
        placeholders = ",".join("?" for _ in _RUN_COLS)
        conn.execute(
            f"INSERT INTO benchmarks ({','.join(_RUN_COLS)}) VALUES ({placeholders})",
            _run_values(benchmark, status=status, payload=payload),
        )
    _replace_tasks(conn, benchmark.benchmark_id, benchmark.tasks)
    conn.commit()
    return benchmark.benchmark_id


def insert_generating(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
    backend: str = "",
) -> str:
    """Insert a placeholder ``generating`` run; returns its id (for the async path)."""
    benchmark_id = _unique_benchmark_id(conn, _isoweek_id(window_end))
    conn.execute(
        "INSERT INTO benchmarks (benchmark_id, window_start, window_end, generated_at, "
        "status, backend, payload_json) VALUES (?,?,?,?, 'generating', ?, '{}')",
        (benchmark_id, window_start, window_end, _now_iso(), backend),
    )
    conn.commit()
    return benchmark_id


def update_status(
    conn: sqlite3.Connection,
    benchmark_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    error: str | None = None,
) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if stage is not None:
        sets.append("stage = ?")
        params.append(stage)
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if not sets:
        return
    params.append(benchmark_id)
    conn.execute(f"UPDATE benchmarks SET {', '.join(sets)} WHERE benchmark_id = ?", params)
    conn.commit()


def finalize_benchmark(
    conn: sqlite3.Connection,
    benchmark_id: str,
    benchmark: bm.Benchmark,
    *,
    status: str = "ready",
) -> None:
    """Attach a generated benchmark to an existing ``generating`` placeholder row."""
    row = conn.execute(
        "SELECT window_start, window_end FROM benchmarks WHERE benchmark_id = ?", (benchmark_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"finalize_benchmark: no placeholder row for benchmark_id {benchmark_id!r}")
    if benchmark.window_start != row["window_start"] or benchmark.window_end != row["window_end"]:
        raise ValueError(
            f"finalize_benchmark window mismatch for {benchmark_id!r}: placeholder "
            f"{row['window_start']}..{row['window_end']} vs object "
            f"{benchmark.window_start}..{benchmark.window_end}"
        )
    benchmark.benchmark_id = benchmark_id
    bm.validate_or_raise(benchmark)
    payload = json.dumps(bm.benchmark_to_dict(benchmark))
    conn.execute(
        "UPDATE benchmarks SET status = ?, stage = NULL, error = NULL, generated_at = ?, "
        "backend = ?, rubric_git_sha = ?, n_tasks = ?, total_points = ?, source_count = ?, "
        "dropped_for_cost = ?, ready_count = ?, needs_staging_count = ?, payload_json = ? "
        "WHERE benchmark_id = ?",
        (
            status, benchmark.generated_at, benchmark.backend, benchmark.rubric_git_sha,
            benchmark.n_tasks, benchmark.total_points, benchmark.source_count,
            benchmark.dropped_for_cost, benchmark.ready_count, benchmark.needs_staging_count,
            payload, benchmark_id,
        ),
    )
    _replace_tasks(conn, benchmark_id, benchmark.tasks)
    conn.commit()


def record_export(
    conn: sqlite3.Connection,
    benchmark_id: str,
    *,
    kind: str,
    path: str | None = None,
    redaction_summary: dict | None = None,
) -> str:
    export_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO benchmark_exports (export_id, benchmark_id, kind, path, created_at, "
        "redaction_summary_json) VALUES (?,?,?,?,?,?)",
        (
            export_id, benchmark_id, kind, path, _now_iso(),
            json.dumps(redaction_summary) if redaction_summary is not None else None,
        ),
    )
    conn.commit()
    return export_id


def delete_benchmark(conn: sqlite3.Connection, benchmark_id: str) -> None:
    """Delete a run; ``benchmark_tasks``/``benchmark_exports`` cascade (FK ON DELETE CASCADE)."""
    conn.execute("DELETE FROM benchmarks WHERE benchmark_id = ?", (benchmark_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
_SUMMARY_COLS = (
    "benchmark_id", "window_start", "window_end", "generated_at", "status",
    "stage", "backend", "rubric_git_sha", "n_tasks", "total_points",
    "source_count", "dropped_for_cost", "ready_count", "needs_staging_count", "error",
)


def _row_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in _SUMMARY_COLS}


def _row_full(row: sqlite3.Row) -> dict[str, Any]:
    """Run summary + the parsed payload (themes/tasks). Operational status/stage/error win."""
    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    except (TypeError, json.JSONDecodeError):
        payload = {}
    out = dict(payload)
    out.update(_row_summary(row))
    return out


def get_benchmark(conn: sqlite3.Connection, benchmark_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM benchmarks WHERE benchmark_id = ?", (benchmark_id,)).fetchone()
    return _row_full(row) if row else None


def get_latest_benchmark(conn: sqlite3.Connection, *, status: str = "ready") -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM benchmarks WHERE status = ? ORDER BY generated_at DESC, rowid DESC LIMIT 1",
        (status,),
    ).fetchone()
    return _row_full(row) if row else None


def list_benchmarks(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {','.join(_SUMMARY_COLS)} FROM benchmarks ORDER BY generated_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_summary(r) for r in rows]


def list_tasks(
    conn: sqlite3.Connection,
    benchmark_id: str,
    *,
    readiness: str | None = None,
    theme: str | None = None,
    source_agent: str | None = None,
) -> list[dict[str, Any]]:
    """Denormalized task rows for the run, with optional filters."""
    sql = "SELECT * FROM benchmark_tasks WHERE benchmark_id = ?"
    params: list[Any] = [benchmark_id]
    if readiness is not None:
        sql += " AND readiness = ?"
        params.append(readiness)
    if theme is not None:
        sql += " AND theme = ?"
        params.append(theme)
    sql += " ORDER BY points DESC, task_id"
    rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        agents = json.loads(r["source_agents_json"] or "[]")
        if source_agent is not None and source_agent not in agents:
            continue
        out.append(
            {
                "task_id": r["task_id"],
                "benchmark_id": r["benchmark_id"],
                "title": r["title"],
                "theme": r["theme"],
                "domains": json.loads(r["domains_json"] or "[]"),
                "source_agents": agents,
                "difficulty": r["difficulty"],
                "points": r["points"],
                "grading": r["grading"],
                "readiness": r["readiness"],
                "leakage_risk": r["leakage_risk"],
                "privacy_risk": r["privacy_risk"],
                "grounded_session_ids": json.loads(r["grounded_session_ids_json"] or "[]"),
            }
        )
    return out


def get_theme_trend(conn: sqlite3.Connection, *, limit_runs: int = 12) -> dict[str, Any]:
    """Theme × run matrix across the latest ``ready`` runs, for the Trend view.

    Returns ``{"runs": [{benchmark_id, window_end}, …], "themes": {name: [freq|0 per run]}}``,
    runs ordered oldest→newest so a sparkline reads left-to-right.
    """
    rows = conn.execute(
        "SELECT benchmark_id, window_end, payload_json FROM benchmarks "
        "WHERE status = 'ready' ORDER BY generated_at DESC, rowid DESC LIMIT ?",
        (limit_runs,),
    ).fetchall()
    rows = list(reversed(rows))  # oldest → newest
    runs = [{"benchmark_id": r["benchmark_id"], "window_end": r["window_end"]} for r in rows]
    per_run_themes: list[dict[str, int]] = []
    all_names: list[str] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        freqs: dict[str, int] = {}
        for theme in payload.get("themes", []) or []:
            name = theme.get("name")
            if not name:
                continue
            freqs[name] = int(theme.get("frequency") or 0)
            if name not in all_names:
                all_names.append(name)
        per_run_themes.append(freqs)
    themes = {name: [run.get(name, 0) for run in per_run_themes] for name in all_names}
    return {"runs": runs, "themes": themes}
