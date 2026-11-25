import json
import sqlite3
from contextlib import closing

import pytest

from contribos.cli import main
from contribos.db import connect
from contribos.legacy import import_legacy


def table(db, name, rows):
    columns = list(rows[0])
    db.execute(f'CREATE TABLE {name} ({",".join(columns)})')
    db.executemany(f'INSERT INTO {name} VALUES ({",".join("?" for _ in columns)})',
                   [tuple(row[key] for key in columns) for row in rows])


@pytest.fixture
def old(tmp_path):
    source = tmp_path / 'old?#.sqlite'
    date = '2024-01-10 11:00:00'
    with closing(sqlite3.connect(source)) as db:
        table(db, 'repositories', [dict(id=1, full_name='dev/project', language='Python', description='A project',
            topics='[]', archived=0, disabled=0, license_spdx='MIT', stars=42, forks=3, pushed_at=None,
            has_contributing_guide=1, health_percentage=78, last_synced_at=date)])
        issue = dict(repository_id=1, title='Fix parsing', state='open', labels='["size: s"]', body='Example',
            issue_updated_at=date, source_queries='[]', score_components='{"project_impact":4}', eligible=1,
            filter_reasons='[]', risk_reasons='[]', score_total=70, risk_penalty=1, first_seen_at=date, last_seen_at=date,
            has_bounty=1, is_strategic=0, is_tech_match=1, bounty_amount_usd=80)
        table(db, 'opportunities', [dict(issue, id=n, github_issue_id=100+n, issue_number=n) for n in (1, 2, 3)])
        preference = dict(primary_goal='learning', weekly_hours=8, minimum_bounty_usd=30, auto_scan_enabled=0,
                          auto_scan_local_time='21:00', created_at=date)
        table(db, 'user_preference_versions', [dict(preference, version=n, preferred_languages=json.dumps([lang]))
              for n, lang in [(2, 'Python'), (1, 'Java')]])
        table(db, 'opportunity_disposition_versions', [dict(opportunity_id=n, state=state, sequence=seq,
              reason_code=None, created_at=date) for n, seq, state in [
                  (1, 1, 'dismissed'), (1, 2, 'shortlisted'), (2, 1, 'dismissed'),
                  (3, 1, 'shortlisted'), (3, 2, 'neutral')]])
        table(db, 'contribution_tasks', [dict(id='old-task', opportunity_id=1)])
        table(db, 'publish_intents', [dict(id='intent', title='Fix parsing', upstream_repository='dev/project')])
        table(db, 'draft_pull_requests', [dict(id=n, number=n, html_url=f'https://github.com/dev/project/pull/{n}',
              task_id='old-task', publish_intent_id='intent', head_commit_sha='a'*40, provider=provider, created_at=date)
              for n, provider in [(21, 'github'), (22, 'fake')]])
        table(db, 'pull_request_events', [dict(draft_pull_request_id=21, event_type='merged', occurred_at='2024-02-10 12:00:00')])
        db.commit()
    return source


def test_import_legacy_cli_merge_and_repeat(old, monkeypatch, tmp_path):
    target = tmp_path / 'v2'
    monkeypatch.setenv('CONTRIBOS_HOME', str(target))
    before = old.read_bytes()
    assert main(['import-legacy', '--db', str(old)]) == 0
    destination = target / 'contribos.db'
    with closing(connect(destination)) as db:
        assert db.execute('SELECT count(*) FROM repositories').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM opportunities').fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM publications').fetchone()[0] == 0
        assert dict(db.execute('SELECT opportunity_id,mark FROM opportunity_marks')) == {'101':'starred','102':'ignored'}
        prefs = {k: json.loads(v) for k, v in db.execute('SELECT key,value_json FROM settings')}
        assert prefs == dict(preferred_languages=['Python'], scan_time='21:00', auto_scan_enabled=False,
                             primary_goal='learning', weekly_hours=8, minimum_bounty_usd=30)
        [pr] = db.execute('SELECT * FROM pull_requests')
        assert pr['task_id'] is None and pr['merged'] and pr['number'] == 21
        assert json.loads(pr['data_json'])['merged_at'] == '2024-02-10T12:00:00+00:00'
        db.execute("UPDATE opportunities SET title='Local title'")
        assert not any(import_legacy(old, destination).values())
        assert db.execute('SELECT DISTINCT title FROM opportunities').fetchone()[0] == 'Local title'
    assert old.read_bytes() == before


def test_import_rejects_same_database_and_rolls_back_invalid_input(old, tmp_path):
    with pytest.raises(ValueError):
        import_legacy(old, old)
    with closing(sqlite3.connect(old)) as db:
        db.execute("UPDATE opportunities SET labels='broken' WHERE id=2")
        db.commit()
    dest = tmp_path / 'new.db'
    with pytest.raises(ValueError):
        import_legacy(old, dest)
    with closing(connect(dest)) as db:
        assert db.execute('SELECT COUNT(*) FROM repositories').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM opportunities').fetchone()[0] == 0
    missing = tmp_path / 'absent'
    assert main(['import-legacy', '--db', str(missing)]) == 1
    assert not missing.exists()
