"""Real entry points and exact retained-text checks for the 72317b7 review."""
import copy
import json

import pytest

from clawjournal import share_cli
from clawjournal.redaction import pii, secrets
from clawjournal.workbench import index
from clawjournal.workbench.review_snapshots import load_review_snapshot


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(index, 'INDEX_DB', tmp_path / 'index.db')
    monkeypatch.setattr(index, 'BLOBS_DIR', tmp_path / 'blobs')
    monkeypatch.setattr(index, 'CONFIG_DIR', tmp_path / 'config')
    # Only external processes are stubbed. SQLite, findings, redaction,
    # revision computation, preview and snapshot storage are all real.
    for engine in ('betterleaks', 'trufflehog'):
        monkeypatch.setattr(f'clawjournal.redaction.{engine}.{engine}_secret_map_from_blob', lambda *a, **kw: {})
    result = index.open_index()
    yield result
    result.close()


def trace(text):
    return {'session_id': 'review-72317b7', 'source': 'codex', 'project': 'synthetic',
            'messages': [{'role': 'user', 'content': text}], 'stats': {'user_messages': 1}}


def share(conn, text):
    return index.apply_share_redactions(conn, trace(text))[0]['messages'][0]['content']


@pytest.mark.parametrize('text', ['plain sentence', 'API_KEY=sk-ant-api03-' + 'Ab09' * 12,
                                  'Please email alice@audit.test'])
def test_cli_real_preview_snapshots_original_and_packages_reviewed_bytes(conn, text):
    index.upsert_sessions(conn, [trace(text)])
    original = index.get_session_detail(conn, 'review-72317b7')
    settings = dict(custom_strings=[], allowlist_entries=[], extra_usernames=[], blocked_domains=[])
    records = share_cli._build_records(conn, settings, [{'session_id': 'review-72317b7'}], False)
    record, = records
    snapshot = load_review_snapshot(conn, record['review_snapshot_id'], 'review-72317b7')
    assert snapshot['messages'] == original['messages']
    assert record['row']['revision_hash'] == index.compute_content_revision(original)
    if text != 'plain sentence':
        assert record['redacted']['messages'][0]['content'] != text
    # An append between preview and packaging must not enter the package.
    index.upsert_sessions(conn, [trace('Later message must remain local')])
    share_id = index.create_share(conn, ['review-72317b7'],
        expected_revisions={'review-72317b7': original['content_revision']},
        review_snapshot_ids={'review-72317b7': record['review_snapshot_id']})
    output, manifest = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id))
    assert not manifest.get('blocked')
    exported = (output / 'sessions.jsonl').read_text()
    assert 'Later message' not in exported
    assert 'alice@audit.test' not in exported
    assert 'sk-ant-api03-' not in exported


@pytest.mark.parametrize('scheme', ['ftp', 'ftps', 'ldap', 'amqp', 'smb', 'https', 'ssh', 'postgres', 'custom+ssh'])
@pytest.mark.parametrize('value', ['%40weird%3Apass', '%4Fweird%3apass', 'before%25after', 'p,a;s(s)word', 'A' * 500])
def test_url_credentials_are_masked_in_every_path(conn, scheme, value):
    text = f'connect {scheme}://svc:{value}@git.audit.test/path'
    for result in (secrets.redact_text(text, strict=True)[0],
                   secrets.redact_session(trace(text), strict=True)[0]['messages'][0]['content'], share(conn, text)):
        assert value not in result
        if scheme == 'postgres':  # Existing DB URL policy redacts the whole URL.
            assert result == 'connect [REDACTED_DB_URL]'
            continue
        assert result.startswith(f'connect {scheme}://')
        assert result.endswith('/path')


def test_existing_percent_url_finding_is_never_dropped_at_apply():
    text = 'connect ftp://svc:%40weird%3Apass@git.audit.test/path'
    result, count = secrets._apply_redaction_set(text, {'40weird%3Apass@git.audit.test': '[REDACTED_EMAIL]'})
    assert result == 'connect ftp://svc:%[REDACTED_EMAIL]/path'
    assert count == 1
    # A percent inside a different, legitimate mailbox remains a boundary.
    other = 'foo%alice@audit.test'
    assert secrets._apply_redaction_set(other, {'alice@audit.test': '[REDACTED_EMAIL]'}) == (other, 0)


@pytest.mark.parametrize('user', ['git', 'u', 'ci', 'deploy'])
def test_url_username_does_not_become_a_global_common_word(conn, user):
    text = f'git clone https://{user}@git.audit.test/team/app.git'
    ordinary = f'git status; {user} and {user.upper()} are ordinary words here'
    original = trace(text)
    original['messages'].append({'role': 'user', 'content': ordinary})
    for result in (secrets.redact_session(copy.deepcopy(original))[0],
                   index.apply_share_redactions(conn, copy.deepcopy(original))[0]):
        assert result['messages'][1]['content'] == ordinary
        assert result['messages'][0]['content'].startswith('git clone https://')
        assert result['messages'][0]['content'].endswith('/team/app.git')
        expected = text.replace('deploy@', '[REDACTED_CREDENTIAL]@') if user == 'deploy' else text
        assert result['messages'][0]['content'] == expected


@pytest.mark.parametrize('address,expected', [('tim@git.corp.acme.com', '[REDACTED_CREDENTIAL]@git.corp.acme.com'),
                                            ('svc@db01.local', '[REDACTED_CREDENTIAL]@[REDACTED_URL]')])
def test_secrets_only_export_separates_credentials_from_private_hosts(address, expected):
    text = 'git clone https://' + address + '/team/app.git'
    assert secrets.redact_text(text, strict=True)[0] == 'git clone https://' + expected + '/team/app.git'


@pytest.mark.parametrize('allowlist', [[{'type':'exact', 'text':'alice@corp.test'}],
                                    [{'type':'category', 'match_type':'email'}]])
def test_email_allowlist_does_not_authorize_url_userinfo(allowlist):
    text = 'clone https://alice@corp.test/path; alice is a word'
    assert secrets.redact_session(trace(text), user_allowlist=allowlist)[0]['messages'][0]['content'] == text.replace('alice@', '[REDACTED_CREDENTIAL]@')
    secret = 'clone https://alice:fictional-password@corp.test/path'
    assert 'fictional-password' not in secrets.redact_session(trace(secret), user_allowlist=allowlist)[0]['messages'][0]['content']


@pytest.mark.parametrize('separator', [',', ';', '|'])
def test_url_followed_by_a_separate_email_keeps_the_original_url(separator):
    text = f'Contacts: https://docs.corp.com{separator}alice.smith@corp.com and more'
    expected = f'Contacts: https://docs.corp.com{separator}[REDACTED_EMAIL] and more'
    assert secrets.redact_session(trace(text))[0]['messages'][0]['content'] == expected


@pytest.mark.parametrize('prefix', ['/' + 'directory/' * 100, './' + 'directory/' * 100,
                                  'EMAIL=', 'owner=', 'http://ops/api?x=' + 'x' * 300 + '&email='])
def test_path_query_and_assignment_keep_their_own_boundaries(conn, prefix):
    text = 'Read "' + prefix + 'alice@audit.test" then continue.'
    assert share(conn, text) == text.replace('alice@audit.test', '[REDACTED_EMAIL]')


@pytest.mark.parametrize('address', ['a/b@audit.test', 'a&b@audit.test', 'a?b@audit.test',
                                   'a=b@audit.test', 'a?b=c@audit.test', 'foo%alice@audit.test',
                                   'a+b.c-d_e@audit.test'])
def test_rfc_atext_is_not_globally_cut_at_path_or_query_punctuation(conn, address):
    for text in ('Contact <' + address + '> now.', 'Contact ' + address + ' now.'):
        assert share(conn, text) == text.replace(address, '[REDACTED_EMAIL]')


@pytest.mark.parametrize('wrapper', ['sudo ssh {}', 'time ssh {}', 'coder ssh {}', 'cd /x && ssh {}',
                                   'dev@laptop:~/proj$ ssh {}', '- ssh {}', '1. ssh {}',
                                   'Step 2: ssh {}', 'Run `ssh {}` first', 'I ran ssh {} and it worked'])
def test_ssh_destinations_inside_command_wrappers_are_detected(conn, wrapper):
    text = wrapper.format('buildbox01')
    assert 'buildbox01' not in share(conn, text)


@pytest.mark.parametrize('word', ['connection', 'read-only', 'likely', 'pipe', 'keys', 'into'])
def test_ssh_explanation_is_not_a_hostname(conn, word):
    text = 'The ssh ' + word + ' is described here. The word ' + word + ' is ordinary.'
    assert share(conn, text) == text


@pytest.mark.parametrize('prefix', ['import jenkins\n', 'from clients import payments as jenkins\n'])
@pytest.mark.parametrize('fence', [False, True])
def test_an_arbitrary_import_never_authorizes_a_private_host(conn, prefix, fence):
    text = prefix + 'jenkins.internal()\n'
    if fence:
        text = '```python\n' + text + '```'
    assert share(conn, text) == text.replace('jenkins.internal', '[REDACTED_URL]')


@pytest.mark.parametrize('prefix', ['', json.dumps({f'key{i}':i for i in range(600)}) + '\n', '#' * 70000 + '\n'],
                         ids=['small', 'token-budget', 'character-budget'])
def test_large_data_before_a_host_lookup_does_not_change_share_policy(conn, prefix):
    text = prefix + 'DB_HOST = svc.internal.' + 'L' * 70 + '(1)'
    assert share(conn, text) == text.replace('svc.internal', '[REDACTED_URL]')


@pytest.mark.parametrize('text', ['{"to":"用户alice@audit.test"}', 'email="用户alice@audit.test"'])
def test_complete_quoted_unicode_mailbox_is_masked(conn, text):
    assert share(conn, text) == text.replace('用户alice@audit.test', '[REDACTED_EMAIL]')


@pytest.mark.parametrize('prefix', ['请把PR225报告发送到工作邮箱', '请把X报告发送到我们团队工作邮箱' * 30])
def test_a_quote_around_prose_is_not_an_international_mailbox_delimiter(conn, prefix):
    text = json.dumps({'note': prefix + 'alice@audit.test'}, ensure_ascii=False)
    assert share(conn, text) == text.replace('alice@audit.test', '[REDACTED_EMAIL]')


def test_local_findings_do_not_reject_unrelated_ambiguous_text_but_share_does():
    from clawjournal.findings import apply_findings_to_session
    from clawjournal.redaction.boundaries import RedactionBoundaryError
    text = '<' + 'a' * 100 + '@audit.test> Contact bob@audit.test'
    finding = {'session_id': 'review-72317b7', 'entity_type':'email', 'entity_text':'bob@audit.test',
               'confidence': .9, 'source':'rule'}
    assert apply_findings_to_session(trace(text), [finding])[0]['messages'][0]['content'] == text.replace('bob@audit.test', '[REDACTED_EMAIL]')
    with pytest.raises(RedactionBoundaryError):
        apply_findings_to_session(trace(text), [finding], strict=True)


def test_edit_strictly_inside_protected_interval_invalidates_it():
    from clawjournal.redaction.code_context import CodeContext
    for field in ('protected', 'ambiguous_emails', 'references'):
        context = CodeContext(**{field: [(10, 100), (110, 120)]})
        context.apply_edits([(30, 70, '[REDACTED]')])
        assert getattr(context, field) == [(80, 90)]


def test_interior_code_edit_cannot_hide_a_later_real_host():
    from clawjournal.findings import apply_findings_to_text
    name = 'A' * 40
    text = 'obj = object()\nobj.' + name + '.local()\nhost = "db.local"\n'
    findings = [
        {'entity_type':'custom_sensitive', 'entity_text':name, 'source':'rule', 'confidence':.9},
        {'entity_type':'private_url', 'entity_text':'db.local', 'source':'rule', 'confidence':.9},
    ]
    result, count = apply_findings_to_text(text, findings, strict=True)
    assert result == text.replace(name, '[REDACTED]').replace('db.local', '[REDACTED_URL]')
    assert count == 2
