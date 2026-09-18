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
_EMAIL_FIELD = re.compile(r"(?<![\w])(?:to|from|email|e[-_]?mail|recipient|sender|cc|bcc)[\"']?\s*[:=]\s*[\"']$", re.I)
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
_SSH_PROSE_TAIL = re.compile(r'\s+(?:is|are|was|were|can|should|must|requires?|provides?|allows?)\b', re.I)
_HOST_CHAIN = re.compile(r"[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*", re.I)
_HOST_SUFFIX = re.compile(r"\.(?:local|internal|corp|lan|intranet|localnet)(?![A-Za-z0-9_])", re.I)
_PERSONAL_HOST = re.compile(
    r"(?<![A-Za-z0-9_])([a-z][a-z0-9]*s?-(?:macbook|imac|laptop|desktop|pc|workstation|server)-?[a-z0-9]*)(?![A-Za-z0-9_])",
    re.I,
)
# A device-name label cannot exceed 63 bytes (RFC 1035), so a candidate longer
# than that is a run of ordinary text or data joined to a device name. The
# scanning rules above and in pii.py are unchanged; personal_host_matches()
# reduces an oversized match to this bounded core inside the match's own
# span: at most 32 characters (33 with a plural s) before the device keyword
# and 16 after it. The rest of the run stays as ordinary text, and the other
# rules still scan it. An oversized ASCII personal_hostname candidate can
# therefore no longer reach the replacement budget in boundaries.py. That
# byte budget still applies to the four non-ASCII letters that
# case-insensitive [a-z] admits (U+0130, U+0131, U+017F, U+212A).
PERSONAL_HOST_CORE_PATTERN = (
    r"((?:(?<![A-Za-z0-9_])[a-z][a-z0-9]{0,31}|[a-z0-9]{32})s?"
    r"-(?:macbook|imac|laptop|desktop|pc|workstation|server)"
    r"(?:-?[a-z0-9]{16}|-?[a-z0-9]{0,16}(?![A-Za-z0-9_])))"
)
PERSONAL_HOST_BUDGET = 63
_PERSONAL_HOST_CORE = re.compile(PERSONAL_HOST_CORE_PATTERN, re.I)
# Every device-name match contains this; skip fields without a keyword.
_PERSONAL_HOST_HINT = re.compile(r"-(?:macbook|imac|laptop|desktop|pc|workstation|server)", re.I)


def has_personal_host_hint(text: str) -> bool:
    """Linear pre-check before the device-name scan."""
    return _PERSONAL_HOST_HINT.search(text) is not None


def personal_host_matches(pattern: re.Pattern[str], core: re.Pattern[str], text: str) -> Iterator[re.Match[str]]:
    """Yield the rule's own matches; reduce an oversized one to its core.

    `pattern` is an unchanged device-name rule and `core` is
    PERSONAL_HOST_CORE_PATTERN compiled with the same flags. A match within
    the replacement budget is returned as is. A longer match is searched
    again inside its own span, which yields the bounded core. If that search
    fails, the original match is returned and the budget check still applies.
    """
    if not has_personal_host_hint(text):
        return
    for match in pattern.finditer(text):
        if len(match.group(1)) <= PERSONAL_HOST_BUDGET:
            yield match
            continue
        bounded = core.search(text, match.start(1), match.end(1))
        yield match if bounded is None else bounded


_ESCAPED_SEPARATOR = re.compile(r"%([234][0aAeE])|\\u00(2[eE]|3[aA]|40)|&#(?:0*(46|58|64)|[xX]0*(2[eE]|3[aA]|40));")
_URL_AUTHORITY = re.compile(r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://([^\s/?#\"'`<>|]+)")


def credentialed_urls(text: str):
    """Return explicit URL userinfo, independently of email length budgets."""
    for match in _URL_AUTHORITY.finditer(text):
        authority = match.group(1)
        # A raw @ is invalid inside RFC userinfo, but occurs in connection
        # strings. Treat everything before the final separator as sensitive.
        at = authority.rfind('@')
        if at > 0 and at < len(authority) - 1:
            # A hostname followed by a list separator and an address is not
            # proof that the preceding URL contains that address as userinfo.
            # Keep punctuation in explicit user:password credentials intact.
            prefix = authority[:at]
            if ':' not in prefix and re.match(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}[,;]", prefix):
                continue
            yield match.start(1), match.start(1) + at, match.end(1)


def email_in_url_userinfo(start: int, end: int, urls: list[tuple[int, int, int]]) -> bool:
    """Look up an email-shaped URL credential without rescanning every URL."""
    i = bisect.bisect_right(urls, start, key=lambda span: span[0]) - 1
    return i >= 0 and start < urls[i][1] and end <= urls[i][2]


def _unspaced_script(char: str) -> bool:
    """Scripts whose prose commonly abuts an ASCII mailbox without spaces."""
    if char.isascii():
        return False
    name = unicodedata.name(char, "")
    return name.startswith(("CJK ", "IDEOGRAPHIC ", "HIRAGANA ", "KATAKANA",
                            "HANGUL ", "THAI ", "LAO ", "KHMER ", "MYANMAR ",
                            "HALFWIDTH KATAKANA", "HALFWIDTH HANGUL",
                            "FULLWIDTH LATIN", "FULLWIDTH DIGIT"))


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
        # In prose, a script transition is a boundary around an ASCII address.
        # Only the script immediately next to the ASCII run matters: earlier
        # prose may contain API names, dates or other ASCII text. RFC mailbox
        # delimiters retain mixed-script local parts. A JSON/string-opening
        # quote is not a mailbox delimiter; it may enclose a whole sentence.
        # Punctuation joins (e.g. ツ-test) remain part of the local part.
        delimited = text[start:start + 1] == '"' or (start > 0 and text[start - 1] == '<')
        # A complete value in an address field is an explicit SMTPUTF8
        # mailbox. Merely quoting prose cannot make its prefix a mailbox.
        if (start and text[start - 1] in "\"'"
                and _EMAIL_FIELD.search(text[max(0, start - 96):start])):
            quoted_domain = _DOMAIN.match(text, at + 1)
            if quoted_domain and text[quoted_domain.end():quoted_domain.end() + 1] == text[start - 1]:
                delimited = True
        if text[start:start + 1] != '"' and not delimited:
            ascii_start = at
            while ascii_start > start and text[ascii_start - 1].isascii():
                ascii_start -= 1
            if (start < ascii_start < at and (text[ascii_start].isalnum() or ascii_start - start > 1)
                    and _unspaced_script(text[ascii_start - 1])):
                start = ascii_start
        if not delimited:
            prefix = text[start:at]
            # RFC atext includes / & ? =. Split only with external evidence
            # of a rooted path, URL query, or an explicit assignment label.
            rooted = prefix.startswith(('/', './', '../', '~/'))
            if rooted and ('?' in prefix or '&' in prefix) and '=' in prefix:
                start += prefix.rfind('=') + 1
            elif rooted:
                start += prefix.rfind('/') + 1
            else:
                assignment = re.match(r'[A-Za-z_][A-Za-z0-9_]*=', prefix)
                query = re.search(r'[?&][A-Za-z_][A-Za-z0-9_.-]*=', prefix)
                long_prefix = len(prefix.encode('utf-8', errors='surrogatepass')) > 64
                if long_prefix and query and '?' in prefix and '&' in prefix:
                    start += prefix.rfind('=') + 1
                elif assignment and (long_prefix or re.fullmatch(r'(?:[A-Z][A-Z0-9_]*|email|mail|recipient|owner)=', assignment.group())
                                     or '/' in prefix[assignment.end():]):
                    start += assignment.end()
                # A relative path requires multiple path components or a
                # suffix/assignment hint; sales/support@example remains valid.
                path_prefix = text[start:at]
                if ('/' in path_prefix and ((long_prefix and path_prefix.count('/') >= 2)
                        or (assignment and start > at - len(prefix)) or path_prefix.startswith(('.', '~')))):
                    start += path_prefix.rfind('/') + 1
        if start == at:
            continue
        domain = _DOMAIN.match(text, at + 1)
        domain_end = domain.end() if domain else at + 1
        # Unicode local parts and international domain labels remain supported.
        # Do not extend an already complete ASCII domain into unspaced prose.
        # Delimited international mailboxes keep their complete original span.
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
            prose_tail = (domain is not None and not delimited
                          and domain_end < cursor_right
                          and _unspaced_script(text[domain_end]))
            if not prose_tail:
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
        if expression and ":" not in value and (
                not _HOST_SUFFIX.search(value) or rule == "host"):
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
    for match in personal_host_matches(_PERSONAL_HOST, _PERSONAL_HOST_CORE, view):
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
            if pattern is _SSH_HOST:
                # A bare English word after prose "ssh" is not host evidence.
                # Keep command-position destinations and explicit user@host /
                # dotted destinations; fields such as DB_HOST cover bare hosts.
                before = view[view.rfind('\n', 0, match.start()) + 1:match.start()].strip()
                value = match.group(1)
                if value.lower() in {'into', 'the', 'keys', 'with', 'to', 'from', 'and', 'is', 'on', 'using'}:
                    continue
                command_prefix = re.search(
                    r'(?:^|[;&|`$>])\s*(?:(?:sudo|time|command|exec|env|coder)\s+)*$', before + ' ',
                ) or re.fullmatch(r'(?:[-*+]|\d+[.)]|Step\s+\d+:)', before, re.I)
                host_shape = '@' in match.group() or '.' in value or any(c.isdigit() for c in value)
                if not command_prefix and not host_shape:
                    continue
                if not host_shape and _SSH_PROSE_TAIL.match(view, match.end()):
                    continue  # "ssh access is required" is an explanatory sentence.
            if (match.group(1).lower() not in {"localhost", "127.0.0.1"}
                    and not (pattern is _HOST_FIELD and is_reference_prefix(match, "host"))):
                yield candidate(*match.span(1), "internal_host_context", "private_url")
