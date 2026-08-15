from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE jobs (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        kind VARCHAR(64) NOT NULL,
        state VARCHAR(32) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL,
        payload JSON NOT NULL,
        payload_hash VARCHAR(64) NOT NULL,
        result_data JSON NOT NULL,
        scan_run_id VARCHAR(36),
        attempt_count INTEGER NOT NULL,
        max_attempts INTEGER NOT NULL,
        timeout_seconds INTEGER NOT NULL,
        run_after DATETIME NOT NULL,
        lease_owner VARCHAR(128),
        lease_expires_at DATETIME,
        heartbeat_at DATETIME,
        cancel_requested_at DATETIME,
        progress_current INTEGER NOT NULL,
        progress_total INTEGER,
        progress_message VARCHAR(500),
        error_code VARCHAR(80),
        error_message TEXT,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        started_at DATETIME,
        completed_at DATETIME,
        CONSTRAINT uq_job_kind_idempotency UNIQUE (kind, idempotency_key),
        CONSTRAINT ck_job_state CHECK (
            state IN (
                'queued', 'leased', 'running', 'succeeded',
                'failed', 'cancelled', 'timed_out'
            )
        ),
        CONSTRAINT ck_job_attempts CHECK (
            attempt_count >= 0
            AND max_attempts >= 1
            AND attempt_count <= max_attempts
        ),
        CONSTRAINT ck_job_timeout CHECK (timeout_seconds >= 1),
        CONSTRAINT ck_job_progress CHECK (
            progress_current >= 0
            AND (progress_total IS NULL OR progress_total >= progress_current)
        ),
        CONSTRAINT ck_job_state_fields CHECK (
            (
                state = 'queued'
                AND lease_owner IS NULL
                AND lease_expires_at IS NULL
                AND completed_at IS NULL
            ) OR (
                state IN ('leased', 'running')
                AND lease_owner IS NOT NULL
                AND lease_expires_at IS NOT NULL
                AND completed_at IS NULL
            ) OR (
                state IN ('succeeded', 'failed', 'cancelled', 'timed_out')
                AND lease_owner IS NULL
                AND lease_expires_at IS NULL
                AND completed_at IS NOT NULL
            )
        ),
        FOREIGN KEY(scan_run_id) REFERENCES scan_runs (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_jobs_kind ON jobs (kind)",
    "CREATE INDEX ix_jobs_state ON jobs (state)",
    "CREATE INDEX ix_jobs_scan_run_id ON jobs (scan_run_id)",
    "CREATE INDEX ix_jobs_run_after ON jobs (run_after)",
    "CREATE INDEX ix_jobs_lease_owner ON jobs (lease_owner)",
    "CREATE INDEX ix_jobs_lease_expires_at ON jobs (lease_expires_at)",
    "CREATE INDEX ix_jobs_created_at ON jobs (created_at)",
    """
    CREATE INDEX ix_jobs_ready
        ON jobs (state, run_after, created_at)
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0003_jobs currently supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0003_jobs",
    description="Add durable leased jobs with idempotency and recovery state",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision only adds the jobs table and indexes. Stop writers and "
        "preserve the SQLite file before upgrade. If it fails, restore the "
        "untouched file and rerun after correcting the reported storage problem; "
        "do not hand-edit job states or migration history."
    ),
)
