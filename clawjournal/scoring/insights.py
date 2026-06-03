"""Token Efficiency Advisor — heuristic insights on usage patterns.

This module is deterministic: SQL aggregates plus rule-based recommendations.
No LLM is involved (the "Insights" UI surface reflects this).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..pricing import downgrade_savings_ratio, format_cost


def collect_advisor_stats(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
) -> dict[str, Any]:
    """Collect aggregate stats for the advisor from the session index.

    Returns a summary dict used to generate the heuristic, rule-based
    recommendations (no LLM is involved).
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    result: dict[str, Any] = {
        "period": f"{start_str} to {end_str}",
        "days": days,
    }

    # Totals
    row = conn.execute(
        "SELECT COUNT(*) as sessions, "
        "SUM(estimated_cost_usd) as cost, "
        "SUM(CASE WHEN estimated_cost_usd IS NOT NULL THEN 1 ELSE 0 END) as priced_sessions, "
        "SUM(input_tokens) as input_tokens, "
        "SUM(output_tokens) as output_tokens "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ?",
        (start_str, end_str),
    ).fetchone()
    result["total_sessions"] = row["sessions"] or 0
    result["total_cost_usd"] = round(row["cost"] or 0, 2)
    # Sessions whose model isn't in the pricing table have a NULL cost: they drop
    # out of SUM/AVG but would otherwise dilute per-session averages if counted in
    # the denominator. Track the priced count so averages use SUM/COUNT(priced).
    result["priced_sessions"] = row["priced_sessions"] or 0
    result["unpriced_sessions"] = (row["sessions"] or 0) - (row["priced_sessions"] or 0)
    result["total_input_tokens"] = row["input_tokens"] or 0
    result["total_output_tokens"] = row["output_tokens"] or 0

    # By model. Exclude parser-fallback `<synthetic>` sessions — they
    # have no real model/cost and would trivially win Most Efficient
    # (cost/session = 0) and can sneak into Highest Quality too. The
    # export path already filters them at `cli.py:479`; mirror that here.
    # Same-model traffic at different reasoning-effort tiers (e.g.
    # `gpt-5.4 @ high` vs `gpt-5.4 @ xhigh`) is split into separate rows
    # so "Most efficient" and "Highest productivity" picks a specific tier.
    # Numerator matches the normalized `resolved` bucket in
    # workbench.index (AI `resolved` or heuristic `tests_passed`).
    # Dropping heuristic `completed` from the success set — it's the
    # "no signal either way" fallback and overstated quality.
    rows = conn.execute(
        "SELECT CASE WHEN model_effort IS NOT NULL AND model_effort != '' "
        "       THEN model || ' @ ' || model_effort ELSE model END as model, "
        "COUNT(*) as sessions, "
        "SUM(estimated_cost_usd) as cost, "
        "AVG(ai_quality_score) as avg_score, "
        "SUM(CASE WHEN ai_outcome_badge = 'resolved' "
        "          OR (ai_outcome_badge IS NULL AND outcome_badge = 'tests_passed') "
        "         THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as resolve_rate "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "AND model IS NOT NULL AND model != '<synthetic>' "
        "GROUP BY 1 ORDER BY cost DESC",
        (start_str, end_str),
    ).fetchall()
    result["by_model"] = [
        {
            "model": r["model"],
            "sessions": r["sessions"],
            "cost": round(r["cost"] or 0, 2),
            "avg_score": round(r["avg_score"] or 0, 1),
            "resolve_rate": round(r["resolve_rate"] or 0, 2),
        }
        for r in rows
    ]

    # By task type
    rows = conn.execute(
        "SELECT COALESCE(ai_task_type, task_type) as task_type, "
        "COUNT(*) as sessions, "
        "SUM(estimated_cost_usd) as cost, "
        "AVG(ai_quality_score) as avg_score "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "AND COALESCE(ai_task_type, task_type) IS NOT NULL "
        "GROUP BY 1 ORDER BY cost DESC",
        (start_str, end_str),
    ).fetchall()
    result["by_task_type"] = [
        {
            "type": r["task_type"],
            "sessions": r["sessions"],
            "cost": round(r["cost"] or 0, 2),
            "avg_score": round(r["avg_score"] or 0, 1),
        }
        for r in rows
    ]

    # By score
    rows = conn.execute(
        "SELECT ai_quality_score as score, "
        "COUNT(*) as sessions, "
        "AVG(estimated_cost_usd) as avg_cost "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "AND ai_quality_score IS NOT NULL "
        "GROUP BY score ORDER BY score DESC",
        (start_str, end_str),
    ).fetchall()
    result["by_score"] = [
        {"score": r["score"], "sessions": r["sessions"], "avg_cost": round(r["avg_cost"] or 0, 2)}
        for r in rows
    ]

    # Peak hours
    rows = conn.execute(
        "SELECT CAST(strftime('%H', start_time) AS INTEGER) as hour, "
        "COUNT(*) as sessions "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "GROUP BY hour ORDER BY sessions DESC LIMIT 5",
        (start_str, end_str),
    ).fetchall()
    result["peak_hours"] = [r["hour"] for r in rows]

    # Low activity days
    rows = conn.execute(
        "SELECT CASE CAST(strftime('%w', start_time) AS INTEGER) "
        "  WHEN 0 THEN 'Sunday' WHEN 1 THEN 'Monday' WHEN 2 THEN 'Tuesday' "
        "  WHEN 3 THEN 'Wednesday' WHEN 4 THEN 'Thursday' WHEN 5 THEN 'Friday' "
        "  WHEN 6 THEN 'Saturday' END as day_name, "
        "COUNT(*) as sessions "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "GROUP BY day_name ORDER BY sessions ASC LIMIT 2",
        (start_str, end_str),
    ).fetchall()
    result["low_activity_days"] = [r["day_name"] for r in rows if r["sessions"] < 3]

    # Long sessions with low scores
    row = conn.execute(
        "SELECT COUNT(*) as count "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "AND duration_seconds > 1800 AND ai_quality_score IS NOT NULL AND ai_quality_score <= 2",
        (start_str, end_str),
    ).fetchone()
    result["long_sessions_with_low_score"] = row["count"] or 0

    # Short sessions with high scores
    row = conn.execute(
        "SELECT COUNT(*) as count "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "AND duration_seconds < 600 AND ai_quality_score IS NOT NULL AND ai_quality_score >= 4",
        (start_str, end_str),
    ).fetchone()
    result["short_sessions_with_high_score"] = row["count"] or 0

    # Model downgrade candidates: expensive models used for simple tasks.
    # Label includes effort so the rec text can say e.g. "gpt-5.4 @ xhigh".
    rows = conn.execute(
        "SELECT session_id, "
        "CASE WHEN model_effort IS NOT NULL AND model_effort != '' "
        "     THEN model || ' @ ' || model_effort ELSE model END as model, "
        "COALESCE(ai_task_type, task_type) as task_type, "
        "ai_quality_score as score, estimated_cost_usd as cost "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "AND estimated_cost_usd > 1.0 "
        "AND ai_quality_score IS NOT NULL AND ai_quality_score <= 3 "
        "AND COALESCE(ai_task_type, task_type) IN ("
        "'docs', 'documentation', 'testing', 'formatting', 'config', 'configuration'"
        ") "
        "ORDER BY cost DESC LIMIT 5",
        (start_str, end_str),
    ).fetchall()
    result["model_downgrade_candidates"] = [
        {
            "session_id": r["session_id"],
            "model": r["model"],
            "task_type": r["task_type"],
            "score": r["score"],
            "cost": round(r["cost"] or 0, 2),
        }
        for r in rows
    ]

    # Interrupt patterns by model@effort
    int_rows = conn.execute(
        "SELECT CASE WHEN model_effort IS NOT NULL AND model_effort != '' "
        "       THEN model || ' @ ' || model_effort ELSE model END as model, "
        "AVG(CAST(user_interrupts AS REAL)) as avg_interrupts, "
        "COUNT(*) as sessions, SUM(tool_uses) as total_tool_uses "
        "FROM sessions WHERE DATE(start_time) >= ? AND DATE(start_time) <= ? "
        "AND user_interrupts > 0 AND model IS NOT NULL AND model != '<synthetic>' "
        "GROUP BY 1 ORDER BY avg_interrupts DESC",
        (start_str, end_str),
    ).fetchall()
    result["interrupt_patterns"] = [
        {
            "model": r["model"],
            "avg_interrupts": round(r["avg_interrupts"], 2),
            "sessions": r["sessions"],
            "total_tool_uses": r["total_tool_uses"] or 0,
        }
        for r in int_rows
    ]

    return result


def generate_recommendations(stats: dict[str, Any]) -> dict[str, Any]:
    """Generate rule-based recommendations from aggregate stats.

    Returns a structured advisor output with headline and recommendations.
    Does not call an LLM — uses heuristic rules for zero-cost recommendations.
    """
    recommendations: list[dict[str, Any]] = []
    total_cost = stats.get("total_cost_usd", 0)
    total_sessions = stats.get("total_sessions", 0)

    if total_sessions == 0:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": stats.get("period", ""),
            "headline": "No sessions found in this period.",
            "recommendations": [],
            "summary_stats": {
                "total_cost_usd": 0,
                "total_sessions": 0,
                "unpriced_sessions": 0,
                "cost_per_session": 0,
                "most_efficient_model": None,
                "highest_quality_model": None,
                "potential_savings_usd": 0,
            },
        }

    # Model downgrade
    candidates = stats.get("model_downgrade_candidates", [])
    if candidates:
        # Estimate per-candidate savings from a real cheapest-same-family
        # substitution (e.g. opus -> haiku). Fall back to a flat 0.6 of cost
        # only when the model (or a cheaper sibling) isn't in the pricing table,
        # so an unpriced model still yields a non-zero recommendation.
        def _candidate_savings(c: dict[str, Any]) -> float:
            cost = c["cost"]
            # Candidate labels may carry an "@ effort" suffix; price the base model.
            base_model = str(c.get("model") or "").split(" @ ", 1)[0]
            ratio = downgrade_savings_ratio(base_model)
            if ratio is None:
                ratio = 0.6
            return cost * ratio

        potential_savings = sum(_candidate_savings(c) for c in candidates)
        recommendations.append({
            "type": "model_downgrade",
            "priority": "high",
            "title": f"Switch {len(candidates)} simple tasks to cheaper models",
            "detail": (
                f"{len(candidates)} sessions used expensive models for simple tasks "
                f"(docs, testing, config) and scored 3/5 or below. "
                f"Estimated weekly savings: {format_cost(potential_savings)}."
            ),
            "estimated_savings_usd": round(potential_savings, 2),
        })

    # Long sessions with low scores
    long_low = stats.get("long_sessions_with_low_score", 0)
    short_high = stats.get("short_sessions_with_high_score", 0)
    if long_low >= 3:
        recommendations.append({
            "type": "session_efficiency",
            "priority": "medium",
            "title": "Break up long low-scoring sessions",
            "detail": (
                f"{long_low} sessions over 30 minutes scored 2/5 or below. "
                f"Meanwhile, {short_high} sessions under 10 minutes scored 4+/5. "
                f"Shorter, focused prompts seem to work better."
            ),
        })

    # Unused capacity (low activity days)
    low_days = stats.get("low_activity_days", [])
    if low_days:
        recommendations.append({
            "type": "unused_capacity",
            "priority": "medium",
            "title": f"Use idle {' and '.join(low_days)} for batch work",
            "detail": (
                f"You had minimal activity on {', '.join(low_days)}. "
                f"Consider scheduling refactoring or documentation tasks for these periods."
            ),
        })

    # Best ROI task type
    by_task = stats.get("by_task_type", [])
    if len(by_task) >= 2:
        scored_tasks = [t for t in by_task if t["avg_score"] > 0]
        if scored_tasks:
            best = max(scored_tasks, key=lambda t: t["avg_score"])
            if best["avg_score"] >= 3.5:
                recommendations.append({
                    "type": "high_roi",
                    "priority": "low",
                    "title": f"{best['type'].title()} work has the best productivity score",
                    "detail": (
                        f"{best['type'].title()} sessions: {best['avg_score']:.1f}/5 avg productivity "
                        f"at {format_cost(best['cost'])} total cost ({best['sessions']} sessions)."
                    ),
                })

    # Model effectiveness comparison
    by_model = stats.get("by_model", [])
    if len(by_model) >= 2:
        scored_models = [m for m in by_model if m["avg_score"] > 0]
        if len(scored_models) >= 2:
            best_quality = max(scored_models, key=lambda m: m["avg_score"])
            cheapest = min(scored_models, key=lambda m: m["cost"] / max(m["sessions"], 1))
            if best_quality["model"] != cheapest["model"]:
                recommendations.append({
                    "type": "model_comparison",
                    "priority": "low",
                    "title": "Model productivity vs cost trade-off",
                    "detail": (
                        f"Highest productivity: {best_quality['model']} ({best_quality['avg_score']:.1f}/5 avg, "
                        f"{format_cost(best_quality['cost'])} total). "
                        f"Most cost-effective: {cheapest['model']} "
                        f"({cheapest['avg_score']:.1f}/5 avg, "
                        f"{format_cost(cheapest['cost'])} total)."
                    ),
                })

    # Agent steering (high interrupts per model)
    interrupt_patterns = stats.get("interrupt_patterns", [])
    high_interrupt_models = [p for p in interrupt_patterns if p["avg_interrupts"] >= 2.0]
    if high_interrupt_models:
        worst = high_interrupt_models[0]
        recommendations.append({
            "type": "agent_steering",
            "priority": "medium",
            "title": f"{worst['model']} sessions average {worst['avg_interrupts']:.1f} interrupts",
            "detail": (
                f"Across {worst['sessions']} interrupted sessions, {worst['model']} "
                f"averaged {worst['avg_interrupts']:.1f} user interrupts. "
                f"Frequent interruptions suggest the agent goes off-track — "
                f"consider more specific prompts or a different model for these tasks."
            ),
        })

    # Generate headline
    if recommendations:
        high_priority = [r for r in recommendations if r["priority"] == "high"]
        if high_priority:
            headline = (
                f"Estimated spend: {format_cost(total_cost)} this period on {total_sessions} sessions. "
                f"{high_priority[0]['title']}."
            )
        else:
            headline = (
                f"Estimated spend: {format_cost(total_cost)} this period on {total_sessions} sessions. "
                f"{len(recommendations)} suggestions available."
            )
    else:
        headline = (
            f"Estimated spend: {format_cost(total_cost)} this period on {total_sessions} sessions. "
            f"No specific optimization suggestions."
        )

    # Summary stats. Average over PRICED sessions only — unpriced (NULL-cost)
    # sessions contribute 0 to total_cost, so counting them in the denominator
    # would understate the true per-session API-equivalent cost.
    priced_sessions = stats.get("priced_sessions", total_sessions)
    cost_per_session = total_cost / priced_sessions if priced_sessions else 0
    scored_models = [m for m in by_model if m["avg_score"] > 0]
    # Unpriced models have NULL cost in SQLite and arrive here as cost=0. Do not
    # let them "win" a cost-efficiency ranking; zero is unknown, not free.
    priced_scored_models = [m for m in scored_models if m.get("cost", 0) > 0]
    most_efficient = min(priced_scored_models, key=lambda m: m["cost"] / max(m["sessions"], 1))["model"] if priced_scored_models else None
    highest_quality = max(scored_models, key=lambda m: m["avg_score"])["model"] if scored_models else None

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": stats.get("period", ""),
        "headline": headline,
        "recommendations": recommendations,
        "summary_stats": {
            "total_cost_usd": total_cost,
            "total_sessions": total_sessions,
            "unpriced_sessions": stats.get("unpriced_sessions", 0),
            "cost_per_session": round(cost_per_session, 2),
            "most_efficient_model": most_efficient,
            "highest_quality_model": highest_quality,
            "potential_savings_usd": sum(
                r.get("estimated_savings_usd", 0) for r in recommendations
            ),
        },
    }
