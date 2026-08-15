from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE plan_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        task_id VARCHAR(36) NOT NULL,
        task_state_version_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        version_number BIGINT NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        task_record_hash VARCHAR(64) NOT NULL,
        task_state_record_hash VARCHAR(64) NOT NULL,
        goal TEXT NOT NULL,
        acceptance_criteria JSON NOT NULL,
        files_to_inspect JSON NOT NULL,
        files_likely_to_change JSON NOT NULL,
        implementation_steps JSON NOT NULL,
        tests_to_add_or_run JSON NOT NULL,
        commands_to_run JSON NOT NULL,
        risks JSON NOT NULL,
        questions_for_maintainer JSON NOT NULL,
        content_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_plan_version_number
            UNIQUE (task_id, version_number),
        CONSTRAINT uq_plan_version_content
            UNIQUE (task_id, content_hash),
        CONSTRAINT ck_plan_version_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_plan_version_number
            CHECK (version_number >= 1),
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_state_version_id)
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_plan_versions_task_id ON plan_versions (task_id)",
    (
        "CREATE INDEX ix_plan_versions_task_state_version_id "
        "ON plan_versions (task_state_version_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_plan_versions_idempotency_key "
        "ON plan_versions (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_plan_versions_record_hash "
        "ON plan_versions (record_hash)"
    ),
    "CREATE INDEX ix_plan_versions_created_at ON plan_versions (created_at)",
    """
    CREATE TRIGGER plan_versions_provenance_insert
    BEFORE INSERT ON plan_versions
    WHEN NOT EXISTS (
        SELECT 1
        FROM contribution_tasks AS task
        JOIN contribution_task_state_versions AS state
          ON state.task_id = task.id
        WHERE task.id = NEW.task_id
          AND task.record_hash = NEW.task_record_hash
          AND state.id = NEW.task_state_version_id
          AND state.record_hash = NEW.task_state_record_hash
          AND state.to_state = 'planning'
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid plan version provenance');
    END
    """,
    """
    CREATE TRIGGER plan_versions_no_update
    BEFORE UPDATE ON plan_versions
    BEGIN
        SELECT RAISE(ABORT, 'plan versions are immutable');
    END
    """,
    """
    CREATE TRIGGER plan_versions_no_delete
    BEFORE DELETE ON plan_versions
    BEGIN
        SELECT RAISE(ABORT, 'plan versions are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0011_plan_versions supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0011_plan_versions",
    description="Add immutable structured PlanVersion records",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds immutable structured plan records without changing "
        "task roots or state history. Stop task writers and preserve the SQLite "
        "file before upgrade. On failure restore it; never rewrite task/state "
        "hashes, store shell command strings in place of argv, or disable "
        "provenance triggers to force a plan insert."
    ),
)
