from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE analysis_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL UNIQUE,
        snapshot_id VARCHAR(36) NOT NULL,
        score_version_id VARCHAR(36) NOT NULL,
        inspect_invocation_id VARCHAR(36) NOT NULL,
        analyze_invocation_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        frozen_input_hash VARCHAR(64) NOT NULL,
        snapshot_inputs_hash VARCHAR(64) NOT NULL,
        score_output_hash VARCHAR(64) NOT NULL,
        inspect_input_hash VARCHAR(64) NOT NULL,
        inspect_output_hash VARCHAR(64) NOT NULL,
        analysis_input_hash VARCHAR(64) NOT NULL,
        analysis_output_hash VARCHAR(64) NOT NULL,
        provider_name VARCHAR(80) NOT NULL,
        adapter_version VARCHAR(80) NOT NULL,
        model_name VARCHAR(120) NOT NULL,
        model_version VARCHAR(120) NOT NULL,
        inspect_prompt_version VARCHAR(128) NOT NULL,
        inspect_policy_version VARCHAR(128) NOT NULL,
        inspect_output_schema_version VARCHAR(128) NOT NULL,
        analyze_prompt_version VARCHAR(128) NOT NULL,
        analyze_policy_version VARCHAR(128) NOT NULL,
        analyze_output_schema_version VARCHAR(128) NOT NULL,
        inspection_structured_output JSON NOT NULL,
        inspection_cited_evidence_ids JSON NOT NULL,
        structured_output JSON NOT NULL,
        cited_evidence_ids JSON NOT NULL,
        input_tokens BIGINT NOT NULL,
        cached_input_tokens BIGINT NOT NULL,
        output_tokens BIGINT NOT NULL,
        estimated_cost_microusd BIGINT NOT NULL,
        duration_ms BIGINT NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_analysis_version_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_analysis_version_usage CHECK (
            input_tokens >= 0
            AND cached_input_tokens >= 0
            AND cached_input_tokens <= input_tokens
            AND output_tokens >= 0
            AND estimated_cost_microusd >= 0
            AND duration_ms >= 0
        ),
        FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
        FOREIGN KEY(snapshot_id)
            REFERENCES opportunity_snapshots (id) ON DELETE RESTRICT,
        FOREIGN KEY(score_version_id)
            REFERENCES score_versions (id) ON DELETE RESTRICT,
        FOREIGN KEY(inspect_invocation_id)
            REFERENCES provider_invocations (id) ON DELETE RESTRICT,
        FOREIGN KEY(analyze_invocation_id)
            REFERENCES provider_invocations (id) ON DELETE RESTRICT
    )
    """,
    "CREATE UNIQUE INDEX ix_analysis_versions_job_id ON analysis_versions (job_id)",
    "CREATE INDEX ix_analysis_versions_snapshot_id ON analysis_versions (snapshot_id)",
    (
        "CREATE INDEX ix_analysis_versions_score_version_id "
        "ON analysis_versions (score_version_id)"
    ),
    (
        "CREATE INDEX ix_analysis_versions_inspect_invocation_id "
        "ON analysis_versions (inspect_invocation_id)"
    ),
    (
        "CREATE INDEX ix_analysis_versions_analyze_invocation_id "
        "ON analysis_versions (analyze_invocation_id)"
    ),
    "CREATE INDEX ix_analysis_versions_created_at ON analysis_versions (created_at)",
    """
    CREATE TRIGGER analysis_versions_provenance_insert
    BEFORE INSERT ON analysis_versions
    WHEN NOT EXISTS (
        SELECT 1
        FROM opportunity_snapshots AS snapshot
        JOIN score_versions AS score
          ON score.snapshot_id = snapshot.id
        WHERE snapshot.id = NEW.snapshot_id
          AND score.id = NEW.score_version_id
          AND snapshot.inputs_hash = NEW.snapshot_inputs_hash
          AND score.inputs_hash = NEW.snapshot_inputs_hash
          AND score.output_hash = NEW.score_output_hash
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid analysis version provenance');
    END
    """,
    """
    CREATE TRIGGER analysis_versions_job_insert
    BEFORE INSERT ON analysis_versions
    WHEN NOT EXISTS (
        SELECT 1
        FROM jobs AS job
        WHERE job.id = NEW.job_id
          AND job.kind = 'provider_analysis'
          AND job.state = 'succeeded'
    )
    BEGIN
        SELECT RAISE(ABORT, 'analysis version requires succeeded provider job');
    END
    """,
    """
    CREATE TRIGGER analysis_versions_invocations_insert
    BEFORE INSERT ON analysis_versions
    WHEN NOT EXISTS (
        SELECT 1
        FROM provider_invocations AS inspect
        JOIN provider_invocations AS analyze
          ON analyze.job_id = inspect.job_id
         AND analyze.attempt_number = inspect.attempt_number
        WHERE inspect.id = NEW.inspect_invocation_id
          AND analyze.id = NEW.analyze_invocation_id
          AND inspect.job_id = NEW.job_id
          AND inspect.stage = 'inspect'
          AND analyze.stage = 'analyze'
          AND inspect.status = 'succeeded'
          AND analyze.status = 'succeeded'
          AND inspect.input_hash = NEW.inspect_input_hash
          AND analyze.input_hash = NEW.analysis_input_hash
          AND inspect.output_hash = NEW.inspect_output_hash
          AND analyze.output_hash = NEW.analysis_output_hash
          AND inspect.provider_name = NEW.provider_name
          AND analyze.provider_name = NEW.provider_name
          AND inspect.adapter_version = NEW.adapter_version
          AND analyze.adapter_version = NEW.adapter_version
          AND inspect.model_name = NEW.model_name
          AND analyze.model_name = NEW.model_name
          AND inspect.model_version = NEW.model_version
          AND analyze.model_version = NEW.model_version
          AND inspect.prompt_version = NEW.inspect_prompt_version
          AND inspect.policy_version = NEW.inspect_policy_version
          AND inspect.output_schema_version =
              NEW.inspect_output_schema_version
          AND analyze.prompt_version = NEW.analyze_prompt_version
          AND analyze.policy_version = NEW.analyze_policy_version
          AND analyze.output_schema_version =
              NEW.analyze_output_schema_version
          AND NEW.input_tokens =
              inspect.input_tokens + analyze.input_tokens
          AND NEW.cached_input_tokens =
              inspect.cached_input_tokens + analyze.cached_input_tokens
          AND NEW.output_tokens =
              inspect.output_tokens + analyze.output_tokens
          AND NEW.estimated_cost_microusd =
              inspect.estimated_cost_microusd
              + analyze.estimated_cost_microusd
          AND NEW.duration_ms =
              inspect.duration_ms + analyze.duration_ms
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid analysis version invocations');
    END
    """,
    """
    CREATE TRIGGER analysis_versions_no_update
    BEFORE UPDATE ON analysis_versions
    BEGIN
        SELECT RAISE(ABORT, 'analysis versions are immutable');
    END
    """,
    """
    CREATE TRIGGER analysis_versions_no_delete
    BEFORE DELETE ON analysis_versions
    BEGIN
        SELECT RAISE(ABORT, 'analysis versions are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0007_analysis_versions supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0007_analysis_versions",
    description="Add immutable hash-bound AnalysisVersion records",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds an immutable analysis table and provenance triggers "
        "without rewriting existing records. Stop Provider workers and preserve "
        "the SQLite file before upgrade. On failure restore that file; never "
        "disable triggers, edit hashes, or synthesize a successful Provider Job "
        "to force an AnalysisVersion insert."
    ),
)
