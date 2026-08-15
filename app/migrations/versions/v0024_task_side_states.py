from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    "DROP TRIGGER contribution_task_state_legal_insert",
    """
    CREATE TRIGGER contribution_task_state_legal_insert
    BEFORE INSERT ON contribution_task_state_versions
    WHEN NEW.sequence > 1 AND NOT (
        (NEW.from_state = 'planning' AND NEW.to_state = 'plan_approved')
        OR (
            NEW.from_state = 'plan_approved'
            AND NEW.to_state IN ('planning', 'executing')
        )
        OR (NEW.from_state = 'executing' AND NEW.to_state = 'reviewing')
        OR (
            NEW.from_state = 'reviewing'
            AND NEW.to_state IN ('executing', 'ready')
        )
        OR (
            NEW.from_state = 'ready'
            AND NEW.to_state IN ('planning', 'draft_pr')
        )
        OR (
            NEW.from_state = 'draft_pr'
            AND NEW.to_state IN ('changes_requested', 'merged')
        )
        OR (
            NEW.from_state = 'changes_requested'
            AND NEW.to_state IN ('planning', 'executing')
        )
        OR (NEW.from_state = 'merged' AND NEW.to_state = 'rewarded')
    )
    BEGIN
        SELECT RAISE(ABORT, 'illegal contribution task state transition');
    END
    """,
    """
    CREATE TABLE task_lifecycle_marks (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        task_id VARCHAR(36) NOT NULL UNIQUE,
        schema_version VARCHAR(32) NOT NULL,
        from_state VARCHAR(40) NOT NULL,
        mark VARCHAR(40) NOT NULL,
        reason_code VARCHAR(100) NOT NULL,
        actor_type VARCHAR(40) NOT NULL,
        actor_id VARCHAR(128) NOT NULL,
        state_version_id VARCHAR(36) NOT NULL,
        state_record_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_task_lifecycle_mark_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_task_lifecycle_mark_value
            CHECK (mark IN ('failed', 'rejected', 'abandoned')),
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT,
        FOREIGN KEY(state_version_id)
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_task_lifecycle_marks_created_at "
        "ON task_lifecycle_marks (created_at)"
    ),
    (
        "CREATE UNIQUE INDEX ix_task_lifecycle_marks_task_id "
        "ON task_lifecycle_marks (task_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_task_lifecycle_marks_record_hash "
        "ON task_lifecycle_marks (record_hash)"
    ),
    """
    CREATE TRIGGER task_lifecycle_marks_provenance_insert
    BEFORE INSERT ON task_lifecycle_marks
    WHEN NOT EXISTS (
        SELECT 1
        FROM contribution_task_state_versions AS current
        WHERE current.id = NEW.state_version_id
          AND current.task_id = NEW.task_id
          AND current.to_state = NEW.from_state
          AND current.record_hash = NEW.state_record_hash
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid task lifecycle mark provenance');
    END
    """,
    """
    CREATE TRIGGER task_lifecycle_marks_no_update
    BEFORE UPDATE ON task_lifecycle_marks
    BEGIN
        SELECT RAISE(ABORT, 'task lifecycle marks are immutable');
    END
    """,
    """
    CREATE TRIGGER task_lifecycle_marks_no_delete
    BEFORE DELETE ON task_lifecycle_marks
    BEGIN
        SELECT RAISE(ABORT, 'task lifecycle marks are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0024_task_side_states supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0024_task_side_states",
    description=(
        "Allow changes_requested to return to planning and persist "
        "failed/rejected/abandoned marks without rewriting the state table"
    ),
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop task-state writers and preserve the SQLite file before "
        "upgrade. This revision only replaces the legal-transition trigger "
        "and adds an immutable marks table. On failure restore the backup; "
        "never rewrite historical to_state values or delete a mark."
    ),
)
