import sqlite3

import pytest

from contribos.db import connect, migrate

TABLES = {
    'repositories', 'opportunities', 'scan_runs', 'daily_picks', 'opportunity_marks',
    'tasks', 'turns', 'task_events', 'task_diffs', 'reviews', 'publications',
    'pull_requests', 'settings', 'schema_migrations',
}


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / 'contribos.db')
    migrate(connection)
    yield connection
    connection.close()


def test_empty_database_and_repeat_migration(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    db.execute("INSERT INTO settings VALUES ('language', '\"zh\"', '2026-11-17T00:00:00Z')")
    before = list(map(tuple, db.execute('SELECT * FROM schema_migrations')))
    migrate(db)
    assert list(map(tuple, db.execute('SELECT * FROM schema_migrations'))) == before
    assert db.execute('SELECT value_json FROM settings').fetchone()[0] == '"zh"'
    assert before[0][1].endswith('Z')


def test_upgrade_order_and_failed_migration_are_atomic(db, tmp_path):
    directory = tmp_path / 'migrations'
    directory.mkdir()
    (directory / '0003_finish.sql').write_text('ALTER TABLE example ADD COLUMN added TEXT;')
    (directory / '0002_example.sql').write_text('CREATE TABLE example (id INTEGER); INSERT INTO example VALUES (1);')
    migrate(db, directory)
    assert db.execute('SELECT id, added FROM example').fetchone()[:] == (1, None)
    (directory / '0004_broken.sql').write_text('CREATE TABLE partial (id INTEGER); INSERT INTO missing VALUES (1);')
    with pytest.raises(sqlite3.OperationalError):
        migrate(db, directory)
    assert db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone() is None
    assert db.execute("SELECT 1 FROM schema_migrations WHERE version='0004_broken.sql'").fetchone() is None
    (directory / '0004_broken.sql').write_text('CREATE TABLE partial (id INTEGER);')
    migrate(db, directory)
    assert db.execute('SELECT COUNT(*) FROM partial').fetchone()[0] == 0


@pytest.fixture
def task(db):
    db.execute("""INSERT INTO tasks
        (id, repo_full_name, issue_number, title, agent_profile, isolation, created_at, updated_at)
        VALUES ('task', 'owner/repo', 1, 'Example', 'codex', 'host', '2026-11-17T00:00:00Z', '2026-11-17T00:00:00Z')""")
    return db


@pytest.mark.parametrize('column', ['status', 'agent_state', 'isolation'])
def test_task_checks(task, column):
    with pytest.raises(sqlite3.IntegrityError):
        task.execute(f"UPDATE tasks SET {column}='not_allowed'")


@pytest.mark.parametrize('kind,status', [('invalid', 'queued'), ('user', 'invalid')])
def test_turn_checks(task, kind, status):
    with pytest.raises(sqlite3.IntegrityError):
        task.execute('INSERT INTO turns(id,task_id,kind,status) VALUES (?,?,?,?)', ('turn', 'task', kind, status))


def test_turn_idempotency_and_foreign_keys(task):
    sql = 'INSERT INTO turns(id,task_id,kind,client_message_id) VALUES (?,?,?,?)'
    task.execute(sql, ('a', 'task', 'user', 'message'))
    with pytest.raises(sqlite3.IntegrityError):
        task.execute(sql, ('b', 'task', 'user', 'message'))
    with pytest.raises(sqlite3.IntegrityError):
        task.execute(sql, ('c', 'missing', 'user', None))


@pytest.mark.parametrize('table,column', [('reviews', 'status'), ('publications', 'kind'), ('publications', 'status')])
def test_review_and_publication_checks(task, table, column):
    if table == 'reviews':
        task.execute("INSERT INTO reviews(id,task_id,agent_profile,diff_hash_before,status,created_at) VALUES ('review','task','cursor','hash','running','2026-11-17T00:00:00Z')")
    else:
        task.execute("INSERT INTO publications(id,task_id,kind,preview_json,preview_hash,status,created_at) VALUES ('pub','task','create_pr','{}','hash','previewed','2026-11-17T00:00:00Z')")
    with pytest.raises(sqlite3.IntegrityError):
        task.execute(f"UPDATE {table} SET {column}='not_allowed'")


def test_mark_check_and_opportunity_uniqueness(db):
    db.execute("INSERT INTO repositories VALUES ('owner/repo','{}','2026-11-17T00:00:00Z')")
    sql = "INSERT INTO opportunities(id,repo_full_name,issue_number,title,url,state,first_seen_at,last_seen_at) VALUES (?,'owner/repo',1,'Example','https://github.com/owner/repo/issues/1','open','2026-11-17T00:00:00Z','2026-11-17T00:00:00Z')"
    db.execute(sql, ('opp',))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, ('other',))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO opportunity_marks VALUES ('opp','invalid','','2026-11-17T00:00:00Z')")
