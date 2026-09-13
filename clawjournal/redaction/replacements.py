"""Keep weak, partial identifiers out of the global secret dictionary."""
from __future__ import annotations
from bisect import bisect_right
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .code_context import CodeContext


def contains_span(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """Look up one span in sorted, merged intervals."""
    index = bisect_right(spans, (start, float("inf"))) - 1
    return index >= 0 and end <= spans[index][1]


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for left, right in sorted(spans):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:
            merged.append((left, right))
    return merged


def replace_spans(text: str, spans: list[tuple[int, int, str]], *,
                  context: CodeContext | None = None) -> tuple[str, int]:
    """Use original, disjoint offsets and copy retained text only once."""
    if not spans:
        return text, 0
    parts = []
    cursor = 0
    for start, end, replacement in spans:
        parts.extend((text[cursor:start], replacement))
        cursor = end
    parts.append(text[cursor:])
    if context is not None:
        context.apply_edits(spans)
    return "".join(parts), len(spans)


def coalesce_replacements(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Cover the union of sensitive spans; never lose a partial overlap."""
    from .secrets import _CREDENTIAL_PLACEHOLDERS
    merged: list[tuple[int, int, str]] = []
    for start, end, replacement in sorted(spans, key=lambda row: (row[0], -row[1])):
        if merged and start < merged[-1][1]:
            left, right, placeholder = merged[-1]
            if placeholder in {'[REDACTED_EMAIL]', '[REDACTED_URL]'} and replacement in _CREDENTIAL_PLACEHOLDERS:
                placeholder = replacement
            merged[-1] = left, max(right, end), placeholder
        else:
            merged.append((start, end, replacement))
    return merged


class ReplacementMap(dict[str, str]):
    """Global full entities plus fragments restricted to actual scanner spans.

    A truncated address's local part can also be an ordinary word. Preserve
    its finding/decision hash, but never promote it to a global replacement.
    """
    def __init__(self) -> None:
        super().__init__()
        self.email_fragments: dict[str, str] = {}
        self.url_userinfo: dict[str, str] = {}

    def __bool__(self) -> bool:
        return bool(len(self) or self.email_fragments or self.url_userinfo)

    def add(self, value: str, replacement: str) -> None:
        """Keep the first finding, except that credential evidence wins."""
        from .secrets import _CREDENTIAL_PLACEHOLDERS

        if value not in self or (
            self[value] in {"[REDACTED_EMAIL]", "[REDACTED_URL]"}
            and replacement in _CREDENTIAL_PLACEHOLDERS
        ):
            self[value] = replacement

    def update(self, other: dict[str, str]) -> None:
        from .secrets import _CREDENTIAL_PLACEHOLDERS

        # A password can also look like an email/host. Do not let a later
        # weak finding erase the evidence that every copy is a credential.
        for value, replacement in other.items():
            if (self.get(value) in _CREDENTIAL_PLACEHOLDERS
                    and replacement in {"[REDACTED_EMAIL]", "[REDACTED_URL]", "[REDACTED]"}):
                continue
            self[value] = replacement
        if isinstance(other, ReplacementMap):
            self.email_fragments.update(other.email_fragments)
            self.url_userinfo.update(other.url_userinfo)


def replace_email_fragments(text: str, fragments: dict[str, str], *, ignore_case: bool = False,
                            context: CodeContext | None = None) -> tuple[str, int]:
    from .candidate_formats import iter_partial_email_candidates

    if ignore_case:
        fragments = {key.casefold(): value for key, value in fragments.items()}
    def key(value: str) -> str:
        return value.casefold() if ignore_case else value
    spans = [
        (match["start"], match["end"], fragments[key(match["match"])])
        for match in iter_partial_email_candidates(text)
        if key(match["match"]) in fragments
    ]
    return replace_spans(text, spans, context=context)


def secret_value_spans(text: str, secret: str, replacement: str):
    """Propagate a captured value without replacing assignment field names."""
    import re
    from .secrets import SECRET_PATTERNS, _secret_matches

    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(secret)}(?![A-Za-z0-9_])" if len(secret) < 20 and secret.isalnum()
        else re.escape(secret),
        re.IGNORECASE if len(secret) < 20 and secret.isalnum() else 0,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return []
    labels = []
    for name, assignment in SECRET_PATTERNS:
        if name not in {"env_secret", "generic_secret"}:
            continue
        for match in _secret_matches(assignment, text):
            start = match.start()
            label_start = start
            while label_start > 0 and (text[label_start - 1].isalnum() or text[label_start - 1] == '_'):
                label_start -= 1
            # Preserve a prefix such as TELEGRAM_BOT_ in a field name. A
            # separately known value glued to that name is still sensitive.
            if secret.casefold() not in text[label_start:start].casefold():
                start = label_start
            labels.append((start, match.start(1)))
    labels = merge_spans(labels)
    return [(m.start(), m.end(), replacement) for m in matches if not contains_span(labels, m.start(), m.end())]


def replace_secret_value(text: str, secret: str, replacement: str, *,
                         context: CodeContext | None = None) -> tuple[str, int]:
    return replace_spans(text, secret_value_spans(text, secret, replacement), context=context)


def email_pattern(value: str, flags: int = 0):
    """Keep Chinese adjacency without matching inside a different address."""
    import re
    return re.compile(r"(?<![A-Za-z0-9._%+-])" + re.escape(value)
                      + r"(?![A-Za-z0-9-]|\.[A-Za-z0-9])", flags)
