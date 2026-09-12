"""Append-only archive, restore and deletion history for contribution tasks."""

from sqlalchemy import Connection
from app.migrations.core import Migration


def _inactive(task: str) -> str:
    return f"""EXISTS (SELECT 1 FROM task_visibility_versions v WHERE v.task_id={task}
        AND v.state IN ('archived','deleted') AND NOT EXISTS
        (SELECT 1 FROM task_visibility_versions n WHERE n.task_id=v.task_id AND n.sequence>v.sequence))"""


def _job_matches(job: str, task: str) -> str:
    return f"""(
        json_extract({job}.payload, '$.task_id') = {task}
        OR json_extract({job}.payload, '$.execution_attempt_id') IN
            (SELECT id FROM execution_attempts WHERE task_id = {task})
        OR json_extract({job}.payload, '$.publish_intent_id') IN
            (SELECT id FROM publish_intents WHERE task_id = {task})
        OR {job}.id IN (SELECT job_id FROM workbench_events WHERE task_id = {task})
        OR {job}.id IN (SELECT job_id FROM execution_stage_runs WHERE
            execution_attempt_id IN (SELECT id FROM execution_attempts WHERE task_id = {task}))
    )"""


def upgrade(connection: Connection) -> None:
    connection.exec_driver_sql("""CREATE TABLE task_visibility_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        task_id VARCHAR(36) NOT NULL REFERENCES contribution_tasks(id) ON DELETE RESTRICT,
        sequence INTEGER NOT NULL, state VARCHAR(16) NOT NULL,
        task_record_hash VARCHAR(64) NOT NULL, previous_hash VARCHAR(64),
        record_hash VARCHAR(64) NOT NULL UNIQUE, created_at DATETIME NOT NULL,
        CONSTRAINT uq_task_visibility_sequence UNIQUE(task_id,sequence),
        CONSTRAINT ck_task_visibility_sequence CHECK(sequence>=1),
        CONSTRAINT ck_task_visibility_state CHECK(state IN ('active','archived','deleted'))
    )""")
    connection.exec_driver_sql("CREATE INDEX ix_task_visibility_versions_task_id ON task_visibility_versions(task_id)")
    connection.exec_driver_sql("""CREATE TRIGGER task_visibility_chain BEFORE INSERT ON task_visibility_versions
        WHEN NOT EXISTS (SELECT 1 FROM contribution_tasks WHERE id=NEW.task_id AND record_hash=NEW.task_record_hash)
        OR (NEW.sequence=1 AND (NEW.state!='archived' OR NEW.previous_hash IS NOT NULL))
        OR (NEW.sequence>1 AND NOT EXISTS (SELECT 1 FROM task_visibility_versions v
            WHERE v.task_id=NEW.task_id AND v.sequence=NEW.sequence-1 AND v.record_hash=NEW.previous_hash
            AND ((v.state='active' AND NEW.state='archived') OR
                 (v.state='archived' AND NEW.state IN ('active','deleted')))))
        BEGIN SELECT RAISE(ABORT, 'Task visibility chain mismatch'); END""")
    for operation in ("UPDATE", "DELETE"):
        connection.exec_driver_sql(f"""CREATE TRIGGER task_visibility_no_{operation.lower()}
            BEFORE {operation} ON task_visibility_versions
            BEGIN SELECT RAISE(ABORT, 'Task visibility history is immutable'); END""")
    connection.exec_driver_sql(f"""CREATE TRIGGER task_visibility_idle BEFORE INSERT ON task_visibility_versions
        WHEN NEW.state!='active' AND EXISTS (SELECT 1 FROM jobs j WHERE j.state NOT IN
            ('succeeded','failed','cancelled','timed_out') AND {_job_matches('j', 'NEW.task_id')})
        BEGIN SELECT RAISE(ABORT, 'Task has active jobs'); END""")
    for operation in ("INSERT", "UPDATE"):
        connection.exec_driver_sql(f"""CREATE TRIGGER jobs_inactive_task_{operation.lower()}
            BEFORE {operation} ON jobs WHEN NEW.state NOT IN ('succeeded','failed','cancelled','timed_out')
            AND EXISTS (SELECT 1 FROM contribution_tasks t WHERE {_inactive('t.id')} AND {_job_matches('NEW', 't.id')})
            BEGIN SELECT RAISE(ABORT, 'Task is archived or deleted'); END""")
    for table in (
        "workbench_events", "contribution_task_state_versions", "plan_versions",
        "plan_locks", "plan_approvals", "plan_conversation_entries",
        "execution_attempts", "review_runs", "publish_intents", "task_lifecycle_marks",
    ):
        connection.exec_driver_sql(f"""CREATE TRIGGER {table}_task_active BEFORE INSERT ON {table}
            WHEN {_inactive('NEW.task_id')}
            BEGIN SELECT RAISE(ABORT, 'Task is archived or deleted'); END""")


MIGRATION = Migration(
    revision="0031_task_visibility",
    description="Append-only task archive, restore and deletion",
    signature="task-visibility-v1:chain:idle:job-retry:task-write-guards",
    upgrade=upgrade,
    recovery="Stop all writers and restore the pre-upgrade SQLite and artifact backup together. "
    "Do not edit visibility history or run an older binary against this schema.",
)
