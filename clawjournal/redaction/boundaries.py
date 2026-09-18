"""Limits on automatic replacement, not limits on sensitive-data detection.

Like scrubadub and email-validator, check address/hostname lengths. Unlike
an input validator, an overlong candidate must not be silently discarded:
it may contain a real secret joined to ordinary prose. Keep it local instead.
No private-key size or delimiter requirement is introduced here.
"""
from __future__ import annotations


class RedactionBoundaryError(ValueError):
    """A candidate needs a clearer boundary before it can be shared."""

    def __init__(self, rule: str):
        self.rule = rule
        # Never include the candidate, its surrounding text or a secret hash.
        super().__init__(
            f"A {rule} candidate has an unclear or unusually long boundary. "
            "This trace remains local to avoid removing a large section of text "
            "or sharing sensitive content. Add a clear separator or an explicit "
            "redaction before sharing it."
        )


def ensure_safe_replacement(value: str, rule: str) -> None:
    """Raise before replacing an ambiguous oversized candidate.

    Email budgets are based on RFC 5321 and the usual 254-byte mailbox limit.
    Hostname budgets are 253 UTF-8 bytes and 63 bytes per label. These are
    conservative replacement checks, not full IDNA/mailbox validators.
    Telegram's 128-character limit is a conservative *replacement budget*,
    not a claim that every present or future bot token has a fixed length.
    Detection still returns the entire candidate, including beyond these
    limits; callers must never turn this exception into a clean verdict.
    """
    from .candidate_formats import _scanning_view

    value = _scanning_view(value)[0]
    if rule.startswith("email"):
        local, separator, domain = value.rpartition("@")
        if not separator:  # legacy truncated-address finding excludes '@'
            local, domain = value, ""
        oversized = (
            len(value.encode("utf-8", errors="surrogatepass")) > 254
            or len(local.encode("utf-8", errors="surrogatepass")) > 64
            or any(len(label.encode("utf-8", errors="surrogatepass")) > 63 for label in domain.split("."))
        )
    elif rule in {"internal_tld_host", "internal_host_context", "personal_hostname", "device_id", "url_hostname"}:
        # personal_hostname candidates are bounded to 62 characters by their
        # own pattern (candidate_formats.PERSONAL_HOST_PATTERN). This check
        # still applies to non-ASCII case-fold letters and to the other rules.
        oversized = len(value.encode("utf-8", errors="surrogatepass")) > 253 or any(
            len(label.encode("utf-8", errors="surrogatepass")) > 63 for label in value.rstrip(".").split(".")
        )
    elif rule.startswith("telegram"):
        oversized = len(value) > 128
    else:
        return
    if oversized:
        raise RedactionBoundaryError(rule)


def ensure_text_boundaries(text: str, *, context=None) -> None:
    """Preflight built-in candidates before any redactor mutates this text.

    Lazy imports avoid the findings/pii/secrets import cycle. This does not
    run external detectors, DNS lookups, AI or live credential validation.
    """
    from .candidate_formats import iter_format_candidates
    from .pii import _PII_CONTENT_PATTERNS_COMPILED, _content_matches
    from .code_context import code_context

    if context is None:
        context = code_context(text)

    import re
    from .candidate_formats import credentialed_urls, email_in_url_userinfo
    url_spans = list(credentialed_urls(text))
    # URL userinfo is separate from its host. Its email exemption must not
    # waive the host replacement budget when the secret pass masks that host.
    for _start, end, authority_end in url_spans:
        host = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]*').match(text, end + 1, authority_end)
        if host and '.' in host.group():
            ensure_safe_replacement(host.group(), 'url_hostname')

    def check(value: str, rule: str, start: int, end: int) -> None:
        if rule.startswith("email") and email_in_url_userinfo(start, end, url_spans):
            return  # Explicit URL userinfo is fully masked by the secret pass.
        # A hint can spare a supported code occurrence from replacement, but
        # must not authorize an otherwise unsafe boundary. Budget outcomes
        # therefore do not depend on whether optional parsing succeeded.
        ensure_safe_replacement(value, rule)
        if context.protects(start, end) and rule in {"email", "email_extended", "internal_tld_host"}:
            return

    # Inspect raw matches before allowlists/no-reply filters. A later engine
    # can still redact one of those matches; an allowlist cannot make an
    # oversized replacement safe. Other PII rules need no length preflight.
    guarded = {"email", "email_truncated", "telegram_bot_token", "personal_hostname", "internal_tld_host"}
    from .candidate_formats import has_personal_host_hint
    if not has_personal_host_hint(text):
        guarded.discard("personal_hostname")
    for rule, pattern, _kind, _confidence, group, _skip_kind in _PII_CONTENT_PATTERNS_COMPILED:
        if rule in guarded:
            for match in _content_matches(pattern, text):
                check(match.group(group), rule, match.start(group), match.end(group))
    for candidate in iter_format_candidates(text, context=context):
        check(candidate["match"], candidate["rule"], candidate["start"], candidate["end"])
