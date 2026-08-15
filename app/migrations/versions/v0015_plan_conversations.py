from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE plan_conversation_entries (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        task_id VARCHAR(36) NOT NULL,
        plan_version_id VARCHAR(36),
        schema_version VARCHAR(32) NOT NULL,
        sequence BIGINT NOT NULL,
        entry_type VARCHAR(20) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        actor_type VARCHAR(40) NOT NULL,
        actor_id VARCHAR(128) NOT NULL,
        content JSON NOT NULL,
        content_hash VARCHAR(64) NOT NULL,
        previous_entry_hash VARCHAR(64),
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_plan_conversation_entry_sequence
            UNIQUE (task_id, sequence),
        CONSTRAINT ck_plan_conversation_entry_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_plan_conversation_entry_sequence
            CHECK (sequence >= 1),
        CONSTRAINT ck_plan_conversation_entry_type
            CHECK (entry_type IN ('message', 'decision')),
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_version_id)
            REFERENCES plan_versions (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_plan_conversation_entries_task_id "
        "ON plan_conversation_entries (task_id)"
    ),
    (
        "CREATE INDEX ix_plan_conversation_entries_plan_version_id "
        "ON plan_conversation_entries (plan_version_id)"
    ),
    (
        "CREATE INDEX ix_plan_conversation_entries_entry_type "
        "ON plan_conversation_entries (entry_type)"
    ),
    (
        "CREATE UNIQUE INDEX ix_plan_conversation_entries_idempotency_key "
        "ON plan_conversation_entries (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_plan_conversation_entries_record_hash "
        "ON plan_conversation_entries (record_hash)"
    ),
    (
        "CREATE INDEX ix_plan_conversation_entries_created_at "
        "ON plan_conversation_entries (created_at)"
    ),
    """
    CREATE TRIGGER plan_conversation_entries_provenance_insert
    BEFORE INSERT ON plan_conversation_entries
    WHEN NOT (
        EXISTS (
            SELECT 1
            FROM contribution_tasks AS task
            WHERE task.id = NEW.task_id
        )
        AND (
            NEW.plan_version_id IS NULL
            OR EXISTS (
                SELECT 1
                FROM plan_versions AS plan
                WHERE plan.id = NEW.plan_version_id
                  AND plan.task_id = NEW.task_id
            )
        )
        AND (
            (
                NEW.sequence = 1
                AND NEW.previous_entry_hash IS NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM plan_conversation_entries AS existing
                    WHERE existing.task_id = NEW.task_id
                )
            )
            OR (
                NEW.sequence > 1
                AND EXISTS (
                    SELECT 1
                    FROM plan_conversation_entries AS previous
                    WHERE previous.task_id = NEW.task_id
                      AND previous.sequence = NEW.sequence - 1
                      AND previous.record_hash = NEW.previous_entry_hash
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM plan_conversation_entries AS later
                    WHERE later.task_id = NEW.task_id
                      AND later.sequence >= NEW.sequence
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid plan conversation provenance');
    END
    """,
    """
    CREATE TRIGGER plan_conversation_entries_no_update
    BEFORE UPDATE ON plan_conversation_entries
    BEGIN
        SELECT RAISE(ABORT, 'plan conversation entries are immutable');
    END
    """,
    """
    CREATE TRIGGER plan_conversation_entries_no_delete
    BEFORE DELETE ON plan_conversation_entries
    BEGIN
        SELECT RAISE(ABORT, 'plan conversation entries are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0015_plan_conversations supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0015_plan_conversations",
    description="Add append-only plan conversation and decision entries",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds an append-only per-task conversation hash chain "
        "without rewriting tasks, plans, locks, or approvals. Stop conversation "
        "writers and preserve the SQLite file before upgrade. On failure restore "
        "it; never renumber entries, fabricate a prior hash, or disable the "
        "immutability triggers."
    ),
)
