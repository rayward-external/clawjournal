"""Additional candidate formats with offsets into the original log text.

These rules complement the legacy rules; they do not declare unmatched text
safe. Escaped separators are decoded only in a temporary scanning view.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from collections.abc import Iterator


_LOCAL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.!#$%&'*+/=?^_`{|}~-_")
_DOMAIN = re.compile(r"(?:\[(?:IPv6:)?[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+\.(?:xn--[A-Za-z0-9-]+|[A-Za-z]{2,}))", re.I)
_TOKEN_TAIL = re.compile(r"[A-Za-z0-9_-]{30,}")
_NAMED_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:telegram_bot_token|telegram_api_token|telegram_token|telegramBotToken)"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_:-]{8,})", re.I,
)
_HOST_FIELD = re.compile(
    r"(?<![A-Za-z0-9_])(?:DB_HOST|DATABASE_HOST|REDIS_HOST|PGHOST|MYSQL_HOST|INTERNAL_HOST|INTERNAL_HOSTNAME)"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9._-]*)", re.I,
)
_SSH_HOST = re.compile(r"(?<![\w-])ssh[ \t]+(?:[A-Za-z0-9_.-]+@)?([A-Za-z0-9][A-Za-z0-9.-]*)")
_HOST_CHAIN = re.compile(r"[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*", re.I)
_HOST_SUFFIX = re.compile(r"\.(?:local|internal|corp|lan|intranet|localnet)(?![A-Za-z0-9_])", re.I)
_PERSONAL_HOST = re.compile(
    r"(?<![A-Za-z0-9_])([a-z][a-z0-9]*s?-(?:macbook|imac|laptop|desktop|pc|workstation|server)-?[a-z0-9]*)(?![A-Za-z0-9_])",
    re.I,
)
_ESCAPED_SEPARATOR = re.compile(r"%([234][0aAeE])|\\u00(2[eE]|3[aA]|40)|&#(?:0*(46|58|64)|[xX]0*(2[eE]|3[aA]|40));")


def _scanning_view(text: str) -> tuple[str, list[int], list[int]]:
    parts: list[str] = []
    offsets, shifts = [0], [0]
    cursor = 0
    decoded_length = 0
    for match in _ESCAPED_SEPARATOR.finditer(text):
        raw = match.group(0)
        if raw.startswith("%"):
            codepoint = int(match.group(1), 16)
        elif match.group(2):
            codepoint = int(match.group(2), 16)
        else:
            codepoint = int(match.group(3), 10) if match.group(3) else int(match.group(4), 16)
        if chr(codepoint) not in ".:@":
            continue
        parts.extend((text[cursor:match.start()], chr(codepoint)))
        decoded_length += match.start() - cursor + 1
        cursor = match.end()
        offsets.append(decoded_length)
        shifts.append(cursor - decoded_length)
    if not parts:
        return text, offsets, shifts
    parts.append(text[cursor:])
    return "".join(parts), offsets, shifts


def _email_spans(text: str) -> Iterator[tuple[int, int]]:
    end_of_match = 0
    cursor = 0
    quote_positions: list[int] | None = None
    while (at := text.find("@", cursor)) != -1:
        cursor = at + 1
        start = at
        if start and text[start - 1] == '"':
            # Index quotes once. Repeated escaped quotes followed by @ must
            # not make each candidate walk the entire preceding text again.
            if quote_positions is None:
                quote_positions = []
                slashes = 0
                for pos, char in enumerate(text):
                    if char == '"' and slashes % 2 == 0:
                        quote_positions.append(pos)
                    slashes = slashes + 1 if char == "\\" else 0
            before_closing = bisect.bisect_left(quote_positions, at - 1) - 1
            if before_closing < 0:
                continue
            start = quote_positions[before_closing]
            if start < end_of_match:
                continue
        else:
            while start > end_of_match and (
                text[start - 1] in _LOCAL_CHARS
                or unicodedata.category(text[start - 1])[0] in "LMN"
            ):
                start -= 1
        if start == at:
            continue
        domain = _DOMAIN.match(text, at + 1)
        domain_end = domain.end() if domain else at + 1
        # Unicode local parts and international domain labels are real email
        # syntax. Do not normalize them in the exported text: offsets and the
        # original bytes must still match. Short adjacent prose can be included;
        # oversized runs are deferred by the replacement boundary check.
        cursor_right = at + 1
        while cursor_right < len(text) and (
            text[cursor_right] in ".-"
            or unicodedata.category(text[cursor_right])[0] in "LMN"
        ):
            cursor_right += 1
        raw_domain = text[at + 1:cursor_right].rstrip(".")
        labels = raw_domain.split(".")
        if len(labels) >= 2 and len(labels[-1]) >= 2 and all(
            unicodedata.category(char)[0] in "LM" for char in labels[-1]
        ):
            domain_end = max(domain_end, at + 1 + len(raw_domain))
        if domain_end > at + 1:
            end_of_match = domain_end
            cursor = end_of_match
            yield start, end_of_match


def iter_partial_email_candidates(text: str) -> Iterator[dict]:
    """Keep legacy partial-address spans, also for escaped separators."""
    from .pii import _TRUNCATED_EMAIL_PATTERN, _content_matches

    view, offsets, shifts = _scanning_view(text)
    for match in _content_matches(_TRUNCATED_EMAIL_PATTERN, view):
        start, end = match.span(1)
        start += shifts[bisect.bisect_right(offsets, start) - 1]
        end += shifts[bisect.bisect_right(offsets, end) - 1]
        yield {"type": "email", "rule": "email_truncated", "match": text[start:end],
               "start": start, "end": end, "confidence": 0.75}


def iter_format_candidates(text: str, *, context=None) -> Iterator[dict]:
    from .code_context import code_context

    view, offsets, shifts = _scanning_view(text)
    # Decoding helps detection, but must not turn encoded data into proof
    # that it is source code. Syntax evidence uses the original text/offsets.
    if context is None:
        context = code_context(text)

    def is_reference_prefix(match: re.Match, rule: str) -> bool:
        start, end = match.span(1)
        original_start = start + shifts[bisect.bisect_right(offsets, start) - 1]
        original_end = end + shifts[bisect.bisect_right(offsets, end) - 1]
        if context.is_reference(original_start, original_end):
            return True
        if start and view[start - 1] in "\"'":
            return False  # A quoted value is data, even if it looks like code.
        value = match.group(1)
        continuation = view[end:end + 1]
        expression = continuation in {"(", "["} or (
            continuation == "." and view[end + 1:end + 2].isidentifier()
        )
        expression |= rule == "host" and "." in value and "_" in value.rsplit(".", 1)[-1]
        # The existing internal-TLD rule supplies separate evidence for
        # db01.local(), and the complete numeric Telegram shape still wins.
        if expression and not _HOST_SUFFIX.search(value) and ":" not in value:
            # This match ends inside a lookup/call, not at the end of a
            # host or token value. Other complete credential/host rules still
            # scan the expression and its arguments. A rejected optional
            # candidate must not abort detection of the rest of the field.
            return True
        return False

    def candidate(start: int, end: int, rule: str, kind: str) -> dict:
        start += shifts[bisect.bisect_right(offsets, start) - 1]
        end += shifts[bisect.bisect_right(offsets, end) - 1]
        return {"type": kind, "rule": rule, "match": text[start:end],
                "start": start, "end": end, "confidence": 0.90}

    for start, end in _email_spans(view):
        yield candidate(start, end, "email_extended", "email")
    if view != text:
        yield from iter_partial_email_candidates(text)

    cursor = 0
    while (colon := view.find(":", cursor)) != -1:
        start = colon
        while start and view[start - 1].isdecimal():
            start -= 1
        cursor = colon + 1
        # Short IDs occur in Telegram's own examples. Require a real left
        # delimiter (or the documented /bot URL prefix), so an XML name such
        # as clm12345:AgencyIdentificationCodeContentType is not a new match.
        # The legacy 8+ digit rule remains unchanged.
        short_id_boundary = (
            not start or not (view[start - 1].isalnum() or view[start - 1] == "_")
            or view[max(0, start - 28):start].endswith("api.telegram.org/bot")
        )
        if colon - start >= 8 or (colon - start >= 5 and short_id_boundary):
            tail = _TOKEN_TAIL.match(view, cursor)
            if tail:
                yield candidate(start, tail.end(), "telegram_extended", "custom_sensitive")
                cursor = tail.end()

    for match in _NAMED_TOKEN.finditer(view):
        if not is_reference_prefix(match, "telegram"):
            yield candidate(*match.span(1), "telegram_named", "custom_sensitive")
    for match in _PERSONAL_HOST.finditer(view):
        yield candidate(*match.span(1), "personal_hostname", "device_id")
    suffixes = iter(_HOST_SUFFIX.finditer(view))
    suffix = next(suffixes, None)
    if suffix is not None:
        for chain in _HOST_CHAIN.finditer(view):
            if suffix is None:
                break
            while suffix is not None and suffix.start() < chain.start():
                suffix = next(suffixes, None)
            end = None
            while suffix is not None and suffix.start() < chain.end():
                if suffix.end() <= chain.end():
                    end = suffix.end()
                suffix = next(suffixes, None)
            if end is not None and not (chain.start() and view[chain.start() - 1] in _LOCAL_CHARS - {".", "-"}):
                yield candidate(chain.start(), end, "internal_tld_host", "private_url")
    for pattern in (_HOST_FIELD, _SSH_HOST):
        for match in pattern.finditer(view):
            if (match.group(1).lower() not in {"localhost", "127.0.0.1"}
                    and not (pattern is _HOST_FIELD and is_reference_prefix(match, "host"))):
                yield candidate(*match.span(1), "internal_host_context", "private_url")
