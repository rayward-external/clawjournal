"""Credential and retained-text contracts from the e419ee3 review."""
import copy

import pytest

from clawjournal.redaction import secrets
from clawjournal.workbench import index


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(index, 'INDEX_DB', tmp_path / 'index.db')
    monkeypatch.setattr(index, 'BLOBS_DIR', tmp_path / 'blobs')
    monkeypatch.setattr(index, 'CONFIG_DIR', tmp_path / 'config')
    for engine in ('betterleaks', 'trufflehog'):
        monkeypatch.setattr(f'clawjournal.redaction.{engine}.{engine}_secret_map_from_blob', lambda *a, **kw: {})
    with index.open_index() as result:
        yield result
    result.close()


def trace(*texts):
    return {'session_id': 'review-e419ee3', 'source': 'codex', 'project': 'synthetic',
            'messages': [{'role': 'user', 'content': text} for text in texts],
            'stats': {'user_messages': len(texts)}}


HOSTS = ('github.com', 'localhost:8080', 'app.acme.io', 'router.acme.io',
         'server.acme.io', 'mcp.acme.io', 'tasks.acme.io', 'pytest.acme.io',
         'anthropic.com', 'example.com', 'users.noreply.github.com',
         '192.168.1.9', '10.2.3.4', '172.17.2.3', '8.8.8.8', '1.1.1.1',
         'git.audit.test', 'buildbox', '[2001:db8::1]:8443')
CREDENTIALS = ('a3b7c9d2e4f6a8b0c1d3e5f7a9b2c4d6e8f0a1b3',
               'Zq7hunter2SuperSecretKey', 'admin%3Ahunter2', 'abcdef0123456789',
               'alllowercaseopaquetoken', 'svc:fictional-password')


@pytest.mark.parametrize('path', ['text', 'session', 'share'])
def test_email_and_ip_exceptions_never_authorize_url_credentials(conn, path):
    # Group the matrix under three short test IDs. A changed destination or
    # an email exception must never change whether userinfo is confidential.
    for host in HOSTS:
        for credential in CREDENTIALS:
            text = f'connect https://{credential}@{host}/v1 done.'
            allowlist = [{'type': 'category', 'match_type': 'email'},
                         {'type': 'exact', 'text': f'{credential}@{host}'}]
            if path == 'text':
                result = secrets.redact_text(text, user_allowlist=allowlist, strict=True)[0]
                expected_host = '[REDACTED_URL]' if host == 'git.audit.test' else host
                assert result == f'connect https://[REDACTED_CREDENTIAL]@{expected_host}/v1 done.'
            elif path == 'session':
                result = secrets.redact_session(trace(text), user_allowlist=allowlist, strict=True)[0]['messages'][0]['content']
            else:
                result = index.apply_share_redactions(conn, trace(text), user_allowlist=allowlist)[0]['messages'][0]['content']
            assert credential not in result, (path, host, credential)
            assert result.startswith('connect https://') and result.endswith(' done.')
            if path != 'share':
                assert '[REDACTED_CREDENTIAL]' in result


@pytest.mark.parametrize('credential,host', [
    ('root:P@ssw0rd', 'mongo1:27017'),
    ('user@corp.test:S3cretTokenValue', 'dbserver'),
    ('tim@example.com:S3cretPass99', '10.20.30.40'),
    ('user@corp.test:pa@ss@word', '[2001:db8::1]:8443'),
    ('ci:tok@en', 'github.com'),
    ('[REDACTED_CREDENTIAL]@remainingPassword', 'buildbox'),
])
def test_complete_userinfo_is_masked_once_and_stays_masked(conn, credential, host):
    text = f'connect mongodb://{credential}@{host}/admin'
    expected = f'connect mongodb://[REDACTED_CREDENTIAL]@{host}/admin'
    assert secrets.redact_text(text, strict=True)[0] == expected
    assert secrets.redact_text(expected, strict=True)[0] == expected
    for result in (secrets.redact_session(trace(text), strict=True)[0],
                   index.apply_share_redactions(conn, trace(text))[0]):
        output = result['messages'][0]['content']
        assert credential not in output
        assert 'ssw0rd' not in output and 'remainingPassword' not in output


@pytest.mark.parametrize('userinfo', ['deploybot:Tr0ub4dor3', 'admin%3AencodedPassword',
                                    'Zq7hunter2SuperSecretKey'])
def test_known_credentials_propagate_but_usernames_do_not(conn, userinfo):
    original = trace(f'connect https://{userinfo}@ci.audit.test/api',
                     f'run curl -u {userinfo} https://ci.audit.test/api',
                     f'The supplied credential is {userinfo}. git and admin are ordinary words.')
    for result in (secrets.redact_session(copy.deepcopy(original))[0],
                   index.apply_share_redactions(conn, copy.deepcopy(original))[0]):
        assert all(userinfo not in m['content'] for m in result['messages'])
        assert result['messages'][2]['content'].endswith('git and admin are ordinary words.')


@pytest.mark.parametrize('userinfo', ['git', 'oauth2', 'x-access-token', 'u', 'ci', 'noreply'])
def test_transport_username_exemption_is_about_the_value_not_the_host(userinfo):
    for host in ('github.com', 'localhost:8080', 'git.audit.test', 'buildbox'):
        text = f'ssh://{userinfo}@{host}/repo; {userinfo} is ordinary text'
        assert secrets.redact_text(text, strict=True)[0] == text.replace('git.audit.test', '[REDACTED_URL]')
        with_password = f'ssh://{userinfo}:hunter2@{host}/repo'
        assert 'hunter2' not in secrets.redact_text(with_password, strict=True)[0]


def test_unknown_username_donor_does_not_change_other_url_policy():
    source = trace('ssh://tim@buildbox/repo', 'https://tim@git.audit.test/repo',
                   'ssh://git@github.com/repo', 'tim and git are ordinary words')
    together = secrets.redact_session(copy.deepcopy(source))[0]['messages']
    for i, msg in enumerate(source['messages']):
        alone = secrets.redact_session(trace(msg['content']))[0]['messages'][0]
        assert together[i]['content'] == alone['content']
    assert together[-1]['content'] == source['messages'][-1]['content']


def test_upload_pii_pass_retains_transport_urls_but_still_masks_emails_and_passwords(tmp_path):
    import json
    from clawjournal.workbench.daemon import _apply_upload_pii_redactions
    original = trace('clone ssh://git@github.com; git status',
                     'email git@audit.test for help',
                     'connect https://git:SecretPassword77@github.com')
    # The real upload path applies deterministic secrets before PII review.
    prepared = secrets.redact_session(original, strict=True)[0]
    path = tmp_path / 'sessions.jsonl'
    path.write_text(json.dumps(prepared) + '\n')
    _apply_upload_pii_redactions(path, ai_pii=False)
    messages = json.loads(path.read_text())['messages']
    assert messages[0]['content'] == original['messages'][0]['content']
    assert 'git@audit.test' not in messages[1]['content']
    assert 'SecretPassword77' not in messages[2]['content']
    assert messages[2]['content'] == 'connect https://[REDACTED_CREDENTIAL]@github.com'


@pytest.mark.parametrize('prefix', ['%', '+', '-', '.'])
def test_detected_email_is_applied_outside_urls_in_every_entry_point(conn, prefix):
    from clawjournal.findings import apply_findings_to_text
    text = f'ftp login failed for {prefix}alice@audit.test'
    assert any(f['match'] == 'alice@audit.test' for f in secrets.scan_text(text))
    expected = text.replace('alice@audit.test', '[REDACTED_EMAIL]')
    assert secrets.redact_text(text)[0] == expected
    assert secrets.redact_session(trace(text))[0]['messages'][0]['content'] == expected
    finding = {'entity_type': 'email', 'entity_text': 'alice@audit.test', 'source': 'rule', 'confidence': .9}
    assert apply_findings_to_text(text, [finding], strict=True)[0] == expected
    output = index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content']
    assert 'alice@audit.test' not in output


@pytest.mark.parametrize('prefix', ['foo%', 'foo+', 'foo-', 'foo.', 'foo%%', 'foo.+', 'foo_', 'x'])
def test_known_short_mailbox_does_not_corrupt_a_different_one(prefix):
    from clawjournal.findings import apply_findings_to_text
    text = prefix + 'alice@audit.test'
    known = {'alice@audit.test': '[REDACTED_EMAIL]'}
    assert secrets._apply_redaction_set(text, known) == (text, 0)
    finding = {'entity_type': 'email', 'entity_text': 'alice@audit.test', 'source': 'rule', 'confidence': .9}
    assert apply_findings_to_text(text, [finding])[0] == text


@pytest.mark.parametrize('module', ['jenkins', 'payments', 'contextvars'])
def test_arbitrary_import_local_method_never_receives_stdlib_exemption(conn, module):
    text = f'```python\nimport {module}\nx = {module}.local()\n```'
    expected = text.replace(f'{module}.local', '[REDACTED_URL]')
    assert index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content'] == expected


@pytest.mark.parametrize('middle', ['for i in range(3):\n    pass', 'if flag:\n    print(1)',
                                  'try:\n    print(1)\nexcept ValueError:\n    pass'])
def test_unrelated_control_flow_preserves_threading_local(conn, middle):
    text = f'```python\nimport threading\n{middle}\n_state = threading.local()\n```'
    assert index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content'] == text


@pytest.mark.parametrize('middle', ['if flag:\n    threading = 1',
                                  'if flag:\n    import payments as threading',
                                  'try:\n    pass\nexcept Exception as threading:\n    pass'])
def test_control_flow_rebinding_invalidates_threading_hint(conn, middle):
    text = f'```python\nimport threading\n{middle}\n_state = threading.local()\n```'
    assert '[REDACTED_URL]' in index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content']


@pytest.mark.parametrize('value', ['hunter2hunter2SERVICE_KEY', 'Tr0ubadour3password'])
def test_known_secret_that_ends_in_a_field_name_cannot_hide_in_an_assignment(value):
    text = f'wrote {value}=abcdefghijklmnop into the config'
    result, count = secrets._apply_redaction_set(text, {value: '[REDACTED_ENV_SECRET]'})
    assert result == 'wrote [REDACTED_ENV_SECRET]=abcdefghijklmnop into the config'
    assert count == 1


@pytest.mark.parametrize('body', ['def get_state():\n    return threading.local()',
                                'class State:\n    value = threading.local()',
                                'if ready:\n    value = threading.local()',
                                'for i in range(3):\n    value = threading.local()',
                                'try:\n    value = threading.local()\nexcept Exception:\n    pass'])
def test_unchanged_stdlib_alias_survives_nested_syntax(conn, body):
    text = '```python\nimport threading\n' + body + '\n```'
    assert index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content'] == text


@pytest.mark.parametrize('body', ['class threading:\n    pass\nx = threading.local()',
                                'def f():\n    global threading\n    threading = 1\nx = threading.local()',
                                'threading.local = other\nx = threading.local()'])
def test_nested_shadowing_cannot_gain_stable_stdlib_evidence(conn, body):
    text = '```python\nimport threading\n' + body + '\n```'
    assert '[REDACTED_URL]' in index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content']


def test_two_imports_in_one_statement_cannot_alias_a_private_host_as_threading(conn):
    text = '```python\nimport threading, payments as threading\nx = threading.local()\n```'
    assert '[REDACTED_URL]' in index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content']


def test_a_function_can_import_threading_locally(conn):
    text = '```python\ndef state():\n    import threading as th\n    return th.local()\n```'
    assert index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content'] == text


def test_parameter_methods_keep_their_separate_existing_code_evidence(conn):
    text = '```python\ndef state(threading):\n    return threading.local()\n```'
    assert index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content'] == text


@pytest.mark.parametrize('word', ['access', 'config', 'connection'])
def test_line_initial_ssh_explanations_do_not_erase_words_elsewhere(conn, word):
    text = f'ssh {word} is required for this box. Ask IT for {word} before the migration.'
    assert index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content'] == text


@pytest.mark.parametrize('punctuation', ['.', '-', '+', '_'])
def test_ascii_punctuation_after_chinese_prose_keeps_the_prose(conn, punctuation):
    text = '请联系邮箱地址' + punctuation + 'alice@audit.test'
    output = index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content']
    assert output.startswith('请联系邮箱地址')
    assert 'alice@audit.test' not in output
