"""Scan-time findings pipeline driver.

Wraps the deterministic-engine scan + transactional DB rebuild in one
entry point (`run_findings_pipeline`) that the Scanner, one-shot CLI
`scan`, and `scan --force` all reach. Also hosts the bounded backfill
drain for sessions flagged at schema-migration time.

Design notes, mirroring docs/security-refactor.md §Scan flow:

- **Settle threshold.** Active sessions (missing or too-recent
  `end_time`) are skipped for this tick — they would otherwise churn
  `findings_revision` on every scan as new messages arrive.
- **Revision short-circuit.** `compute_findings_revision(session,
  config=...)` runs first; a match against `sessions.findings_revision`
  means nothing has changed and the rebuild is skipped.
- **Transactional rebuild.** The actual delete + insert + revision
  update runs inside `BEGIN IMMEDIATE` so concurrent CLI decisions and
  Scanner rebuilds serialize cleanly on the write lock.
- **Zero-findings revisions still persist.** A settled session that
  scans clean writes its revision anyway so the next tick short-
  circuits; otherwise a clean session would re-scan every pass.
- **Force mode.** `force=True` bypasses both the settle and revision
  checks — used by `scan --force` and its API mirror.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..findings import (
    SESSION_SETTLE_SECONDS,
    compute_findings_revision,
    write_findings_to_db,
)
from ..findings import get_enabled_engines
from ..redaction.betterleaks import (
    BETTERLEAKS_ENGINE_ID,
    scan_session_for_betterleaks_findings,
)
from ..redaction.pii import (
    PII_ENGINE_ID,
    scan_session_for_pii_findings,
)
from ..redaction.secrets import (
    SECRETS_ENGINE_ID,
    scan_session_for_findings,
)
from ..redaction.trufflehog import (
    TRUFFLEHOG_ENGINE_ID,
    scan_session_for_trufflehog_findings,
)
from .index import read_blob

logger = logging.getLogger(__name__)


def _session_is_settled(end_time: Any, *, now: datetime | None = None) -> bool:
    """True when the session's `end_time` is past the settle threshold.

    Active sessions (NULL end_time, unparseable timestamp, or inside
    the settle window) are intentionally left alone — the Scanner will
    pick them up on a later tick.
    """
    if not end_time:
        return False
    try:
        parsed = datetime.fromisoformat(str(end_time))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - parsed).total_seconds() >= SESSION_SETTLE_SECONDS


def run_findings_pipeline(
    conn: sqlite3.Connection,
    session_id: str,
    session_blob: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    force: bool = False,
    safe_logging: bool = False,
) -> dict[str, Any]:
    """Scan one session and rebuild its findings if anything changed.

    Return keys:
    - `status`: one of `missing_session`, `active_skip`, `unchanged`, `rebuilt`.
    - `revision`: the newly-persisted revision (only when rebuilt).
    - `count`: number of findings written (only when rebuilt).

    Transaction is scoped to the rebuild path: we delete the old rows,
    write the new ones, and bump `sessions.findings_revision` inside
    one `BEGIN IMMEDIATE` so a concurrent CLI decision either lands
    before the rebuild (and gets overwritten, since revision moved) or
    after (and sees the fresh rows).
    """
    row = conn.execute(
        "SELECT findings_revision FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        # Parser surfaces sessions that upsert filters out (e.g. slash-
        # only titles). Writing findings for them would violate the FK.
        return {"status": "missing_session"}

    if not force and not _session_is_settled(session_blob.get("end_time")):
        return {"status": "active_skip"}

    new_revision = compute_findings_revision(session_blob, config=config)

    if not force and row["findings_revision"] == new_revision:
        return {"status": "unchanged"}

    user_allowlist = None
    if config is not None:
        user_allowlist = config.get("allowlist_entries")

    # Run every enabled engine and union their RawFinding lists. The
    # engine identifier is carried on each row, so write_findings_to_db
    # and the share-time apply path can keep them separate.
    enabled = set(get_enabled_engines(config))
    raw: list = []
    if SECRETS_ENGINE_ID in enabled:
        raw.extend(scan_session_for_findings(session_blob, user_allowlist=user_allowlist))
    if PII_ENGINE_ID in enabled:
        raw.extend(scan_session_for_pii_findings(session_blob, user_allowlist=user_allowlist))
    if TRUFFLEHOG_ENGINE_ID in enabled:
        try:
            raw.extend(scan_session_for_trufflehog_findings(session_blob, user_allowlist=user_allowlist))
        except Exception:
            # Fail-soft: a flaky subprocess shouldn't abort the whole
            # findings rebuild — regex + PII layers still ran.
            if safe_logging:
                logger.warning(
                    "Findings engine failure stage=trufflehog code=engine_error",
                    extra={"strict_scan_safe": True},
                )
            else:
                logger.warning("TruffleHog engine errored during scan", exc_info=True)
    if BETTERLEAKS_ENGINE_ID in enabled:
        try:
            raw.extend(scan_session_for_betterleaks_findings(session_blob, user_allowlist=user_allowlist))
        except Exception:
            # Same fail-soft posture as the trufflehog engine above.
            if safe_logging:
                logger.warning(
                    "Findings engine failure stage=betterleaks code=engine_error",
                    extra={"strict_scan_safe": True},
                )
            else:
                logger.warning("Betterleaks engine errored during scan", exc_info=True)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM findings WHERE session_id = ?", (session_id,))
        count = write_findings_to_db(conn, session_id, raw, revision=new_revision)
        conn.execute(
            "UPDATE sessions SET findings_revision = ? WHERE session_id = ?",
            (new_revision, session_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"status": "rebuilt", "revision": new_revision, "count": count}


def drain_findings_backfill(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any] | None = None,
    progress_every: int = 10,
    safe_logging: bool = False,
) -> dict[str, int]:
    """Process rows flagged with `findings_backfill_needed=1`.

    Loads each session's stored blob from disk, runs the pipeline,
    then clears the flag on success. Sessions whose blob has been
    removed off disk get the flag cleared too (nothing we can do).
    Per-row updates so a crash leaves remaining rows for the next
    scan to resume from.
    """
    flagged = conn.execute(
        "SELECT session_id FROM sessions WHERE findings_backfill_needed = 1"
    ).fetchall()
    if not flagged:
        return {"processed": 0, "missing_blob": 0, "errored": 0}

    processed = 0
    missing_blob = 0
    errored = 0
    total = len(flagged)

    for i, row in enumerate(flagged, start=1):
        session_id = row["session_id"]
        blob = read_blob(session_id, log_errors=not safe_logging)
        if blob is None:
            conn.execute(
                "UPDATE sessions SET findings_backfill_needed = 0 WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            missing_blob += 1
            continue
        try:
            run_findings_pipeline(
                conn,
                session_id,
                blob,
                config=config,
                force=True,
                safe_logging=safe_logging,
            )
            conn.execute(
                "UPDATE sessions SET findings_backfill_needed = 0 WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            processed += 1
        except Exception:  # noqa: BLE001 — drain should continue past per-row failures
            if safe_logging:
                logger.warning(
                    "Findings backfill failure stage=session code=backfill_failed",
                    extra={"strict_scan_safe": True},
                )
            else:
                logger.exception("Findings backfill failed for %s", session_id)
            errored += 1

        if i % progress_every == 0:
            logger.info(
                "Findings backfill progress: %d/%d",
                i,
                total,
                extra={"strict_scan_safe": True} if safe_logging else None,
            )

    return {"processed": processed, "missing_blob": missing_blob, "errored": errored}
