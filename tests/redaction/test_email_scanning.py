"""Email coverage and long-input regression for issue #224."""
import itertools
import random
import re
import subprocess
import sys

import pytest

from clawjournal.redaction.pii import (
    _content_findings_for_text,
    _content_matches,
    scan_text_for_pii,
)


# Independent copies of the previous rules define the compatibility contract.
EMAIL_RULES = (
    r"([A-Za-z0-9_.+-]{3,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    r"([A-Za-z0-9_.+-]{3,})@(?=\s|$)",
)


@pytest.mark.parametrize(("text", "full", "truncated"), [
    ("@example.com ab@example.com", [], []),
    ("<+.-@example.com>", ["+.-@example.com"], []),
    ("中文éAlice+tag@sub.example.COM，", ["Alice+tag@sub.example.COM"], []),
    ("o'connor@example.com", ["connor@example.com"], []),
    ("abc@\r\n", [], ["abc"]),
    ("abc@\u00a0def@\u2003", [], ["abc", "def"]),
    ("@@abc@", [], ["abc"]),
    ("bad@invalid def@example.com", ["def@example.com"], []),
    ("abc@host.123 def@example.com", ["def@example.com"], []),
    ("abc@foo.bar+baz@example.com", ["abc@foo.bar", "+baz@example.com"], []),
    ("aaa@bbb.ccc_xyz@ddd.example", ["aaa@bbb.ccc", "_xyz@ddd.example"], []),
    # A second candidate cannot reuse characters consumed by a prior match.
    ("aaa@bbb.ccc@ddd.example", ["aaa@bbb.ccc"], []),
    # Separate full/truncated rules retain their independent overlap behavior.
    ("aaa@bbb.ccc@ ", ["aaa@bbb.ccc"], ["bbb.ccc"]),
    ("abc@x.ab1def@next.example", ["abc@x.ab", "1def@next.example"], []),
])
def test_email_corner_cases_keep_matches_and_redaction_offsets(text, full, truncated):
    for rule, expected in zip(EMAIL_RULES, [full, truncated]):
        pattern = re.compile(rule)
        old = list(pattern.finditer(text))
        new = list(_content_matches(pattern, text))
        assert [match.group(1) for match in old] == expected
        assert [(match.group(0), match.span(0), match.group(1), match.span(1)) for match in new] == [
            (match.group(0), match.span(0), match.group(1), match.span(1)) for match in old
        ]
    expected_emails = set(full + truncated)
    # The old adapters stay equivalent. Additional format coverage now also
    # detects short local parts and preserves the prefix before an apostrophe.
    if text == "@example.com ab@example.com":
        expected_emails.add("ab@example.com")
    if text == "o'connor@example.com":
        expected_emails.add("o'connor@example.com")
    if text == "中文éAlice+tag@sub.example.COM，":
        expected_emails.add("中文éAlice+tag@sub.example.COM")
    indexed = [match for match in scan_text_for_pii(text) if match["type"] == "email"]
    assert {match["match"] for match in indexed} == expected_emails
    assert all(text[match["start"]:match["end"]] == match["match"] for match in indexed)
    reviewed = _content_findings_for_text("synthetic", 0, "content", text)
    assert {match["entity_text"] for match in reviewed if match["entity_type"] == "email"} == expected_emails


def test_exhaustive_short_email_strings_match_previous_rules():
    # Exhaust all 349,525 strings of length 0..9 over a small alphabet. This
    # includes minimum-size full addresses, truncated ones and failed prefixes.
    patterns = [re.compile(rule) for rule in EMAIL_RULES]
    for size in range(10):
        for chars in itertools.product("a.@ ", repeat=size):
            text = "".join(chars)
            for pattern in patterns:
                old = [(match.group(0), match.span(0), match.span(1)) for match in pattern.finditer(text)]
                new = [(match.group(0), match.span(0), match.span(1)) for match in _content_matches(pattern, text)]
                assert new == old, repr(text)


@pytest.mark.parametrize("rule", EMAIL_RULES)
def test_candidate_search_preserves_previous_matches_and_offsets(rule):
    pattern = re.compile(rule)
    samples = [
        "", "no email here", "abc@example.com", "ab@example.com",
        "中文：alice+tag@sub.example.com，bob@example.org",
        "abc@ def@\nxyz@", "abc@\u2003next", "abc@x.y", "abc@x.yz!",
        "aaa@bbb.ccc@ddd.example@eee.test", "aaa@bbb.ccc.xyz@ ",
        "...abc@foo..bar..test---", "abc@example.comabc@next.example",
        "@abc@example.com", "@@@", "noreply@example.com", "ABC@EXAMPLE.COM",
    ]
    rng = random.Random(224)
    fragments = ["abc", "ab", "@", ".", "-", "+", "_", "123", " ", "\n", "中", "/"]
    samples.extend("".join(rng.choices(fragments, k=45)) for _ in range(1500))
    for text in samples:
        expected = [(m.group(1), m.span(1)) for m in pattern.finditer(text)]
        actual = [(m.group(1), m.span(1)) for m in _content_matches(pattern, text)]
        assert actual == expected, repr(text)


def test_both_pii_entry_points_keep_email_coverage():
    text = "中文 alice+tag@sub.example.com; bob@\n ABC@EXAMPLE.ORG noreply@example.com"
    expected = {"alice+tag@sub.example.com", "bob", "ABC@EXAMPLE.ORG"}
    indexed = [m for m in scan_text_for_pii(text) if m["type"] == "email"]
    assert {m["match"] for m in indexed} == expected
    assert all(text[m["start"]:m["end"]] == m["match"] for m in indexed)
    reviewed = _content_findings_for_text("synthetic", 0, "content", text)
    assert {m["entity_text"] for m in reviewed if m["entity_type"] == "email"} == expected


def test_long_text_scans_finish_and_do_not_skip_emails():
    # A subprocess deadline prevents a regression from wedging the test runner.
    # The old unanchored rules take minutes on the first input alone. Exercise
    # complete scanners, not just the candidate helper, with and without @.
    code = '''
from clawjournal.redaction.pii import scan_text_for_pii, _content_findings_for_text
run = "A" * 200_000
samples = [run, run + " abc@example.com " + run, run + "@example.com", run + "@ ", "@" * 200_000]
expected = [set(), {"abc@example.com"}, {run + "@example.com"}, {run}, set()]
for text, emails in zip(samples, expected):
    assert {m["match"] for m in scan_text_for_pii(text) if m["type"] == "email"} == emails
    assert {m["entity_text"] for m in _content_findings_for_text("synthetic", 0, "content", text) if m["entity_type"] == "email"} == emails
'''
    subprocess.run([sys.executable, "-c", code], check=True, timeout=15, capture_output=True, text=True)
