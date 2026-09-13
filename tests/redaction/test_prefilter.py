"""Literal rejection must preserve complete deterministic detection results."""
import concurrent.futures
import random
import re
import subprocess
import sys

import pytest

from clawjournal.redaction import pii, prefilter, secrets

TOKEN = 'AbCdEf0123456789_-' * 3
ALNUM = 'AbCdEf0123456789' * 4
CASES = [
    ('jwt', 'eyJ' + TOKEN + '.' + TOKEN + '.' + TOKEN),
    ('jwt-partial', 'eyJ' + TOKEN),
    ('postgres', 'postgres://alice:secret123@db.internal/data'),
    ('postgresql', 'postgresql://alice:secret123@db.internal/data'),
    ('anthropic', 'sk-ant-' + TOKEN), ('openai', 'sk-' + ALNUM),
    ('huggingface', 'hf_' + ALNUM),
    *[('github-' + p, p + ALNUM) for p in ('ghp_', 'gho_', 'ghs_', 'ghr_', 'github_pat_')],
    ('pypi', 'pypi-' + TOKEN), ('npm', 'npm_' + ALNUM),
    *[('stripe-' + p + v, p + '_' + v + '_' + ALNUM)
      for p in ('sk', 'pk', 'rk') for v in ('live', 'test')],
    ('webhook', 'whsec_' + ALNUM), ('aws', 'AKIA' + 'A1' * 8),
    ('aws-secret', 'aws_secret_access_key="' + ALNUM[:40] + '"'),
    ('aws-secret-unicode-case', 'ſecret_Key="' + ALNUM[:40] + '"'),
    *[('slack-' + c, 'xox' + c + '-' + ALNUM) for c in 'bpsa'],
    ('discord', 'https://discord.com/api/webhooks/123456/' + TOKEN),
    ('discordapp', 'http://discordapp.com/api/webhooks/١٢٣٤٥٦/' + TOKEN),
    ('private-key', '-----BEGIN RSA PRIVATE KEY-----\r\nsynthetic\r\n-----END EC PRIVATE KEY-----'),
    ('private-key-unclosed', '-----BEGIN PRIVATE KEY-----' * 20),
    ('cli-flag', '--ACCESS-TOKEN\u2003' + TOKEN),
    ('env-secret', 'API_KEY="' + TOKEN + '"'),
    ('generic-secret', 'apİ_Key: "' + ALNUM + '"'),
    ('bearer-unicode-space', 'Bearer\u00a0' + TOKEN),
    ('bearer-ascii-control-space', 'Bearer\x1c' + TOKEN),
    ('query', '?ACCESS_TOKEN=' + ALNUM), ('query-ampersand', '&key=' + ALNUM),
    ('email', 'alice@example.com'), ('email-short', 'a@b.com'), ('email-partial', 'alice@\n'),
    ('email-chinese', '请发送到我的工作邮箱地址alice@example.com，谢谢。'),
    ('email-encoded', 'alice%40example.com'), ('email-quoted', '"中文alice"@example.com'),
    ('high-entropy', '"' + ALNUM + '"'),
    ('github-url', 'https://github.com/privatealice/private-repo'),
    ('github-raw', 'https://raw.githubusercontent.com/privatealice/repo'),
    ('telegram', '12345678:' + TOKEN), ('telegram-unicode', '١٢٣٤٥٦٧٨:' + TOKEN),
    ('telegram-encoded', '12345678%3A' + TOKEN),
    ('telegram-named', 'telegram_token="' + ALNUM + '"'),
    *[('hostname-' + kind, 'alice-' + kind + '-01')
      for kind in ('macbook', 'imac', 'laptop', 'desktop', 'pc', 'workstation', 'server')],
    ('home-mac', '/Users/alice/projects/private'), ('home-linux', '/home/alice/projects/private'),
    ('ip10', '10.12.34.56'), ('ip172', '172.16.23.45'), ('ip192', '192.168.1.23'),
    ('ip-public', '91.198.174.192'), ('mac-colon', 'ab:cd:ef:01:23:45'),
    ('mac-hyphen', 'ab-cd-ef-01-23-45'), ('ssn', '123-45-6789'), ('phone', '+1-202-555-0174'),
    *[('suffix-' + suffix, 'api01.' + suffix) for suffix in ('local','internal','corp','lan','intranet','localnet')],
    ('host-unicode-case', 'api01.İNTERNAL'), ('host-no-suffix', 'DB_HOST="private-api"'),
    ('private-url', 'http://10.12.34.56:8000/private'),
    ('loopback-url', 'https://127.0.0.1:8080/private'),
    ('localhost-url', 'http://localhost:8080/private'),
    ('npm-scoped', '@privateorg/package'), ('package-version', 'privatepkg@1.2.3'),
    ('gitlab', 'https://gitlab.com/privatealice/repo'),
    ('bitbucket', 'https://bitbucket.org/privatealice/repo'),
    ('arn', 'arn:aws:s3:us-east-1:123456789012:bucket/private'),
    ('ecr', '123456789012.dkr.ecr.us-east-1.amazonaws.com'),
    ('gcp', 'projects/private-project'), ('card', '4111 1111 1111 1111'),
    ('ordinary', 'The ordinary report contains no sensitive values.'),
    ('code', 'import threading\nx = threading.local()'),
    ('surrogate', '\ud800😀\x00alice@example.com'),
    ('surrogate-nul-token', '\ud800😀\x00github_pat_' + ALNUM),
]


def scan_both(text):
    return secrets.scan_text(text), pii.scan_text_for_pii(text)


def baseline(monkeypatch, function, text):
    with monkeypatch.context() as patch:
        patch.setattr(prefilter, '_AUTOMATON', None)
        return function(text)


@pytest.mark.parametrize('case,value', CASES, ids=[name for name, _ in CASES])
def test_complete_findings_and_local_replacements_match_without_prefilter(case, value, monkeypatch):
    text = 'ordinary words\n' * 24 + value + '\nordinary end'
    assert prefilter._AUTOMATON is not None, 'CI must exercise the installed native extension'
    assert scan_both(text) == baseline(monkeypatch, scan_both, text)
    assert secrets.redact_text(text) == baseline(monkeypatch, secrets.redact_text, text)


def test_all_hinted_rules_have_a_positive_sample():
    values = [value for _, value in CASES]
    for pattern in prefilter._PATTERN_LITERALS:
        matching = [value for value in values if pattern.search(value)]
        assert matching, pattern.pattern
        for value in matching:
            rules = [('sample', pattern)]
            assert prefilter.filter_rules(' ' * 300 + value, rules) == rules


def test_unknown_patterns_changed_patterns_and_flags_always_run():
    original = next(p for n, p in secrets.SECRET_PATTERNS if n == 'hf_token')
    changed = re.compile(r'newprefix_[A-Za-z0-9]{20,}')
    changed_flags = re.compile(original.pattern, re.IGNORECASE)
    rules = [('old', original), ('new', changed), ('flags', changed_flags)]
    assert prefilter.filter_rules(' ' * 300 + 'HF_' + ALNUM, rules) == rules[1:]


@pytest.mark.parametrize('length', [0, 6, 255, 256, 257, 8192, 200000])
def test_threshold_and_large_absent_runs_preserve_findings(length, monkeypatch):
    text = 'A' * length + ' a@b.com'
    assert scan_both(text) == baseline(monkeypatch, scan_both, text)


def test_absent_marker_skips_regex_execution(monkeypatch):
    original = secrets._secret_matches
    calls = []
    def track(pattern, text):
        calls.append(pattern)
        return original(pattern, text)
    monkeypatch.setattr(secrets, '_secret_matches', track)
    secrets.scan_text('ordinary words ' * 100)
    assert all(p not in prefilter._PATTERN_MASKS for p in calls)
    assert calls  # Rules without a proven marker still execute.


@pytest.mark.parametrize('error', [RuntimeError, MemoryError, ValueError, OSError])
def test_failure_after_partial_marker_scan_runs_every_rule(error, monkeypatch):
    text = ' ' * 300 + 'hf_' + ALNUM + ' alice@example.com'
    expected = baseline(monkeypatch, scan_both, text)
    class Broken:
        def iter(self, text):
            yield 0, 1
            raise error('synthetic accelerator failure')
    monkeypatch.setattr(prefilter, '_AUTOMATON', Broken())
    assert scan_both(text) == expected


def test_extension_import_failure_preserves_scanning():
    code = '''
import builtins
original = builtins.__import__
def load(name, *args, **kwargs):
    if name == "ahocorasick":
        raise ImportError("synthetic missing extension")
    return original(name, *args, **kwargs)
builtins.__import__ = load
from clawjournal.redaction import prefilter, secrets
assert prefilter._AUTOMATON is None
assert any(f["type"] == "github_token" for f in secrets.scan_text(" " * 300 + "github_pat_" + "Ab12" * 10))
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


def test_generated_unicode_and_adjacent_candidates_match(monkeypatch):
    rng = random.Random(225)
    alphabet = list('ab19.-_+%@:\n\r\t ="') + ['中', 'İ', 'ı', 'ſ', 'K', '١', '²', '\x00', '\ud800', '😀', '\u2003']
    for _ in range(200):
        text = ''.join(rng.choices(alphabet, k=260))
        for _ in range(3):
            pos = rng.randrange(len(text) + 1)
            text = text[:pos] + rng.choice(CASES)[1] + text[pos:]
        assert scan_both(text) == baseline(monkeypatch, scan_both, text)


def test_concurrent_fields_do_not_reuse_marker_state(monkeypatch):
    fields = [' ' * 300 + value for _, value in CASES]
    expected = [baseline(monkeypatch, scan_both, text) for text in fields]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(scan_both, fields)) == expected
