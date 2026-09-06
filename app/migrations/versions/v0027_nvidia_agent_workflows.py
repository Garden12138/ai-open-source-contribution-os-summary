from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_TABLES = (
    """
    CREATE TABLE coding_sessions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        execution_attempt_id VARCHAR(36) NOT NULL UNIQUE,
        context_job_id VARCHAR(36) NOT NULL UNIQUE,
        plan_version_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        plan_content_hash VARCHAR(64) NOT NULL,
        plan_record_hash VARCHAR(64) NOT NULL,
        base_commit_sha VARCHAR(64) NOT NULL,
        explore_result_hash VARCHAR(64) NOT NULL,
        context_artifact_id VARCHAR(64) NOT NULL,
        context_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_coding_session_schema
            CHECK (schema_version = 'coding-session-v1'),
        FOREIGN KEY(execution_attempt_id)
            REFERENCES execution_attempts (id) ON DELETE RESTRICT,
        FOREIGN KEY(context_job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_version_id)
            REFERENCES plan_versions (id) ON DELETE RESTRICT,
        FOREIGN KEY(context_artifact_id)
            REFERENCES artifacts (id) ON DELETE RESTRICT
    )
    """,
    "CREATE UNIQUE INDEX ix_coding_sessions_execution_attempt_id ON coding_sessions (execution_attempt_id)",
    "CREATE INDEX ix_coding_sessions_plan_version_id ON coding_sessions (plan_version_id)",
    "CREATE INDEX ix_coding_sessions_created_at ON coding_sessions (created_at)",
    """
    CREATE TABLE coding_turns (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        session_id VARCHAR(36) NOT NULL,
        job_id VARCHAR(36) UNIQUE,
        sequence BIGINT NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        role VARCHAR(16) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        content TEXT NOT NULL,
        content_hash VARCHAR(64) NOT NULL,
        provider_name VARCHAR(80),
        model_name VARCHAR(120),
        previous_turn_hash VARCHAR(64),
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_coding_turn_sequence UNIQUE (session_id, sequence),
        CONSTRAINT ck_coding_turn_schema
            CHECK (schema_version = 'coding-turn-v1'),
        CONSTRAINT ck_coding_turn_sequence CHECK (sequence >= 1),
        CONSTRAINT ck_coding_turn_role CHECK (role IN ('user', 'assistant')),
        CONSTRAINT ck_coding_turn_provider CHECK (
            (role = 'user' AND provider_name IS NULL AND model_name IS NULL)
            OR (role = 'assistant' AND provider_name IS NOT NULL
                AND model_name IS NOT NULL)
        ),
        FOREIGN KEY(session_id)
            REFERENCES coding_sessions (id) ON DELETE RESTRICT,
        FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_coding_turns_session_id ON coding_turns (session_id)",
    "CREATE INDEX ix_coding_turns_created_at ON coding_turns (created_at)",
    """
    CREATE TABLE agent_invocations (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL,
        attempt_number INTEGER NOT NULL,
        role VARCHAR(24) NOT NULL,
        provider_name VARCHAR(80) NOT NULL,
        adapter_version VARCHAR(80) NOT NULL,
        model_name VARCHAR(120) NOT NULL,
        model_version VARCHAR(120) NOT NULL,
        input_hash VARCHAR(64) NOT NULL,
        output_hash VARCHAR(64) NOT NULL,
        input_tokens BIGINT NOT NULL,
        cached_input_tokens BIGINT NOT NULL,
        output_tokens BIGINT NOT NULL,
        duration_ms BIGINT NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_agent_invocation_attempt
            UNIQUE (job_id, attempt_number),
        CONSTRAINT ck_agent_invocation_role
            CHECK (role IN ('coding', 'change_set', 'review')),
        CONSTRAINT ck_agent_invocation_attempt CHECK (attempt_number >= 1),
        CONSTRAINT ck_agent_invocation_usage CHECK (
            input_tokens >= 0 AND cached_input_tokens >= 0
            AND cached_input_tokens <= input_tokens AND output_tokens >= 0
            AND duration_ms >= 0
        ),
        FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_agent_invocations_job_id ON agent_invocations (job_id)",
    "CREATE INDEX ix_agent_invocations_role ON agent_invocations (role)",
    "CREATE INDEX ix_agent_invocations_created_at ON agent_invocations (created_at)",
    """
    CREATE TABLE change_set_proposals (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        session_id VARCHAR(36) NOT NULL,
        job_id VARCHAR(36) NOT NULL UNIQUE,
        agent_invocation_id VARCHAR(36) NOT NULL UNIQUE,
        schema_version VARCHAR(32) NOT NULL,
        conversation_hash VARCHAR(64) NOT NULL,
        change_set_artifact_id VARCHAR(64) NOT NULL,
        change_set_hash VARCHAR(64) NOT NULL UNIQUE,
        summary VARCHAR(1000) NOT NULL,
        paths JSON NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_change_set_proposal_schema
            CHECK (schema_version = 'change-set-proposal-v1'),
        FOREIGN KEY(session_id)
            REFERENCES coding_sessions (id) ON DELETE RESTRICT,
        FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
        FOREIGN KEY(agent_invocation_id)
            REFERENCES agent_invocations (id) ON DELETE RESTRICT,
        FOREIGN KEY(change_set_artifact_id)
            REFERENCES artifacts (id) ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_change_set_proposals_session_id ON change_set_proposals (session_id)",
    "CREATE INDEX ix_change_set_proposals_created_at ON change_set_proposals (created_at)",
)

_IMMUTABLE = (
    "coding_sessions",
    "coding_turns",
    "agent_invocations",
    "change_set_proposals",
)


def _statements() -> tuple[str, ...]:
    statements = list(_TABLES)
    for table in _IMMUTABLE:
        statements.extend(
            (
                f"""
                CREATE TRIGGER {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} rows are immutable');
                END
                """,
                f"""
                CREATE TRIGGER {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} rows are immutable');
                END
                """,
            )
        )
    return tuple(statements)


_STATEMENTS = _statements()


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0027_nvidia_agent_workflows supports SQLite only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0027_nvidia_agent_workflows",
    description="Add immutable NVIDIA coding sessions and provider proposals",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop API and workers and preserve the SQLite database plus artifact "
        "root before upgrade. This additive migration does not rewrite existing "
        "business records. On failure restore both backups and rerun; never "
        "fabricate coding sessions, invocations, or accepted proposal hashes."
    ),
)
