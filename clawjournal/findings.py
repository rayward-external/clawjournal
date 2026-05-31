"""DB-backed findings substrate.

Deterministic redaction engines emit `RawFinding` records that the Scanner
hashes and persists as `Finding` rows. The DB stores salted hashes only —
plaintext stays in the session blob on disk. Entity decisions
(accepted/ignored) are keyed on `(session_id, engine, entity_type,
entity_hash)` so one choice covers every occurrence.

Module contents:
- `RawFinding` / `Finding` dataclasses.
- `ENGINE_VERSION`, `SESSION_SETTLE_SECONDS`, and `get_enabled_engines`
  (all participate in `compute_findings_revision`).
- `hash_entity`, `compute_finding_id`, `compute_findings_revision`.
- `write_findings_to_db`, `load_findings_from_db`, `set_finding_status`,
  `dedupe_findings_by_entity`, `derive_preview`.
- `allowlist_list` / `allowlist_add` / `allowlist_remove` — the DB-backed
  allowlist is retroactive: adding an entry flips matching `open`
  findings to `ignored` in the same transaction; removing it reassigns
  or reverts based on whether any other allowlist row still matches.
- Lifted file-based `PIIFinding` utilities (`load_findings`,
  `write_findings`, `merge_findings`, `apply_findings_to_text`,
  `apply_findings_to_session`, `normalize_finding`,
  `replacement_for_type`, `PLACEHOLDER_BY_TYPE`, `ALLOWED_ENTITY_TYPES`)
  preserved for the legacy `pii-review`/`pii-apply` flow.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from .paths import ensure_hash_salt

ENGINE_VERSION = 2  # round-4 bump: A2 added stripe_key, stripe_webhook_secret,
                    # and bearer_generic patterns + tightened the JWT bearer
                    # regex (`\b` prefix, case-insensitive keyword). Old
                    # findings rows for sessions scanned with ENGINE_VERSION=1
                    # missed these, so revision drift from this constant
                    # forces a re-scan via `compute_findings_revision` next
                    # time the session settles. `apply_findings_to_blob`
                    # already re-scans live at share time, so the bump is
                    # about the user-facing review UI showing accurate
                    # findings counts on pre-A2 sessions.
SESSION_SETTLE_SECONDS = 120
REVISION_FORMAT = "v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def get_enabled_engines(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Return the sorted tuple of deterministic engine IDs enabled in config.

    Participates in `findings_revision` so toggling an engine in config
    automatically triggers a rescan. Both `regex_pii` and `regex_secrets`
    are wired through the findings pipeline + share-time apply path.
    """
    default = ("regex_pii", "regex_secrets", "trufflehog")
    if not config:
        return default
    engines = config.get("enabled_findings_engines")
    if not isinstance(engines, (list, tuple)):
        return default
    filtered = tuple(sorted(str(name) for name in engines if isinstance(name, str)))
    return filtered or default


# ---------------------------------------------------------------------------
# Salt handling
# ---------------------------------------------------------------------------

_salt_cache: dict[str, bytes] = {}


def _install_dir() -> Path:
    """Resolve the current install directory from `INDEX_DB`.

    Imported lazily so tests that monkeypatch `INDEX_DB` after this
    module loads still see the updated path at call time.
    """
    from .workbench.index import INDEX_DB  # noqa: PLC0415 — lazy on purpose
    return Path(str(INDEX_DB)).parent


def _get_salt() -> bytes:
    install_dir = _install_dir()
    key = str(install_dir)
    if key not in _salt_cache:
        _salt_cache[key] = ensure_hash_salt(install_dir)
    return _salt_cache[key]


def reset_salt_cache() -> None:
    """Clear the per-install-dir salt cache. For tests only."""
    _salt_cache.clear()


def hash_entity(text: str) -> str:
    """Return `sha256(install_salt || text.encode('utf-8'))` as hex."""
    digest = hashlib.sha256()
    digest.update(_get_salt())
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Finding records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RawFinding:
    """What a deterministic engine emits before persistence.

    Carries `entity_text` transiently so the Scanner can hash it at
    write time. `write_findings_to_db` never persists `entity_text`;
    it computes `entity_hash` and discards the plaintext.
    """
    engine: str
    rule: str
    entity_type: str
    entity_text: str
    field: str
    offset: int
    length: int
    confidence: float
    message_index: int | None = None
    tool_field: str | None = None


@dataclass(frozen=True)
class Finding:
    """A persisted finding row. No plaintext — `entity_hash` only."""
    finding_id: str
    session_id: str
    engine: str
    rule: str
    entity_type: str
    entity_hash: str
    entity_length: int
    field: str
    message_index: int | None
    tool_field: str | None
    offset: int
    length: int
    confidence: float
    status: str
    decided_by: str | None
    decision_source_id: str | None
    decided_at: str | None
    decision_reason: str | None
    revision: str
    created_at: str


# ---------------------------------------------------------------------------
# Deterministic identifiers + revision
# ---------------------------------------------------------------------------

def compute_finding_id(
    *,
    session_id: str,
    revision: str,
    engine: str,
    rule: str,
    field: str,
    message_index: int | None,
    tool_field: str | None,
    offset: int,
    length: int,
) -> str:
    """Deterministic 32-hex ID. Byte-stable across re-scans of unchanged input."""
    payload = "|".join([
        session_id,
        revision,
        engine,
        rule or "",
        field,
        "" if message_index is None else str(message_index),
        tool_field or "",
        str(offset),
        str(length),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_findings_revision(
    session: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> str:
    """Return `v1:<sha256-hex>` over engine + config + session content.

    Inputs pinned per spec: ENGINE_VERSION, ENABLED_ENGINES,
    config.allowlist_entries (canonical JSON), display_title, project,
    git_branch, then per-message role/content/thinking, then per-tool
    tool/input/output. Any change flips the revision and forces rebuild.
    """
    enabled = get_enabled_engines(config)
    parts: list[str] = [
        f"engine_version={ENGINE_VERSION}",
        f"enabled_engines={','.join(enabled)}",
        f"allowlist_entries={_canonical_json((config or {}).get('allowlist_entries', []))}",
        f"display_title={session.get('display_title') or ''}",
        f"project={session.get('project') or ''}",
        f"git_branch={session.get('git_branch') or ''}",
    ]
    # Fold the TruffleHog binary fingerprint into the revision so
    # installing the binary (or upgrading it) automatically
    # invalidates every session's cached findings — a session scanned
    # when trufflehog was missing or at version X must re-scan once
    # the presence/version flips.
    if "trufflehog" in enabled:
        try:
            from .redaction.trufflehog import engine_fingerprint  # noqa: PLC0415 — lazy

            parts.append(f"trufflehog_fp={engine_fingerprint()}")
        except Exception:  # noqa: BLE001 — revision computation must never raise
            parts.append("trufflehog_fp=error")

    messages = session.get("messages") or []
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            parts.append(f"role={msg.get('role') or ''}")
            parts.append(f"content={msg.get('content') or ''}")
            parts.append(f"thinking={msg.get('thinking') or ''}")
            for tool_use in msg.get("tool_uses") or []:
                if not isinstance(tool_use, dict):
                    continue
                parts.append(f"tool={tool_use.get('tool') or ''}")
                parts.append(f"input={_canonical_json(tool_use.get('input'))}")
                parts.append(f"output={_canonical_json(tool_use.get('output'))}")

    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"{REVISION_FORMAT}:{digest}"


# ---------------------------------------------------------------------------
# DB I/O
# ---------------------------------------------------------------------------

def _row_to_finding(row: sqlite3.Row) -> Finding:
    return Finding(
        finding_id=row["finding_id"],
        session_id=row["session_id"],
        engine=row["engine"],
        rule=row["rule"] or "",
        entity_type=row["entity_type"] or "",
        entity_hash=row["entity_hash"],
        entity_length=row["entity_length"] or 0,
        field=row["field"],
        message_index=row["message_index"],
        tool_field=row["tool_field"],
        offset=row["offset"],
        length=row["length"],
        confidence=float(row["confidence"]) if row["confidence"] is not None else 0.0,
        status=row["status"] or "open",
        decided_by=row["decided_by"],
        decision_source_id=row["decision_source_id"],
        decided_at=row["decided_at"],
        decision_reason=row["decision_reason"],
        revision=row["revision"],
        created_at=row["created_at"],
    )


def _lookup_allowlist_match(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_hash: str,
) -> sqlite3.Row | None:
    """Pick the most specific still-applicable allowlist row for a hash.

    Prefers a typed match over a NULL-type (any-type) match; within a
    tier, oldest `added_at` wins for deterministic tie-breaking.
    """
    if entity_type:
        typed = conn.execute(
            "SELECT allowlist_id, reason FROM findings_allowlist "
            "WHERE entity_hash = ? AND entity_type = ? "
            "ORDER BY added_at ASC LIMIT 1",
            (entity_hash, entity_type),
        ).fetchone()
        if typed is not None:
            return typed
    return conn.execute(
        "SELECT allowlist_id, reason FROM findings_allowlist "
        "WHERE entity_hash = ? AND entity_type IS NULL "
        "ORDER BY added_at ASC LIMIT 1",
        (entity_hash, ),
    ).fetchone()


def write_findings_to_db(
    conn: sqlite3.Connection,
    session_id: str,
    raw: Iterable[RawFinding],
    *,
    revision: str,
) -> int:
    """Persist raw findings, applying allowlist auto-ignore on insert.

    Never stores plaintext: `entity_hash` is derived here and
    `RawFinding.entity_text` is discarded after hashing. Rows matched
    by `findings_allowlist` land with `status='ignored'`,
    `decided_by='allowlist'`, `decision_source_id=<allowlist_id>`.
    Non-matching entries are `status='open'`, `decided_by='auto'`.
    Caller provides `revision` (should match `sessions.findings_revision`
    after the enclosing rebuild commits).
    """
    now = _now_iso()
    count = 0
    for item in raw:
        entity_hash = hash_entity(item.entity_text)
        entity_length = len(item.entity_text)
        finding_id = compute_finding_id(
            session_id=session_id,
            revision=revision,
            engine=item.engine,
            rule=item.rule,
            field=item.field,
            message_index=item.message_index,
            tool_field=item.tool_field,
            offset=item.offset,
            length=item.length,
        )
        allow_row = _lookup_allowlist_match(conn, item.entity_type, entity_hash)
        if allow_row is not None:
            status = "ignored"
            decided_by: str | None = "allowlist"
            decision_source_id: str | None = allow_row["allowlist_id"]
            decided_at: str | None = now
            decision_reason: str | None = allow_row["reason"]
        else:
            status = "open"
            decided_by = "auto"
            decision_source_id = None
            decided_at = None
            decision_reason = None

        conn.execute(
            "INSERT OR REPLACE INTO findings ("
            "  finding_id, session_id, engine, rule, entity_type, "
            "  entity_hash, entity_length, field, message_index, tool_field, "
            "  offset, length, confidence, status, decided_by, "
            "  decision_source_id, decided_at, decision_reason, revision, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                finding_id, session_id, item.engine, item.rule, item.entity_type,
                entity_hash, entity_length, item.field, item.message_index, item.tool_field,
                item.offset, item.length, item.confidence, status, decided_by,
                decision_source_id, decided_at, decision_reason, revision, now,
            ),
        )
        count += 1
    return count


def load_findings_from_db(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    status_filter: set[str] | None = None,
) -> list[Finding]:
    query = "SELECT * FROM findings WHERE session_id = ?"
    params: list[Any] = [session_id]
    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        query += f" AND status IN ({placeholders})"
        params.extend(sorted(status_filter))
    query += " ORDER BY confidence DESC, message_index ASC, offset ASC"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_finding(r) for r in rows]


def set_finding_status(
    conn: sqlite3.Connection,
    finding_ids: Iterable[str],
    status: str,
    *,
    decided_by: str = "user",
    reason: str | None = None,
    also_allowlist: bool = False,
    allowlist_label_for: dict[tuple[str, str], str] | None = None,
) -> int:
    """Bulk set status, expanding per-row IDs to their entity groups.

    Every finding in the same `(session_id, engine, entity_type,
    entity_hash)` group shares one decision — a user accepting one
    occurrence applies to every occurrence. When `also_allowlist=True`
    and `status='ignored'`, each distinct entity is inserted into
    `findings_allowlist` and the retroactive-add flow flips matching
    `open` findings in *other* sessions too.
    """
    ids = list(finding_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    group_rows = conn.execute(
        f"SELECT session_id, engine, entity_type, entity_hash "
        f"FROM findings WHERE finding_id IN ({placeholders})",
        ids,
    ).fetchall()
    groups: set[tuple[str, str, str, str]] = {
        (r["session_id"], r["engine"], r["entity_type"] or "", r["entity_hash"])
        for r in group_rows
    }
    if not groups:
        return 0

    now = _now_iso()
    total = 0
    for session_id, engine, entity_type, entity_hash in groups:
        set_params: list[Any] = [status, decided_by, now, reason]
        if entity_type == "":
            type_sql = "entity_type IS NULL"
            where_params: list[Any] = [session_id, engine, entity_hash]
        else:
            type_sql = "entity_type = ?"
            where_params = [session_id, engine, entity_type, entity_hash]
        cursor = conn.execute(
            f"UPDATE findings SET "
            f"  status = ?, decided_by = ?, decided_at = ?, decision_reason = ?, "
            f"  decision_source_id = NULL "
            f"WHERE session_id = ? AND engine = ? AND {type_sql} AND entity_hash = ?",
            set_params + where_params,
        )
        total += cursor.rowcount

    if also_allowlist and status == "ignored":
        for session_id, engine, entity_type, entity_hash in groups:
            label = None
            if allowlist_label_for is not None:
                label = allowlist_label_for.get((entity_type, entity_hash))
            allowlist_add_by_hash(
                conn,
                entity_hash=entity_hash,
                entity_type=entity_type or None,
                entity_label=label,
                reason=reason,
                added_by="user",
            )

    return total


def dedupe_findings_by_entity(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    """Group findings into one row per `(engine, entity_type, entity_hash)`.

    Returns dicts so callers (API, CLI formatter) can layer display
    fields (`occurrences`, `finding_ids`, `max_confidence`) without
    fighting the frozen dataclass. Preserves per-group representative
    fields from the highest-confidence occurrence.
    """
    groups: dict[tuple[str, str, str], list[Finding]] = {}
    for f in findings:
        key = (f.engine, f.entity_type or "", f.entity_hash)
        groups.setdefault(key, []).append(f)

    out: list[dict[str, Any]] = []
    for key, items in groups.items():
        items.sort(key=lambda x: x.confidence, reverse=True)
        rep = items[0]
        out.append({
            "engine": rep.engine,
            "rule": rep.rule,
            "entity_type": rep.entity_type,
            "entity_hash": rep.entity_hash,
            "entity_length": rep.entity_length,
            "occurrences": len(items),
            "finding_ids": [i.finding_id for i in items],
            "max_confidence": rep.confidence,
            "status": rep.status,
            "sample": {
                "field": rep.field,
                "message_index": rep.message_index,
                "tool_field": rep.tool_field,
                "offset": rep.offset,
                "length": rep.length,
            },
        })
    out.sort(key=lambda g: (-g["max_confidence"], g["engine"], g["rule"]))
    return out


# ---------------------------------------------------------------------------
# Previews (read-time; never persisted)
# ---------------------------------------------------------------------------

def _resolve_field_text(blob: dict[str, Any], finding: Finding) -> str | None:
    """Return the anonymized string the finding's offsets point into, if any."""
    field = finding.field
    if finding.message_index is None:
        value = blob.get(field)
        return value if isinstance(value, str) else None

    messages = blob.get("messages")
    if not isinstance(messages, list):
        return None
    if finding.message_index < 0 or finding.message_index >= len(messages):
        return None
    msg = messages[finding.message_index]
    if not isinstance(msg, dict):
        return None

    if field in ("content", "thinking"):
        value = msg.get(field)
        return value if isinstance(value, str) else None

    # tool_uses[<idx>].<input|output> or tool_uses[<idx>].<input|output>.<key>
    match = re.match(r"tool_uses\[(\d+)\]\.(\w+)(?:\.(.+))?$", field)
    if match:
        tool_idx = int(match.group(1))
        branch = match.group(2)
        nested_key = match.group(3)
        tool_uses = msg.get("tool_uses")
        if not isinstance(tool_uses, list) or tool_idx >= len(tool_uses):
            return None
        tool = tool_uses[tool_idx]
        if not isinstance(tool, dict):
            return None
        value = tool.get(branch)
        if nested_key:
            if isinstance(value, dict) and isinstance(value.get(nested_key), str):
                return value[nested_key]
            return None
        return value if isinstance(value, str) else None

    # Widened message model (author / invocations[...] / snippets[...] /
    # extra.*). The labels come from iter_widened_text_locations; resolve by
    # re-walking and matching the label rather than re-parsing the path.
    from .parsing.widened import iter_widened_text_locations  # noqa: PLC0415 — lazy

    for text, label in iter_widened_text_locations(msg):
        if label == field:
            return text
    return None


def derive_preview(
    blob: dict[str, Any],
    finding: Finding,
    *,
    context_chars: int = 60,
) -> dict[str, str]:
    """Return `{before, after, match_placeholder}` derived from the blob.

    The matched bytes are replaced with `[...]` before returning, so
    API responses never carry raw secret text. Offsets are interpreted
    as Python string code-point indices into the anonymized field
    content (see Decision 22). If the field can't be resolved or the
    offsets fall outside it, returns empty strings — callers render
    the finding without a preview rather than fail.
    """
    text = _resolve_field_text(blob, finding)
    if text is None:
        return {"before": "", "after": "", "match_placeholder": "[...]"}
    start = max(0, finding.offset)
    end = min(len(text), start + max(0, finding.length))
    if start > len(text):
        return {"before": "", "after": "", "match_placeholder": "[...]"}
    before = text[max(0, start - context_chars):start]
    after = text[end:end + context_chars]
    return {"before": before, "after": after, "match_placeholder": "[...]"}


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

class AllowlistEntry(TypedDict):
    allowlist_id: str
    entity_type: str | None
    entity_label: str | None
    scope: str
    reason: str | None
    added_by: str
    added_at: str


def allowlist_list(conn: sqlite3.Connection) -> list[AllowlistEntry]:
    rows = conn.execute(
        "SELECT allowlist_id, entity_type, entity_label, scope, reason, added_by, added_at "
        "FROM findings_allowlist ORDER BY added_at DESC"
    ).fetchall()
    return [
        AllowlistEntry(
            allowlist_id=row["allowlist_id"],
            entity_type=row["entity_type"],
            entity_label=row["entity_label"],
            scope=row["scope"],
            reason=row["reason"],
            added_by=row["added_by"],
            added_at=row["added_at"],
        )
        for row in rows
    ]


def _retroactive_apply_allowlist(
    conn: sqlite3.Connection,
    *,
    allowlist_id: str,
    entity_type: str | None,
    entity_hash: str,
    reason: str | None,
) -> tuple[int, int]:
    """Flip matching `open` findings to `ignored` via this allowlist entry.

    Returns `(rows_updated, distinct_sessions)`. Only rows with
    `status='open'` are touched — user-authored decisions are left
    alone. A NULL `entity_type` on the allowlist matches any type;
    a typed allowlist only matches the same type.
    """
    now = _now_iso()
    if entity_type is None:
        where = "entity_hash = ? AND status = 'open'"
        params: list[Any] = [entity_hash]
    else:
        where = "entity_hash = ? AND entity_type = ? AND status = 'open'"
        params = [entity_hash, entity_type]

    distinct = conn.execute(
        f"SELECT COUNT(DISTINCT session_id) AS n FROM findings WHERE {where}",
        params,
    ).fetchone()["n"]

    cursor = conn.execute(
        f"UPDATE findings SET "
        f"  status = 'ignored', decided_by = 'allowlist', "
        f"  decision_source_id = ?, decided_at = ?, decision_reason = ? "
        f"WHERE {where}",
        [allowlist_id, now, reason, *params],
    )
    return cursor.rowcount, distinct


def allowlist_add_by_hash(
    conn: sqlite3.Connection,
    *,
    entity_hash: str,
    entity_type: str | None,
    entity_label: str | None,
    reason: str | None,
    added_by: str,
) -> tuple[AllowlistEntry, int, int]:
    """Lower-level add: caller supplies the pre-computed hash.

    Used by `set_finding_status(..., also_allowlist=True)` where the
    hash is already known from the finding row.
    """
    # Idempotent: if an equivalent entry exists (same type+hash+scope),
    # return it without a second insert or retroactive pass.
    existing = _lookup_allowlist_match(conn, entity_type or "", entity_hash)
    if existing is not None:
        row = conn.execute(
            "SELECT * FROM findings_allowlist WHERE allowlist_id = ?",
            (existing["allowlist_id"],),
        ).fetchone()
        return (
            AllowlistEntry(
                allowlist_id=row["allowlist_id"],
                entity_type=row["entity_type"],
                entity_label=row["entity_label"],
                scope=row["scope"],
                reason=row["reason"],
                added_by=row["added_by"],
                added_at=row["added_at"],
            ),
            0,
            0,
        )

    allowlist_id = str(uuid.uuid4())
    now = _now_iso()
    conn.execute(
        "INSERT INTO findings_allowlist ("
        "  allowlist_id, entity_type, entity_hash, entity_label, "
        "  scope, reason, added_by, added_at"
        ") VALUES (?, ?, ?, ?, 'global', ?, ?, ?)",
        (allowlist_id, entity_type, entity_hash, entity_label, reason, added_by, now),
    )
    updates, sessions = _retroactive_apply_allowlist(
        conn,
        allowlist_id=allowlist_id,
        entity_type=entity_type,
        entity_hash=entity_hash,
        reason=reason,
    )
    entry = AllowlistEntry(
        allowlist_id=allowlist_id,
        entity_type=entity_type,
        entity_label=entity_label,
        scope="global",
        reason=reason,
        added_by=added_by,
        added_at=now,
    )
    return entry, updates, sessions


def allowlist_add(
    conn: sqlite3.Connection,
    *,
    entity_text: str,
    entity_type: str | None = None,
    entity_label: str | None = None,
    reason: str | None = None,
    added_by: str = "user",
) -> tuple[AllowlistEntry, int, int]:
    """CLI/API entry point: accepts plaintext, hashes locally, discards it."""
    entity_hash = hash_entity(entity_text)
    return allowlist_add_by_hash(
        conn,
        entity_hash=entity_hash,
        entity_type=entity_type,
        entity_label=entity_label,
        reason=reason,
        added_by=added_by,
    )


def allowlist_remove(
    conn: sqlite3.Connection,
    allowlist_id: str,
) -> tuple[bool, int, int]:
    """Remove an allowlist entry with symmetric retroactive revert.

    Returns `(removed, reverted, reassigned)`. Findings stamped with
    this allowlist's `decision_source_id` are either reassigned to
    another still-matching allowlist entry (keeping `status='ignored'`)
    or flipped back to `status='open'` when no remaining match exists.
    User-authored decisions (`decided_by='user'`) are not touched.
    """
    entry = conn.execute(
        "SELECT entity_type, entity_hash FROM findings_allowlist WHERE allowlist_id = ?",
        (allowlist_id,),
    ).fetchone()
    if entry is None:
        return False, 0, 0
    entity_type = entry["entity_type"]
    entity_hash = entry["entity_hash"]

    affected = conn.execute(
        "SELECT finding_id, entity_type, entity_hash FROM findings "
        "WHERE decision_source_id = ? AND decided_by = 'allowlist'",
        (allowlist_id,),
    ).fetchall()

    conn.execute(
        "DELETE FROM findings_allowlist WHERE allowlist_id = ?",
        (allowlist_id,),
    )

    now = _now_iso()
    reverted = 0
    reassigned = 0
    for row in affected:
        # After deletion, find any remaining allowlist row that still
        # matches this finding's (entity_hash, entity_type). Prefer
        # typed match over any-type, per _lookup_allowlist_match.
        replacement = _lookup_allowlist_match(conn, row["entity_type"] or "", row["entity_hash"])
        if replacement is not None:
            conn.execute(
                "UPDATE findings SET "
                "  decision_source_id = ?, decision_reason = ?, decided_at = ? "
                "WHERE finding_id = ?",
                (replacement["allowlist_id"], replacement["reason"], now, row["finding_id"]),
            )
            reassigned += 1
        else:
            conn.execute(
                "UPDATE findings SET "
                "  status = 'open', decided_by = 'auto', "
                "  decision_source_id = NULL, decided_at = NULL, decision_reason = NULL "
                "WHERE finding_id = ?",
                (row["finding_id"],),
            )
            reverted += 1
    return True, reverted, reassigned


def allowlist_remove_by_text(
    conn: sqlite3.Connection,
    entity_text: str,
    *,
    entity_type: str | None = None,
) -> list[tuple[str, int, int]]:
    """Convenience path: hash plaintext, remove all matching entries."""
    entity_hash = hash_entity(entity_text)
    where = "entity_hash = ?"
    params: list[Any] = [entity_hash]
    if entity_type is not None:
        where += " AND entity_type = ?"
        params.append(entity_type)
    rows = conn.execute(
        f"SELECT allowlist_id FROM findings_allowlist WHERE {where}",
        params,
    ).fetchall()
    out: list[tuple[str, int, int]] = []
    for row in rows:
        removed, reverted, reassigned = allowlist_remove(conn, row["allowlist_id"])
        if removed:
            out.append((row["allowlist_id"], reverted, reassigned))
    return out


# ===========================================================================
# Legacy file-based PII substrate (lifted from clawjournal/redaction/pii.py)
# ===========================================================================
#
# These helpers operate on exported JSONL + sidecar findings.json and are
# the back-compat surface for `pii-review` / `pii-apply`. They intentionally
# carry plaintext `entity_text` because they predate the no-plaintext
# invariants and target a different threat model (a user-authored review
# workflow against an already-exported bundle on disk). Do not use these
# for DB-backed flows — use the salted-hash helpers above instead.

class PIIFinding(TypedDict, total=False):
    session_id: str
    message_index: int
    field: str
    entity_text: str
    entity_type: str
    confidence: float
    reason: str
    replacement: str
    source: str


PLACEHOLDER_BY_TYPE: dict[str, str] = {
    "person_name": "[REDACTED_NAME]",
    "email": "[REDACTED_EMAIL]",
    "phone": "[REDACTED_PHONE]",
    "username": "[REDACTED_USERNAME]",
    "user_id": "[REDACTED_USER_ID]",
    "org_name": "[REDACTED_ORG]",
    "project_name": "[REDACTED_PROJECT]",
    "private_url": "[REDACTED_URL]",
    "domain": "[REDACTED_DOMAIN]",
    "address": "[REDACTED_ADDRESS]",
    "location": "[REDACTED_LOCATION]",
    "bot_name": "[REDACTED_BOT]",
    "device_id": "[REDACTED_DEVICE_ID]",
    "path": "[REDACTED_PATH]",
    "custom_sensitive": "[REDACTED]",
}

ALLOWED_ENTITY_TYPES = set(PLACEHOLDER_BY_TYPE) | {"custom_sensitive"}


def replacement_for_type(entity_type: str) -> str:
    return PLACEHOLDER_BY_TYPE.get(entity_type, "[REDACTED]")


def normalize_finding(finding: dict[str, Any]) -> PIIFinding:
    entity_type = str(finding.get("entity_type") or "custom_sensitive")
    entity_text = str(finding.get("entity_text") or "")
    replacement = str(finding.get("replacement") or replacement_for_type(entity_type))
    confidence = finding.get("confidence", 1.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 1.0
    return PIIFinding(
        session_id=str(finding.get("session_id") or ""),
        message_index=int(finding.get("message_index") or 0),
        field=str(finding.get("field") or "content"),
        entity_text=entity_text,
        entity_type=entity_type,
        confidence=max(0.0, min(1.0, confidence)),
        reason=str(finding.get("reason") or ""),
        replacement=replacement,
        source=str(finding.get("source") or "rule"),
    )


def load_findings(path: Path) -> list[PIIFinding]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "findings" in data:
        raw = data["findings"]
    else:
        raw = data
    if not isinstance(raw, list):
        raise ValueError("Findings file must contain a list or an object with a 'findings' list.")
    return [normalize_finding(item) for item in raw]


def write_findings(path: Path, findings: list[PIIFinding], meta: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"findings": findings}
    if meta:
        payload.update(meta)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def merge_findings(findings: list[PIIFinding], min_confidence: float = 0.0) -> list[PIIFinding]:
    filtered = [f for f in findings if f.get("entity_text") and float(f.get("confidence", 0.0)) >= min_confidence]
    grouped: dict[tuple[str, int, str], list[PIIFinding]] = {}
    for finding in filtered:
        key = (finding.get("session_id", ""), int(finding.get("message_index", 0)), finding.get("field", "content"))
        grouped.setdefault(key, []).append(finding)

    merged: list[PIIFinding] = []
    for items in grouped.values():
        items.sort(key=lambda f: (-len(f.get("entity_text", "")), -float(f.get("confidence", 0.0)), f.get("entity_type", "")))
        chosen: list[PIIFinding] = []
        for item in items:
            text = item.get("entity_text", "")
            text_lower = text.lower()
            if any(text_lower == existing.get("entity_text", "").lower() for existing in chosen):
                continue
            if any(text_lower in existing.get("entity_text", "").lower() for existing in chosen):
                continue
            chosen.append(item)
        merged.extend(chosen)
    return sorted(merged, key=lambda f: (f.get("session_id", ""), int(f.get("message_index", 0)), f.get("field", "content"), -len(f.get("entity_text", ""))))


def apply_findings_to_text(text: str, findings: list[PIIFinding]) -> tuple[str, int]:
    if not text or not findings:
        return text, 0
    ordered = sorted(
        [f for f in findings if f.get("entity_text")],
        key=lambda f: (-len(f.get("entity_text", "")), -float(f.get("confidence", 0.0))),
    )
    count = 0
    result = text
    for finding in ordered:
        target = finding.get("entity_text", "")
        replacement = finding.get("replacement") or replacement_for_type(str(finding.get("entity_type") or "custom_sensitive"))
        if len(target) < 3:
            continue
        escaped = re.escape(target)
        pattern = re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
        result, n = pattern.subn(replacement, result)
        count += n
    return result, count


def apply_findings_to_session(
    session: dict[str, Any],
    findings: list[PIIFinding],
    min_confidence: float = 0.0,
) -> tuple[dict[str, Any], int]:
    total = 0
    session_id = str(session.get("session_id") or "")

    session_findings = [
        f for f in merge_findings(findings, min_confidence=min_confidence)
        if f.get("session_id") == session_id
    ]
    if not session_findings:
        return session, 0

    for meta_field in (
        "project", "git_branch", "display_title",
        "ai_learning_summary", "ai_score_reason", "ai_display_title",
        "ai_summary",
    ):
        value = session.get(meta_field)
        if isinstance(value, str):
            new_value, n = apply_findings_to_text(value, session_findings)
            session[meta_field] = new_value
            total += n

    raw_detail = session.get("ai_scoring_detail")
    if isinstance(raw_detail, str) and raw_detail:
        try:
            detail = json.loads(raw_detail)
        except json.JSONDecodeError:
            new_value, n = apply_findings_to_text(raw_detail, session_findings)
            session["ai_scoring_detail"] = new_value
            total += n
        else:
            redacted_detail, n = _apply_findings_to_value(detail, session_findings)
            session["ai_scoring_detail"] = json.dumps(redacted_detail)
            total += n

    messages = session.get("messages", [])
    if not isinstance(messages, list):
        return session, total

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for field in ("content", "thinking"):
            value = msg.get(field)
            if isinstance(value, str):
                new_value, n = apply_findings_to_text(value, session_findings)
                msg[field] = new_value
                total += n
        for tool_use in msg.get("tool_uses", []):
            if not isinstance(tool_use, dict):
                continue
            for branch in ("input", "output"):
                value = tool_use.get(branch)
                if isinstance(value, dict):
                    for key in list(value.keys()):
                        if isinstance(value[key], str):
                            new_value, n = apply_findings_to_text(value[key], session_findings)
                            value[key] = new_value
                            total += n
                elif isinstance(value, str):
                    new_value, n = apply_findings_to_text(value, session_findings)
                    tool_use[branch] = new_value
                    total += n
    return session, total


def _apply_findings_to_value(
    value: Any,
    findings: list[PIIFinding],
) -> tuple[Any, int]:
    if isinstance(value, str):
        return apply_findings_to_text(value, findings)
    if isinstance(value, list):
        total = 0
        out: list[Any] = []
        for item in value:
            redacted, n = _apply_findings_to_value(item, findings)
            out.append(redacted)
            total += n
        return out, total
    if isinstance(value, dict):
        total = 0
        out: dict[str, Any] = {}
        for key, item in value.items():
            redacted, n = _apply_findings_to_value(item, findings)
            out[key] = redacted
            total += n
        return out, total
    return value, 0
