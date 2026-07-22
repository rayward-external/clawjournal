"""Detect and redact secrets in conversation data.

Two surfaces coexist:

1. **Legacy mutate-in-place API** (`scan_text`, `redact_session`, etc.).
   Still used by the parse path and by older direct-redaction helpers.
   Works on session dicts directly, returns the
   redacted copy plus a metadata-only log.

2. **DB-backed findings API** (`scan_session_for_findings`,
   `apply_findings_to_blob`). Emits `RawFinding` records keyed on
   offsets into anonymized field content so the Scanner can persist
   salted hashes and the share-time apply shim can re-scan, consult
   per-entity status, and produce byte-equivalent output to the
   legacy path when every decision is open/accepted.
"""

import json
import math
import re
import sqlite3
from collections.abc import Iterable
from typing import Any

from ..findings import RawFinding, hash_entity
from ..parsing.widened import iter_widened_text_locations

REDACTED = "[REDACTED]"

# Ordered from most specific to least specific
SECRET_PATTERNS = [
    # JWT tokens — full 3-segment form
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}")),

    # JWT tokens — partial (header only or header+partial payload, e.g. truncated)
    ("jwt_partial", re.compile(r"eyJ[A-Za-z0-9_-]{15,}")),

    # PostgreSQL/database connection strings with passwords
    ("db_url", re.compile(r"postgres(?:ql)?://[^:]+:[^@\s]+@[^\s\"'`]+")),

    # Anthropic API keys
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),

    # OpenAI API keys
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{40,}")),

    # Hugging Face tokens
    ("hf_token", re.compile(r"hf_[A-Za-z0-9]{20,}")),

    # GitHub tokens
    ("github_token", re.compile(r"(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{30,}")),

    # PyPI tokens
    ("pypi_token", re.compile(r"pypi-[A-Za-z0-9_-]{50,}")),

    # NPM tokens
    ("npm_token", re.compile(r"npm_[A-Za-z0-9]{30,}")),

    # Stripe API keys — `sk_live_…`, `pk_live_…`, `rk_live_…`, plus the
    # `_test_…` variants. Cass's table flags this as a likely gap; verified
    # absent before adding. The 24+ char tail matches every real Stripe
    # key shape (current keys are 24-32 chars after the underscore).
    #
    # Round-3 fix: trailing terminator is `(?![A-Za-z0-9])` instead of
    # `\b`. A real Stripe key abutted by `_word` (e.g. inside an
    # f-string like `f"{sk_live_xxx}_id"`) would never satisfy `\b`
    # because `_` is a word character — engine would backtrack the
    # greedy `{24,}` body all the way down and never find a boundary.
    # The negative lookahead correctly terminates on `_` and any other
    # non-alphanumeric character while still blocking match into a
    # longer alphanumeric run (preserves the round-1 identifier-
    # embedding regression test).
    ("stripe_key", re.compile(
        r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{24,}(?![A-Za-z0-9])"
    )),

    # Stripe webhook signing secrets — used to verify the authenticity
    # of incoming webhook events. Different secret class than the API
    # keys above (signature verification rather than API auth), so a
    # distinct entry + placeholder. Same boundary discipline.
    ("stripe_webhook_secret", re.compile(
        r"\bwhsec_[A-Za-z0-9]{24,}(?![A-Za-z0-9])"
    )),

    # AWS access key IDs (but not in regex pattern context)
    ("aws_key", re.compile(r"(?<![A-Za-z0-9\[])AKIA[0-9A-Z]{16}(?![0-9A-Z\]{}])")),

    # AWS secret keys (40 chars, mixed case + special)
    ("aws_secret", re.compile(
        r"(?:aws_secret_access_key|secret_key)\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?",
        re.IGNORECASE,
    )),

    # Slack tokens
    ("slack_token", re.compile(r"xox[bpsa]-[A-Za-z0-9-]{20,}")),

    # Discord webhook URLs (contain a secret token in the path)
    ("discord_webhook", re.compile(
        r"https?://(?:discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"
    )),

    # Private keys
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        r"[\s\S]*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    )),

    # CLI flags that pass tokens/secrets: --token VALUE, --access-token VALUE, etc.
    ("cli_token_flag", re.compile(
        r"(?:--|-)(?:access[_-]?token|auth[_-]?token|api[_-]?key|secret|password|token)"
        r"[\s=]+([A-Za-z0-9_/+=.-]{8,})",
        re.IGNORECASE,
    )),

    # Environment variable assignments with secret-like names (with or without quotes)
    ("env_secret", re.compile(
        r"(?:SECRET|PASSWORD|TOKEN|API_KEY|AUTH_KEY|ACCESS_KEY|SERVICE_KEY|DB_PASSWORD"
        r"|SUPABASE_KEY|SUPABASE_SERVICE|ANON_KEY|SERVICE_ROLE)"
        r"\s*[=]\s*['\"]?([^\s'\"]{6,})['\"]?",
        re.IGNORECASE,
    )),

    # Generic secret assignments: SECRET_KEY = "value", api_key: "value", etc.
    ("generic_secret", re.compile(
        r"""(?:secret[_-]?key|api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token"""
        r"""|service[_-]?role[_-]?key|private[_-]?key)"""
        r"""\s*[=:]\s*['"]([A-Za-z0-9_/+=.-]{20,})['"]""",
        re.IGNORECASE,
    )),

    # Bearer tokens in headers, JWT-shaped. Three character runs bounded
    # at {20,2048} per segment (was {20,}) — closes the polynomial-
    # backtracking surface from three unbounded character classes
    # separated by literal `.`. Real JWTs sit well under 2048 per
    # segment; the cap fails-loud rather than producing pathological
    # match times on adversarial input.
    #
    # `\b(?i:Bearer)` round-2 hardening: the leading `\b` prevents the
    # regex from splitting an identifier (`myBearer eyJ…` no longer
    # carves out `Bearer eyJ…` and leaves `my` orphaned), and `(?i:)`
    # accepts `BEARER`/`bearer`/`Bearer` so case-mangled headers still
    # land at the higher-confidence `bearer` classification (0.85)
    # rather than falling through to `bearer_generic` (0.75).
    ("bearer", re.compile(
        r"\b(?i:Bearer)\s+(eyJ[A-Za-z0-9_-]{20,2048}\.[A-Za-z0-9_-]{20,2048}\.[A-Za-z0-9_-]{20,2048})"
    )),

    # Generic Bearer tokens — non-JWT-shaped opaque/OAuth bearers that
    # the JWT-shaped pattern above misses. Bounded at {20,2048} for the
    # same ReDoS-prevention reason. Distinct entry so confidence (0.75)
    # can sit lower than JWT-shaped (0.85) — generic bearers are higher-
    # FP because the character class is broader.
    #
    # Leading `\b` matches the JWT-shaped variant's round-2 hardening
    # so this regex also can't carve a substring out of a longer
    # identifier.
    ("bearer_generic", re.compile(
        r"\b(?i:Bearer)\s+([A-Za-z0-9\-_.~+/]{20,2048}=*)"
    )),

    # IP addresses (public, non-loopback, non-private-by-default)
    ("ip_address", re.compile(
        r"\b(?!127\.0\.0\.)(?!0\.0\.0\.0)(?!255\.255\.)"
        r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
    )),

    # URL query params with secrets: ?key=VALUE, &token=VALUE, etc.
    ("url_token", re.compile(
        r"[?&](?:key|token|secret|password|apikey|api_key|access_token|auth)"
        r"=([A-Za-z0-9_/+=.-]{8,})",
        re.IGNORECASE,
    )),

    # Email addresses (for PII removal) — require at least 2-char local part
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),

    # Long base64-like strings in quotes (checked for entropy — see scan_text)
    ("high_entropy", re.compile(r"""['"][A-Za-z0-9_/+=.-]{40,}['"]""")),
]

# Confidence scores per pattern type, used in redaction reports.
# High (>=0.90): exact prefix/format matches, almost certainly secrets.
# Medium (0.70-0.89): structural patterns, worth reviewing.
# Low (<0.70): heuristic/PII, may be false positives.
CONFIDENCE: dict[str, float] = {
    "jwt": 0.98, "private_key": 0.98,
    "anthropic_key": 0.98, "openai_key": 0.98,
    "github_token": 0.98, "hf_token": 0.98,
    "pypi_token": 0.98, "npm_token": 0.98,
    "stripe_key": 0.98, "stripe_webhook_secret": 0.98,
    "aws_key": 0.98, "aws_secret": 0.95,
    "slack_token": 0.98, "discord_webhook": 0.95,
    "jwt_partial": 0.80, "db_url": 0.85,
    "bearer": 0.85, "bearer_generic": 0.75, "cli_token_flag": 0.80,
    "env_secret": 0.75, "generic_secret": 0.80,
    "url_token": 0.80,
    "ip_address": 0.60, "email": 0.65,
    "high_entropy": 0.55,
}

# Typed placeholders per secret type — preserves what kind of secret was
# removed so model-training data retains task structure.
SECRET_PLACEHOLDER: dict[str, str] = {
    "jwt": "[REDACTED_JWT]",
    "jwt_partial": "[REDACTED_JWT]",
    "db_url": "[REDACTED_DB_URL]",
    "anthropic_key": "[REDACTED_ANTHROPIC_KEY]",
    "openai_key": "[REDACTED_OPENAI_KEY]",
    "hf_token": "[REDACTED_HF_TOKEN]",
    "github_token": "[REDACTED_GITHUB_TOKEN]",
    "pypi_token": "[REDACTED_PYPI_TOKEN]",
    "npm_token": "[REDACTED_NPM_TOKEN]",
    "aws_key": "[REDACTED_AWS_KEY]",
    "aws_secret": "[REDACTED_AWS_SECRET]",
    "slack_token": "[REDACTED_SLACK_TOKEN]",
    "discord_webhook": "[REDACTED_DISCORD_WEBHOOK]",
    "private_key": "[REDACTED_PRIVATE_KEY]",
    "cli_token_flag": "[REDACTED_CLI_TOKEN]",
    "env_secret": "[REDACTED_ENV_SECRET]",
    "generic_secret": "[REDACTED_SECRET]",
    "bearer": "[REDACTED_BEARER]",
    "bearer_generic": "[REDACTED_BEARER]",
    "stripe_key": "[REDACTED_STRIPE_KEY]",
    "stripe_webhook_secret": "[REDACTED_STRIPE_WEBHOOK_SECRET]",
    "ip_address": "[REDACTED_IP]",
    "url_token": "[REDACTED_URL_TOKEN]",
    "email": "[REDACTED_EMAIL]",
    "high_entropy": "[REDACTED_SECRET]",
}

TOOL_SERVER_ID_PLACEHOLDER = "[REDACTED_TOOL_SERVER_ID]"

_TOOL_SERVER_ID_ASSIGNMENT_RE = re.compile(
    r"""(?i)(?:"serverId"|serverId|server_id|server-id)\s*[:=]\s*["'](?P<value>[A-Za-z0-9_-]{8,})["']"""
)


def _is_tool_server_id_key(key: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", key.lower()) == "serverid"


def _collect_infrastructure_secret_map(value: Any) -> dict[str, str]:
    """Collect local tool/browser infrastructure IDs that should not export.

    Preview/browser tools often store opaque handles as ``serverId``. Some
    handles are harmless locally but look like provider tokens to TruffleHog
    once serialized as JSON, so redact them deterministically before the
    mandatory final scan.
    """
    out: dict[str, str] = {}

    def add(raw: str) -> None:
        if len(raw) < 8 or raw.startswith("[REDACTED"):
            return
        out.setdefault(raw, TOOL_SERVER_ID_PLACEHOLDER)

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if isinstance(key, str) and _is_tool_server_id_key(key):
                    if isinstance(nested, str):
                        add(nested)
                    else:
                        walk(nested)
                    continue
                walk(nested)
            return
        if isinstance(item, list):
            for nested in item:
                walk(nested)
            return
        if isinstance(item, str):
            for match in _TOOL_SERVER_ID_ASSIGNMENT_RE.finditer(item):
                add(match.group("value"))

    walk(value)
    return out

ALLOWLIST = [
    re.compile(r"noreply@"),
    re.compile(r"@example\.com"),
    re.compile(r"@localhost"),
    re.compile(r"@anthropic\.com"),
    re.compile(r"@github\.com"),
    re.compile(r"@users\.noreply\.github\.com"),
    re.compile(r"AKIA\["),  # regex patterns about AWS keys
    re.compile(r"sk-ant-\.\*"),  # regex patterns about API keys
    re.compile(r"postgres://user:pass@"),  # example/documentation URLs
    re.compile(r"postgres://username:password@"),
    re.compile(r"@pytest"),  # Python decorator false positives
    re.compile(r"@tasks\."),
    re.compile(r"@mcp\."),
    re.compile(r"@server\."),
    re.compile(r"@app\."),
    re.compile(r"@router\."),
    re.compile(r"192\.168\."),  # private IPs (low risk)
    re.compile(r"10\.\d+\.\d+\.\d+"),
    re.compile(r"172\.(?:1[6-9]|2\d|3[01])\."),
    re.compile(r"8\.8\.8\.8"),  # Google DNS
    re.compile(r"8\.8\.4\.4"),
    re.compile(r"1\.1\.1\.1"),  # Cloudflare DNS
]


def _shannon_entropy(s: str) -> float:
    """Higher values indicate more random-looking strings."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


def _has_mixed_char_types(s: str) -> bool:
    """Check if string has a mix of uppercase, lowercase, and digits."""
    has_upper = any(c.isupper() for c in s)
    has_lower = any(c.islower() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return has_upper and has_lower and has_digit


def _check_user_allowlist(matched_text: str, match_type: str,
                         user_allowlist: list[dict] | None) -> bool:
    """Return True if the finding should be skipped due to user allowlist."""
    if not user_allowlist:
        return False
    for entry in user_allowlist:
        etype = entry.get("type", "exact")
        if etype == "exact" and entry.get("text") == matched_text:
            return True
        if etype == "pattern":
            regex = entry.get("regex", "")
            if regex:
                try:
                    if re.search(regex, matched_text):
                        return True
                except re.error:
                    pass  # invalid regex in user config, skip this entry
        if etype == "category" and entry.get("match_type") == match_type:
            return True
    return False


# Minimum text length worth scanning — the shortest possible match is an
# email like "a@b.co" (6 chars).  Anything shorter cannot contain a secret.
_MIN_SCAN_LENGTH = 6

# Patterns that match key=value / key: value assignments.  These are the
# most expensive regexes (IGNORECASE alternation).  If the text contains
# no '=' or ':' character, all three can be skipped.
_ASSIGNMENT_PATTERNS = frozenset({"env_secret", "generic_secret", "aws_secret"})


# Negative-context heuristic for `ip_address`. The IP regex blacklists
# obvious private/reserved prefixes via lookaheads but cannot tell a
# real IPv4 address from a 4-segment version string. Two cheap checks
# in the surrounding text catch the common false-positive shapes:
#
#   1. **Version-introducer keyword in the immediate prefix.** Strings
#      like ``version 1.2.3.4``, ``commit 1.2.3.4``, ``release 2.0.1.4``
#      look exactly like IP addresses to the regex.
#   2. **Part of a longer dotted-numeric run.** Inside ``1.2.3.4.5`` the
#      regex can match both ``1.2.3.4`` and ``2.3.4.5``; both should be
#      suppressed because the surrounding shape is a 5-segment version,
#      not two adjacent IPs.
#
# Real configs DO contain real IPs (`dns_server=8.8.8.8`), so we do
# not also suppress on `=`/`:` assignment context — the existing
# ALLOWLIST handles known-good public IPs (Google/Cloudflare DNS) by
# explicit literal entry.
# Intentionally conservative — only fires when the keyword IMMEDIATELY
# precedes the IP (with optional whitespace and `:`/`=`). Strings like
# ``Apache version 2.4 and the 1.2.3.4 server`` will NOT suppress the
# IP because `version` doesn't sit right before `1.2.3.4`. This is the
# right trade-off: false negatives (the IP redacts when it's actually
# a version far back in the sentence) are recoverable; false positives
# (a real public IP gets passed through because of an unrelated
# `version` somewhere upstream) would silently leak.
#
# Trailing `\s*[:=]?\s*$` accepts the unreachable "keyword followed
# immediately by the IP with no separator" case as a no-op: the IP
# regex's own leading `\b` cannot fire when the previous char is a
# word character (the keyword's last char), so this branch is
# inert in practice. Documenting rather than tightening because
# tightening would add complexity without changing behavior.
_IP_VERSION_INTRODUCER = re.compile(
    r"(?:^|\s|[\(\[<])(?:v|ver|version|commit|release|build|tag|sha|rev"
    r"|major|minor|patch)\s*[:=]?\s*$",
    re.IGNORECASE,
)


def _ip_looks_like_version(text: str, match: "re.Match[str]") -> bool:
    """True when an ip_address-shaped match is more plausibly a version."""

    start, end = match.start(), match.end()

    # Char immediately before is `.` preceded by a digit → match is the
    # tail of a longer dotted-numeric run, e.g. `0.1.2.3.4`.
    if (
        start >= 2
        and text[start - 1] == "."
        and text[start - 2].isdigit()
    ):
        return True

    # Char immediately after is `.` followed by a digit → match is the
    # head of a longer dotted-numeric run, e.g. `1.2.3.4.5`.
    if (
        end + 1 <= len(text) - 1
        and text[end] == "."
        and text[end + 1].isdigit()
    ):
        return True

    # Trailing 24 chars before the match end with a version introducer.
    ctx_start = max(0, start - 24)
    prefix = text[ctx_start:start]
    if _IP_VERSION_INTRODUCER.search(prefix):
        return True

    return False


def scan_text(text: str, user_allowlist: list[dict] | None = None) -> list[dict]:
    if not text or len(text) < _MIN_SCAN_LENGTH:
        return []

    # Fast-reject: skip the expensive assignment-style patterns when the
    # text has no separator character.
    has_assignment_sep = "=" in text or ":" in text

    findings = []
    for name, pattern in SECRET_PATTERNS:
        if not has_assignment_sep and name in _ASSIGNMENT_PATTERNS:
            continue

        for match in pattern.finditer(text):
            matched_text = match.group(0)

            if any(allow_pat.search(matched_text) for allow_pat in ALLOWLIST):
                continue

            if _check_user_allowlist(matched_text, name, user_allowlist):
                continue

            # For high_entropy, verify string actually looks like a secret
            if name == "high_entropy":
                inner = matched_text[1:-1]  # strip quotes
                if not _has_mixed_char_types(inner):
                    continue
                if _shannon_entropy(inner) < 3.5:
                    continue
                if inner.count(".") > 2:
                    continue

            # For ip_address, suppress version-shaped matches
            # (e.g. `version 1.2.3.4`, the slices of `1.2.3.4.5`).
            # The regex itself can't carry this context check.
            if name == "ip_address" and _ip_looks_like_version(text, match):
                continue

            findings.append({
                "type": name,
                "start": match.start(),
                "end": match.end(),
                "match": matched_text,
                "confidence": CONFIDENCE.get(name, 0.5),
            })

    return findings


def redact_text(
    text: str, user_allowlist: list[dict] | None = None,
) -> tuple[str, int, list[dict]]:
    """Redact secrets from text. Returns (redacted_text, count, redaction_log)."""
    if not text:
        return text, 0, []

    findings = scan_text(text, user_allowlist=user_allowlist)
    if not findings:
        return text, 0, []

    # Sort by position (descending start) to replace without shifting indices
    findings.sort(key=lambda f: f["start"], reverse=True)

    # Deduplicate overlapping findings (keep the later-starting match on overlap)
    deduped = []
    for f in findings:
        if not deduped or f["end"] <= deduped[-1]["start"]:
            deduped.append(f)

    # Build redaction log (no secret text — only metadata)
    log: list[dict] = []
    for f in deduped:
        entry: dict = {
            "type": f["type"],
            "confidence": f["confidence"],
            "original_length": len(f["match"]),
        }
        # Capture surrounding context for medium/low confidence findings
        if f["confidence"] < 0.90:
            start, end = f["start"], f["end"]
            ctx_before = text[max(0, start - 40):start]
            ctx_after = text[end:end + 40]
            # Redact high-confidence secrets in context. Two layers:
            # (1) blank any byte that overlaps a high-conf finding's
            # span in the FULL findings list — catches the "secret
            # tail leaks into the 40-char window because the regex
            # can't re-match a truncated tail" gap (round-4 self-
            # review). (2) regex-based pass for any high-conf secret
            # that fully fits inside the window. Order matters: span
            # blanking first so the regex sees clean text.
            safe_before = _blank_high_conf_overlaps(
                ctx_before, max(0, start - 40), findings,
            )
            safe_after = _blank_high_conf_overlaps(
                ctx_after, end, findings,
            )
            safe_before = _redact_high_confidence_only(safe_before)
            safe_after = _redact_high_confidence_only(safe_after)
            entry["context_before"] = safe_before
            entry["context_after"] = safe_after
        log.append(entry)

    # Replace from end-to-start (deduped is already in descending start order)
    result = text
    for f in deduped:
        placeholder = SECRET_PLACEHOLDER.get(f["type"], REDACTED)
        result = result[:f["start"]] + placeholder + result[f["end"]:]

    return result, len(deduped), log


def _blank_high_conf_overlaps(
    substring: str,
    sub_start: int,
    all_findings: list[dict],
) -> str:
    """Round-4 self-review: blank bytes in a context window that overlap
    a high-confidence finding's span in the source text.

    `_redact_high_confidence_only`'s regex-based pass cannot re-match
    a truncated tail of a high-conf secret (the regex needs the prefix
    + minimum-length tail). Without this pre-pass, the trailing bytes
    of an outer secret leak into the context_before/context_after
    fields of a neighboring medium-conf finding's redaction-log entry.
    Round 4 found this leak widening with the new bearer_generic
    pattern (broader alphabet) plus stripe_key/stripe_webhook_secret
    (new high-conf families); fix is to blank known high-conf spans
    using the full findings list instead of relying on regex re-match
    against truncated input.

    `sub_start` is the offset of `substring` in the source text so
    overlap can be computed in source coordinates.
    """

    if not substring:
        return substring
    sub_end = sub_start + len(substring)
    overlaps: list[tuple[int, int, str]] = []
    for f in all_findings:
        if f["confidence"] < 0.90:
            continue
        # Overlap range in source-text coordinates
        ov_start = max(f["start"], sub_start)
        ov_end = min(f["end"], sub_end)
        if ov_start < ov_end:
            placeholder = SECRET_PLACEHOLDER.get(f["type"], REDACTED)
            # Translate to substring coordinates
            overlaps.append((ov_start - sub_start, ov_end - sub_start, placeholder))
    if not overlaps:
        return substring
    # Sort descending by start so right-to-left replacement preserves
    # earlier offsets.
    overlaps.sort(key=lambda x: x[0], reverse=True)
    out = substring
    for s, e, ph in overlaps:
        out = out[:s] + ph + out[e:]
    return out


def _redact_high_confidence_only(text: str) -> str:
    """Redact only high-confidence patterns (>=0.90) in context snippets."""
    if not text:
        return text
    findings = scan_text(text)
    high_conf = [f for f in findings if f["confidence"] >= 0.90]
    if not high_conf:
        return text
    high_conf.sort(key=lambda f: f["start"], reverse=True)
    # Deduplicate overlapping matches (same logic as redact_text)
    deduped = []
    for f in high_conf:
        if not deduped or f["end"] <= deduped[-1]["start"]:
            deduped.append(f)
    result = text
    for f in deduped:
        placeholder = SECRET_PLACEHOLDER.get(f["type"], REDACTED)
        result = result[:f["start"]] + placeholder + result[f["end"]:]
    return result


def redact_custom_strings(text: str, strings: list[str]) -> tuple[str, int]:
    if not text or not strings:
        return text, 0

    count = 0
    for target in strings:
        if not target or len(target) < 3:
            continue
        escaped = re.escape(target)
        pattern = rf"\b{escaped}\b" if len(target) >= 4 else escaped
        text, replacements = re.subn(pattern, "[REDACTED_CUSTOM]", text)
        count += replacements

    return text, count


def _redact_value(
    value: Any, custom_strings: list[str] | None = None,
    user_allowlist: list[dict] | None = None,
) -> tuple[Any, int, list[dict]]:
    """Recursively redact secrets from a string, list, or dict value."""
    if isinstance(value, str):
        result, count, log = redact_text(value, user_allowlist=user_allowlist)
        if custom_strings:
            result, n = redact_custom_strings(result, custom_strings)
            count += n
        return result, count, log
    if isinstance(value, dict):
        total = 0
        all_log: list[dict] = []
        out = {}
        for k, v in value.items():
            out[k], n, log = _redact_value(v, custom_strings, user_allowlist)
            total += n
            all_log.extend(log)
        return out, total, all_log
    if isinstance(value, list):
        total = 0
        all_log = []
        out_list = []
        for item in value:
            redacted, n, log = _redact_value(item, custom_strings, user_allowlist)
            out_list.append(redacted)
            total += n
            all_log.extend(log)
        return out_list, total, all_log
    return value, 0, []


def _collect_all_text(session: dict) -> list[tuple[str, str, int | None, str | None]]:
    """Collect all text fields from a session for scanning.

    Returns list of (text, field_name, message_index, tool_field).
    """
    texts: list[tuple[str, str, int | None, str | None]] = []

    for field in ("display_title", "project", "git_branch"):
        val = session.get(field)
        if val and isinstance(val, str):
            texts.append((val, field, None, None))

    for msg_idx, msg in enumerate(session.get("messages", [])):
        for field in ("content", "thinking"):
            val = msg.get(field)
            if val and isinstance(val, str):
                texts.append((val, field, msg_idx, None))
        for tool_use in msg.get("tool_uses", []):
            for tf in ("input", "output"):
                val = tool_use.get(tf)
                if val and isinstance(val, str):
                    texts.append((val, f"tool_{tf}", msg_idx, tf))
                elif val and isinstance(val, (dict, list)):
                    # Flatten structured tool data to scan
                    flat = _flatten_to_strings(val)
                    for s in flat:
                        texts.append((s, f"tool_{tf}", msg_idx, tf))
        # Widened message model (phase-2 C1): invocations / snippets /
        # extra / author flow through the same scan as content.
        # `extra` especially — invariant 8 in phase-2/01: do not
        # short-circuit because the field looks like metadata.
        for text, field_label in iter_widened_text_locations(msg):
            texts.append((text, field_label, msg_idx, None))

    return texts


def _flatten_to_strings(value: Any) -> list[str]:
    """Recursively extract all string values from a nested dict/list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for v in value.values():
            result.extend(_flatten_to_strings(v))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_flatten_to_strings(item))
        return result
    return []


def _build_redaction_set(
    texts: list[tuple[str, str, int | None, str | None]],
    user_allowlist: list[dict] | None = None,
    custom_strings: list[str] | None = None,
) -> tuple[dict[str, str], list[dict]]:
    """Scan all text at once to build a global map of secret strings to typed placeholders.

    Returns (secret_map, redaction_log) where secret_map maps each secret
    string to its typed replacement (e.g. ``"sk-ant-xxx" -> "[REDACTED_ANTHROPIC_KEY]"``).
    """
    secret_map: dict[str, str] = {}
    all_log: list[dict] = []

    for text, field, msg_idx, _tool_field in texts:
        findings = scan_text(text, user_allowlist=user_allowlist)
        for f in findings:
            matched = f["match"]
            placeholder = SECRET_PLACEHOLDER.get(f["type"], REDACTED)
            secret_map.setdefault(matched, placeholder)

            # For patterns with capture groups (env_secret, generic_secret,
            # cli_token_flag, aws_secret, url_token), also add the group
            for _name, pattern in SECRET_PATTERNS:
                if _name == f["type"]:
                    m = pattern.search(text[f["start"]:f["end"]])
                    if m and m.lastindex:
                        secret_map.setdefault(m.group(m.lastindex), placeholder)
                    break

            # Build log entry
            entry: dict = {
                "type": f["type"],
                "confidence": f["confidence"],
                "original_length": len(matched),
                "field": field,
            }
            if msg_idx is not None:
                entry["message_index"] = msg_idx
            if f["confidence"] < 0.90:
                start, end = f["start"], f["end"]
                ctx_before = text[max(0, start - 40):start]
                ctx_after = text[end:end + 40]
                entry["context_before"] = _redact_high_confidence_only(ctx_before)
                entry["context_after"] = _redact_high_confidence_only(ctx_after)
            all_log.append(entry)

    # Add custom strings to the redaction set
    if custom_strings:
        for s in custom_strings:
            if s and len(s) >= 3:
                secret_map.setdefault(s, "[REDACTED_CUSTOM]")

    return secret_map, all_log


def _apply_redaction_set(text: str, secret_map: dict[str, str]) -> tuple[str, int]:
    """Replace all known secrets in text using typed placeholders.

    Returns (redacted_text, replacement_count).

    Short secrets (< 20 chars) that look like plain words use word-boundary
    matching to avoid false positives (e.g. "Kai" matching inside "Kaizen").
    Long secrets and those containing special characters use plain str.replace
    since they are unique enough to match safely.
    """
    if not text or not secret_map:
        return text, 0

    count = 0
    # Sort by length descending so longer matches replace first
    for secret in sorted(secret_map, key=len, reverse=True):
        replacement = secret_map[secret]
        # Short alphanumeric strings need word boundaries to avoid matching
        # inside unrelated words (e.g. custom redact_strings like "Kai").
        if len(secret) < 20 and secret.isalnum():
            pattern = re.compile(rf"\b{re.escape(secret)}\b", re.IGNORECASE)
            text, n = pattern.subn(replacement, text)
            count += n
        elif secret in text:
            n = text.count(secret)
            text = text.replace(secret, replacement)
            count += n

    return text, count


def _apply_to_value(value: Any, secret_map: dict[str, str]) -> tuple[Any, int]:
    """Recursively apply redaction map to a value (string, dict, or list)."""
    if isinstance(value, str):
        return _apply_redaction_set(value, secret_map)
    if isinstance(value, dict):
        total = 0
        out = {}
        for k, v in value.items():
            out[k], n = _apply_to_value(v, secret_map)
            total += n
        return out, total
    if isinstance(value, list):
        total = 0
        out_list = []
        for item in value:
            redacted, n = _apply_to_value(item, secret_map)
            out_list.append(redacted)
            total += n
        return out_list, total
    return value, 0


def _apply_widened_message_fields(msg: dict, secret_map: dict[str, str]) -> int:
    """Redact secrets across the widened-message fields in place.

    Mutates ``msg`` directly, replacing strings inside ``invocations``,
    ``snippets``, ``extra`` (recursively) and the scalar ``author``.
    Returns the number of replacements applied. Used by both
    ``redact_session`` (legacy path) and ``apply_findings_to_blob``
    (DB-backed path) so the two stay in sync.

    Skipped silently when none of the widened fields are populated —
    the cost on legacy-shape messages is one ``msg.get`` per field.
    """
    total = 0
    if not isinstance(msg, dict) or not secret_map:
        return total

    author = msg.get("author")
    if isinstance(author, str) and author:
        msg["author"], n = _apply_redaction_set(author, secret_map)
        total += n

    invocations = msg.get("invocations")
    if isinstance(invocations, list):
        msg["invocations"], n = _apply_to_value(invocations, secret_map)
        total += n

    snippets = msg.get("snippets")
    if isinstance(snippets, list):
        msg["snippets"], n = _apply_to_value(snippets, secret_map)
        total += n

    extra = msg.get("extra")
    if isinstance(extra, dict):
        msg["extra"], n = _apply_to_value(extra, secret_map)
        total += n

    return total


def redact_session(
    session: dict, custom_strings: list[str] | None = None,
    user_allowlist: list[dict] | None = None,
    max_passes: int = 3,
) -> tuple[dict, int, list[dict]]:
    """Redact all secrets in a session dict using scan-mark-replace strategy.

    Strategy:
    1. Collect all text from the entire trace
    2. Scan once to build a global set of secret strings
    3. Replace all secrets across all fields in one sweep
    4. Repeat 2-3 passes to catch secrets revealed by earlier replacements

    Returns (redacted_session, total_redactions, redaction_log).
    The log contains metadata about each redaction (type, confidence,
    original_length, field) but never the secret text itself.
    """
    all_log: list[dict] = []
    total = 0

    for pass_num in range(max_passes):
        # Step 1: Collect all text from the session
        texts = _collect_all_text(session)

        # Step 2: Scan to build global redaction map (secret -> typed placeholder)
        secret_map, log = _build_redaction_set(
            texts, user_allowlist=user_allowlist,
            custom_strings=custom_strings if pass_num == 0 else None,  # custom strings only on first pass
        )
        secret_map.update(_collect_infrastructure_secret_map(session))

        if not secret_map:
            break  # Nothing more to redact

        # Only keep log from the first pass (subsequent passes are verification)
        if pass_num == 0:
            all_log = log

        # Step 3: Apply typed redaction map across all fields
        pass_count = 0

        for field in ("display_title", "project", "git_branch"):
            if session.get(field) and isinstance(session[field], str):
                session[field], n = _apply_redaction_set(session[field], secret_map)
                pass_count += n

        for msg in session.get("messages", []):
            for field in ("content", "thinking"):
                if msg.get(field) and isinstance(msg[field], str):
                    msg[field], n = _apply_redaction_set(msg[field], secret_map)
                    pass_count += n
            for tool_use in msg.get("tool_uses", []):
                for tf in ("input", "output"):
                    if tool_use.get(tf):
                        tool_use[tf], n = _apply_to_value(tool_use[tf], secret_map)
                        pass_count += n
            # Widened message model: redact across invocations /
            # snippets / extra / author with the same secret_map.
            n = _apply_widened_message_fields(msg, secret_map)
            pass_count += n

        total += pass_count

        if pass_count == 0 and pass_num > 0:
            break  # Verification pass found nothing new

    return session, total, all_log


# ---------------------------------------------------------------------------
# DB-backed findings: scan → RawFinding records, apply via DB status lookup.
# ---------------------------------------------------------------------------

SECRETS_ENGINE_ID = "regex_secrets"


def _iter_text_locations(
    session: dict,
) -> Iterable[tuple[str, str, int | None, str | None, str, str]]:
    """Yield every (text, field, message_index, tool_field, parent_path, parent_key).

    `parent_path`/`parent_key` let apply_findings_to_blob write the
    redacted value back into the right slot — either None (meaning
    replace the top-level scalar `session[field]`) or a path locating
    the leaf inside a `tool_uses[i]` dict/list.

    Field-name convention matches `derive_preview`'s parser in
    findings.py: top-level fields use their own name (`display_title`,
    `project`, `git_branch`); per-message string fields use `content`
    or `thinking`; tool_use strings use `tool_uses[<idx>].<branch>` or
    `tool_uses[<idx>].<branch>.<key>` when nested under a dict.
    """
    # (text, field, message_index, tool_field)
    # parent_path / parent_key indicate how to write back; emitted as
    # (text, field, message_index, tool_field, "write_kind", "write_key")
    # where write_kind ∈ {"top", "msg", "tool_str", "tool_dict"}.
    for field in ("display_title", "project", "git_branch"):
        val = session.get(field)
        if isinstance(val, str) and val:
            yield val, field, None, None, "top", field

    for msg_idx, msg in enumerate(session.get("messages", []) or []):
        if not isinstance(msg, dict):
            continue
        for field in ("content", "thinking"):
            val = msg.get(field)
            if isinstance(val, str) and val:
                yield val, field, msg_idx, None, "msg", field
        for tool_idx, tool_use in enumerate(msg.get("tool_uses", []) or []):
            if not isinstance(tool_use, dict):
                continue
            for branch in ("input", "output"):
                val = tool_use.get(branch)
                if isinstance(val, str) and val:
                    yield (
                        val,
                        f"tool_uses[{tool_idx}].{branch}",
                        msg_idx,
                        branch,
                        "tool_str",
                        f"{tool_idx}:{branch}",
                    )
                elif isinstance(val, dict):
                    for key, nested in val.items():
                        if isinstance(nested, str) and nested:
                            yield (
                                nested,
                                f"tool_uses[{tool_idx}].{branch}.{key}",
                                msg_idx,
                                branch,
                                "tool_dict",
                                f"{tool_idx}:{branch}:{key}",
                            )
        # Widened message model (phase-2 C1): yield strings from
        # invocations / snippets / extra / author so the findings
        # scan covers them. write_kind="widened" signals
        # ``apply_findings_to_blob`` to use the recursive writeback.
        for text, field_label in iter_widened_text_locations(msg):
            yield text, field_label, msg_idx, None, "widened", field_label


def _dedupe_overlapping_matches(matches: list[dict]) -> list[dict]:
    """Keep the longest/highest-confidence match per overlapping region.

    Mirrors `redact_text`'s "descending start + non-overlap" dedupe, but
    runs at scan time so each text region emits one finding. This keeps
    entity-level decisions coherent: ignoring a JWT also suppresses the
    overlapping `jwt_partial` that shares the same bytes.
    """
    if not matches:
        return []
    ordered = sorted(
        matches,
        key=lambda m: (-(m["end"] - m["start"]), -m["confidence"], m["start"]),
    )
    kept: list[dict] = []
    for candidate in ordered:
        overlaps = any(
            candidate["start"] < existing["end"] and candidate["end"] > existing["start"]
            for existing in kept
        )
        if not overlaps:
            kept.append(candidate)
    kept.sort(key=lambda m: m["start"])
    return kept


def scan_session_for_findings(
    session: dict,
    *,
    user_allowlist: list[dict] | None = None,
) -> list[RawFinding]:
    """Deterministic-engine scan that returns `RawFinding` records.

    Same pattern/allowlist semantics as `scan_text`; the caller
    (Scanner) hashes each match and persists the hash via
    `write_findings_to_db`. Offsets are into the anonymized field
    content as a Python `str` (code points), matching
    `derive_preview`'s expectation. Per-text overlapping matches
    collapse to one finding so entity-level decisions stay coherent.
    """
    findings: list[RawFinding] = []
    for text, field, msg_idx, tool_field, _write_kind, _write_key in _iter_text_locations(session):
        raw_matches = scan_text(text, user_allowlist=user_allowlist)
        for match in _dedupe_overlapping_matches(raw_matches):
            findings.append(RawFinding(
                engine=SECRETS_ENGINE_ID,
                rule=match["type"],
                entity_type=match["type"],
                entity_text=match["match"],
                field=field,
                offset=match["start"],
                length=match["end"] - match["start"],
                confidence=match["confidence"],
                message_index=msg_idx,
                tool_field=tool_field,
            ))
    return findings


def _secret_map_from_text_decisions(
    text: str,
    decisions: dict[str, str],
    user_allowlist: list[dict] | None,
) -> dict[str, str]:
    """Build a replace-map for one text value, skipping ignored hashes.

    `decisions` maps `entity_hash → status`; `scan_text` findings whose
    hash lands in `ignored` are dropped. Capture-group-style patterns
    (env_secret, etc.) keep their inner-group expansion so byte-
    equivalent output to `_build_redaction_set` is preserved when all
    statuses are open/accepted.
    """
    secret_map: dict[str, str] = {}
    raw_matches = scan_text(text, user_allowlist=user_allowlist)
    # Same dedupe the scan path uses — keeps entity-level decisions
    # coherent: if a user ignored the longer match, the overlapping
    # shorter one can't be picked up by the redact-for-safety path.
    for finding in _dedupe_overlapping_matches(raw_matches):
        matched = finding["match"]
        entity_hash = hash_entity(matched)
        status = decisions.get(entity_hash)
        if status == "ignored":
            continue
        placeholder = SECRET_PLACEHOLDER.get(finding["type"], REDACTED)
        secret_map.setdefault(matched, placeholder)
        for _name, pattern in SECRET_PATTERNS:
            if _name == finding["type"]:
                inner = pattern.search(text[finding["start"]:finding["end"]])
                if inner and inner.lastindex:
                    secret_map.setdefault(inner.group(inner.lastindex), placeholder)
                break
    return secret_map


def apply_findings_to_blob(
    blob: dict,
    conn: sqlite3.Connection,
    session_id: str,
    *,
    user_allowlist: list[dict] | None = None,
    max_passes: int = 3,
    include_trufflehog: bool = True,
) -> tuple[dict, int]:
    """Re-scan the blob, apply decisions from the `findings` table.

    For each match, compute salted hash and look up status in
    `findings` by `(session_id, entity_hash)`. `ignored` → leave alone;
    `open`/`accepted` → replace with the engine placeholder. Both the
    `regex_secrets` and `regex_pii` engines are applied; their replace
    maps merge before the actual substitution pass. Passes sweep the
    full blob repeatedly (bounded by `max_passes`) to catch secrets
    revealed by earlier replacements. Byte-equivalent to `redact_session`
    when only secrets are present and every decision is open/accepted.
    """
    # Decisions are engine-agnostic at the apply step — same hash, same
    # answer. The pipeline guarantees that hashes are unique per
    # (session, entity), so collapsing to a single dict is safe.
    decision_rows = conn.execute(
        "SELECT entity_hash, status FROM findings WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    decisions: dict[str, str] = {row["entity_hash"]: row["status"] for row in decision_rows}

    # Lazy import to avoid pii.py → secrets.py import cycle.
    from .betterleaks import betterleaks_secret_map_from_blob
    from .pii import pii_secret_map_from_text_decisions

    # Betterleaks is always part of the local redaction pass. TruffleHog is
    # optional here so the Share path can defer it to its mandatory final
    # package gate. Each enabled engine runs once on the original blob rather
    # than inside the per-pass loop.
    trufflehog_map: dict[str, str] = {}
    if include_trufflehog:
        from .trufflehog import trufflehog_secret_map_from_blob
        trufflehog_map = trufflehog_secret_map_from_blob(blob, decisions, user_allowlist)
    betterleaks_map = betterleaks_secret_map_from_blob(blob, decisions, user_allowlist)

    total = 0
    for pass_num in range(max_passes):
        # Build a global replace map from every text location's current state.
        secret_map: dict[str, str] = {}
        for text, _f, _m, _tf, _wk, _wkey in _iter_text_locations(blob):
            secret_map.update(
                _secret_map_from_text_decisions(text, decisions, user_allowlist)
            )
            secret_map.update(
                pii_secret_map_from_text_decisions(text, decisions, user_allowlist)
            )
        # Also include matches found inside judge-generated free-text fields
        # — entities that survive in paraphrases need redaction even when
        # the original message text has already been cleaned in a prior pass.
        for ai_text in _iter_ai_text_strings(blob):
            secret_map.update(
                _secret_map_from_text_decisions(ai_text, decisions, user_allowlist)
            )
            secret_map.update(
                pii_secret_map_from_text_decisions(ai_text, decisions, user_allowlist)
            )
        secret_map.update(_collect_infrastructure_secret_map(blob))
        secret_map.update(trufflehog_map)
        secret_map.update(betterleaks_map)
        # Note on loop termination: once an engine map is non-empty the
        # `not secret_map` guard never fires, so the pass loop now
        # relies on the `pass_count == 0 and pass_num > 0` guard below
        # to exit. That guard still works because the second pass's
        # `_apply_redaction_set` finds nothing to replace (the raws are
        # already gone from the blob) and returns 0.
        if not secret_map:
            break

        pass_count = 0
        for field in ("display_title", "project", "git_branch"):
            val = blob.get(field)
            if isinstance(val, str) and val:
                blob[field], n = _apply_redaction_set(val, secret_map)
                pass_count += n

        for msg in blob.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            for field in ("content", "thinking"):
                val = msg.get(field)
                if isinstance(val, str) and val:
                    msg[field], n = _apply_redaction_set(val, secret_map)
                    pass_count += n
            for tool_use in msg.get("tool_uses", []) or []:
                if not isinstance(tool_use, dict):
                    continue
                for branch in ("input", "output"):
                    val = tool_use.get(branch)
                    if val is None:
                        continue
                    tool_use[branch], n = _apply_to_value(val, secret_map)
                    pass_count += n
            pass_count += _apply_widened_message_fields(msg, secret_map)

        pass_count += _apply_redaction_set_to_ai_text(blob, secret_map)

        total += pass_count
        if pass_count == 0 and pass_num > 0:
            break

    return blob, total


# Field names walked by AI-text redaction helpers. These constants are the
# single source of truth — both _iter_ai_text_strings /
# _apply_redaction_set_to_ai_text in this module and _apply_to_ai_text in
# workbench/index.py consume them, so adding a new judge-emitted field only
# requires extending these lists.
AI_TEXT_TOP_FIELD = "ai_learning_summary"
AI_TEXT_DETAIL_FIELD = "ai_scoring_detail"
AI_TEXT_DETAIL_STR_FIELDS: tuple[str, ...] = (
    # The judge writes "reasoning" (not "reason") into the detail blob —
    # see ``scoring.score_session``'s detail_data construction.
    "reasoning",
    "display_title",
    "summary",
    "ai_learning_summary",
)
AI_TEXT_DETAIL_LIST_FIELDS: tuple[str, ...] = (
    "ai_failure_evidence",
    "project_areas",
)


def _iter_ai_text_strings(blob: dict) -> Iterable[str]:
    """Yield every judge-generated free-text string in ``blob``.

    Covers the top-level ``ai_learning_summary`` column plus the inner
    str / list-of-str fields inside the JSON-encoded ``ai_scoring_detail``
    blob, per the ``AI_TEXT_*`` constants above.
    """
    val = blob.get(AI_TEXT_TOP_FIELD)
    if isinstance(val, str) and val:
        yield val

    raw = blob.get(AI_TEXT_DETAIL_FIELD)
    if not isinstance(raw, str) or not raw:
        return
    try:
        detail = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(detail, dict):
        return

    for field in AI_TEXT_DETAIL_STR_FIELDS:
        value = detail.get(field)
        if isinstance(value, str) and value:
            yield value

    for list_field in AI_TEXT_DETAIL_LIST_FIELDS:
        items = detail.get(list_field)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str) and item:
                    yield item


def _apply_redaction_set_to_ai_text(
    blob: dict,
    secret_map: dict[str, str],
) -> int:
    """Apply ``secret_map`` redactions to judge-generated free-text fields.

    The judge sees only anonymized message content but can paraphrase or
    quote secrets/PII back into its summary, evidence list, or learning
    summary. Findings detected from message text are applied here so the
    paraphrase is redacted too. Mirrors the AI-text walk in
    ``index._apply_to_ai_text`` but applies the secret_map instead of a
    free transform.
    """
    total = 0

    val = blob.get(AI_TEXT_TOP_FIELD)
    if isinstance(val, str) and val:
        blob[AI_TEXT_TOP_FIELD], n = _apply_redaction_set(val, secret_map)
        total += n

    raw = blob.get(AI_TEXT_DETAIL_FIELD)
    if not isinstance(raw, str) or not raw:
        return total
    try:
        detail = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return total
    if not isinstance(detail, dict):
        return total

    for field in AI_TEXT_DETAIL_STR_FIELDS:
        value = detail.get(field)
        if isinstance(value, str) and value:
            detail[field], n = _apply_redaction_set(value, secret_map)
            total += n

    for list_field in AI_TEXT_DETAIL_LIST_FIELDS:
        items = detail.get(list_field)
        if isinstance(items, list):
            new_items: list = []
            for item in items:
                if isinstance(item, str) and item:
                    redacted, n = _apply_redaction_set(item, secret_map)
                    total += n
                    new_items.append(redacted)
                else:
                    new_items.append(item)
            detail[list_field] = new_items

    blob[AI_TEXT_DETAIL_FIELD] = json.dumps(detail)
    return total
