"""User-visible contracts for the post-merge PR225 follow-up."""
import copy
import hashlib
import io
import json
import zipfile

import pytest

from clawjournal import auto_upload, share_cli
from clawjournal.redaction import secrets
from clawjournal.redaction.boundaries import RedactionBoundaryError
from clawjournal.workbench import index, review_snapshots


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(index, 'INDEX_DB', tmp_path / 'index.db')
    monkeypatch.setattr(index, 'BLOBS_DIR', tmp_path / 'blobs')
    monkeypatch.setattr(index, 'CONFIG_DIR', tmp_path)
    for scanner in ('betterleaks', 'trufflehog'):
        monkeypatch.setattr(f'clawjournal.redaction.{scanner}.{scanner}_secret_map_from_blob', lambda *a, **kw: {})
    with index.open_index() as db:
        yield db
    db.close()


def trace(sid, text):
    return {'session_id': sid, 'source': 'codex', 'project': 'synthetic-followup',
            'messages': [{'role': 'user', 'content': text}], 'stats': {'user_messages': 1}}


@pytest.mark.parametrize('bad_text', [
    '<' + 'a' * 70 + '@audit.test>',
    'token 123456789:' + 'AbCDef01_' * 20,
    'host ' + 'a' * 70 + '.internal',
], ids=['oversized-email', 'oversized-token', 'oversized-host'])
def test_one_boundary_failure_keeps_49_safe_traces_and_names_the_omission(conn, bad_text):
    rows = [trace(f'case-{i:02}', bad_text if i == 23 else f'ordinary text {i}') for i in range(50)]
    index.upsert_sessions(conn, rows)
    share_id = index.create_share(conn, [row['session_id'] for row in rows])
    folder, manifest = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id))
    assert not manifest.get('blocked')
    actual = [json.loads(line) for line in (folder / 'sessions.jsonl').read_text().splitlines()]
    assert len(actual) == manifest['session_count'] == 49
    assert {row['session_id'] for row in actual} == {row['session_id'] for row in rows} - {'case-23'}
    assert manifest['skipped_sessions'][0]['session_id'] == 'case-23'
    assert manifest['skipped_sessions'][0]['reason'] == 'redaction_boundary'
    assert bad_text not in (folder / 'sessions.jsonl').read_text()
    assert bad_text not in json.dumps(manifest)
    assert len(index.get_share(conn, share_id)['sessions']) == 49
    assert index.get_session_detail(conn, 'case-23')['messages'][0]['content'] == bad_text


def test_all_bad_traces_fail_closed_with_ids_without_writing_partial_data(conn):
    index.upsert_sessions(conn, [trace('blocked-case', '<' + 'a'*70 + '@audit.test>')])
    sid = index.create_share(conn, ['blocked-case'])
    folder, manifest = index.export_share_to_disk(conn, sid, index.get_share(conn, sid))
    assert manifest['blocked'] and manifest['session_count'] == 0
    assert manifest['blocked_sessions'][0]['session_id'] == 'blocked-case'
    assert not (folder / 'sessions.jsonl').exists()
    assert not list(folder.glob('*.tmp'))


def test_cli_preview_drops_only_bad_trace_and_reports_id(conn, capsys):
    rows = [trace('before', 'ordinary before'), trace('refused', '<' + 'a'*70 + '@audit.test>'), trace('after', 'ordinary after')]
    index.upsert_sessions(conn, rows)
    records = share_cli._build_records(conn, dict(custom_strings=[], allowlist_entries=[], extra_usernames=[], blocked_domains=[]), rows, False)
    assert [record['row']['session_id'] for record in records] == ['before', 'after']
    assert 'refused' in capsys.readouterr().err
    assert conn.execute("SELECT count(*) FROM share_review_snapshots WHERE session_id='refused'").fetchone()[0] == 0


@pytest.mark.parametrize('prefix', ['build_cache=' + 'part/'*25,
    'callback?' + 'x=1&'*25 + 'email=', 'relative/path/'*15, 'cache_value=' + 'x'*5])
def test_path_and_assignment_boundaries_keep_prefix_and_mask_address(conn, prefix):
    text = prefix + 'alice@audit.test'
    result = index.apply_share_redactions(conn, trace('boundary', text))[0]['messages'][0]['content']
    assert 'alice@audit.test' not in result
    if len(prefix) > 64:
        assert result.startswith(prefix)


@pytest.mark.parametrize('mailbox', ['a/b/c@audit.test', 'sales/support@audit.test',
    'a?b=c@audit.test', 'a?x=1&b=alice@audit.test', 'first+tag@audit.test', 'x&y=z@audit.test', 'foo%bar@audit.test'])
def test_legal_punctuation_in_a_mailbox_is_still_fully_redacted(conn, mailbox):
    for text in (mailbox, '<' + mailbox + '>'):
        output = index.apply_share_redactions(conn, trace('legal-mail', text))[0]['messages'][0]['content']
        assert mailbox not in output
        assert text.replace(mailbox, '[REDACTED_EMAIL]') == output


def test_pattern_email_exception_cannot_disable_independent_password_detection(conn):
    text = 'psql postgres://alice.smith@corp.example.com:hunter2@db.example.com/prod'
    policy = [{'type': 'pattern', 'regex': r'@corp\.example\.com'}]
    assert 'hunter2' not in secrets.redact_text(text, strict=True, user_allowlist=policy)[0]
    result = index.apply_share_redactions(conn, trace('password', text), user_allowlist=policy)[0]
    assert 'hunter2' not in result['messages'][0]['content']


@pytest.mark.parametrize('user', ['git', 'ci', 'readonly', 'svc:fakePassword77'])
def test_authenticated_private_host_has_independent_redaction(conn, user):
    text = f'clone https://{user}@build.acmecorp.com/platform/infra.git now; git status'
    for output in (secrets.redact_text(text, strict=True)[0],
                   index.apply_share_redactions(conn, trace('private-url', text))[0]['messages'][0]['content']):
        assert 'build.acmecorp.com' not in output
        assert output.endswith('now; git status')
        assert 'fakePassword77' not in output
    assert secrets.redact_text('clone ssh://git@github.com; git status', strict=True)[0] == 'clone ssh://git@github.com; git status'


@pytest.mark.parametrize('workbench', [False, True])
def test_events_export_boundary_is_a_controlled_failure(conn, workbench):
    from clawjournal.events.export.bundle import _BundleRedactor, _RedactionCounts, ExportGateBlocked
    from clawjournal.redaction.anonymizer import Anonymizer
    redactor = _BundleRedactor(conn, Anonymizer(), [], [], [], _RedactionCounts(),
                              workbench_session_ids={'event-session': 'event-session'} if workbench else {})
    redactor.prepare('piece', '<'+'a'*70+'@audit.test>', session_key='event-session', field='raw_json')
    with pytest.raises(ExportGateBlocked, match='Export blocked') as error:
        redactor.finalize()
    assert error.value.exit_code == 2
    assert 'a'*70 not in str(error.value)
    with pytest.raises(RuntimeError):
        redactor.get('piece')


def test_skill_scrub_omits_an_ambiguous_excerpt_before_ai(conn):
    from clawjournal.skill.distill import _scrub
    from clawjournal.redaction.anonymizer import Anonymizer
    text = 'Secret context <' + 'a'*70 + '@audit.test>'
    assert _scrub(text, Anonymizer()) == '[REDACTED_AMBIGUOUS_EXCERPT]'
    assert _scrub('Ordinary useful instructions', Anonymizer()) == 'Ordinary useful instructions'


def make_archive(conn, tmp_path):
    index.upsert_sessions(conn, [trace('archived', 'already sanitized')])
    share_id = index.create_share(conn, ['archived'])
    payload = (json.dumps(trace('archived', 'already sanitized')) + '\n').encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('sessions.jsonl', payload)
    path = tmp_path / 'sealed.zip'; path.write_bytes(stream.getvalue())
    conn.execute("UPDATE shares SET submission_channel='auto_weekly', submission_state='sealed', sealed_artifact_path=?, sealed_artifact_sha256=? WHERE share_id=?", (str(path), hashlib.sha256(path.read_bytes()).hexdigest(), share_id))
    conn.commit()
    return share_id, path, payload


def test_auto_receipt_pins_the_jsonl_hash_for_later_export(conn, tmp_path):
    share_id, path, payload = make_archive(conn, tmp_path)
    auto_upload._commit_receipt(conn, share_id=share_id, receipt={'receipt_id': 'synthetic-receipt'})
    assert conn.execute('SELECT bundle_hash FROM shares WHERE share_id=?', (share_id,)).fetchone()[0] == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize('damaged', [False, True])
def test_old_auto_receipt_recovers_only_from_hash_checked_zip(conn, tmp_path, monkeypatch, damaged):
    share_id, path, payload = make_archive(conn, tmp_path)
    conn.execute("UPDATE shares SET submission_state='accepted', shared_at='2026-09-14', bundle_hash=NULL WHERE share_id=?", (share_id,)); conn.commit()
    if damaged:
        path.write_bytes(path.read_bytes() + b'changed')
    monkeypatch.setattr(index, 'get_session_detail', lambda *a: pytest.fail('Do not reconstruct a completed export from new source data'))
    folder, manifest = review_snapshots.copy_completed_share(conn, share_id, str(tmp_path / 'copy'))
    assert bool(manifest.get('blocked')) is damaged
    if not damaged:
        assert (folder / 'sessions.jsonl').read_bytes() == payload
        assert manifest['local_copy_only']


def test_multiple_preview_queues_keep_their_issued_snapshot_ids(conn):
    ids = []
    for i in range(150):
        sid = f'preview-{i}'
        index.upsert_sessions(conn, [trace(sid, f'ordinary {i}')])
        snapshot = review_snapshots.save_review_snapshot(conn, index.get_session_detail(conn, sid))
        ids.append((sid, snapshot))
    first = dict(ids[:50])
    share = index.create_share(conn, list(first), review_snapshot_ids=first)
    assert len(index.get_share(conn, share)['sessions']) == 50


def test_full_cache_rejects_new_preview_without_evicting_an_issued_one(conn, monkeypatch):
    index.upsert_sessions(conn, [trace('saved', 'First reviewed message'), trace('new', 'New message')])
    snapshot = review_snapshots.save_review_snapshot(conn, index.get_session_detail(conn, 'saved'))
    used = conn.execute('SELECT length(CAST(payload AS BLOB)) FROM share_review_snapshots').fetchone()[0]
    monkeypatch.setattr(review_snapshots, 'MAX_SNAPSHOT_BYTES', used + 1)
    with pytest.raises(review_snapshots.ReviewSnapshotError, match='Existing previews are preserved'):
        review_snapshots.save_review_snapshot(conn, index.get_session_detail(conn, 'new'))
    assert review_snapshots.load_review_snapshot(conn, snapshot, 'saved')


def test_new_preview_of_same_session_keeps_earlier_review_available(conn):
    index.upsert_sessions(conn, [trace('same', 'Earlier reviewed content')])
    earlier = review_snapshots.save_review_snapshot(conn, index.get_session_detail(conn, 'same'))
    index.upsert_sessions(conn, [trace('same', 'Later content')])
    later = review_snapshots.save_review_snapshot(conn, index.get_session_detail(conn, 'same'))
    assert earlier != later
    assert review_snapshots.load_review_snapshot(conn, earlier, 'same')['messages'][0]['content'] == 'Earlier reviewed content'


def test_share_package_finalizes_survivors_through_both_scan_gates(conn, monkeypatch):
    from clawjournal.share_flow import package, build_zip
    from clawjournal.redaction import betterleaks, trufflehog
    # Keep packaging, gate orchestration and ZIP sealing real. Substitute
    # only external scanner results so CI does not download or run binaries.
    calls = []
    for module, report_type in ((betterleaks, betterleaks.BetterleaksReport),
                                (trufflehog, trufflehog.TruffleHogReport)):
        monkeypatch.delenv('CLAWJOURNAL_SKIP_' + module.__name__.rsplit('.', 1)[-1].upper())
        monkeypatch.setattr(module, 'is_available', lambda: True)
        monkeypatch.setattr(module, 'engine_fingerprint', lambda: 'synthetic-test-scanner')
        def scan(path, *, results=None, report_type=report_type):
            calls.append((report_type, path))
            return report_type(scanned_path=str(path), scanned_sha256=hashlib.sha256(path.read_bytes()).hexdigest()), []
        monkeypatch.setattr(module, 'scan_file_with_raws', scan)
    rows = [trace('package-safe', 'contact alice@audit.test; ordinary ending'),
            trace('package-refused', '<' + 'a'*70 + '@audit.test>')]
    index.upsert_sessions(conn, rows)
    for row in rows:
        index.set_hold_state(conn, row['session_id'], 'released', changed_by='test', reason='synthetic')
    settings = dict(custom_strings=[], extra_usernames=[], excluded_projects=[], blocked_domains=[],
                    source_filter=['codex'], allowlist_entries=[])
    result = package(conn, [row['session_id'] for row in rows], settings, ai_pii=False)
    assert result['ok'], result
    assert result['manifest']['session_count'] == 1
    assert result['manifest']['skipped_sessions'][0]['session_id'] == 'package-refused'
    with zipfile.ZipFile(io.BytesIO(build_zip(result['export_dir']))) as archive:
        payload = archive.read('sessions.jsonl').decode()
        assert 'secret-scan.json' in archive.namelist()
        assert 'secret-scan.post-pii.json' in archive.namelist()
    assert 'package-refused' not in payload
    assert 'alice@audit.test' not in payload
    assert 'ordinary ending' in payload
    assert len(calls) >= 4  # Both engines before and after final redaction.


@pytest.mark.parametrize('include_safe', [False, True])
def test_bundle_export_cli_names_boundary_omissions(conn, tmp_path, monkeypatch, capsys, include_safe):
    from types import SimpleNamespace
    from clawjournal import cli
    rows = [trace('cli-refused', '<' + 'a'*70 + '@audit.test>')]
    if include_safe:
        rows.append(trace('cli-safe', 'ordinary safe content'))
    index.upsert_sessions(conn, rows)
    share_id = index.create_share(conn, [row['session_id'] for row in rows])
    monkeypatch.setattr(cli, 'load_config', lambda: {})
    output = tmp_path / 'cli-export'
    args = SimpleNamespace(share_id=share_id, output=str(output), zip=False,
                           ai_pii_review=False, training_format=False, json=True)
    if include_safe:
        cli._run_bundle_export(args)
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result['session_count'] == 1
        assert result['skipped_sessions'][0]['session_id'] == 'cli-refused'
        assert 'cli-refused' in captured.err
    else:
        with pytest.raises(SystemExit) as error:
            cli._run_bundle_export(args)
        assert error.value.code == 2
        captured = capsys.readouterr()
        assert 'cli-refused' in captured.out
        assert 'Report:' not in captured.out  # No nonexistent scan-report link.
        assert not (output / 'sessions.jsonl').exists()
