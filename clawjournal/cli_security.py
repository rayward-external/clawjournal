"""CLI handlers for the security-refactor verbs.

Kept separate from `cli.py` to contain this module's surface without
growing that already-large file. Handlers take an already-parsed argparse
`Namespace`, open a DB connection, and print JSON results on stdout. They
exit with non-zero status on hard errors (missing session, invalid flags,
unknown ID) so scripts can react.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Callable

from .findings import (
    allowlist_add,
    allowlist_list,
    allowlist_remove,
    allowlist_remove_by_text,
    dedupe_findings_by_entity,
    derive_preview,
    hash_entity,
    load_findings_from_db,
    set_finding_status,
)
from .workbench.findings_pipeline import (
    drain_findings_backfill,
    run_findings_pipeline,
)
from .workbench.index import (
    get_hold_history,
    open_index,
    read_blob,
    set_logical_hold_state,
)
from .config import load_config


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def _fail(message: str, **extra: Any) -> None:
    _emit({"error": message, **extra})
    sys.exit(1)


def _with_connection(func: Callable[[sqlite3.Connection], Any]) -> Any:
    conn = open_index()
    try:
        return func(conn)
    finally:
        conn.close()


def _lookup_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT session_id, hold_state, embargo_until FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# hold / release / embargo / hold-history
# ---------------------------------------------------------------------------

def run_hold(args) -> None:
    def body(conn: sqlite3.Connection) -> None:
        if _lookup_session(conn, args.session_id) is None:
            _fail("session not found", session_id=args.session_id)
        set_logical_hold_state(
            conn, args.session_id, "pending_review",
            changed_by="user", reason=args.reason,
        )
        _emit({"session_id": args.session_id, "hold_state": "pending_review"})
    _with_connection(body)


def run_release(args) -> None:
    def body(conn: sqlite3.Connection) -> None:
        if _lookup_session(conn, args.session_id) is None:
            _fail("session not found", session_id=args.session_id)
        set_logical_hold_state(
            conn, args.session_id, "released",
            changed_by="user", reason=args.reason,
        )
        _emit({"session_id": args.session_id, "hold_state": "released"})
    _with_connection(body)


def run_embargo(args) -> None:
    def body(conn: sqlite3.Connection) -> None:
        if _lookup_session(conn, args.session_id) is None:
            _fail("session not found", session_id=args.session_id)
        # Accept YYYY-MM-DD (treated as midnight UTC) or full ISO 8601.
        until = args.until
        if len(until) == 10 and until.count("-") == 2:
            until = f"{until}T00:00:00+00:00"
        try:
            set_logical_hold_state(
                conn, args.session_id, "embargoed",
                changed_by="user", reason=args.reason, embargo_until=until,
            )
        except ValueError as exc:
            _fail(str(exc), session_id=args.session_id, until=args.until)
        _emit({
            "session_id": args.session_id,
            "hold_state": "embargoed",
            "embargo_until": until,
        })
    _with_connection(body)


def run_hold_history(args) -> None:
    def body(conn: sqlite3.Connection) -> None:
        if _lookup_session(conn, args.session_id) is None:
            _fail("session not found", session_id=args.session_id)
        history = get_hold_history(conn, args.session_id)
        _emit({"session_id": args.session_id, "history": history})
    _with_connection(body)


# ---------------------------------------------------------------------------
# findings verb
# ---------------------------------------------------------------------------

def _resolve_entity_refs(
    conn: sqlite3.Connection, session_id: str, refs: list[str],
) -> tuple[list[str], list[str]]:
    """Map CLI entity-refs to concrete finding_ids.

    An entity-ref can be:
    - A full 32-char finding_id (exact match).
    - A prefix (>=4 chars) of a finding_id.
    - A prefix (>=4 chars) of an entity_hash — in which case we expand
      to every finding for that entity within the session.

    Returns `(finding_ids, unresolved_refs)`. Plaintext is never
    accepted (the dedup listing hides it) — callers show hash prefixes
    to the user and pass those back.
    """
    resolved: list[str] = []
    unresolved: list[str] = []
    for ref in refs:
        if len(ref) < 4:
            unresolved.append(ref)
            continue
        rows = conn.execute(
            "SELECT finding_id FROM findings "
            "WHERE session_id = ? AND "
            "      (finding_id = ? OR finding_id LIKE ? OR entity_hash LIKE ?)",
            (session_id, ref, f"{ref}%", f"{ref}%"),
        ).fetchall()
        if not rows:
            unresolved.append(ref)
            continue
        resolved.extend(row["finding_id"] for row in rows)
    return resolved, unresolved


def _list_findings(conn: sqlite3.Connection, session_id: str, *, show_all: bool) -> None:
    status_filter = None if show_all else {"open"}
    findings = load_findings_from_db(conn, session_id, status_filter=status_filter)
    groups = dedupe_findings_by_entity(findings)
    blob = read_blob(session_id)
    for group in groups:
        # Attach a masked preview derived from the blob (no raw match).
        sample_finding = next(
            (f for f in findings if f.finding_id == group["finding_ids"][0]),
            None,
        )
        if blob is not None and sample_finding is not None:
            group["preview"] = derive_preview(blob, sample_finding)
        else:
            group["preview"] = {"before": "", "after": "", "match_placeholder": "[...]"}
        group["entity_hash_prefix"] = group["entity_hash"][:8]
    _emit({"session_id": session_id, "total": len(groups), "entities": groups})


def run_findings(args) -> None:
    def body(conn: sqlite3.Connection) -> None:
        if _lookup_session(conn, args.session_id) is None:
            _fail("session not found", session_id=args.session_id)

        # Determine operation: list (default) vs bulk/per-entity decision.
        want_accept = list(args.accept or [])
        want_ignore = list(args.ignore or [])
        bulk_accept_all = bool(args.accept_all)
        bulk_ignore_all = bool(args.ignore_all)
        accept_engine = args.accept_engine
        ignore_engine = args.ignore_engine

        if not any([want_accept, want_ignore, bulk_accept_all, bulk_ignore_all,
                    accept_engine, ignore_engine]):
            _list_findings(conn, args.session_id, show_all=bool(args.all))
            return

        if sum(bool(x) for x in [
            want_accept, want_ignore, bulk_accept_all, bulk_ignore_all,
            accept_engine, ignore_engine,
        ]) > 1:
            _fail(
                "pick one of --accept / --ignore / --accept-all / --ignore-all "
                "/ --accept-engine / --ignore-engine",
            )

        status: str
        target_ids: list[str]
        if bulk_accept_all or bulk_ignore_all:
            status = "accepted" if bulk_accept_all else "ignored"
            rows = conn.execute(
                "SELECT finding_id FROM findings WHERE session_id = ? AND status = 'open'",
                (args.session_id,),
            ).fetchall()
            target_ids = [r["finding_id"] for r in rows]
        elif accept_engine or ignore_engine:
            status = "accepted" if accept_engine else "ignored"
            engine_name = accept_engine or ignore_engine
            rows = conn.execute(
                "SELECT finding_id FROM findings "
                "WHERE session_id = ? AND engine = ? AND status = 'open'",
                (args.session_id, engine_name),
            ).fetchall()
            target_ids = [r["finding_id"] for r in rows]
        else:
            status = "accepted" if want_accept else "ignored"
            refs = want_accept or want_ignore
            resolved, unresolved = _resolve_entity_refs(conn, args.session_id, refs)
            if unresolved:
                _fail("unknown finding or entity reference(s)", refs=unresolved)
            target_ids = resolved

        if not target_ids:
            _emit({"session_id": args.session_id, "updated": 0, "allowlisted": 0})
            return

        also_allowlist = bool(args.global_) and status == "ignored"
        updated = set_finding_status(
            conn, target_ids, status,
            reason=args.reason,
            also_allowlist=also_allowlist,
        )
        conn.commit()
        _emit({
            "session_id": args.session_id,
            "status": status,
            "updated": updated,
            "allowlisted": bool(also_allowlist),
        })

    _with_connection(body)


# ---------------------------------------------------------------------------
# allowlist CRUD
# ---------------------------------------------------------------------------

def run_allowlist(args) -> None:
    def body(conn: sqlite3.Connection) -> None:
        if args.op == "list":
            _emit({"entries": list(allowlist_list(conn))})
            return
        if args.op == "add":
            entry, updates, sessions = allowlist_add(
                conn,
                entity_text=args.entity_text,
                entity_type=args.type,
                entity_label=args.label,
                reason=args.reason,
            )
            conn.commit()
            _emit({
                "entry": dict(entry),
                "retroactive_updates": updates,
                "retroactive_sessions": sessions,
            })
            return
        if args.op == "remove":
            if args.by_text:
                outcomes = allowlist_remove_by_text(
                    conn, args.by_text, entity_type=args.type,
                )
                conn.commit()
                _emit({
                    "removed": [
                        {"allowlist_id": aid, "reverted": r, "reassigned": x}
                        for aid, r, x in outcomes
                    ],
                })
                return
            if not args.allowlist_id:
                _fail("allowlist remove needs <allowlist_id> or --by-text")
            removed, reverted, reassigned = allowlist_remove(conn, args.allowlist_id)
            if not removed:
                _fail("allowlist entry not found", allowlist_id=args.allowlist_id)
            conn.commit()
            _emit({
                "allowlist_id": args.allowlist_id,
                "removed": True,
                "reverted": reverted,
                "reassigned": reassigned,
            })
            return
        _fail(f"unknown allowlist op: {args.op!r}")
    _with_connection(body)


# ---------------------------------------------------------------------------
# scan --force
# ---------------------------------------------------------------------------

def run_scan_force(args) -> None:
    def body(conn: sqlite3.Connection) -> None:
        config = dict(load_config())
        if args.session_ids:
            targets = list(args.session_ids)
        elif args.all:
            rows = conn.execute("SELECT session_id FROM sessions").fetchall()
            targets = [r["session_id"] for r in rows]
        else:
            _fail("scan --force needs --all or one or more session ids")

        processed = 0
        missing = []
        errored = []
        for session_id in targets:
            blob = read_blob(session_id)
            if blob is None:
                missing.append(session_id)
                continue
            try:
                run_findings_pipeline(conn, session_id, blob, config=config, force=True)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                errored.append({"session_id": session_id, "error": str(exc)})

        # Also drain the bounded backfill window in the same pass so
        # post-migration flagged rows get picked up alongside forced
        # rebuilds.
        drain = drain_findings_backfill(conn, config=config)

        _emit({
            "processed": processed,
            "missing_blob": missing,
            "errored": errored,
            "backfill_drain": drain,
        })

    _with_connection(body)


# ---------------------------------------------------------------------------
# Legacy pii-review / pii-apply notice
# ---------------------------------------------------------------------------

_LEGACY_PII_NOTICE = (
    "pii-review/pii-apply remain the path for LLM-PII review. "
    "Deterministic regex secrets/PII have moved to 'clawjournal findings <id>' "
    "+ 'clawjournal share'. LLM-PII will migrate once a no-plaintext DB "
    "design lands; these commands will remain until then."
)


def emit_legacy_pii_notice() -> None:
    """Print the stderr notice for `pii-review` / `pii-apply` invocations.

    Not labeled as a DeprecationWarning — removal is conditional on the
    LLM-PII migration (Decision 21). Behavior of the underlying command
    is unchanged; only the notice is added.
    """
    print(_LEGACY_PII_NOTICE, file=sys.stderr)
