"""Complete-candidate, retained-text and linear-scanning contracts for PR #225."""
import hashlib
import json
import random
import re
import sqlite3
import subprocess

import pytest

from clawjournal.redaction import pii, secrets


def signature(matches):
    return [(m.group(), m.span(), m.groups(), tuple(m.span(i) for i in range(1, len(m.groups()) + 1)))
            for m in matches]


@pytest.mark.parametrize("value", [
    "alice@example.com", "abc@\r\n", "中文éAlice+tag@sub.example.COM，",
    "abc@x.ab1def@next.example", "aaa@bbb.ccc@ddd.example", "aaa@bbb.ccc@ ",
    "abc@foo.bar+baz@example.com", "email%tag@example.com", "o'connor@example.com",
    "12345678:" + "AbCdEf0123456789_-" * 2,
    "١٢٣٤٥٦٧٨:" + "AbCdEf0123456789_-" * 2,
    "before<one.internal.two.local>after", "db.locality db.local_中文",
    "İntranet.INTERNAL", "db.local\u00a0", "@abc@\u2003next", "abc@invalid",
])
def test_every_character_offset_preserves_original_matches(value):
    patterns = [pii._EMAIL_PATTERN, pii._TRUNCATED_EMAIL_PATTERN,
                pii._TELEGRAM_PATTERN, pii._INTERNAL_HOST_PATTERN]
    # Put every character of each sample immediately before a core boundary.
    for cut in range(len(value) + 1):
        text = " " * (128 - cut) + value + " ordinary end " * 12
        for pattern in patterns:
            assert signature(pii._content_matches(pattern, text)) == signature(pattern.finditer(text)), (value, cut)
        pattern = secrets._SECRET_EMAIL_PATTERN
        assert signature(secrets._secret_matches(pattern, text)) == signature(pattern.finditer(text)), (value, cut)


@pytest.mark.parametrize("value,pattern", [
    ("a" * 300 + "@host.test", pii._EMAIL_PATTERN),
    ("alice@" + "a." * 160 + "test", pii._EMAIL_PATTERN),
    ("abc@" + "bbb.ccc@" * 40 + " ", pii._TRUNCATED_EMAIL_PATTERN),
    ("1" * 300 + ":" + "A" * 300, pii._TELEGRAM_PATTERN),
    ("a." * 160 + "local", pii._INTERNAL_HOST_PATTERN),
])
def test_long_candidate_is_checked_complete(value, pattern, monkeypatch):
    text = "prefix " + value + " suffix"
    assert signature(pii._content_matches(pattern, text)) == signature(pattern.finditer(text))


@pytest.fixture
def builtin_conn(monkeypatch):
    for scanner in ("betterleaks", "trufflehog"):
        monkeypatch.setattr(f"clawjournal.redaction.{scanner}.{scanner}_secret_map_from_blob", lambda *a, **k: {})
    for module in (pii, secrets):
        monkeypatch.setattr(module, "hash_entity", lambda text: hashlib.sha256(text.encode()).hexdigest())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE findings (session_id TEXT, entity_hash TEXT, status TEXT)")
    yield conn
    conn.close()


@pytest.mark.parametrize("value,expected", [
    ("before<alice@example.com>after", "before<[REDACTED_EMAIL]>after"),
    ("before<alice%40example.com>after", "before<[REDACTED_EMAIL]>after"),
    (r"before<alice\u0040example.com>after", "before<[REDACTED_EMAIL]>after"),
    ("before<alice&#64;example.com>after", "before<[REDACTED_EMAIL]>after"),
    ("before<db01.internal>after", "before<[REDACTED_URL]>after"),
    ("before<12345678:" + "AbCdEf0123456789_-" * 2 + ">after", "before<[REDACTED]>after"),
    ("before<12345678%3A" + "AbCdEf0123456789_-" * 2 + ">after", "before<[REDACTED]>after"),
    ("db.locality and configuration stay", "db.locality and configuration stay"),
])
def test_full_output_preserves_ordinary_text_at_each_cut(value, expected, builtin_conn):
    for cut in range(len(value) + 1):
        before, after = " " * (128 - cut), " ordinary words " * 15
        blob, _ = secrets.apply_findings_to_blob(
            {"messages": [{"content": before + value + after}]}, builtin_conn, "chunk-test")
        assert blob["messages"][0]["content"] == before + expected + after


def test_distant_code_context_and_cross_field_credential_evidence_survive(builtin_conn):
    code = "import numpy\nimport torch\n" + "# ordinary comment\n" * 20 + "value = numpy.array@torch.tensor"
    original, _ = secrets.apply_findings_to_blob({"messages": [{"content": code}]}, builtin_conn, "chunk-code")
    assert original["messages"][0]["content"] == code
    for messages in ([{"content": 'PASSWORD="numpy.array@torch.tensor"'}, {"content": code}],
                     [{"content": code}, {"content": 'PASSWORD="numpy.array@torch.tensor"'}]):
        result, _ = secrets.apply_findings_to_blob({"messages": messages}, builtin_conn, "chunk-secret")
        assert "numpy.array@torch.tensor" not in json.dumps(result)
        assert "# ordinary comment\n" * 20 in next(m["content"] for m in result["messages"] if "import numpy" in m["content"])


def test_multiline_private_key_retains_whole_original_block(builtin_conn):
    key = "-----BEGIN RSA PRIVATE KEY-----\n" + "SYNTHETIC_BODY\n" * 80 + "-----END EC PRIVATE KEY-----"
    blob, _ = secrets.apply_findings_to_blob({"messages": [{"content": "before<" + key + ">after"}]}, builtin_conn, "chunk-key")
    assert blob["messages"][0]["content"] == "before<[REDACTED_PRIVATE_KEY]>after"


@pytest.mark.parametrize("length", [511, 512, 513])
@pytest.mark.parametrize("kind", ["email", "partial", "telegram", "host"])
def test_former_window_boundaries_preserve_original_matches(length, kind, monkeypatch):
    suffix = {"email": "@host.test", "partial": "@", "telegram": ":" + "A" * 30, "host": ".internal"}[kind]
    value = ("1" if kind == "telegram" else "a") * (length - len(suffix)) + suffix
    for start in [0, 511, 512, 513, 1023, 1024, 1025, 1535, 1536, 1537, 8191]:
        text = " " * start + value + " ordinary words " * 600
        for pattern in [pii._EMAIL_PATTERN, pii._TRUNCATED_EMAIL_PATTERN, pii._TELEGRAM_PATTERN,
                        pii._INTERNAL_HOST_PATTERN, secrets._SECRET_EMAIL_PATTERN]:
            fn = secrets._secret_matches if pattern == secrets._SECRET_EMAIL_PATTERN else pii._content_matches
            assert signature(fn(pattern, text)) == signature(pattern.finditer(text)), (kind, length, start)


def test_generated_unicode_and_adjacent_candidates_keep_original_spans(monkeypatch):
    rng = random.Random(225)
    alphabet = list("ab19.-_+%@:\n\r\t ") + ["\u00a0", "\u2003", "中", "İ", "ſ", "K", "١", "²", "\x00"]
    values = ["aaa@bbb.ccc@ddd.example", "aa@host.test", "abc@", "db.local", "host.internal.local",
              "１２３４５６７８:" + "A" * 30, "éabc@example.com中", "abc@x.ab1def@next.example"]
    for _ in range(200):
        text = "".join(rng.choices(alphabet, k=rng.randrange(150)))
        for _ in range(rng.randrange(1, 4)):
            at = rng.randrange(len(text) + 1)
            text = text[:at] + rng.choice(values) + text[at:]
        for pattern in [pii._EMAIL_PATTERN, pii._TRUNCATED_EMAIL_PATTERN, pii._TELEGRAM_PATTERN,
                        pii._INTERNAL_HOST_PATTERN, secrets._SECRET_EMAIL_PATTERN]:
            fn = secrets._secret_matches if pattern == secrets._SECRET_EMAIL_PATTERN else pii._content_matches
            assert signature(fn(pattern, text)) == signature(pattern.finditer(text))


def test_unicode_offsets_and_non_utf8_codepoints(monkeypatch):
    text = "中文😀\ud800" + " ordinary words " * 800 + "<alice@example.com>\r\n"
    assert signature(pii._content_matches(pii._EMAIL_PATTERN, text)) == signature(pii._EMAIL_PATTERN.finditer(text))


def test_many_matches_keep_all_results():
    text = "<alice@example.com> " * 10001
    assert signature(pii._content_matches(pii._EMAIL_PATTERN, text)) == signature(pii._EMAIL_PATTERN.finditer(text))


def test_ordinary_token_fields_preserve_original_matches(monkeypatch):
    text = ("123456789:" + "AbCdEf0123456789_-" * 2 + "\n") * 300
    assert signature(pii._content_matches(pii._TELEGRAM_PATTERN, text)) == signature(pii._TELEGRAM_PATTERN.finditer(text))


@pytest.mark.parametrize("address", ["a@b.com", "alice@example.com"])
def test_separated_long_run_uses_complete_candidates(address, monkeypatch):
    text = "A" * 200000 + " " + address
    matches = list(pii._content_matches(pii._EMAIL_PATTERN, text))
    expected = [] if address == "a@b.com" else [(address, (200001, len(text)))]
    assert [(m.group(), m.span()) for m in matches] == expected
    # The legacy regex has a three-character local minimum. Supplemental
    # rules must still find the short email in the reviewer's exact example.
    assert {m["match"] for m in pii.scan_text_for_pii(text) if m["type"] == "email"} == {address}


@pytest.mark.parametrize('length', [1023, 1024, 1025, 8191, 8192, 8193, 200000])
def test_absent_email_marker_never_enters_regex_search(length, monkeypatch):
    class ForbiddenPattern:
        pattern = pii._EMAIL_PATTERN.pattern
        flags = pii._EMAIL_PATTERN.flags
        def finditer(self, *a):
            pytest.fail('Marker-free text reached a regex search')
        def match(self, *a):
            pytest.fail('Marker-free text reached a regex match')
    pattern = ForbiddenPattern()
    monkeypatch.setattr(pii, '_EMAIL_PATTERN', pattern)
    assert list(pii._content_matches(pattern, 'A' * length)) == []


@pytest.mark.parametrize('pattern', [pii._EMAIL_PATTERN, pii._TRUNCATED_EMAIL_PATTERN,
                                   pii._TELEGRAM_PATTERN, pii._INTERNAL_HOST_PATTERN])
def test_pattern_dispatch_survives_regex_cache_eviction(pattern):
    re.purge()
    fresh = re.compile(pattern.pattern, pattern.flags)
    text = 'alice@example.com abc@ 123456789:' + 'A' * 32 + ' db.local'
    assert signature(pii._content_matches(fresh, text)) == signature(pattern.finditer(text))


def test_pathological_complete_candidates_have_a_deadline():
    import sys
    code = r'''
from clawjournal.redaction import pii, secrets
for n in (200000, 400000):
    for text in ('A'*n + ' a@b.com', 'a'*n + '@audit.test', '1'*n + ':' + 'A'*40,
                 'a.'*(n//2)+'internal', 'A'*n):
        list(pii._content_matches(pii._EMAIL_PATTERN, text))
        list(pii._content_matches(pii._TELEGRAM_PATTERN, text))
        list(pii._content_matches(pii._INTERNAL_HOST_PATTERN, text))
        list(secrets._secret_matches(secrets._SECRET_EMAIL_PATTERN, text))
'''
    subprocess.run([sys.executable, '-c', code], check=True, timeout=10, capture_output=True)
