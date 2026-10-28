import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.agent_v2.orchestrator import TaskOrchestrator
from app.workbench import Workbench
from tests.test_agent_v2_orchestrator import prepared


def test_upgrade_backup_restore_preserves_v1_hashes_and_enforces_new_cas(tmp_path,monkeypatch):
    import app.migrations.versions as registry
    migrations=registry.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(registry,'MIGRATIONS',migrations[:-1])
        db,s,task,attempt,_=prepared(tmp_path)
        with db.session() as session:
            wb=Workbench(session,s.artifact_root)
            original=[(e.id,e.record_hash) for e in wb.history(task)]
        backup=tmp_path/'backup.db'
        with sqlite3.connect(db.engine.url.database) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
    report=db.create_schema()
    assert report.applied==('0038_agent_runtime_v2',)
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        assert [(e.id,e.record_hash) for e in wb.history(task)]==original
        j=TaskOrchestrator(wb,s).start(attempt);session.commit()
        cp=j.checkpoint()
        with pytest.raises(IntegrityError):
            j.append('checkpoint',{'checkpoint':cp.model_copy(update={'revision':'e'*64}).model_dump()},key='bad-cas')
        session.rollback()
        assert j.checkpoint()==cp
    restored=Database('sqlite:///'+str(backup))
    restored.create_schema()
    with restored.session() as session:
        assert [(e.id,e.record_hash) for e in Workbench(session,s.artifact_root).history(task)]==original
        assert session.connection().exec_driver_sql('PRAGMA integrity_check').scalar_one()=='ok'
        assert not session.connection().exec_driver_sql('PRAGMA foreign_key_check').all()
