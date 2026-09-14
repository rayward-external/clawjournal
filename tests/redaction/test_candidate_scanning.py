"""Compatibility and long-input regressions for delimiter-based scanning."""
import itertools
import random
import re
import subprocess
import sys

import pytest

from clawjournal.redaction import pii, secrets


# Independent copies of the original expressions define the match contract.
TELEGRAM = re.compile(r"(\d{8,}:[A-Za-z0-9_-]{30,})")
HOST = re.compile(
    r"\b([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*"
    r"\.(?:local|internal|corp|lan|intranet|localnet))\b", re.IGNORECASE,
)
PRIVATE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    r"[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
)
KEY_TYPES = ("", "RSA ", "EC ", "DSA ", "OPENSSH ")


def signature(matches):
    return [(m.group(0), m.span(0), m.groups()) for m in matches]


def test_telegram_keeps_unicode_digits_and_nonoverlapping_spans():
    body = "A" * 30
    texts = [
        f"١٢٣٤٥٦٧٨:{body}", f"１２３４５６７８:{body}",
        f"1234567²:{body}", f"²12345678:{body}",
        f"12345678:{body}12345678:{body}",
        f"12345678:{body} 123456789:{body}",
        f"12345678:short 12345678:{body}",
        f"12345678::{body}", f"1234567:{body}",
        f"12345678:{'A' * 29}K", f"12345678:{body}:trailing",
    ]
    for text in texts:
        expected = list(TELEGRAM.finditer(text))
        assert signature(pii._content_matches(TELEGRAM, text)) == signature(expected)
        indexed = [m for m in pii.scan_text_for_pii(text) if m["rule"] == "telegram_bot_token"]
        assert [(m["match"], (m["start"], m["end"])) for m in indexed] == [
            (m.group(1), m.span(1)) for m in expected
        ]
        reviewed = pii._content_findings_for_text("synthetic", 0, "content", text)
        assert [m["entity_text"] for m in reviewed if m["reason"] == "Likely Telegram bot token"] == [
            m.group(1) for m in expected
        ]


@pytest.mark.parametrize("text", [
    "a.local.b.internal", "a.localnet", "a.local-tail", "a.local_", "a.local中",
    "中a.local", "中a.b.local", "_a.local", "-a.local", "a..b.local",
    "a.-b.local", "a-.local", "a.local..b.local", "a.local-a.b.local-tail",
    "K.İNTERNAL", "ſ.ıNTERNAL", "a.localnetworks", "a.localnet-works",
    ".local", "a.a.a..b.local", "a.local.a.a.a", "a.local_ignored.b.local",
])
def test_hostname_suffixes_preserve_greedy_matches_and_unicode_boundaries(text):
    expected = list(HOST.finditer(text))
    assert signature(pii._content_matches(HOST, text)) == signature(expected)
    actual = [m for m in pii.scan_text_for_pii(text) if m["rule"] == "internal_tld_host"]
    expected_spans = [(m.group(1), m.span(1)) for m in expected]
    extra = {
        "a.local中": ("a.local", (0, 7)),
        "中a.local": ("a.local", (1, 8)),
        "中a.b.local": ("a.b.local", (1, 10)),
    }
    if text in extra:
        expected_spans.append(extra[text])
    assert [(m["match"], (m["start"], m["end"])) for m in actual] == expected_spans


@pytest.mark.parametrize("begin_type,end_type", list(itertools.product(KEY_TYPES, repeat=2)))
def test_private_key_keeps_all_previously_matched_header_footer_pairs(begin_type, end_type):
    key = f"-----BEGIN {begin_type}PRIVATE KEY-----\nSYNTHETIC_BODY\n-----END {end_type}PRIVATE KEY-----"
    text = "before " + key + " after"
    assert signature(secrets._secret_matches(PRIVATE, text)) == signature(PRIVATE.finditer(text))
    assert [m["match"] for m in secrets.scan_text(text) if m["type"] == "private_key"] == [key]
    redacted, _, _ = secrets.redact_text(text)
    assert redacted == "before [REDACTED_PRIVATE_KEY] after"


@pytest.mark.parametrize("text", [
    "-----BEGIN PRIVATE KEY-----outer-----BEGIN RSA PRIVATE KEY-----inner-----END EC PRIVATE KEY-----tail",
    "-----END PRIVATE KEY-----before-----BEGIN PRIVATE KEY-----body-----END PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----body-----END PRIVATE KEY----------BEGIN EC PRIVATE KEY-----two-----END EC PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----body without an end",
    "-----BEGIN PRIVATE KEY-----body-----END PRIVATE KEY----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----body-----END ENCRYPTED PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----END PRIVATE KEY-----",  # shared dashes are not reusable
    "-----BEGIN PRIVATE KEY-----\\nbody\x00中文\r\n-----END PRIVATE KEY-----",
])
def test_private_key_keeps_nested_truncated_and_embedded_marker_behavior(text, monkeypatch):
    assert signature(secrets._secret_matches(PRIVATE, text)) == signature(PRIVATE.finditer(text))
    new_output = secrets.redact_text(text)
    with monkeypatch.context() as old:
        old.setattr(secrets, "_secret_matches", lambda pattern, value: pattern.finditer(value))
        old_output = secrets.redact_text(text)
    assert new_output == old_output


@pytest.mark.parametrize("kind", ["telegram", "host", "private"])
def test_seeded_delimiter_cases_match_original_rules(kind):
    rng = random.Random(225)
    if kind == "telegram":
        pattern, matcher = TELEGRAM, pii._content_matches
        parts = ["12345678", "١٢٣٤٥٦٧٨", "²", ":", "::", "A" * 29, "A" * 30, "-", "_", " "]
    elif kind == "host":
        pattern, matcher = HOST, pii._content_matches
        parts = ["a", "-", ".", "..", "_", "中", "K", ".local", ".localnet", ".İNTERNAL", " "]
    else:
        pattern, matcher = PRIVATE, secrets._secret_matches
        parts = [f"-----{action} {label}PRIVATE KEY-----" for action in ["BEGIN", "END"] for label in KEY_TYPES]
        parts += ["body", "\r\n", "\\n", "\x00", "-", " "]
    for _ in range(10000):
        text = "".join(rng.choices(parts, k=rng.randrange(1, 35)))
        assert signature(matcher(pattern, text)) == signature(pattern.finditer(text)), repr(text)


def test_exhaustive_private_key_marker_order_matches_the_original():
    markers = ["-----BEGIN PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----",
               "-----END PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----", "body"]
    for parts in itertools.product(markers, repeat=6):
        text = "".join(parts)
        assert signature(secrets._secret_matches(PRIVATE, text)) == signature(PRIVATE.finditer(text))


def test_secret_email_word_boundaries_match_the_original():
    pattern = re.compile(r"\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    rng = random.Random(22509)
    parts = ["a", "ab", ".", "%", "+", "-", "_", "@", "é", "中", " ", "audit.test", "@audit.test"]
    for _ in range(20000):
        text = "".join(rng.choices(parts, k=rng.randrange(1, 25)))
        assert signature(secrets._secret_matches(pattern, text)) == signature(pattern.finditer(text)), repr(text)


def test_other_regex_flags_keep_their_original_semantics():
    for pattern, text in [
        (re.compile(TELEGRAM.pattern, re.ASCII), "١٢٣٤٥٦٧٨:" + "A" * 30),
        (re.compile(HOST.pattern), "a.LOCAL"),
        (re.compile(PRIVATE.pattern, re.IGNORECASE), "-----begin private key-----body-----end private key-----"),
    ]:
        matcher = secrets._secret_matches if pattern.pattern == PRIVATE.pattern else pii._content_matches
        assert signature(matcher(pattern, text)) == signature(pattern.finditer(text))


def test_missing_markers_do_not_skip_other_detection_rules():
    # A token fragment can lack ':', but named secret assignments must still
    # run. These short-circuits are per rule, never whole-text safety decisions.
    text = "TELEGRAM_BOT_TOKEN=" + "AbCdEf0123456789_-" * 2
    assert ":" not in text
    assert any(m["type"] == "env_secret" for m in secrets.scan_text(text))
    text = 'private_key="' + "AbCdEf0123456789_-" * 3 + '"'
    assert "BEGIN" not in text and "END" not in text
    assert any(m["type"] == "generic_secret" for m in secrets.scan_text(text))
    text = "http://10.1.2.3/"
    assert not list(HOST.finditer(text))
    assert any(m["rule"] == "private_ip_url" for m in pii.scan_text_for_pii(text))


def test_long_negative_prefixes_finish_without_losing_later_matches():
    # Run complete built-in scanners with a deadline so a regression cannot
    # wedge pytest. No binaries, AI calls or external uploads are involved.
    code = r'''
from clawjournal.redaction import pii, secrets
body = "A" * 30
text = "1" * 200_000 + " abc 12345678:" + body
assert [m["match"] for m in pii.scan_text_for_pii(text) if m["rule"] == "telegram_bot_token"] == ["12345678:" + body]
for text, expected in [
    ("a." * 100_000, []),
    ("a." * 100_000 + ".target.local", ["target.local"]),
    ("target.local." + "a." * 100_000, ["target.local"]),
    ("a." * 100_000 + "local", ["a." * 100_000 + "local"]),
]:
    assert [m["match"] for m in pii.scan_text_for_pii(text) if m["rule"] == "internal_tld_host"] == expected
begin = "-----BEGIN PRIVATE KEY-----"
text = (begin + "\n") * 8_000
assert not [m for m in secrets.scan_text(text) if m["type"] == "private_key"]
text += "-----END RSA PRIVATE KEY-----"
assert [m["match"] for m in secrets.scan_text(text) if m["type"] == "private_key"] == [text]
'''
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10, capture_output=True, text=True)
