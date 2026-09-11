"""Keep weak, partial identifiers out of the global secret dictionary."""
from __future__ import annotations
from bisect import bisect_right


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


def replace_spans(text: str, spans: list[tuple[int, int, str]]) -> tuple[str, int]:
    """Use original, disjoint offsets and copy retained text only once."""
    if not spans:
        return text, 0
    parts = []
    cursor = 0
    for start, end, replacement in spans:
        parts.extend((text[cursor:start], replacement))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), len(spans)


class ReplacementMap(dict[str, str]):
    """Global full entities plus fragments restricted to actual scanner spans.

    A truncated address's local part can also be an ordinary word. Preserve
    its finding/decision hash, but never promote it to a global replacement.
    """
    def __init__(self) -> None:
        super().__init__()
        self.email_fragments: dict[str, str] = {}

    def __bool__(self) -> bool:
        return bool(len(self) or self.email_fragments)

    def update(self, other: dict[str, str]) -> None:
        super().update(other)
        if isinstance(other, ReplacementMap):
            self.email_fragments.update(other.email_fragments)


def replace_email_fragments(text: str, fragments: dict[str, str], *, ignore_case: bool = False) -> tuple[str, int]:
    from .pii import _TRUNCATED_EMAIL_PATTERN, _content_matches

    if ignore_case:
        fragments = {key.casefold(): value for key, value in fragments.items()}
    def key(value: str) -> str:
        return value.casefold() if ignore_case else value
    spans = [
        (match.start(1), match.end(1), fragments[key(match.group(1))])
        for match in _content_matches(_TRUNCATED_EMAIL_PATTERN, text)
        if key(match.group(1)) in fragments
    ]
    return replace_spans(text, spans)


def replace_secret_value(text: str, secret: str, replacement: str) -> tuple[str, int]:
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
        return text, 0
    labels = []
    for name, assignment in SECRET_PATTERNS:
        if name not in {"env_secret", "generic_secret"}:
            continue
        for match in _secret_matches(assignment, text):
            start = match.start()
            while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_.-"):
                start -= 1
            labels.append((start, match.start(1)))
    labels = merge_spans(labels)
    spans = [(m.start(), m.end(), replacement) for m in matches if not contains_span(labels, m.start(), m.end())]
    return replace_spans(text, spans)
