"""A local coordination lock conflict must not kill the worker or retry effects."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.database import Database
from app.worker import ContribOSWorker


def worker_fixture(monkeypatch, advance):
    class Idle:
        def __init__(self): self.calls = 0
        async def run_once(self, **kwargs):
            self.calls += 1
            return None
    worker = ContribOSWorker.__new__(ContribOSWorker)
    worker.database, worker.settings = object(), SimpleNamespace()
    worker.task_erasure = Idle()
    worker.planning_archive = Idle()
    worker.contribution_start = Idle()
    worker.discovery = Idle()
    worker.archives = Idle()
    worker.analysis = worker.execution = None
    monkeypatch.setattr(worker, '_sync_product_experience', lambda **kw: None)
    monkeypatch.setattr('app.agent_runtime.advance_tasks', advance)
    return worker


def test_real_sqlite_busy_defers_only_coordination_until_next_tick(tmp_path, monkeypatch, caplog):
    database = Database('sqlite:///' + str(tmp_path / 'busy.db'))
    with database.engine.begin() as connection:
        connection.execute(text('CREATE TABLE marker (id INTEGER PRIMARY KEY)'))
    def advance(*args):
        with database.session() as session:
            session.execute(text('PRAGMA busy_timeout=0'))
            session.execute(text('BEGIN IMMEDIATE'))
            session.execute(text('INSERT OR IGNORE INTO marker VALUES (1)'))
            session.commit()
    worker = worker_fixture(monkeypatch, advance)
    with database.engine.connect() as holder:
        holder.exec_driver_sql('BEGIN IMMEDIATE')
        assert asyncio.run(worker.run_once()) is None
        assert worker.planning_archive.calls == worker.discovery.calls == 0
        holder.rollback()
    assert asyncio.run(worker.run_once()) is None
    assert asyncio.run(worker.run_once()) is None
    with database.engine.connect() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM marker')).scalar_one() == 1
    assert worker.planning_archive.calls == 2
    assert 'sqlite_busy' in caplog.text
    assert 'BEGIN IMMEDIATE' not in caplog.text and 'busy.db' not in caplog.text
    database.close()


@pytest.mark.parametrize('kind', ['locked', 'io_error', 'non_sqlite'])
def test_unknown_database_failures_still_fail_closed(tmp_path, monkeypatch, kind):
    if kind == 'non_sqlite':
        original = RuntimeError('database is locked')
    else:
        original = sqlite3.OperationalError('sensitive SQL diagnostic must not be logged')
        original.sqlite_errorcode = sqlite3.SQLITE_LOCKED if kind == 'locked' else sqlite3.SQLITE_IOERR
    def advance(*args):
        raise OperationalError('statement', {}, original)
    worker = worker_fixture(monkeypatch, advance)
    with pytest.raises(OperationalError):
        asyncio.run(worker.run_once())
    assert worker.planning_archive.calls == worker.discovery.calls == 0


def test_busy_during_effect_dispatch_is_not_implicitly_retried(monkeypatch):
    worker = worker_fixture(monkeypatch, lambda *args: None)
    original = sqlite3.OperationalError('effect outcome may be ambiguous')
    original.sqlite_errorcode = sqlite3.SQLITE_BUSY
    class FailedEffect:
        calls = 0
        async def run_once(self, **kw):
            self.calls += 1
            raise OperationalError('effect', {}, original)
    worker.contribution_start = FailedEffect()
    with pytest.raises(OperationalError):
        asyncio.run(worker.run_once())
    assert worker.contribution_start.calls == 1 and worker.discovery.calls == 0
