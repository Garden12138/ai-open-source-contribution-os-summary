from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE pull_request_events (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        draft_pull_request_id VARCHAR(36) NOT NULL,
        task_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        remote_event_id VARCHAR(128) NOT NULL,
        event_type VARCHAR(40) NOT NULL,
        payload JSON NOT NULL,
        payload_hash VARCHAR(64) NOT NULL,
        previous_event_hash VARCHAR(64),
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        occurred_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_pull_request_event_remote
            UNIQUE (draft_pull_request_id, remote_event_id),
        CONSTRAINT ck_pull_request_event_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_pull_request_event_type
            CHECK (
                event_type IN (
                    'opened', 'review', 'check', 'changes_requested',
                    'merged', 'closed'
                )
            ),
        FOREIGN KEY(draft_pull_request_id)
            REFERENCES draft_pull_requests (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_pull_request_events_task_id "
        "ON pull_request_events (task_id)"
    ),
    (
        "CREATE INDEX ix_pull_request_events_created_at "
        "ON pull_request_events (created_at)"
    ),
    (
        "CREATE UNIQUE INDEX ix_pull_request_events_record_hash "
        "ON pull_request_events (record_hash)"
    ),
    """
    CREATE TRIGGER pull_request_events_provenance_insert
    BEFORE INSERT ON pull_request_events
    WHEN NOT (
        json_valid(NEW.payload)
        AND json_type(NEW.payload) = 'object'
        AND EXISTS (
            SELECT 1
            FROM draft_pull_requests AS draft
            WHERE draft.id = NEW.draft_pull_request_id
              AND draft.task_id = NEW.task_id
        )
        AND (
            (
                NEW.previous_event_hash IS NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM pull_request_events AS existing
                    WHERE existing.draft_pull_request_id =
                        NEW.draft_pull_request_id
                )
            )
            OR (
                NEW.previous_event_hash IS NOT NULL
                AND EXISTS (
                    SELECT 1
                    FROM pull_request_events AS previous
                    WHERE previous.draft_pull_request_id =
                        NEW.draft_pull_request_id
                      AND previous.record_hash = NEW.previous_event_hash
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid pull request event provenance');
    END
    """,
    """
    CREATE TRIGGER pull_request_events_no_update
    BEFORE UPDATE ON pull_request_events
    BEGIN
        SELECT RAISE(ABORT, 'pull request events are immutable');
    END
    """,
    """
    CREATE TRIGGER pull_request_events_no_delete
    BEFORE DELETE ON pull_request_events
    BEGIN
        SELECT RAISE(ABORT, 'pull request events are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0023_pull_request_events supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0023_pull_request_events",
    description="Add append-only PullRequestEvent observations",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop PR-event writers and preserve the SQLite file before "
        "upgrade. This additive revision only stores local observations. "
        "On failure restore the backup; never rewrite, reorder, or "
        "deduplicate by deleting events. Replay the same remote_event_id "
        "instead of inventing a replacement hash."
    ),
)
