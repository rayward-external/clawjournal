"""Server-rendered session timeline view for phase-1 plan 06."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from clawjournal.events.cost.schema import ensure_cost_schema
from clawjournal.events.incidents.schema import ensure_incidents_schema
from clawjournal.events.schema import ensure_schema as ensure_events_schema
from clawjournal.events.view import canonical_events, capability_join, ensure_view_schema
from clawjournal.session_titles import resolve_session_title

_WHITESPACE_RE = re.compile(r"\s+")
_SUMMARY_KEYS = (
    "command",
    "text",
    "content",
    "output",
    "stderr",
    "stdout",
    "message",
    "name",
    "tool_name",
    "arguments",
)


@dataclass(frozen=True)
class TimelineEvent:
    event_id: int | None
    anchor_id: str
    turn_number: int
    type: str
    event_key: str | None
    event_at: str | None
    ingested_at: str | None
    source: str
    confidence: str
    lossiness: str
    summary: str
    raw_ref: tuple[str, int, int] | None
    origin: str | None
    raw_excerpt: str | None
    payload_excerpt: str | None
    token_usage: dict[str, Any] | None
    anomalies: tuple[dict[str, Any], ...]
    incidents: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class TimelineTurn:
    number: int
    label: str
    events: tuple[TimelineEvent, ...]


@dataclass(frozen=True)
class CoverageBucket:
    state: str
    label: str
    event_types: tuple[str, ...]


@dataclass(frozen=True)
class TimelineSession:
    session_key: str
    client: str
    status: str
    started_at: str | None
    ended_at: str | None
    parent_session_key: str | None
    title: str
    project: str | None
    model: str | None
    turns: tuple[TimelineTurn, ...]
    direct_event_count: int
    lossy_event_count: int
    low_confidence_count: int
    coverage: tuple[CoverageBucket, ...]
    session_anomalies: tuple[dict[str, Any], ...]
    children: tuple["TimelineSession", ...]


@dataclass(frozen=True)
class TimelinePage:
    requested_session_key: str
    canonical_session_key: str | None
    redirect_session_key: str | None
    root: TimelineSession | None
    workbench_row: dict[str, Any] | None


def load_timeline_page(
    conn: sqlite3.Connection, requested_session_key: str
) -> TimelinePage:
    """Load a full parent-rooted timeline tree for `requested_session_key`."""
    ensure_events_schema(conn)
    ensure_view_schema(conn)
    ensure_cost_schema(conn)
    ensure_incidents_schema(conn)

    event_session = _load_event_session(conn, requested_session_key)
    workbench_row = _load_workbench_session(conn, requested_session_key)
    if event_session is None:
        return TimelinePage(
            requested_session_key=requested_session_key,
            canonical_session_key=(
                requested_session_key if workbench_row is not None else None
            ),
            redirect_session_key=None,
            root=None,
            workbench_row=workbench_row,
        )

    root_key = _resolve_root_session_key(conn, requested_session_key)
    return TimelinePage(
        requested_session_key=requested_session_key,
        canonical_session_key=root_key,
        redirect_session_key=root_key if root_key != requested_session_key else None,
        root=_load_session_tree(conn, root_key, set()),
        workbench_row=_load_workbench_session(conn, root_key),
    )


def render_timeline_html(page: TimelinePage) -> str:
    """Render a full standalone HTML page for a timeline request."""
    workbench = page.workbench_row or {}
    fallback_title = str(
        page.canonical_session_key or page.requested_session_key
    )
    title = (
        page.root.title
        if page.root is not None
        else resolve_session_title(workbench, fallback=fallback_title)
    )
    body = (
        _render_timeline_page(page)
        if page.root is not None
        else _render_pending_ingest_page(page)
    )
    canonical = (
        f"clawjournal://session/{page.canonical_session_key}"
        if page.canonical_session_key
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)} · Session Timeline</title>
<style>
:root {{
  color-scheme: light;
  --bg: #f5efe5;
  --panel: #fffdf8;
  --panel-strong: #fff7eb;
  --ink: #1b1f1f;
  --muted: #5d5d57;
  --line: #ddd1bf;
  --direct: #0f766e;
  --lossy: #b45309;
  --low: #92400e;
  --missing: #9f1239;
  --present: #166534;
  --shadow: 0 16px 40px rgba(56, 39, 15, 0.08);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  background:
    radial-gradient(circle at top left, rgba(245, 158, 11, 0.14), transparent 28rem),
    linear-gradient(180deg, #fcfaf4 0%, var(--bg) 100%);
  color: var(--ink);
}}
a {{ color: inherit; }}
main {{
  max-width: 1100px;
  margin: 0 auto;
  padding: 32px 20px 80px;
}}
.crumb {{
  display: inline-block;
  margin-bottom: 18px;
  font-size: 0.94rem;
  color: var(--muted);
  text-decoration: none;
}}
.hero, .session, .turn, .event, .pending {{
  background: var(--panel);
  border: 1px solid var(--line);
  box-shadow: var(--shadow);
}}
.hero {{
  padding: 24px;
  border-radius: 22px;
  margin-bottom: 24px;
}}
.hero h1 {{
  margin: 0 0 8px;
  font-size: clamp(1.8rem, 2vw + 1rem, 3rem);
}}
.lede {{
  margin: 0 0 18px;
  color: var(--muted);
  max-width: 60rem;
}}
.meta-row, .coverage-row, .badge-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}}
.pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: #f9f4ea;
  font-size: 0.88rem;
  line-height: 1.1;
}}
.pill--direct {{
  border-color: rgba(15, 118, 110, 0.35);
  background: rgba(15, 118, 110, 0.11);
  color: var(--direct);
}}
.pill--lossy {{
  border-color: rgba(180, 83, 9, 0.3);
  background: rgba(245, 158, 11, 0.16);
  color: var(--lossy);
}}
.pill--low {{
  border-color: rgba(146, 64, 14, 0.28);
  background: rgba(251, 191, 36, 0.18);
  color: var(--low);
}}
.pill--missing {{
  border-style: dashed;
  border-color: rgba(159, 18, 57, 0.38);
  background:
    repeating-linear-gradient(
      -45deg,
      rgba(252, 165, 165, 0.2),
      rgba(252, 165, 165, 0.2) 8px,
      rgba(254, 226, 226, 0.85) 8px,
      rgba(254, 226, 226, 0.85) 16px
    );
  color: var(--missing);
}}
.pill--present {{
  border-color: rgba(22, 101, 52, 0.22);
  background: rgba(134, 239, 172, 0.2);
  color: var(--present);
}}
.pill--incident {{
  border-color: rgba(190, 24, 93, 0.25);
  background: rgba(244, 114, 182, 0.13);
  color: #9d174d;
}}
.pill--usage {{
  border-color: rgba(29, 78, 216, 0.22);
  background: rgba(96, 165, 250, 0.12);
  color: #1d4ed8;
}}
.session {{
  border-radius: 20px;
  padding: 22px;
  margin-bottom: 22px;
}}
.session--child {{
  margin-top: 18px;
  background: var(--panel-strong);
}}
.session h2, .session h3 {{
  margin: 0;
}}
.session-header {{
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}}
.session-copy {{
  display: grid;
  gap: 6px;
}}
.session-copy p,
.coverage-copy,
.pending-copy,
.provenance-list,
.event-summary {{
  color: var(--muted);
}}
.coverage {{
  margin: 18px 0 14px;
  display: grid;
  gap: 10px;
}}
.coverage h3,
.turn h3 {{
  margin: 0 0 10px;
  font-size: 1rem;
}}
.timeline {{
  display: grid;
  gap: 16px;
}}
.turn {{
  border-radius: 18px;
  padding: 16px;
}}
.event-list {{
  display: grid;
  gap: 12px;
}}
.event {{
  border-radius: 16px;
  padding: 14px;
  border-left: 5px solid var(--line);
}}
.event--direct {{
  border-left-color: var(--direct);
}}
.event--lossy {{
  border-left-color: var(--lossy);
  background: #fffbf4;
}}
.event--low {{
  box-shadow: inset 0 0 0 1px rgba(180, 83, 9, 0.16), var(--shadow);
}}
.event-topline {{
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: baseline;
}}
.event-anchor {{
  font-weight: 700;
  text-decoration: none;
}}
.event-type {{
  font-size: 1.02rem;
}}
.event-time {{
  color: var(--muted);
  font-size: 0.9rem;
}}
.event-summary {{
  margin: 10px 0 12px;
  font-size: 0.97rem;
}}
details {{
  border-top: 1px solid rgba(93, 93, 87, 0.12);
  margin-top: 12px;
  padding-top: 10px;
}}
summary {{
  cursor: pointer;
  font-weight: 600;
}}
.provenance-list {{
  margin: 10px 0 0;
  padding-left: 18px;
}}
pre {{
  margin: 10px 0 0;
  padding: 12px;
  border-radius: 12px;
  background: #f8f1e4;
  border: 1px solid rgba(93, 93, 87, 0.12);
  overflow-x: auto;
  font-size: 0.85rem;
}}
.children {{
  margin-top: 22px;
  display: grid;
  gap: 16px;
}}
.pending {{
  border-radius: 20px;
  padding: 24px;
}}
.legend {{
  margin-top: 14px;
  display: grid;
  gap: 8px;
}}
.legend strong {{
  font-size: 0.92rem;
}}
code {{
  background: #f6efe2;
  padding: 2px 6px;
  border-radius: 8px;
}}
@media (max-width: 720px) {{
  main {{ padding: 20px 14px 48px; }}
  .hero, .session, .turn, .event {{ padding: 16px; }}
}}
</style>
</head>
<body>
<main>
<a class="crumb" href="/">Workbench</a>
<section class="hero">
  <h1>Session Timeline</h1>
  <p class="lede">Server-rendered normalized event view with inline cost and incident signals. Works fully offline on the existing localhost daemon.</p>
  <div class="meta-row">
    {_render_meta_pill("Canonical session", page.canonical_session_key or page.requested_session_key)}
    {_render_meta_pill("Deep link", canonical) if canonical else ""}
    {_render_meta_pill("Requested", page.requested_session_key)}
  </div>
  <div class="legend">
    <strong>Visual treatments</strong>
    <div class="badge-row">
      <span class="pill pill--direct">Captured directly</span>
      <span class="pill pill--lossy">Captured but lossy</span>
      <span class="pill pill--missing">Not captured by this client</span>
    </div>
  </div>
</section>
{body}
</main>
<script>
if (window.location.hash) {{
  const target = document.getElementById(window.location.hash.slice(1));
  if (target) {{
    target.scrollIntoView({{block: "center"}});
  }}
}}
</script>
</body>
</html>"""


def render_not_found_html(session_key: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session not found</title>
<style>
body {{
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: #f6f0e6;
  color: #1b1f1f;
  font-family: Georgia, "Iowan Old Style", serif;
}}
main {{
  max-width: 42rem;
  margin: 0 auto;
  padding: 24px;
  background: #fffdf8;
  border: 1px solid #ddd1bf;
  border-radius: 18px;
}}
code {{
  background: #f6efe2;
  padding: 2px 6px;
  border-radius: 8px;
}}
</style>
</head>
<body>
<main>
  <h1>Session not found</h1>
  <p>No event timeline or workbench session was found for <code>{_h(session_key)}</code>.</p>
</main>
</body>
</html>"""


def canonical_session_path(session_key: str) -> str:
    """Return the HTTP path that serves this session's timeline page.

    Distinct from the public ``clawjournal://session/<session_key>`` deep-link
    contract (ADR-001): the deep link is the durable bookmarkable identifier;
    the HTTP path is an implementation detail that avoids colliding with the
    SPA's existing ``/session/:id`` client-side route.
    """
    return f"/timeline/{quote(session_key, safe='')}"


def _load_event_session(
    conn: sqlite3.Connection, session_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, session_key, parent_session_key, client, client_version,
               started_at, ended_at, status
          FROM event_sessions
         WHERE session_key = ?
        """,
        (session_key,),
    ).fetchone()


def _load_workbench_session(
    conn: sqlite3.Connection, session_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT session_id, session_key, project, source, model,
               start_time, end_time, display_title, ai_display_title,
               raw_source_path, fork_of, fork_nickname
          FROM sessions
         WHERE session_key = ?
         ORDER BY COALESCE(updated_at, indexed_at, start_time, '') DESC
         LIMIT 1
        """,
        (session_key,),
    ).fetchone()
    return None if row is None else dict(row)


def _resolve_root_session_key(conn: sqlite3.Connection, session_key: str) -> str:
    current_key = session_key
    seen: set[str] = set()
    while current_key not in seen:
        seen.add(current_key)
        row = _load_event_session(conn, current_key)
        if row is None:
            return session_key
        parent_key = row["parent_session_key"]
        if parent_key is None:
            return current_key
        parent_row = _load_event_session(conn, str(parent_key))
        if parent_row is None:
            # Child rows can arrive before their parent transcript has been
            # ingested. Keep rendering the concrete child session we do have
            # instead of walking to a missing root and crashing.
            return current_key
        current_key = str(parent_row["session_key"])
    return session_key


def _load_session_tree(
    conn: sqlite3.Connection, session_key: str, seen: set[str]
) -> TimelineSession:
    if session_key in seen:
        raise ValueError(f"Session hierarchy cycle detected at {session_key}")
    seen.add(session_key)
    try:
        session_row = _load_event_session(conn, session_key)
        if session_row is None:
            raise KeyError(f"session_key not found: {session_key}")
        session_id = int(session_row["id"])
        workbench = _load_workbench_session(conn, session_key)
        events = _load_events(conn, session_id)
        coverage = _load_coverage(conn, session_id)
        session_anomalies, anomalies_by_event = _load_cost_anomalies(conn, session_id)
        incidents_by_event = _load_incidents(conn, session_id)
        usage_by_event = _load_token_usage(conn, session_id)

        enriched_events: list[TimelineEvent] = []
        for event in events:
            enriched_events.append(
                TimelineEvent(
                    event_id=event.event_id,
                    anchor_id=(
                        f"event-{event.event_id}"
                        if event.event_id is not None
                        else (
                            "event-key-"
                            f"{_anchor_slug(session_key)}-"
                            f"{_anchor_slug(event.event_key or event.type)}"
                        )
                    ),
                    turn_number=event.turn_number,
                    type=event.type,
                    event_key=event.event_key,
                    event_at=event.event_at,
                    ingested_at=event.ingested_at,
                    source=event.source,
                    confidence=event.confidence,
                    lossiness=event.lossiness,
                    summary=event.summary,
                    raw_ref=event.raw_ref,
                    origin=event.origin,
                    raw_excerpt=event.raw_excerpt,
                    payload_excerpt=event.payload_excerpt,
                    token_usage=(
                        None
                        if event.event_id is None
                        else usage_by_event.get(event.event_id)
                    ),
                    anomalies=tuple(
                        ()
                        if event.event_id is None
                        else anomalies_by_event.get(event.event_id, ())
                    ),
                    incidents=tuple(
                        ()
                        if event.event_id is None
                        else incidents_by_event.get(event.event_id, ())
                    ),
                )
            )

        turns = _group_turns(enriched_events)
        child_rows = conn.execute(
            """
            SELECT session_key
              FROM event_sessions
             WHERE parent_session_key = ?
             ORDER BY started_at IS NULL, started_at, session_key
            """,
            (session_key,),
        ).fetchall()
        return TimelineSession(
            session_key=session_key,
            client=str(session_row["client"]),
            status=str(session_row["status"]),
            started_at=session_row["started_at"],
            ended_at=session_row["ended_at"],
            parent_session_key=session_row["parent_session_key"],
            title=_session_title(workbench, session_row),
            project=(workbench or {}).get("project"),
            model=(workbench or {}).get("model"),
            turns=tuple(turns),
            direct_event_count=sum(
                1 for event in enriched_events if event.lossiness == "none"
            ),
            lossy_event_count=sum(
                1 for event in enriched_events if event.lossiness != "none"
            ),
            low_confidence_count=sum(
                1 for event in enriched_events if event.confidence != "high"
            ),
            coverage=tuple(coverage),
            session_anomalies=tuple(session_anomalies),
            children=tuple(
                _load_session_tree(conn, str(row["session_key"]), seen)
                for row in child_rows
            ),
        )
    finally:
        seen.remove(session_key)


def _load_events(conn: sqlite3.Connection, session_id: int) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    turn_number = 0
    canonical = sorted(
        canonical_events(conn, session_id),
        key=_timeline_event_sort_key,
    )
    for event in canonical:
        if event.type == "user_message":
            turn_number += 1
        events.append(
            TimelineEvent(
                event_id=event.event_id,
                anchor_id="",
                turn_number=turn_number,
                type=event.type,
                event_key=event.event_key,
                event_at=event.event_at,
                ingested_at=event.ingested_at,
                source=event.source,
                confidence=event.confidence,
                lossiness=event.lossiness,
                summary=_event_summary(event.payload_json or event.raw_json, event.type),
                raw_ref=event.raw_ref,
                origin=event.origin,
                raw_excerpt=_excerpt_text(event.raw_json),
                payload_excerpt=_excerpt_text(event.payload_json),
                token_usage=None,
                anomalies=(),
                incidents=(),
            )
        )
    return events


def _timeline_event_sort_key(event) -> tuple[Any, ...]:
    """Restore chronological order before assigning timeline turns.

    `canonical_events()` yields base rows in canonical vendor order and then
    appends hook-only overrides whose event_key never appeared on a base row.
    The timeline renderer needs a single chronological stream so early
    hook-only events (for example a prompt captured only by hooks) do not get
    shoved after later vendor rows and distort turn numbering.
    """
    source_path = ""
    source_offset = -1
    seq = -1
    if event.raw_ref is not None:
        source_path, source_offset, seq = event.raw_ref
    return (
        event.event_at is None,
        event.event_at or "",
        event.event_id is None,
        event.event_id if event.event_id is not None else 2**63 - 1,
        source_path,
        source_offset,
        seq,
        event.event_key or "",
        event.type,
    )


def _load_coverage(conn: sqlite3.Connection, session_id: int) -> list[CoverageBucket]:
    labels = {
        "missing": "Not captured by this client",
        "supported_but_absent": "Client supports it, but it did not occur here",
        "present": "Observed in this session",
    }
    grouped: dict[str, list[str]] = {
        "missing": [],
        "supported_but_absent": [],
        "present": [],
    }
    for state in capability_join(conn, session_id):
        grouped[state.state].append(state.event_type)
    return [
        CoverageBucket(
            state=state,
            label=labels[state],
            event_types=tuple(sorted(grouped[state])),
        )
        for state in ("missing", "supported_but_absent", "present")
        if grouped[state]
    ]


def _load_token_usage(conn: sqlite3.Connection, session_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT event_id, model, service_tier, data_source, input, output,
               cache_read, cache_write, reasoning, cost_estimate
          FROM token_usage
         WHERE session_id = ?
        """,
        (session_id,),
    ).fetchall()
    return {int(row["event_id"]): dict(row) for row in rows}


def _load_cost_anomalies(
    conn: sqlite3.Connection, session_id: int
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    rows = conn.execute(
        """
        SELECT turn_event_id, kind, confidence, evidence_json
          FROM cost_anomalies
         WHERE session_id = ?
         ORDER BY id
        """,
        (session_id,),
    ).fetchall()
    session_level: list[dict[str, Any]] = []
    by_event: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        entry = {
            "kind": row["kind"],
            "confidence": row["confidence"],
            "evidence": _load_json(row["evidence_json"]),
        }
        if row["turn_event_id"] is None:
            session_level.append(entry)
            continue
        by_event.setdefault(int(row["turn_event_id"]), []).append(entry)
    return session_level, by_event


def _load_incidents(
    conn: sqlite3.Connection, session_id: int
) -> dict[int, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT first_event_id, last_event_id, kind, count, confidence, evidence_json
          FROM incidents
         WHERE session_id = ?
         ORDER BY first_event_id, id
        """,
        (session_id,),
    ).fetchall()
    by_event: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_event.setdefault(int(row["first_event_id"]), []).append(
            {
                "kind": row["kind"],
                "count": int(row["count"]),
                "confidence": row["confidence"],
                "last_event_id": int(row["last_event_id"]),
                "evidence": _load_json(row["evidence_json"]),
            }
        )
    return by_event


def _group_turns(events: list[TimelineEvent]) -> list[TimelineTurn]:
    grouped: dict[int, list[TimelineEvent]] = {}
    order: list[int] = []
    for event in events:
        if event.turn_number not in grouped:
            grouped[event.turn_number] = []
            order.append(event.turn_number)
        grouped[event.turn_number].append(event)
    turns: list[TimelineTurn] = []
    for number in order:
        label = "Session setup" if number == 0 else f"Turn {number}"
        turns.append(
            TimelineTurn(number=number, label=label, events=tuple(grouped[number]))
        )
    return turns


def _session_title(
    workbench: dict[str, Any] | None, session_row: sqlite3.Row
) -> str:
    if workbench:
        fallback = str(
            workbench.get("project")
            or workbench.get("session_key")
            or session_row["session_key"]
        )
        return resolve_session_title(workbench, fallback=fallback)
    return str(session_row["session_key"])


def _event_summary(raw_json: str | None, event_type: str) -> str:
    if not raw_json:
        return event_type.replace("_", " ")
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return _compact(raw_json, 220)
    strings: list[str] = []
    _collect_summary_strings(payload, strings, limit=6)
    if strings:
        return _compact(" · ".join(strings), 220)
    return _compact(raw_json, 220)


def _collect_summary_strings(value: Any, out: list[str], *, limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(value, str):
        cleaned = _compact(value, 160)
        if cleaned:
            out.append(cleaned)
        return
    if isinstance(value, list):
        for item in value:
            _collect_summary_strings(item, out, limit=limit)
            if len(out) >= limit:
                return
        return
    if not isinstance(value, dict):
        return
    for key in _SUMMARY_KEYS:
        if key in value:
            _collect_summary_strings(value[key], out, limit=limit)
            if len(out) >= limit:
                return
    for key, child in value.items():
        if key in _SUMMARY_KEYS:
            continue
        _collect_summary_strings(child, out, limit=limit)
        if len(out) >= limit:
            return


def _excerpt_text(raw_text: str | None, limit: int = 640) -> str | None:
    if not raw_text:
        return None
    stripped = raw_text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "…"


def _compact(value: str, limit: int) -> str:
    squashed = _WHITESPACE_RE.sub(" ", value).strip()
    if len(squashed) <= limit:
        return squashed
    return squashed[: limit - 1] + "…"


def _load_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else {"value": value}


def _render_timeline_page(page: TimelinePage) -> str:
    assert page.root is not None
    return _render_session(page.root, level=0)


def _render_pending_ingest_page(page: TimelinePage) -> str:
    session_key = page.canonical_session_key or page.requested_session_key
    wb = page.workbench_row or {}
    return f"""
<section class="pending">
  <h2>Events not ingested yet</h2>
  <p class="pending-copy">Workbench metadata exists for <code>{_h(session_key)}</code>, but there is no normalized event timeline yet.</p>
  <div class="meta-row">
    {_render_meta_pill("Project", wb.get("project"))}
    {_render_meta_pill("Model", wb.get("model"))}
    {_render_meta_pill("Source", wb.get("source"))}
  </div>
  <pre>clawjournal events ingest</pre>
</section>"""


def _render_session(session: TimelineSession, *, level: int) -> str:
    session_kind = "Root session" if level == 0 else "Subagent session"
    child_markup = "".join(
        _render_session(child, level=level + 1) for child in session.children
    )
    coverage_markup = "".join(_render_coverage_bucket(bucket) for bucket in session.coverage)
    anomalies_markup = "".join(
        f'<span class="pill pill--lossy">{_h(anomaly["kind"])} ({_h(anomaly["confidence"])})</span>'
        for anomaly in session.session_anomalies
    )
    turns_markup = "".join(_render_turn(turn) for turn in session.turns)
    children_block = f'<div class="children">{child_markup}</div>' if child_markup else ""
    session_class = "session session--child" if level else "session"
    anomalies_block = ""
    if anomalies_markup:
        anomalies_block = (
            '<section class="coverage"><h3>Session-level cost anomalies</h3>'
            f'<div class="badge-row">{anomalies_markup}</div></section>'
        )
    empty_block = (
        '<section class="turn"><p class="coverage-copy">No canonical events ingested yet.</p></section>'
        if not turns_markup
        else turns_markup
    )
    return f"""
<section class="{session_class}">
  <div class="session-header">
    <div class="session-copy">
      <p>{_h(session_kind)}</p>
      <h2>{_h(session.title)}</h2>
      <p>{_h(session.session_key)}</p>
    </div>
    <div class="meta-row">
      {_render_meta_pill("Client", session.client)}
      {_render_meta_pill("Status", session.status)}
      {_render_meta_pill("Project", session.project)}
      {_render_meta_pill("Model", session.model)}
      {_render_meta_pill("Started", session.started_at)}
      {_render_meta_pill("Ended", session.ended_at)}
      {_render_meta_pill("Direct", str(session.direct_event_count))}
      {_render_meta_pill("Lossy", str(session.lossy_event_count))}
      {_render_meta_pill("Low confidence", str(session.low_confidence_count))}
    </div>
  </div>
  <section class="coverage">
    <h3>Coverage</h3>
    <p class="coverage-copy">Missing-capability states are rendered separately from low-confidence rows so "not captured by this client" never looks like weak evidence.</p>
    <div class="coverage-row">{coverage_markup}</div>
  </section>
  {anomalies_block}
  <div class="timeline">{empty_block}</div>
  {children_block}
</section>"""


def _render_coverage_bucket(bucket: CoverageBucket) -> str:
    tooltip = ", ".join(bucket.event_types)
    if bucket.state == "present":
        tone = "present"
    elif bucket.state == "missing":
        tone = "missing"
    else:
        tone = "lossy"
    return (
        f'<span class="pill pill--{tone}" title="{_h(tooltip)}">'
        f"{_h(bucket.label)} · {_h(str(len(bucket.event_types)))}</span>"
    )


def _render_turn(turn: TimelineTurn) -> str:
    events_markup = "".join(_render_event(event) for event in turn.events)
    return f"""
<section class="turn">
  <h3>{_h(turn.label)}</h3>
  <div class="event-list">{events_markup}</div>
</section>"""


def _render_event(event: TimelineEvent) -> str:
    event_classes = ["event"]
    if event.lossiness == "none":
        event_classes.append("event--direct")
    else:
        event_classes.append("event--lossy")
    if event.confidence != "high":
        event_classes.append("event--low")

    badge_row = [
        f'<span class="pill">{_h(event.source)}</span>',
        (
            '<span class="pill pill--direct">high confidence</span>'
            if event.confidence == "high"
            else f'<span class="pill pill--low">{_h(event.confidence)} confidence</span>'
        ),
        (
            '<span class="pill pill--direct">captured directly</span>'
            if event.lossiness == "none"
            else f'<span class="pill pill--lossy">{_h(event.lossiness)}</span>'
        ),
    ]
    if event.token_usage is not None:
        badge_row.append(
            f'<span class="pill pill--usage">{_h(_usage_label(event.token_usage))}</span>'
        )
    for anomaly in event.anomalies:
        badge_row.append(
            f'<span class="pill pill--lossy">{_h(anomaly["kind"])} ({_h(anomaly["confidence"])})</span>'
        )
    for incident in event.incidents:
        badge_row.append(
            f'<span class="pill pill--incident">{_h(incident["kind"])} ×{incident["count"]}</span>'
        )

    provenance_items = [
        f"<li>source: <code>{_h(event.source)}</code></li>",
        f"<li>confidence: <code>{_h(event.confidence)}</code></li>",
        f"<li>lossiness: <code>{_h(event.lossiness)}</code></li>",
        (
            f"<li>raw_ref: <code>{_h(event.raw_ref[0])}:{event.raw_ref[1]}:{event.raw_ref[2]}</code></li>"
            if event.raw_ref is not None
            else "<li>raw_ref: <code>(hook-only)</code></li>"
        ),
        f"<li>event_key: <code>{_h(event.event_key or '(none)')}</code></li>",
        f"<li>ingested_at: <code>{_h(event.ingested_at or '(unknown)')}</code></li>",
    ]
    if event.origin:
        provenance_items.append(f"<li>origin: <code>{_h(event.origin)}</code></li>")

    excerpts = []
    if event.payload_excerpt:
        excerpts.append(f"<pre>{_h(event.payload_excerpt)}</pre>")
    if event.raw_excerpt:
        excerpts.append(f"<pre>{_h(event.raw_excerpt)}</pre>")

    anchor_label = "#" + str(event.event_id) if event.event_id is not None else "#hook"
    return f"""
<article id="{_h(event.anchor_id)}" class="{' '.join(event_classes)}">
  <div class="event-topline">
    <a class="event-anchor" href="#{_h(event.anchor_id)}">{_h(anchor_label)}</a>
    <strong class="event-type">{_h(event.type)}</strong>
    <span class="event-time">{_h(event.event_at or '(no event_at)')}</span>
  </div>
  <p class="event-summary">{_h(event.summary)}</p>
  <div class="badge-row">{''.join(badge_row)}</div>
  <details>
    <summary>Provenance</summary>
    <ul class="provenance-list">{''.join(provenance_items)}</ul>
    {''.join(excerpts)}
  </details>
</article>"""


def _usage_label(usage: dict[str, Any]) -> str:
    parts: list[str] = [str(usage.get("data_source") or "usage")]
    if usage.get("input") is not None:
        parts.append(f"in {usage['input']}")
    if usage.get("output") is not None:
        parts.append(f"out {usage['output']}")
    if usage.get("cache_read") is not None:
        parts.append(f"cache-read {usage['cache_read']}")
    if usage.get("reasoning") is not None:
        parts.append(f"reasoning {usage['reasoning']}")
    if usage.get("cost_estimate") is not None:
        parts.append(f"${float(usage['cost_estimate']):.4f}")
    return " · ".join(parts)


def _anchor_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug or "event"


def _render_meta_pill(label: str, value: str | None) -> str:
    if not value:
        return ""
    return f'<span class="pill"><strong>{_h(label)}:</strong> {_h(value)}</span>'


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)
