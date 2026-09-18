"""Issue 230: AI proposes bounded spans; local checks and saved reviews win."""
import copy
import json
from types import SimpleNamespace

import pytest

from clawjournal.redaction import boundary_recovery as recovery
from clawjournal.redaction.anonymizer import Anonymizer
from clawjournal.redaction.boundaries import RedactionBoundaryError
from clawjournal.workbench import index
from clawjournal.workbench.review_snapshots import save_review_snapshot, load_review_snapshot

PREFIX = 'ordinaryprose' * 6
HOST = 'alex-laptop'
TEXT = 'Before ' + PREFIX + HOST + ' after'


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(index, 'INDEX_DB', tmp_path / 'index.db')
    monkeypatch.setattr(index, 'BLOBS_DIR', tmp_path / 'blobs')
    monkeypatch.setattr(index, 'CONFIG_DIR', tmp_path)
    db = index.open_index()
    yield db
    db.close()


@pytest.fixture
def clean_scanner(monkeypatch):
    monkeypatch.setattr('clawjournal.redaction.betterleaks.scan_text',
        lambda text: SimpleNamespace(bypassed=False, binary_missing=False, scan_error=None, findings=[]))


def trace(text=TEXT):
    return {'session_id': 'recovery', 'source': 'codex', 'project': 'test-project',
            'messages': [{'role': 'user', 'content': text}], 'stats': {}}


def finding(i=0, text=HOST):
    return {'message_index': i, 'field': 'content', 'entity_type': 'device_id', 'entity_text': text}


def propose(conn, detail, reviewer=lambda *a, **kw: [finding()], **kwargs):
    return recovery.propose_boundary_plan(
        detail, redact_locally=lambda value: index.apply_share_redactions(conn, value)[0],
        review=reviewer, anonymize=Anonymizer(extra_usernames=['PrivateUser']).text, **kwargs)


def test_recovery_preserves_prose_pins_input_and_replays_at_export(conn, clean_scanner):
    original = trace()
    index.upsert_sessions(conn, [original])
    detail = index.get_session_detail(conn, 'recovery')
    plan = propose(conn, detail)
    expected = 'Before ' + PREFIX + recovery.PLACEHOLDER + ' after'
    redacted, count, log = index.apply_share_redactions(conn, copy.deepcopy(detail), boundary_plan=plan)
    assert redacted['messages'][0]['content'] == expected
    assert count >= 1 and log[0]['source'] == 'ai_boundary_recovery'
    assert HOST not in json.dumps(plan)
    snapshot = save_review_snapshot(conn, detail, boundary_plan=plan)
    assert load_review_snapshot(conn, snapshot, 'recovery')['_review_boundary_plan'] == plan
    # Later content cannot enter the recovered, explicitly selected snapshot.
    index.upsert_sessions(conn, [trace('new unreviewed text')])
    share_id = index.create_share(conn, ['recovery'],
        expected_revisions={'recovery': detail['content_revision']},
        review_snapshot_ids={'recovery': snapshot})
    output, manifest = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id))
    assert not manifest.get('blocked')
    exported = json.loads((output / 'sessions.jsonl').read_text())
    assert exported['messages'][0]['content'] == expected
    assert '_review_boundary_plan' not in exported
    assert exported['revision_hash'] == detail['content_revision']
    assert original['messages'][0]['content'] == TEXT


@pytest.mark.parametrize('findings', [[], [finding(text='other-laptop')],
    [finding(text=PREFIX + HOST)], [finding(text='aptop')], [finding(text='alex')],
    [finding(i=-1)], [finding(i=True)], [finding(), finding()],
    [{**finding(), 'field': 'project'}], [{**finding(), 'entity_type': 'email'}]])
def test_invalid_or_incomplete_model_output_cannot_create_a_plan(conn, clean_scanner, findings):
    original = trace()
    with pytest.raises(RedactionBoundaryError):
        propose(conn, original, lambda *a, **kw: findings)
    assert original == trace()


@pytest.mark.parametrize('failure', ['bypassed', 'binary_missing', 'scan_error', 'findings'])
def test_scanner_failure_never_reaches_ai(conn, monkeypatch, failure):
    state = dict(bypassed=False, binary_missing=False, scan_error=None, findings=[])
    state[failure] = ['hit'] if failure == 'findings' else True
    monkeypatch.setattr('clawjournal.redaction.betterleaks.scan_text', lambda text: SimpleNamespace(**state))
    with pytest.raises(RedactionBoundaryError):
        propose(conn, trace(), lambda *a, **kw: pytest.fail('AI must not run'))


def test_ai_context_is_small_and_locally_masked(conn, clean_scanner):
    detail = trace('PrivateUser /Users/PrivateUser/private.file alice@example.com ' + TEXT)
    def reviewer(request, **kwargs):
        text = json.dumps(request)
        assert 'PrivateUser' not in text and '/Users/PrivateUser' not in text
        assert 'alice@example.com' not in text
        content = json.loads(request['messages'][0]['content'])
        assert content['candidate'] == PREFIX + HOST
        assert len(content['context']) <= 2 * recovery.CONTEXT_CHARS + len('[CANDIDATE]')
        assert 'untrusted' in kwargs['rubric']
        return [finding()]
    propose(conn, detail, reviewer)


@pytest.mark.parametrize('path', ['thinking', 'tool', 'project', 'extra'])
def test_recovery_covers_export_text_fields(conn, clean_scanner, path):
    detail = trace('normal')
    if path == 'project':
        detail['project'] = TEXT
    elif path == 'tool':
        detail['messages'][0]['tool_uses'] = [{'input': {'deep': {'value': TEXT}}}]
    elif path == 'extra':
        detail['messages'][0]['extra'] = {'nested': [TEXT]}
    else:
        detail['messages'][0][path] = TEXT
    plan = propose(conn, detail)
    result = index.apply_share_redactions(conn, copy.deepcopy(detail), boundary_plan=plan)[0]
    assert HOST not in json.dumps(result)
    assert PREFIX in json.dumps(result)
    previews = recovery.recovered_field_previews(result, plan)
    assert len(previews) == 1 and HOST not in previews[0]['text']
    assert PREFIX in previews[0]['text']


@pytest.mark.parametrize('change', ['text', 'hash', 'offset', 'path', 'overlap'])
def test_changed_input_or_invalid_plan_fails_atomically(conn, clean_scanner, change):
    detail = trace()
    plan = propose(conn, detail)
    if change == 'text':
        detail['messages'][0]['content'] += '!'
    elif change == 'hash':
        plan[0]['field_hash'] = 'invalid'
    elif change == 'offset':
        plan[0]['start'] = -1
    elif change == 'path':
        plan[0]['path'] = ['session_id']
    else:
        plan.append(copy.deepcopy(plan[0]))
    before = copy.deepcopy(detail)
    with pytest.raises(RedactionBoundaryError):
        recovery.apply_boundary_plan(detail, plan)
    assert detail == before


def test_snapshot_rejects_plan_from_untrusted_trace_metadata(conn):
    index.upsert_sessions(conn, [trace()])
    detail = index.get_session_detail(conn, 'recovery')
    detail['_review_boundary_plan'] = [{'unsafe': True}]
    snapshot = save_review_snapshot(conn, detail)
    assert '_review_boundary_plan' not in load_review_snapshot(conn, snapshot, 'recovery')


def test_other_ambiguous_types_and_large_inputs_stay_blocked(conn, clean_scanner):
    for text in ['a' * 70 + '@audit.test', 'a' * 600 + '-laptop', '\n'.join([TEXT] * 9)]:
        with pytest.raises(RedactionBoundaryError):
            propose(conn, trace(text), lambda *a, **kw: pytest.fail('AI must not run'))


def test_known_credential_overlap_is_not_sent_even_if_scanner_returns_clean(conn, clean_scanner, monkeypatch):
    monkeypatch.setattr('clawjournal.redaction.secrets.scan_text', lambda text: [
        {'start': 7, 'end': 7 + len(PREFIX + HOST), 'match': PREFIX + HOST}])
    with pytest.raises(RedactionBoundaryError):
        propose(conn, trace(), lambda *a, **kw: pytest.fail('AI must not run'))


def test_local_preparation_masks_all_recovery_context_before_ai(conn, clean_scanner):
    detail = trace('safe')
    detail['fork_nickname'] = 'PrivateUser /Users/PrivateUser/cache ProjectSecret '
    detail['messages'][0]['extra'] = {'nested': [
        'PrivateUser /Users/PrivateUser/cache ProjectSecret private.domain.test ' + TEXT,
    ]}
    prepared = index.prepare_share_redactions(conn, detail,
        custom_strings=['ProjectSecret'], extra_usernames=['PrivateUser'],
        blocked_domains=['private.domain.test'])[0]
    serialized = json.dumps(prepared)
    for private in ('PrivateUser', 'ProjectSecret', 'private.domain.test'):
        assert private not in serialized
    plan = propose(conn, prepared)
    final = index.apply_share_redactions(conn, prepared, boundary_plan=plan)[0]
    for private in ('PrivateUser', 'ProjectSecret', 'private.domain.test', HOST):
        assert private not in json.dumps(final)
