"""Widened normalized-message shape (NormalizedMessage / NormalizedInvocation / NormalizedSnippet).

Backward-compatible extension to the message dict produced by every
``parse_*_session`` function. Existing parsers continue to emit
``role`` / ``content`` / ``thinking`` / ``tool_uses``; this module adds
four optional fields that future connectors (phase-2 D1-D8) can populate:

- ``invocations``: list of structured tool/skill invocations with both
  the agent-emitted ``raw_name`` and the canonical ``name``. Distinct
  from the legacy ``tool_uses`` list, which the existing connectors
  keep emitting.
- ``snippets``: list of structured code-region references.
- ``extra``: dict preserving raw agent-specific JSON the normalizer
  cannot map onto the canonical shape.
- ``author``: string distinct from ``role`` (sub-agent / skill wrapper
  identity).

All four fields are subject to the same redaction passes as ``content``
— see the corresponding plumbing in ``clawjournal/redaction/secrets.py``
and ``clawjournal/redaction/pii.py``. The "looks like metadata" trap
(invariant 8 in phase-2/01) is the reason ``extra`` ships through
``_apply_to_value`` recursively rather than via a metadata fast path.

Reference: ``franken_agent_detection/src/types.rs`` ``NormalizedMessage``.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

INVOCATION_ARGUMENTS_CAP_BYTES = 64 * 1024


def truncate_invocation_arguments(arguments: Any) -> tuple[Any, bool]:
    """Cap an invocation's ``arguments`` payload at 64 KiB serialized.

    Returns ``(value, was_truncated)``. When the JSON-serialized form
    exceeds the cap, the value is replaced with the UTF-8-truncated
    serialization (a string) and ``was_truncated=True`` is returned so
    the caller can attach the ``arguments_truncated: true`` marker to
    the invocation dict.

    String inputs are measured against the cap directly; large strings
    are truncated by character without re-encoding through ``json``.
    Other types are JSON-serialized to measure size; oversized values
    are returned as a string holding the truncated JSON prefix (so the
    reader sees the leading bytes of the original structure).

    The ``ensure_ascii=False`` flag matches how the rest of the
    workbench writes blobs.
    """
    if arguments is None:
        return None, False

    if isinstance(arguments, str):
        encoded = arguments.encode("utf-8")
        if len(encoded) <= INVOCATION_ARGUMENTS_CAP_BYTES:
            return arguments, False
        truncated = encoded[:INVOCATION_ARGUMENTS_CAP_BYTES].decode("utf-8", errors="ignore")
        return truncated, True

    serialized = json.dumps(arguments, default=str, ensure_ascii=False)
    encoded = serialized.encode("utf-8")
    if len(encoded) <= INVOCATION_ARGUMENTS_CAP_BYTES:
        return arguments, False
    truncated = encoded[:INVOCATION_ARGUMENTS_CAP_BYTES].decode("utf-8", errors="ignore")
    return truncated, True


def build_invocation(
    *,
    name: str,
    raw_name: str | None = None,
    arguments: Any = None,
    **extras: Any,
) -> dict[str, Any]:
    """Construct a NormalizedInvocation dict with the 64 KiB arguments cap applied.

    ``raw_name`` is only stored when it differs from ``name`` (Amp's
    ``skill('foo')`` wrapper is the prototype; for connectors where the
    agent-emitted name already matches the canonical one, omitting
    ``raw_name`` keeps blobs compact).

    ``extras`` lets callers attach connector-specific fields (e.g.
    Pi-Agent's ``thinking_level``) without broadening this signature.
    """
    capped, truncated = truncate_invocation_arguments(arguments)
    inv: dict[str, Any] = {"name": name}
    if raw_name is not None and raw_name != name:
        inv["raw_name"] = raw_name
    inv["arguments"] = capped
    if truncated:
        inv["arguments_truncated"] = True
    for k, v in extras.items():
        inv[k] = v
    return inv


def build_snippet(
    *,
    path: str | None = None,
    content: str = "",
    lang: str | None = None,
    **extras: Any,
) -> dict[str, Any]:
    """Construct a NormalizedSnippet dict.

    ``path`` is the filesystem path the snippet was extracted from
    (display-only — never feed back to the filesystem). ``content`` is
    the snippet text; ``lang`` is an optional language hint. Connector-
    specific extras (line ranges, blob hashes) ride along untouched.
    """
    snip: dict[str, Any] = {"content": content}
    if path is not None:
        snip["path"] = path
    if lang is not None:
        snip["lang"] = lang
    for k, v in extras.items():
        snip[k] = v
    return snip


def iter_widened_text_locations(msg: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield ``(text, field_label)`` pairs for every string in a message's widened fields.

    Walks ``invocations``, ``snippets``, ``extra``, and ``author`` —
    callers handle the legacy ``content`` / ``thinking`` / ``tool_uses``
    fields themselves. Field labels are stable strings that locate the
    leaf inside the message for findings storage:

    - ``author``
    - ``invocations[<idx>].<key>`` and recursively ``...<key>.<subkey>``
    - ``snippets[<idx>].<key>``
    - ``extra.<key>`` and recursively ``extra.<key>.<subkey>``

    The traversal is fully recursive — nested secrets in
    ``extra.foo.bar.token`` are yielded with field
    ``extra.foo.bar.token``. This matches the recursive writeback in
    ``_apply_to_value`` so scan and apply stay in sync.
    """
    author = msg.get("author")
    if isinstance(author, str) and author:
        yield author, "author"

    invocations = msg.get("invocations")
    if isinstance(invocations, list):
        for idx, inv in enumerate(invocations):
            if isinstance(inv, dict):
                yield from _walk_strings(inv, f"invocations[{idx}]")

    snippets = msg.get("snippets")
    if isinstance(snippets, list):
        for idx, snip in enumerate(snippets):
            if isinstance(snip, dict):
                yield from _walk_strings(snip, f"snippets[{idx}]")

    extra = msg.get("extra")
    if isinstance(extra, dict):
        yield from _walk_strings(extra, "extra")


def _walk_strings(value: Any, prefix: str) -> Iterator[tuple[str, str]]:
    """Recursively yield ``(string, field_label)`` from a JSON-shaped value.

    Non-string scalars (ints, bools, None) are skipped. Lists yield
    their items with ``prefix[i]``; dicts yield their values with
    ``prefix.<key>``. Empty strings are skipped.
    """
    if isinstance(value, str):
        if value:
            yield value, prefix
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, f"{prefix}.{k}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk_strings(item, f"{prefix}[{i}]")
