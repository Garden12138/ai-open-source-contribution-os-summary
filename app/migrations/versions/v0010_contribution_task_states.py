from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Connection, text

from app.migrations.core import Migration, MigrationError
from app.provenance import content_hash


_SCHEMA_VERSION = "1"
_INITIAL_REASON = "created_from_analysis"
_STATEMENTS = (
    """
    CREATE TABLE contribution_task_state_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        task_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        sequence BIGINT NOT NULL,
        from_state VARCHAR(40),
        to_state VARCHAR(40) NOT NULL,
        reason_code VARCHAR(100) NOT NULL,
        task_record_hash VARCHAR(64) NOT NULL,
        previous_state_hash VARCHAR(64),
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_contribution_task_state_sequence
            UNIQUE (task_id, sequence),
        CONSTRAINT ck_contribution_task_state_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_contribution_task_state_sequence
            CHECK (sequence >= 1),
        CONSTRAINT ck_contribution_task_state_value
            CHECK (
                to_state IN (
                    'planning', 'plan_approved', 'executing', 'reviewing',
                    'ready', 'draft_pr', 'changes_requested', 'merged',
                    'rewarded'
                )
            ),
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_contribution_task_state_versions_task_id "
        "ON contribution_task_state_versions (task_id)"
    ),
    (
        "CREATE INDEX ix_contribution_task_state_versions_to_state "
        "ON contribution_task_state_versions (to_state)"
    ),
    (
        "CREATE UNIQUE INDEX ix_contribution_task_state_versions_record_hash "
        "ON contribution_task_state_versions (record_hash)"
    ),
    (
        "CREATE INDEX ix_contribution_task_state_versions_created_at "
        "ON contribution_task_state_versions (created_at)"
    ),
    """
    CREATE TRIGGER contribution_task_state_provenance_insert
    BEFORE INSERT ON contribution_task_state_versions
    WHEN NOT (
        EXISTS (
            SELECT 1
            FROM contribution_tasks AS task
            WHERE task.id = NEW.task_id
              AND task.record_hash = NEW.task_record_hash
        )
        AND (
            (
                NEW.sequence = 1
                AND NEW.from_state IS NULL
                AND NEW.to_state = 'planning'
                AND NEW.reason_code = 'created_from_analysis'
                AND NEW.previous_state_hash IS NULL
            )
            OR (
                NEW.sequence > 1
                AND EXISTS (
                    SELECT 1
                    FROM contribution_task_state_versions AS previous
                    WHERE previous.task_id = NEW.task_id
                      AND previous.sequence = NEW.sequence - 1
                      AND previous.to_state = NEW.from_state
                      AND previous.record_hash = NEW.previous_state_hash
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid contribution task state provenance');
    END
    """,
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
            AND NEW.to_state = 'executing'
        )
        OR (NEW.from_state = 'merged' AND NEW.to_state = 'rewarded')
    )
    BEGIN
        SELECT RAISE(ABORT, 'illegal contribution task state transition');
    END
    """,
    """
    CREATE TRIGGER contribution_task_state_no_update
    BEFORE UPDATE ON contribution_task_state_versions
    BEGIN
        SELECT RAISE(ABORT, 'contribution task states are immutable');
    END
    """,
    """
    CREATE TRIGGER contribution_task_state_no_delete
    BEFORE DELETE ON contribution_task_state_versions
    BEGIN
        SELECT RAISE(ABORT, 'contribution task states are immutable');
    END
    """,
)
_BACKFILL_SIGNATURE = (
    "backfill each existing contribution task at sequence 1 from null to "
    "planning with reason created_from_analysis and a canonical record hash"
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0010_contribution_task_states supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())
    tasks = connection.execute(
        text(
            "SELECT id, record_hash, created_at "
            "FROM contribution_tasks ORDER BY id"
        )
    ).mappings()
    for task in tasks:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "task_id": str(task["id"]),
            "task_record_hash": str(task["record_hash"]),
            "sequence": 1,
            "from_state": None,
            "to_state": "planning",
            "reason_code": _INITIAL_REASON,
            "previous_state_hash": None,
        }
        connection.execute(
            text(
                "INSERT INTO contribution_task_state_versions ("
                "id, task_id, schema_version, sequence, from_state, to_state, "
                "reason_code, task_record_hash, previous_state_hash, "
                "record_hash, created_at"
                ") VALUES ("
                ":id, :task_id, :schema_version, :sequence, :from_state, "
                ":to_state, :reason_code, :task_record_hash, "
                ":previous_state_hash, :record_hash, :created_at"
                ")"
            ),
            {
                "id": str(uuid4()),
                **payload,
                "record_hash": content_hash(payload),
                "created_at": task["created_at"],
            },
        )


MIGRATION = Migration(
    revision="0010_contribution_task_states",
    description="Add append-only CAS ContributionTask state versions",
    signature=(
        "\n-- statement --\n".join(
            " ".join(statement.split()) for statement in _STATEMENTS
        )
        + "\n-- backfill --\n"
        + _BACKFILL_SIGNATURE
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds an append-only state-version table and backfills "
        "one planning state for every existing task without changing task "
        "roots. Stop task writers and preserve the SQLite file before upgrade. "
        "On failure restore it; never delete state history, weaken legal "
        "transition checks, or fabricate a previous-state hash."
    ),
)
