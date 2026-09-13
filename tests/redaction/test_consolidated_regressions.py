"""Concrete output regressions from the consolidated 59698d7 review."""
import json

import pytest

from clawjournal.findings import apply_findings_to_text
from clawjournal.redaction import code_context as cc, pii, secrets
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
    result, _, _ = index.apply_share_redactions(conn, {'session_id': 'synthetic-consolidated',
        'messages': [{'role': 'assistant', 'content': text}]})
    return result['messages'][0]['content']


@pytest.mark.parametrize('newline', ['\n', '\r\n'], ids=['lf', 'crlf'])
@pytest.mark.parametrize('quote', ['"', "'", '"""', "'''"], ids=['double', 'single', 'triple-double', 'triple-single'])
@pytest.mark.parametrize('prefix', ['', 'r', 'b', 'f'])
def test_prose_cannot_hide_opening_of_a_string_containing_a_fence(conn, newline, quote, prefix):
    text = ('I ran the "fix script for C:\\' + newline + 'HELP = ' + prefix + quote + 'x \\' + newline
            + '```python' + newline + 'from clients import payments' + newline
            + 'balance = payments.internal()' + newline + '```' + newline + quote + newline)
    assert not cc.code_context(text).protected
    assert share(conn, text) == text.replace('payments.internal', '[REDACTED_URL]')


@pytest.mark.parametrize('newline', ['\n', '\r\n'], ids=['lf', 'crlf'])
@pytest.mark.parametrize('opening,closing', [('```python','```'), ('~~~py','~~~'), ('````python','````')])
def test_unambiguous_fences_keep_real_code_and_redact_quoted_hosts(conn, newline, opening, closing):
    text = newline.join(['Example:', opening, 'obj = object()', 'obj.local()', 'host = "obj.local"', closing])
    assert cc.code_context(text).protected
    assert share(conn, text) == text.replace('"obj.local"', '"[REDACTED_URL]"')


@pytest.mark.parametrize('prefix', [
    '请发送到我的工作邮箱X地址',
    '请把第2批测试报告发送到我的工作邮箱',
    '关于Q3季度的财务报告请发送到我们财务部门的公共邮箱并抄送给张经理',
    '这份PR225报告请发送到团队的工作邮箱' * 100,
    'メールAPIの連絡先は', 'ﾒｰﾙAPIの連絡先は', 'ﾒｰﾙｱﾄﾞﾚｽ',
    '이메일API주소는', 'กรุณาติดต่อAPIที่',
], ids=['ascii-letter','digit','long-clause','very-long','japanese','halfwidth-mixed','halfwidth','korean','thai'])
@pytest.mark.parametrize('wrapper', ['plain', 'json', 'python-string'])
@pytest.mark.parametrize('address', ['alice@example.com', 'a@example.com', 'alice%40example.com'])
def test_mixed_prose_preserves_exact_text_on_both_sides(conn, prefix, wrapper, address):
    value = prefix + address + '谢谢'
    text = value if wrapper == 'plain' else (json.dumps({'note':value},ensure_ascii=False) if wrapper=='json' else 'msg = "' + value + '"')
    expected = text.replace(address, '[REDACTED_EMAIL]')
    assert share(conn, text) == expected
    assert apply_findings_to_text(text, pii._content_findings_for_text('test',0,'content',text))[0] == expected
    secrets.redact_text(text, strict=True)


@pytest.mark.parametrize('address', ['用户alice@audit.test', '用户.alice@audit.test', 'ツ-test@audit.test', 'ñoñó@audit.test', '"用户alice"@audit.test'])
def test_explicit_mailbox_delimiters_keep_complete_unicode_addresses(conn, address):
    text = '这是API报告，请发送到<' + address + '>谢谢'
    assert share(conn, text) == '这是API报告，请发送到<[REDACTED_EMAIL]>谢谢'


@pytest.mark.parametrize('lead', ['Bearer ', '--token '])
def test_partial_overlap_cannot_leave_assignment_value(conn, lead):
    value = 'hunter2hunter2'
    text = lead + 'AWS_SECRET_ACCESS_KEY="' + value + '"'
    assert value not in secrets.redact_text(text)[0]
    blob = {'messages': [{'content': text}, {'content': 'known value: ' + value}]}
    result, _ = secrets.apply_findings_to_blob(blob, conn, 'synthetic')
    assert value not in json.dumps(result)


@pytest.mark.parametrize('join', ['', '.', '-', '_', 'Q'])
def test_known_secret_glued_to_next_assignment_stays_redacted(join):
    value = 'hunter2hunter2hunter2'
    blob = {'messages': [{'content': 'DB_PASSWORD=' + value},
                         {'content': value + join + 'MY_TOKEN=abcdefgh'}]}
    result, _, _ = secrets.redact_session(blob)
    assert value not in json.dumps(result)
    assert 'MY_TOKEN=' in result['messages'][1]['content']


@pytest.mark.parametrize('parameter,entity,rule', [
    ('ip', '10.0.0.7', 'private_ip_10'), ('host', 'prod-db.local', 'internal_tld_host'),
    ('token', '1234567890:AAHfSomeTokenValueThatIsLongEnough_123456', 'telegram_bot_token'),
])
def test_query_email_does_not_swallow_an_independent_entity(conn, parameter, entity, rule):
    text = f'curl "http://ops/api?{parameter}={entity}&owner=alice@corp.example.com"'
    matches = pii._dedupe_overlapping_pii(pii.scan_text_for_pii(text))
    assert any(m['rule'] == rule and m['match'] == entity for m in matches)
    blob = {'messages': [{'content': text}, {'content': 'other copy: ' + entity}]}
    result, _ = secrets.apply_findings_to_blob(blob, conn, 'synthetic')
    assert entity not in json.dumps(result)
    assert 'alice@corp.example.com' not in json.dumps(result)
    assert 'http://ops/api' in result['messages'][0]['content']


def test_personal_host_partial_overlap_keeps_whole_telegram_token(conn):
    token = '1234567890:AAHfSomeTokenValueThatIsLongEnough_123456'
    text = '設定kais-macbook-pro' + 'A' * 30 + token
    assert token.split(':')[1] not in share(conn, text)


@pytest.mark.parametrize('value', ['hunter2', 'glpat-' + 'Ab0129zx' * 80, 'X9a' * 220])
def test_explicit_url_credentials_have_complete_boundaries(conn, value):
    text = 'git clone https://svc:' + value + '@git.audit.test/project.git'
    for result in (secrets.redact_text(text)[0], secrets.redact_text(text, strict=True)[0], share(conn, text)):
        assert value not in result
        assert 'git clone https://' in result
        assert '/project.git' in result


def test_many_url_credentials_do_not_exempt_neighbouring_real_emails():
    urls = [f'https://user{i}:fictional-password-{i}@git.audit.test/repo' for i in range(400)]
    text = 'before@audit.test\n' + '\n'.join(urls) + '\nafter@audit.test'
    result = secrets.redact_text(text, strict=True)[0]
    assert result == '[REDACTED_EMAIL]\n' + '\n'.join(
        'https://[REDACTED_CREDENTIAL]/repo' for _ in urls
    ) + '\n[REDACTED_EMAIL]'


@pytest.mark.parametrize('word', ['into', 'the', 'keys', 'with'])
def test_ssh_prose_does_not_create_global_word_redactions(conn, word):
    text = f'You can ssh {word} the box. Later {word} appears again.'
    assert share(conn, text) == text


@pytest.mark.parametrize('text', ['ssh private-box', '$ ssh private-box', 'Run ssh alice@private-box', 'Try ssh db.local'])
def test_explicit_ssh_destinations_still_redact(conn, text):
    assert 'private-box' not in share(conn, text)
    assert 'db.local' not in share(conn, text)


@pytest.mark.parametrize('other', ['malice@corp.com', 'alice@corp.commerce.example', 'alice@corp.com.uk'])
def test_known_email_does_not_corrupt_a_different_address(other):
    finding = {'entity_type': 'email', 'entity_text': 'alice@corp.com', 'confidence': .9, 'source': 'rule'}
    text = '请联系alice@corp.com，谢谢。 Other: ' + other
    assert apply_findings_to_text(text, [finding])[0] == '请联系[REDACTED_EMAIL]，谢谢。 Other: ' + other


def test_only_confirmed_truncated_identifiers_propagate_to_bare_copies():
    from clawjournal.findings import hash_entity
    text = 'contact jane.doe@ then ping jane.doe about the invoice'
    open_map = pii.pii_secret_map_from_text_decisions(text, {}, None)
    accepted_map = pii.pii_secret_map_from_text_decisions(text, {hash_entity('jane.doe'): 'accepted'}, None)
    assert secrets._apply_redaction_set(text, open_map)[0] == 'contact [REDACTED_EMAIL]@ then ping jane.doe about the invoice'
    assert secrets._apply_redaction_set(text, accepted_map)[0] == 'contact [REDACTED_EMAIL]@ then ping [REDACTED_EMAIL] about the invoice'


def test_assignment_placeholders_reach_a_fixed_point(conn):
    blob = {'messages': [{'content': 'API_KEY="sk-ant-api03-' + 'A' * 44 + '"\nDB_PASSWORD=hunter2hunter2hunter2'}]}
    result, count = secrets.apply_findings_to_blob(blob, conn, 'synthetic')
    assert count == 2
    assert secrets.apply_findings_to_blob(result, conn, 'synthetic')[1] == 0


def test_hint_character_budget_prevents_tokenization(monkeypatch):
    assert cc._MAX_PARSE_CHARS == 65_536
    text = 'obj = object()\nobj.local()\n' + '#' * 65536
    monkeypatch.setattr(cc, '_parse', lambda *_: pytest.fail('Oversized field reached parser'))
    monkeypatch.setattr(cc, '_fenced_sources', lambda *_: pytest.fail('Oversized field reached tokenizer'))
    assert not cc.code_context(text).protected


def test_warning_policy_is_unchanged_while_parser_runs(monkeypatch):
    import warnings
    observed = []
    def probe(source):
        observed.append(list(warnings.filters))
        return None
    monkeypatch.setattr(cc, '_ast_parse', probe)
    before = list(warnings.filters)
    cc.code_context('obj = object()\nobj.local()')
    assert observed and all(filters == before for filters in observed)


def test_explicit_surrogate_in_a_candidate_never_raises_encoding_error():
    from clawjournal.redaction.boundaries import ensure_safe_replacement
    ensure_safe_replacement('"a\ud800b"@audit.test', 'email')


@pytest.mark.parametrize('address', ['noreply%40audit.test', 'no-reply&#64;audit.test', 'noreply\\u0040audit.test'])
def test_encoded_no_reply_addresses_keep_original_skip_policy(address):
    assert not [m for m in pii.scan_text_for_pii(address) if m['type'] == 'email']
    assert not [m for m in pii._content_findings_for_text('synthetic', 0, 'content', address) if m['entity_type'] == 'email']


def test_dispatch_test_exercises_the_linear_adapter_after_cache_eviction(monkeypatch):
    import re
    pattern = re.compile(pii._EMAIL_PATTERN.pattern, pii._EMAIL_PATTERN.flags)
    for n in range(1000):
        re.compile(f'unrelated-{n}')
    used = []
    original = pii._separator_matches
    def track(*args, **kwargs):
        used.append(True)
        yield from original(*args, **kwargs)
    monkeypatch.setattr(pii, '_separator_matches', track)
    assert list(pii._content_matches(pattern, 'A' * 1000 + ' alice@b.com'))
    assert used
