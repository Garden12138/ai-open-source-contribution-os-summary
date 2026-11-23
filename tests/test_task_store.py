import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import pytest

from contribos.db import MIGRATIONS, connect, migrate
from contribos.tasks.issue import IssueRef
from contribos.tasks.store import TaskError, TaskStore


def ready_task(store):
    task = store.create(IssueRef('owner/repo', 1), 'codex', 'host')
    store.ready(task['id'], {'title': 'Issue'}, 'main', 'abc', 'work')
    return task['id']


def test_task_crud_and_message_idempotency(tmp_path):
    with closing(connect(tmp_path / 'db')) as db:
        migrate(db)
        store = TaskStore(db)
        tid = ready_task(store)
        assert store.list_tasks()[0]['title'] == 'Issue'
        original, created = store.enqueue(tid, 'first', 'message-1')
        repeat, repeated = store.enqueue(tid, 'replaced', 'message-1', interrupt=True)
        assert created and not repeated and original == repeat
        assert original['prompt_text'] == 'first'
        other = ready_task(store)
        with pytest.raises(TaskError, match='already in use'):
            store.enqueue(other, 'bad', 'message-1')
        store.delete(tid)
        assert store.turns(tid) == []
        with pytest.raises(TaskError, match='does not exist'):
            store.get(tid)


def test_concurrent_database_claims_cannot_start_two_turns(tmp_path):
    path = tmp_path / 'db'
    with closing(connect(path)) as db:
        migrate(db)
        store = TaskStore(db)
        tid = ready_task(store)
        first = store.claim(tid)
        assert store.finish(tid, first['id'], 'completed')
        assert not store.finish(tid, first['id'], 'failed')
        barrier = Barrier(2)

        def submit(text):
            with closing(connect(path)) as connection:
                worker = TaskStore(connection)
                worker.enqueue(tid, text, text)
                barrier.wait(timeout=5)
                return worker.claim(tid)

        with ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(submit, ['one', 'two']))
        assert sum(turn is not None for turn in claims) == 1
        assert store.get(tid)['agent_state'] == 'running'
        assert [turn['status'] for turn in store.turns(tid)] == ['completed', 'running', 'queued']


def test_m3_migration_upgrades_legacy_tasks_and_repairs_duplicate_running_turns(tmp_path):
    old = tmp_path / 'old'
    old.mkdir()
    (old / '0001_init.sql').write_bytes((MIGRATIONS / '0001_init.sql').read_bytes())
    with closing(connect(tmp_path / 'db')) as db:
        migrate(db, old)
        db.execute("""INSERT INTO tasks(id,repo_full_name,issue_number,title,agent_profile,isolation,
                   created_at,updated_at) VALUES ('task','owner/repo',1,'Issue','codex','host','2026','2026')""")
        db.executemany("INSERT INTO turns(id,task_id,kind,status) VALUES (?,'task','user','running')",
                       [('older',), ('latest',)])
        migrate(db)
        assert [tuple(row) for row in db.execute('SELECT id,status,prompt_text FROM turns ORDER BY rowid')] == [
            ('older', 'interrupted', None), ('latest', 'running', None)]
        assert db.execute('SELECT runtime_managed FROM tasks').fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE turns SET status='running' WHERE id='older'")
