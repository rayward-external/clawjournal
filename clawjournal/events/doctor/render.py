"""Doctor output renderers + sanitization helpers (phase-1 plan 08).

Human renderer prints a tabular report; JSON renderer emits the
schema-versioned ``events_doctor_schema_version: "1.0"`` envelope.

Sanitization scrubs control characters from vendor- or user-controlled
strings (``client_version`` from JSONLs; overlay ``reason`` text)
before they reach human output. JSON output passes the value verbatim
so JSON encoding handles escaping. Length-cap at 200 bytes.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import TextIO

from clawjournal.events.doctor.probes import (
    DoctorReport,
    INSTALL_DB_CORRUPT,
    INSTALL_DB_MISSING,
    INSTALL_EVENTS_EMPTY,
    INSTALL_FRESH,
    INSTALL_HEALTHY,
    INSTALL_WORKBENCH_ONLY,
    VERDICT_COMPATIBLE,
    VERDICT_PARTIAL,
    VERDICT_UNKNOWN_SCHEMA,
    report_to_dict,
)

_MAX_DISPLAY_LEN = 200


def sanitize_for_human(text: str | None) -> str:
    """Strip control characters; cap length; never return None."""

    if text is None:
        return ""
    cleaned = "".join(ch for ch in text if ch.isprintable() or ch == " ")
    if len(cleaned) > _MAX_DISPLAY_LEN:
        cleaned = cleaned[:_MAX_DISPLAY_LEN] + "…"
    return cleaned


def render_human(report: DoctorReport, *, stream: TextIO | None = None) -> str:
    """Render a human-readable report. Returns the text; optionally writes."""

    buf = StringIO()
    buf.write(f"clawjournal {sanitize_for_human(report.clawjournal_version)}\n")
    buf.write(f"  bundle_schema_version:    {report.bundle_schema_version}\n")
    buf.write(f"  recorder_schema_version:  {report.recorder_schema_version}\n")
    if report.security_schema_version is not None:
        buf.write(
            f"  security_schema_version:  {report.security_schema_version}\n"
        )
    buf.write("\n")

    for warning in report.warnings:
        buf.write(f"warning: {sanitize_for_human(warning.get('message'))}\n")
    if report.warnings:
        buf.write("\n")

    if report.install_state in (INSTALL_FRESH, INSTALL_DB_MISSING):
        buf.write(f"Install state: {report.install_state}\n")
        buf.write(f"  {report.install_hint}\n")
        if _maybe_write_fs_clients(buf, report):
            pass
        if stream is not None:
            stream.write(buf.getvalue())
        return buf.getvalue()

    buf.write(f"Index DB:    {report.index_db_path}")
    if report.install_state == INSTALL_DB_CORRUPT:
        buf.write(" (unreadable)\n")
        buf.write(f"  {report.install_hint}\n")
        if stream is not None:
            stream.write(buf.getvalue())
        return buf.getvalue()
    buf.write(
        f" (events: {report.events_count} rows, sessions: {report.sessions_count})\n"
    )
    th = report.trufflehog
    if th.state == "present":
        buf.write(f"TruffleHog:  {sanitize_for_human(th.version)} (present)\n")
    elif th.state == "missing":
        buf.write(
            "TruffleHog:  missing — install via `clawjournal trufflehog install` "
            "(or `brew install trufflehog`)\n"
        )
    else:
        buf.write(
            f"TruffleHog:  {sanitize_for_human(th.version) or 'unknown'} "
            f"({th.state})\n"
        )

    if report.install_state == INSTALL_WORKBENCH_ONLY:
        buf.write(f"\n{report.install_hint}\n")
        if stream is not None:
            stream.write(buf.getvalue())
        return buf.getvalue()

    if report.install_state == INSTALL_EVENTS_EMPTY:
        buf.write(f"\n{report.install_hint}\n")
        _maybe_write_fs_clients(buf, report)
        if stream is not None:
            stream.write(buf.getvalue())
        return buf.getvalue()

    if report.clients:
        buf.write("\nClients observed:\n")
        for client in report.clients:
            line = _format_client_line(client)
            buf.write(f"  {line}\n")
            for sub in _format_client_detail_lines(client):
                buf.write(f"    {sub}\n")

    if report.cost is not None:
        buf.write("\nCost ledger:\n")
        buf.write(f"  token_usage:    {report.cost.token_usage_rows} rows\n")
        buf.write(f"  cost_anomalies: {report.cost.cost_anomalies_rows}\n")
        if report.cost.last_event_id is not None:
            buf.write(
                f"  caught up to event_id {report.cost.last_event_id}"
                + (
                    f" ({sanitize_for_human(report.cost.last_event_at)})"
                    if report.cost.last_event_at
                    else ""
                )
                + "\n"
            )
        else:
            buf.write("  not yet ingested\n")

    if report.incidents is not None:
        buf.write("\nIncidents (loop detector lite):\n")
        if report.incidents.counts_by_kind:
            for kind, count in sorted(report.incidents.counts_by_kind.items()):
                buf.write(f"  {sanitize_for_human(kind)}: {count}\n")
        else:
            buf.write("  none yet\n")
        if report.incidents.last_event_id is not None:
            buf.write(
                f"  caught up to event_id {report.incidents.last_event_id}\n"
            )

    suggestions = _suggested_next_steps(report)
    if suggestions:
        buf.write("\nSuggested next steps:\n")
        for line in suggestions:
            buf.write(f"  • {line}\n")

    text = buf.getvalue()
    if stream is not None:
        stream.write(text)
    return text


def _suggested_next_steps(report: DoctorReport) -> list[str]:
    """Per-state hints surfaced at the end of human output when any
    verdict requires user action."""

    has_partial = any(c.verdict == VERDICT_PARTIAL for c in report.clients)
    has_unknown = any(c.verdict == VERDICT_UNKNOWN_SCHEMA for c in report.clients)
    if not (has_partial or has_unknown):
        return []
    lines: list[str] = []
    if has_partial:
        lines.append(
            "run `clawjournal events doctor --fix` to write a user overlay "
            "(additive drift only)"
        )
        lines.append(
            "inspect a schema_unknown row: `clawjournal events inspect "
            "<event_id>` (find ids via `sqlite3 ~/.clawjournal/index.db "
            "\"SELECT id FROM events WHERE type='schema_unknown' LIMIT 5\"`)"
        )
    if has_unknown:
        lines.append(
            "file an issue with the --json payload attached "
            "(structural drift requires a code change; --fix refuses it)"
        )
    return lines


def _maybe_write_fs_clients(buf: StringIO, report: DoctorReport) -> bool:
    if not report.fs_clients:
        return False
    buf.write("\nFilesystem-detected clients (not yet ingested):\n")
    for name in report.fs_clients:
        buf.write(f"  {name}\n")
    return True


def _format_client_line(client) -> str:
    version_str = sanitize_for_human(client.client_version) or "(no version)"
    sessions_word = "session" if client.sessions_count == 1 else "sessions"
    if client.verdict == VERDICT_COMPATIBLE:
        types_word = (
            "type"
            if len(client.event_types_observed) == 1
            else "types"
        )
        suffix = (
            f"compatible ({len(client.event_types_observed)} of "
            f"{client.matrix_supported_count} matrix-supported event {types_word} observed, "
            f"all fields known, {client.sessions_count} {sessions_word})"
        )
    elif client.verdict == VERDICT_PARTIAL:
        suffix = "partially-compatible"
    else:
        unknown = ", ".join(client.unknown_event_types) or "none"
        suffix = (
            f"unknown-schema (unknown event types: {sanitize_for_human(unknown)})"
        )
    return f"{client.client}  {version_str}   {suffix}"


def _format_client_detail_lines(client) -> list[str]:
    if client.verdict == VERDICT_COMPATIBLE:
        return []
    lines: list[str] = []
    if client.verdict == VERDICT_PARTIAL:
        if client.schema_unknown_rows > 0:
            lines.append(
                f"{client.schema_unknown_rows} schema_unknown row(s) in this "
                f"client's sessions"
            )
        if client.unsupported_event_types:
            lines.append(
                f"event types observed but not supported in matrix: "
                f"{', '.join(client.unsupported_event_types)}"
            )
    return lines


def render_json(
    report: DoctorReport,
    *,
    request_id: str | None = None,
    stream: TextIO | None = None,
) -> str:
    payload = report_to_dict(report)
    if request_id is not None:
        payload["_meta"] = {"request_id": request_id}
    text = json.dumps(payload, indent=2, sort_keys=True)
    if stream is not None:
        stream.write(text)
        stream.write("\n")
    return text


__all__ = [
    "render_human",
    "render_json",
    "sanitize_for_human",
]
