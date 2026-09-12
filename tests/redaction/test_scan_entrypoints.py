"""Content-derived failures must not lose findings or an ingest batch."""
import json

import pytest

from clawjournal.parsing import parser
from clawjournal.redaction import code_context as cc, pii, secrets
from clawjournal.redaction.anonymizer import Anonymizer
from clawjournal.scoring.badges import compute_all_badges
from clawjournal.workbench import daemon, index
from clawjournal.workbench.findings_pipeline import run_findings_pipeline

EMAIL = 'alice@audit.test'
TOKEN = 'ghp_' + 'AbCd012345' * 4
FENCE = '```python\nobj.local()\n```'
JSON_OUTPUT = json.dumps({f'configuration_entry_{i}': {'email': EMAIL, 'description': 'ordinary value'}
                          for i in range(900)}, indent=2)
CASES = {
    'apostrophe_before': "Here's the fix:\n" + FENCE + '\n' + EMAIL,
    'apostrophe_after': FENCE + "\nThat doesn't work. " + EMAIL,
    'json': JSON_OUTPUT,
    'nul': 'tree:\n    parent/\n       \x00 child=leaf\n contact ' + EMAIL,
    'reference_prose': "Notes:\nDB_HOST=cfg['db_host']\nContact " + EMAIL,
}


def session(sid, content):
    return {'session_id': sid, 'source': 'claude', 'project': 'synthetic',
            'start_time': '2025-01-01T00:00:00+00:00', 'end_time': '2025-01-01T00:10:00+00:00',
            'messages': [{'role': 'user', 'content': 'Inspect the output', 'tool_uses': []},
                         {'role': 'assistant', 'content': content, 'tool_uses': []}]}


@pytest.fixture
def isolated_index(tmp_path, monkeypatch):
    monkeypatch.setattr(index, 'INDEX_DB', tmp_path / 'index.db')
    monkeypatch.setattr(index, 'BLOBS_DIR', tmp_path / 'blobs')
    monkeypatch.setattr(index, 'CONFIG_DIR', tmp_path / 'config')
    conn = index.open_index()
    yield conn
    conn.close()


@pytest.mark.parametrize('name', CASES)
def test_all_scan_entrypoints_keep_sensitive_findings(name):
    text = CASES[name] + '\n' + TOKEN
    assert any(f['match'] == EMAIL for f in secrets.scan_text(text))
    assert any(f['match'] == EMAIL for f in pii.scan_text_for_pii(text))
    assert compute_all_badges(session(name, text))['risk_badges']
    for strict in (False, True):
        result, n, _ = secrets.redact_text(text, strict=strict)
        assert result == text.replace(EMAIL, '[REDACTED_EMAIL]').replace(TOKEN, '[REDACTED_GITHUB_TOKEN]')
        assert n == text.count(EMAIL) + 1


@pytest.mark.parametrize('name', CASES)
def test_findings_are_persisted_and_next_tick_is_unchanged(isolated_index, name):
    blob = session(name, CASES[name])
    assert index.upsert_sessions(isolated_index, [blob]) == 1
    config = {'findings_engines': ['regex_secrets', 'regex_pii']}
    result = run_findings_pipeline(isolated_index, name, blob, config=config)
    assert result['status'] == 'rebuilt'
    assert result['count'] > 0
    for _ in range(3):
        assert run_findings_pipeline(isolated_index, name, blob, config=config)['status'] == 'unchanged'
    row = isolated_index.execute('SELECT hold_state, findings_revision FROM sessions WHERE session_id=?', (name,)).fetchone()
    assert row['findings_revision']
    assert row['hold_state'] == 'auto_redacted'


def test_badge_scan_does_not_drop_later_sessions_in_a_batch(isolated_index):
    blobs = [session(name, text) for name, text in CASES.items()]
    blobs.insert(0, session('before', 'Ordinary first session'))
    blobs.append(session('after', 'Ordinary final session'))
    assert index.upsert_sessions(isolated_index, blobs) == len(blobs)
    assert {r['session_id'] for r in isolated_index.execute('SELECT session_id FROM sessions')} == {s['session_id'] for s in blobs}


def test_real_parser_store_and_findings_keep_every_session(isolated_index, tmp_path, monkeypatch):
    projects = tmp_path / 'projects'
    project = projects / 'synthetic-project'
    project.mkdir(parents=True)
    monkeypatch.setattr(parser, 'PROJECTS_DIR', projects)
    monkeypatch.setattr(daemon, 'load_config', lambda: {'findings_engines': ['regex_secrets', 'regex_pii']})
    monkeypatch.setattr(daemon, 'discover_projects', lambda **kw: [{'source': 'claude', 'dir_name': project.name, 'locator': None}])
    cases = dict(CASES, ordinary='Keep this final session')
    for sid, content in cases.items():
        rows = [
            {'type': 'user', 'timestamp': 1706000000000, 'message': {'content': 'Inspect the output'}, 'cwd': str(project)},
            {'type': 'assistant', 'timestamp': 1706000001000, 'message': {'model': 'm', 'content': [
                {'type': 'text', 'text': content},
                {'type': 'tool_use', 'id': 'synthetic-tool', 'name': 'Bash', 'input': {'command': 'echo ' + TOKEN}},
            ], 'usage': {'input_tokens': 1, 'output_tokens': 1}}},
        ]
        (project / f'{sid}.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    report = daemon.Scanner().scan_once_strict(['claude'])
    assert report['ok'], report
    rows = isolated_index.execute('SELECT session_id, hold_state, findings_revision FROM sessions').fetchall()
    assert {r['session_id'] for r in rows} == set(cases)
    assert all(r['hold_state'] == 'auto_redacted' and r['findings_revision'] for r in rows)


def test_local_titles_and_search_snippets_redact_overlong_email(isolated_index):
    from clawjournal.events.search.render import _scrub_snippet
    from clawjournal.session_titles import fork_title_suffix
    address = 'a' * 70 + '@a.io'
    text = 'contact ' + address
    blob = session('title', text)
    blob['messages'][0]['content'] = address
    index.upsert_sessions(isolated_index, [blob])
    row = isolated_index.execute('SELECT display_title FROM sessions WHERE session_id="title"').fetchone()
    assert '@' not in row['display_title']
    assert '[REDACTED_EMAIL]' in row['display_title']
    assert _scrub_snippet(text, anonymizer=Anonymizer(enabled=False)) == 'contact [REDACTED_EMAIL]'
    assert address not in fork_title_suffix({'fork_nickname': address})


@pytest.mark.parametrize('padding', [0, 80000])
def test_arbitrary_import_aliases_cannot_hide_real_email(padding):
    text = 'import numpy as alice\nimport numpy as audit\n' + '#' * padding + '\nvalue = alice.smith@audit.test\n'
    assert not cc.code_context(text).protected
    assert any(f['match'] == 'alice.smith@audit.test' for f in secrets.scan_text(text))
    assert any(f['match'] == 'alice.smith@audit.test' for f in pii.scan_text_for_pii(text))


def test_python_syntax_warnings_do_not_print_source(capfd):
    # The source may itself contain secrets. Parsing it as a hint must not
    # print an invalid-escape warning with the original source line.
    text = 'value = "\\q alice@audit.test"\n'
    assert secrets.redact_text(text)[0] == text.replace(EMAIL, '[REDACTED_EMAIL]')
    assert capfd.readouterr().err == ''
