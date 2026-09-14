"""Reproductions from the 3a7e2f5 review, with exact retained-text controls."""
import hashlib
import json
import re
import sqlite3
import subprocess
import sys

import pytest

from clawjournal.findings import apply_findings_to_text
from clawjournal.redaction import code_context as cc, pii, secrets
from clawjournal.redaction.boundaries import RedactionBoundaryError, ensure_text_boundaries
from clawjournal.scoring.badges import compute_all_badges
from clawjournal.workbench import index


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(index, 'INDEX_DB', tmp_path / 'index.db')
    monkeypatch.setattr(index, 'BLOBS_DIR', tmp_path / 'blobs')
    monkeypatch.setattr(index, 'CONFIG_DIR', tmp_path / 'config')
    for engine in ('betterleaks', 'trufflehog'):
        monkeypatch.setattr(f'clawjournal.redaction.{engine}.{engine}_secret_map_from_blob', lambda *a, **kw: {})
    result = index.open_index()
    yield result
    result.close()


def share(conn, text):
    result, _, _ = index.apply_share_redactions(conn, {'session_id': 'synthetic-review',
        'messages': [{'role': 'assistant', 'content': text}]})
    return result['messages'][0]['content']


@pytest.mark.parametrize('before,after', [
    ('请发送到我的工作邮箱地址', '，谢谢。'),
    ('这是我们团队目前正在使用的主要工作邮件地址请务必妥善保管', '谢谢您的配合'),
    ('連絡先は', 'までお願いします'),
    ('連絡先のメールは', 'までお願いします'),
    ('这是普通正文' * 1000, '这是普通结尾' * 1000),
    ('이메일주소는', '입니다'),
    ('กรุณาติดต่อ', 'ขอบคุณ'),
], ids=['chinese', 'chinese-long', 'japanese', 'japanese-mail', 'long-prose', 'korean', 'thai'])
@pytest.mark.parametrize('address', ['alice@example.com', 'a@example.com', 'alice%40example.com', r'alice\u0040example.com'])
def test_prose_script_transitions_preserve_both_sides(conn, before, after, address):
    text = before + address + after
    expected = before + '[REDACTED_EMAIL]' + after
    assert share(conn, text) == expected
    assert apply_findings_to_text(text, pii._content_findings_for_text('test', 0, 'content', text))[0] == expected
    assert all(text[m['start']:m['end']] == m['match'] for m in pii.scan_text_for_pii(text))


@pytest.mark.parametrize('address', [
    '用户@例子.公司', 'user@例子.test', 'ñoñó@audit.test', 'δοκιμή@audit.test',
    'ツ-test@audit.test', '用户.alice@audit.test', 's\u0323\u0307@audit.test',
    '"用户alice"@audit.test', '用户alice@audit.test', 'alice@example.公司',
])
def test_delimited_international_mailboxes_still_redact_completely(conn, address):
    assert share(conn, '普通正文<' + address + '>普通结尾') == '普通正文<[REDACTED_EMAIL]>普通结尾'


@pytest.mark.parametrize('padding', [0, 17000, 65535, 65536, 65537, 181000])
@pytest.mark.parametrize('fence', ['```python', '~~~python', '````python', '``` python', ''])
def test_unbound_host_call_is_detected_at_all_field_sizes(conn, padding, fence):
    code = 'h = api01.internal()\n'
    text = (fence + '\n' + code + ('~~~~' if fence.startswith('~') else '````') + '\n') if fence else code
    text += '\n' + json.dumps({'data': 'x' * padding})
    assert any(m['match'] == 'api01.internal' for m in pii.scan_text_for_pii(text))
    assert share(conn, text) == text.replace('api01.internal', '[REDACTED_URL]')


@pytest.mark.parametrize('source', [
    'obj = object()\nobj.local()\n',
    'def f(obj):\n    return obj.local()\n',
    'def f(*, obj):\n    return obj.local()\n',
    'obj = Client()\nx = obj.local("obj.local")\n',
    'import threading\nx = threading.local()\n',
    'import threading as obj\nx = obj.local("obj.local")\n',
])
def test_bound_receiver_preserves_calls_but_never_literals(conn, source):
    assert share(conn, source) == source.replace('"obj.local"', '"[REDACTED_URL]"')


@pytest.mark.parametrize('prefix', [
    '', 'obj = "a hostname"\n', 'obj = Client()\nobj = "a hostname"\n',
    'if flag:\n    obj = Client()\n', 'def f(obj):\n    pass\n',
    'obj = Client()\ndel obj\n', 'obj = Client()\nif flag:\n    obj = unknown\n',
    'obj = Client()\n(obj := "hostname")\n',
    'from project import *\n',
])
def test_unbound_rebound_and_other_scope_receivers_do_not_hide_hosts(conn, prefix):
    text = prefix + 'result = obj.internal()\n'
    assert share(conn, text).endswith('result = [REDACTED_URL]()\n')


def test_assignment_expression_cannot_reuse_stale_receiver_evidence(conn):
    text = 'obj = Client()\nresult = ((obj := "hostname"), obj.internal())\n'
    assert share(conn, text) == text.replace('obj.internal', '[REDACTED_URL]')


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('quote', ['"', "'"])
def test_continued_quoted_data_cannot_authorize_embedded_fence(conn, newline, quote):
    text = ('HELP = ' + quote + 'Example: \\' + newline + '```python' + newline
            + 'obj = object()' + newline + 'conn = obj.internal()' + newline
            + '```' + newline + quote + newline)
    assert not cc.code_context(text).protected
    assert share(conn, text) == text.replace('obj.internal', '[REDACTED_URL]')


@pytest.mark.parametrize('large', [False, True])
def test_oversized_host_policy_does_not_depend_on_optional_hints(conn, large):
    name = 'a' * 70
    text = name + ' = object()\nh = ' + name + '.internal()\nvalues = [' + '0,' * (40000 if large else 1) + ']\n'
    if not large:
        assert cc.code_context(text).protected  # Real proof that the hint path is covered.
    else:
        assert not cc.code_context(text).protected
    with pytest.raises(RedactionBoundaryError, match='internal_tld_host'):
        share(conn, text)
    assert share(conn, text.replace(name + '.internal', 'synthetic.local'))


def test_missing_hint_keeps_ordinary_large_json_shareable(conn, monkeypatch):
    text = json.dumps({'values': list(range(20000)), 'email': 'alice@audit.test'})
    monkeypatch.setattr(cc, '_ast_parse', lambda *a: None)
    assert share(conn, text) == text.replace('alice@audit.test', '[REDACTED_EMAIL]')


@pytest.mark.parametrize('padding', [0, 70000])
def test_long_fine_grained_pat_head_is_not_retained_locally(padding):
    token = 'github_pat_' + 'Synthetic0123456789_' * 32
    prefix = 'ordinary ' * (padding // 9)
    text = prefix + 'git clone https://' + token + '@git.audit.test/project.git'
    result, count, log = secrets.redact_text(text)
    assert result == prefix + 'git clone https://[REDACTED_GITHUB_TOKEN]@[REDACTED_URL]/project.git'
    assert count == 2
    assert any(entry['type'] == 'github_token' and entry['original_length'] == len(token) for entry in log)
    assert token[:40] not in result


@pytest.mark.parametrize('failure', ['missing-interpreter', 'fd-exhaustion', 'timeout'])
def test_deterministic_scan_and_batch_store_need_no_worker(conn, monkeypatch, failure):
    text = '<aaa@invalid> ordinary words ' * 11000 + 'alice@audit.test'
    monkeypatch.setattr(sys, 'executable', '/no-such-clawjournal-interpreter')
    def fail(*a, **kw):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('synthetic worker', 30)
        raise OSError('synthetic unavailable interpreter or descriptors')
    monkeypatch.setattr(subprocess, 'run', fail)
    expected = text.replace('alice@audit.test', '[REDACTED_EMAIL]')
    assert secrets.redact_text(text)[0] == expected
    assert secrets.redact_text(text, strict=True)[0] == expected
    assert pii.scan_text_for_pii(text)
    rows = [{'session_id': sid, 'source': 'claude', 'project': 'synthetic',
             'messages': [{'role': 'user', 'content': content}]} for sid, content in [('long', text), ('next', 'ordinary')]]
    assert compute_all_badges(rows[0])['risk_badges']
    assert index.upsert_sessions(conn, rows) == 2
    assert {r['session_id'] for r in conn.execute('SELECT session_id FROM sessions')} == {'long', 'next'}
    assert share(conn, text) == expected


def test_warning_scopes_are_serial_and_tokenizer_is_covered():
    # Run outside pytest's warning interception: stderr and the restored
    # operator filter are both part of the production contract.
    code = r'''
import warnings, time
from concurrent.futures import ThreadPoolExecutor
from clawjournal.redaction import code_context as cc
from clawjournal.redaction.code_context import code_context
from clawjournal.redaction.pii import scan_text_for_pii
from clawjournal.redaction.secrets import scan_text
samples = [r'value = "\q"', r'p = f"C:\Users\{who}"', r'v = f"\q{n}"']
original = cc._ast_parse
def slow_parse(source):
    time.sleep(0.0005)  # Force overlapping warning scopes if the lock is removed.
    return original(source)
cc._ast_parse = slow_parse
for source in samples:
    code_context(source)
warnings.simplefilter('error', SyntaxWarning)
before = list(warnings.filters)
def work(i):
    for j in range(20):
        scan_text_for_pii(samples[(i+j) % len(samples)])
        scan_text(samples[(i+j) % len(samples)])
with ThreadPoolExecutor(max_workers=16) as pool:
    list(pool.map(work, range(32)))
assert warnings.filters == before
try:
    warnings.warn('operator-warning', SyntaxWarning)
except SyntaxWarning:
    pass
else:
    raise AssertionError('Operator warning policy was lost')
'''
    result = subprocess.run([sys.executable, '-Walways', '-c', code], check=True, timeout=30, capture_output=True, text=True)
    assert result.stderr == ''
