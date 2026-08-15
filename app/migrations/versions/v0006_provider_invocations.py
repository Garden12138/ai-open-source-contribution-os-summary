from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE provider_invocations (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL,
        attempt_number INTEGER NOT NULL,
        stage VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL,
        request_id VARCHAR(128) NOT NULL,
        correlation_id VARCHAR(128) NOT NULL,
        input_hash VARCHAR(64) NOT NULL,
        output_hash VARCHAR(64),
        provider_name VARCHAR(80) NOT NULL,
        adapter_version VARCHAR(80) NOT NULL,
        model_name VARCHAR(120) NOT NULL,
        model_version VARCHAR(120) NOT NULL,
        prompt_version VARCHAR(128) NOT NULL,
        policy_version VARCHAR(128) NOT NULL,
        output_schema_version VARCHAR(128) NOT NULL,
        input_tokens BIGINT NOT NULL,
        cached_input_tokens BIGINT NOT NULL,
        output_tokens BIGINT NOT NULL,
        estimated_cost_microusd BIGINT NOT NULL,
        duration_ms BIGINT NOT NULL,
        error_code VARCHAR(80),
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        started_at DATETIME NOT NULL,
        completed_at DATETIME NOT NULL,
        CONSTRAINT uq_provider_invocation_attempt_stage
            UNIQUE (job_id, attempt_number, stage),
        CONSTRAINT ck_provider_invocation_attempt
            CHECK (attempt_number >= 1),
        CONSTRAINT ck_provider_invocation_stage
            CHECK (stage IN ('inspect', 'analyze')),
        CONSTRAINT ck_provider_invocation_status
            CHECK (status IN ('succeeded', 'failed', 'timed_out', 'cancelled')),
        CONSTRAINT ck_provider_invocation_usage CHECK (
            input_tokens >= 0
            AND cached_input_tokens >= 0
            AND cached_input_tokens <= input_tokens
            AND output_tokens >= 0
            AND estimated_cost_microusd >= 0
            AND duration_ms >= 0
        ),
        CONSTRAINT ck_provider_invocation_outcome CHECK (
            (
                status = 'succeeded'
                AND output_hash IS NOT NULL
                AND error_code IS NULL
            ) OR (
                status != 'succeeded'
                AND output_hash IS NULL
                AND error_code IS NOT NULL
            )
        ),
        FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_provider_invocations_job_id ON provider_invocations (job_id)",
    "CREATE INDEX ix_provider_invocations_status ON provider_invocations (status)",
    (
        "CREATE INDEX ix_provider_invocations_correlation_id "
        "ON provider_invocations (correlation_id)"
    ),
    """
    CREATE TRIGGER provider_invocations_no_update
    BEFORE UPDATE ON provider_invocations
    BEGIN
        SELECT RAISE(ABORT, 'provider invocations are immutable');
    END
    """,
    """
    CREATE TRIGGER provider_invocations_no_delete
    BEFORE DELETE ON provider_invocations
    BEGIN
        SELECT RAISE(ABORT, 'provider invocations are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0006_provider_invocations supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0006_provider_invocations",
    description="Add immutable per-attempt provider invocation accounting",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds an immutable accounting table and triggers without "
        "rewriting existing rows. Stop workers and preserve the SQLite file "
        "before upgrade. On failure restore that file and rerun after correcting "
        "the storage issue; never drop the immutability triggers or fabricate "
        "provider usage records."
    ),
)
