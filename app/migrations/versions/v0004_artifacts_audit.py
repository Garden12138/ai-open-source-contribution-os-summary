from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE artifacts (
        id VARCHAR(64) NOT NULL PRIMARY KEY,
        algorithm VARCHAR(16) NOT NULL,
        size_bytes BIGINT NOT NULL,
        media_type VARCHAR(255) NOT NULL,
        storage_key VARCHAR(255) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_artifact_algorithm CHECK (algorithm = 'sha256'),
        CONSTRAINT ck_artifact_size CHECK (size_bytes >= 0)
    )
    """,
    "CREATE INDEX ix_artifacts_created_at ON artifacts (created_at)",
    """
    CREATE TABLE job_artifacts (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL,
        artifact_id VARCHAR(64) NOT NULL,
        role VARCHAR(80) NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_job_artifact_role
            UNIQUE (job_id, artifact_id, role),
        FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
        FOREIGN KEY(artifact_id) REFERENCES artifacts (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_job_artifacts_job_id ON job_artifacts (job_id)",
    "CREATE INDEX ix_job_artifacts_artifact_id ON job_artifacts (artifact_id)",
    """
    CREATE TABLE audit_events (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        sequence BIGINT NOT NULL UNIQUE,
        event_type VARCHAR(100) NOT NULL,
        actor_type VARCHAR(40) NOT NULL,
        actor_id VARCHAR(128) NOT NULL,
        correlation_id VARCHAR(128) NOT NULL,
        payload JSON NOT NULL,
        payload_hash VARCHAR(64) NOT NULL,
        previous_event_hash VARCHAR(64),
        event_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_audit_sequence CHECK (sequence >= 1)
    )
    """,
    "CREATE UNIQUE INDEX ix_audit_events_sequence ON audit_events (sequence)",
    "CREATE INDEX ix_audit_events_event_type ON audit_events (event_type)",
    "CREATE INDEX ix_audit_events_correlation_id ON audit_events (correlation_id)",
    "CREATE INDEX ix_audit_events_created_at ON audit_events (created_at)",
    """
    CREATE TRIGGER artifacts_no_update
    BEFORE UPDATE ON artifacts
    BEGIN
        SELECT RAISE(ABORT, 'artifacts are immutable');
    END
    """,
    """
    CREATE TRIGGER artifacts_no_delete
    BEFORE DELETE ON artifacts
    BEGIN
        SELECT RAISE(ABORT, 'artifacts are immutable');
    END
    """,
    """
    CREATE TRIGGER job_artifacts_no_update
    BEFORE UPDATE ON job_artifacts
    BEGIN
        SELECT RAISE(ABORT, 'job artifact links are immutable');
    END
    """,
    """
    CREATE TRIGGER job_artifacts_no_delete
    BEFORE DELETE ON job_artifacts
    BEGIN
        SELECT RAISE(ABORT, 'job artifact links are immutable');
    END
    """,
    """
    CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit events are append-only');
    END
    """,
    """
    CREATE TRIGGER audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit events are append-only');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0004_artifacts_audit supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0004_artifacts_audit",
    description="Add immutable artifacts, job links, and audit hash chain",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds immutable metadata tables and triggers. Stop writers "
        "and preserve both the SQLite file and artifact root before upgrade. On "
        "failure restore both together; never remove immutable triggers or alter "
        "hashes to force validation."
    ),
)
