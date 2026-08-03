"""Capability overlay loader (phase-1 plan 08).

User overlay at ``~/.clawjournal/capability_overlay.yaml`` merges over
the shipped ``CAPABILITY_MATRIX`` at first read. PyYAML is lazy-imported
here so it does not load on ``clawjournal --help`` or any non-events
CLI invocation.

The shipped wheel matrix is never edited (would be lost on
``pip install --upgrade``); the overlay is the only user-writable
surface. Refuses to remove a shipped entry or downgrade
``supported: true → false`` — those are quiet ways to disable
redaction-relevant assertions.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Mapping

from clawjournal import config as config_module
from clawjournal.events.capabilities import CAPABILITY_MATRIX
from clawjournal.events.types import EVENT_TYPES

OVERLAY_FILENAME = "capability_overlay.yaml"
OVERLAY_VERSION = 1
MAX_OVERLAY_ENTRIES = 100
SUPPORTED_OVERLAY_CLIENTS = ("claude", "codex", "openclaw")


def overlay_path() -> Path:
    return Path(config_module.CONFIG_DIR) / OVERLAY_FILENAME


_cached_matrix: dict[tuple[str, str], tuple[bool, str]] | None = None


def reset_cache() -> None:
    """Drop the cached merged matrix so the next call re-reads the overlay."""

    global _cached_matrix
    _cached_matrix = None


def effective_matrix() -> dict[tuple[str, str], tuple[bool, str]]:
    """Return ``CAPABILITY_MATRIX`` merged with the user overlay.

    First call reads the YAML overlay and caches the merged dict.
    Subsequent calls return the cache. ``reset_cache()`` clears it
    (used in tests; ``--fix`` also resets after writing).
    """

    global _cached_matrix
    if _cached_matrix is not None:
        return _cached_matrix
    base: dict[tuple[str, str], tuple[bool, str]] = dict(CAPABILITY_MATRIX)
    overlay = _read_overlay(overlay_path())
    if overlay is not None:
        for entry in overlay.get("entries", []):
            _apply_entry(base, entry)
    _cached_matrix = base
    return _cached_matrix


def _apply_entry(
    base: dict[tuple[str, str], tuple[bool, str]],
    entry: Mapping[str, Any],
) -> None:
    client = entry.get("client")
    event_type = entry.get("event_type")
    supported = entry.get("supported")
    reason = entry.get("reason", "")
    if client not in SUPPORTED_OVERLAY_CLIENTS:
        warnings.warn(
            f"capability overlay: unknown client {client!r}; skipping entry",
            stacklevel=3,
        )
        return
    if event_type not in EVENT_TYPES:
        warnings.warn(
            f"capability overlay: unknown event_type {event_type!r}; skipping entry",
            stacklevel=3,
        )
        return
    if not isinstance(supported, bool):
        warnings.warn(
            f"capability overlay: `supported` must be a bool for "
            f"{client}/{event_type}; skipping entry",
            stacklevel=3,
        )
        return
    if not isinstance(reason, str):
        warnings.warn(
            f"capability overlay: `reason` must be a string for "
            f"{client}/{event_type}; skipping entry",
            stacklevel=3,
        )
        return
    key = (client, event_type)
    shipped = base.get(key, (False, ""))
    if shipped[0] is True and supported is False:
        warnings.warn(
            f"capability overlay: refuses to downgrade shipped capability "
            f"{client}/{event_type} (supported: true → false); shipped value wins",
            stacklevel=3,
        )
        return
    base[key] = (supported, reason)


def _read_overlay(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        import yaml
    except ImportError:
        warnings.warn(
            "PyYAML not installed; capability overlay ignored",
            stacklevel=3,
        )
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.warn(
            f"capability overlay: cannot read {path}: {exc}",
            stacklevel=3,
        )
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        warnings.warn(
            f"capability overlay: malformed YAML in {path}: {exc}",
            stacklevel=3,
        )
        return None
    if data is None:
        return None
    if not isinstance(data, dict):
        warnings.warn(
            f"capability overlay: expected mapping at top level in {path}",
            stacklevel=3,
        )
        return None
    version = data.get("version")
    if not isinstance(version, int):
        warnings.warn(
            f"capability overlay: missing or invalid `version` (got {version!r})",
            stacklevel=3,
        )
        return None
    if version > OVERLAY_VERSION:
        warnings.warn(
            f"capability overlay: version {version} is newer than supported "
            f"{OVERLAY_VERSION}; ignoring overlay",
            stacklevel=3,
        )
        return None
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        warnings.warn(
            f"capability overlay: `entries` must be a list "
            f"(got {type(entries).__name__})",
            stacklevel=3,
        )
        return None
    if len(entries) > MAX_OVERLAY_ENTRIES:
        warnings.warn(
            f"capability overlay: {len(entries)} entries exceeds maximum of "
            f"{MAX_OVERLAY_ENTRIES}; ignoring overlay",
            stacklevel=3,
        )
        return None
    return data


def fix_additive_drift(
    report,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Detect additive drift in a ``DoctorReport`` and write the overlay.

    "Additive drift" = a (client, event_type) tuple observed in
    ``event_sessions`` where:
      - ``event_type`` is in ``EVENT_TYPES`` (known structural type), AND
      - the shipped matrix has ``supported: false`` for it.

    Refuses on **structural** drift (event types not in ``EVENT_TYPES``)
    — those are reported separately and require a code change.

    Returns ``{"added": [...], "skipped_structural": [...], "no_op": bool, "path": str}``.
    """

    from clawjournal.events.capabilities import CAPABILITY_MATRIX

    additive: list[dict[str, Any]] = []
    structural: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()

    for client_obs in report.clients:
        if client_obs.unknown_event_types:
            for unknown in client_obs.unknown_event_types:
                structural.append(
                    {
                        "client": client_obs.client,
                        "event_type": unknown,
                        "client_version": client_obs.client_version,
                    }
                )
        for event_type in client_obs.unsupported_event_types:
            key = (client_obs.client, event_type)
            if key in seen_keys:
                continue
            shipped = CAPABILITY_MATRIX.get(key, (False, ""))
            if shipped[0] is True:
                continue
            seen_keys.add(key)
            additive.append(
                {
                    "client": client_obs.client,
                    "event_type": event_type,
                    "supported": True,
                    "reason": (
                        f"observed in client_version {client_obs.client_version!r} "
                        f"({client_obs.sessions_count} session(s))"
                    ),
                }
            )

    target = path or overlay_path()
    if additive:
        write_overlay_entries(additive, path=target)

    return {
        "added": additive,
        "skipped_structural": structural,
        "no_op": not additive,
        "path": str(target),
    }


def write_overlay_entries(
    entries: list[dict[str, Any]],
    *,
    path: Path | None = None,
) -> Path:
    """Write or extend the overlay with new entries (used by ``--fix``).

    Reads the existing overlay (if any), merges new entries by
    ``(client, event_type)``, sorts deterministically, writes back, and
    resets the cache. Raises ``ValueError`` if the merged total would
    exceed ``MAX_OVERLAY_ENTRIES``.

    If the existing overlay file is present but unparseable (malformed
    YAML, wrong version, etc.), the previous content is preserved as
    ``<target>.bak`` before the new content is written, so a user
    mid-edit doesn't silently lose their work to ``events doctor --fix``.
    """

    target = path or overlay_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict[str, Any]] = []
    if target.exists():
        loaded = _read_overlay(target)
        if loaded:
            existing = list(loaded.get("entries", []))
        else:
            # Existing file is unparseable — back it up before we
            # overwrite. Distinguish from a clean prior overlay where
            # `loaded` is a valid (possibly empty) dict.
            backup = target.with_suffix(target.suffix + ".bak")
            try:
                backup.write_bytes(target.read_bytes())
                warnings.warn(
                    f"capability overlay at {target} was unparseable; "
                    f"previous content preserved as {backup} before write",
                    stacklevel=2,
                )
            except OSError as exc:
                warnings.warn(
                    f"capability overlay at {target} was unparseable; "
                    f"could not write backup ({exc}) — proceeding with overwrite",
                    stacklevel=2,
                )
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for record in existing:
        client = record.get("client")
        event_type = record.get("event_type")
        if client and event_type:
            by_key[(client, event_type)] = dict(record)
    for entry in entries:
        client = entry["client"]
        event_type = entry["event_type"]
        by_key[(client, event_type)] = dict(entry)
    sorted_entries = sorted(
        by_key.values(),
        key=lambda e: (e.get("client", ""), e.get("event_type", "")),
    )
    if len(sorted_entries) > MAX_OVERLAY_ENTRIES:
        raise ValueError(
            f"overlay would have {len(sorted_entries)} entries, "
            f"exceeds max {MAX_OVERLAY_ENTRIES}"
        )
    payload = {"version": OVERLAY_VERSION, "entries": sorted_entries}
    import yaml

    text = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    target.write_text(text, encoding="utf-8")
    reset_cache()
    return target
