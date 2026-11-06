"""Forward-only response event upgrade with an actual old database/backup."""
import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.agent_v2.journal import AgentJournal
from app.workbench import Workbench
from tests.test_native_protocol_repairs import fixture


def test_0041_upgrade_preserves_old_rows_and_backup_recovery(tmp_path, monkeypatch):
    from app.migrations import versions
    with monkeypatch.context() as patch:
        patch.setattr(versions, 'MIGRATIONS', tuple(m for m in versions.MIGRATIONS if m.revision <= '0041_native_model_tools'))
        db, settings, task, sid, *_ = fixture(tmp_path)
    backup = tmp_path / 'before-harness.db'
    with sqlite3.connect(db.engine.url.database) as source:
        before = list(source.execute('SELECT * FROM workbench_events ORDER BY task_id, sequence'))
        with sqlite3.connect(backup) as target:
            source.backup(target)
    assert db.create_schema().applied == ('0042_agent_response_evidence', '0043_implementation_progress', '0044_native_read_batches', '0045_source_file_observations', '0046_read_capacity')
    with sqlite3.connect(db.engine.url.database) as source:
        assert list(source.execute('SELECT * FROM workbench_events ORDER BY task_id, sequence')) == before
        assert source.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not list(source.execute('PRAGMA foreign_key_check'))
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        aid = j.wb.artifact({'version': 'agent-model-response-v1'})
        with pytest.raises(IntegrityError):
            j.append('model_response', {'artifact_id': aid, 'prompt_artifact': 'a' * 64}, key='forged-response')
        session.rollback()
    restored = Database('sqlite:///' + str(backup))
    assert restored.current_revision() == '0041_native_model_tools'
    assert restored.create_schema().applied == ('0042_agent_response_evidence', '0043_implementation_progress', '0044_native_read_batches', '0045_source_file_observations', '0046_read_capacity')
    with restored.engine.connect() as connection:
        assert list(connection.connection.driver_connection.execute('SELECT * FROM workbench_events ORDER BY task_id, sequence')) == before
    restored.close()
