from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError
from app.migrations.versions.v0026_review_artifact_bindings import (
    _STATEMENTS as _REVIEW_BINDING_STATEMENTS,
)


_COLUMNS = (
    "id, execution_attempt_id, task_id, schema_version, review_number, "
    "idempotency_key, actor_type, actor_id, reviewer_kind, plan_version_id, "
    "plan_content_hash, plan_record_hash, base_commit_sha, "
    "repository_archive_hash, sandbox_policy_hash, attempt_record_hash, "
    "implement_stage_version_id, diff_hash, verify_stage_version_id, "
    "verify_result_hash, test_results_hash, binding_hash, "
    "reviewing_state_version_id, reviewing_state_record_hash, "
    "ready_state_version_id, ready_state_record_hash, verdict, status, "
    "reason_code, findings, findings_hash, reviewer_invocation_id, "
    "record_hash, created_at"
)

_CREATE = """
CREATE TABLE review_runs (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    execution_attempt_id VARCHAR(36) NOT NULL UNIQUE,
    task_id VARCHAR(36) NOT NULL,
    schema_version VARCHAR(32) NOT NULL,
    review_number BIGINT NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    actor_type VARCHAR(40) NOT NULL,
    actor_id VARCHAR(128) NOT NULL,
    reviewer_kind VARCHAR(40) NOT NULL,
    plan_version_id VARCHAR(36) NOT NULL,
    plan_content_hash VARCHAR(64) NOT NULL,
    plan_record_hash VARCHAR(64) NOT NULL,
    base_commit_sha VARCHAR(64) NOT NULL,
    repository_archive_hash VARCHAR(64) NOT NULL,
    sandbox_policy_hash VARCHAR(64) NOT NULL,
    attempt_record_hash VARCHAR(64) NOT NULL,
    implement_stage_version_id VARCHAR(36) NOT NULL,
    diff_hash VARCHAR(64) NOT NULL,
    verify_stage_version_id VARCHAR(36) NOT NULL,
    verify_result_hash VARCHAR(64) NOT NULL,
    test_results_hash VARCHAR(64) NOT NULL,
    binding_hash VARCHAR(64) NOT NULL,
    reviewing_state_version_id VARCHAR(36) NOT NULL UNIQUE,
    reviewing_state_record_hash VARCHAR(64) NOT NULL,
    ready_state_version_id VARCHAR(36),
    ready_state_record_hash VARCHAR(64),
    verdict VARCHAR(16) NOT NULL,
    status VARCHAR(20) NOT NULL,
    reason_code VARCHAR(100) NOT NULL,
    findings JSON NOT NULL,
    findings_hash VARCHAR(64) NOT NULL,
    reviewer_invocation_id VARCHAR(36) NOT NULL,
    record_hash VARCHAR(64) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_review_run_number UNIQUE (task_id, review_number),
    CONSTRAINT ck_review_run_schema CHECK (schema_version = '1'),
    CONSTRAINT ck_review_run_number CHECK (review_number >= 1),
    CONSTRAINT ck_review_run_kind CHECK (
        reviewer_kind IN ('fake', 'fake_blocking', 'nvidia_nim')
    ),
    CONSTRAINT ck_review_run_verdict CHECK (verdict IN ('pass', 'block')),
    CONSTRAINT ck_review_run_status CHECK (status IN ('succeeded', 'failed')),
    CONSTRAINT ck_review_run_ready CHECK (
        (verdict = 'block' AND ready_state_version_id IS NULL
         AND ready_state_record_hash IS NULL)
        OR
        (verdict = 'pass' AND ready_state_version_id IS NOT NULL
         AND ready_state_record_hash IS NOT NULL)
    ),
    FOREIGN KEY(execution_attempt_id)
        REFERENCES execution_attempts (id) ON DELETE RESTRICT,
    FOREIGN KEY(task_id)
        REFERENCES contribution_tasks (id) ON DELETE RESTRICT,
    FOREIGN KEY(plan_version_id)
        REFERENCES plan_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(implement_stage_version_id)
        REFERENCES execution_stage_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(verify_stage_version_id)
        REFERENCES execution_stage_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(reviewing_state_version_id)
        REFERENCES contribution_task_state_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(ready_state_version_id)
        REFERENCES contribution_task_state_versions (id) ON DELETE RESTRICT
)
"""

_NVIDIA_INVOCATION_BINDING = """
        AND (
            NEW.reviewer_kind != 'nvidia_nim'
            OR EXISTS (
                SELECT 1
                FROM agent_invocations AS invocation
                WHERE invocation.id = NEW.reviewer_invocation_id
                  AND invocation.role = 'review'
                  AND invocation.provider_name = 'nvidia_nim'
            )
        )
"""

_PROVENANCE_TRIGGER = _REVIEW_BINDING_STATEMENTS[1].replace(
    "        AND (\n            (\n                NEW.verdict = 'block'",
    _NVIDIA_INVOCATION_BINDING
    + "        AND (\n            (\n                NEW.verdict = 'block'",
    1,
)
if _PROVENANCE_TRIGGER == _REVIEW_BINDING_STATEMENTS[1]:
    raise RuntimeError("Review provenance trigger template changed")

_STATEMENTS = (
    "DROP TRIGGER review_runs_provenance_insert",
    "DROP TRIGGER review_runs_no_update",
    "DROP TRIGGER review_runs_no_delete",
    "ALTER TABLE review_runs RENAME TO review_runs_legacy",
    _CREATE,
    f"INSERT INTO review_runs ({_COLUMNS}) SELECT {_COLUMNS} FROM review_runs_legacy",
    "DROP TABLE review_runs_legacy",
    "CREATE INDEX ix_review_runs_task_id ON review_runs (task_id)",
    "CREATE INDEX ix_review_runs_plan_version_id ON review_runs (plan_version_id)",
    "CREATE INDEX ix_review_runs_created_at ON review_runs (created_at)",
    "CREATE UNIQUE INDEX ix_review_runs_execution_attempt_id ON review_runs (execution_attempt_id)",
    "CREATE UNIQUE INDEX ix_review_runs_idempotency_key ON review_runs (idempotency_key)",
    "CREATE UNIQUE INDEX ix_review_runs_reviewing_state_version_id ON review_runs (reviewing_state_version_id)",
    "CREATE UNIQUE INDEX ix_review_runs_record_hash ON review_runs (record_hash)",
    _PROVENANCE_TRIGGER,
    """
    CREATE TRIGGER review_runs_no_update
    BEFORE UPDATE ON review_runs
    BEGIN
        SELECT RAISE(ABORT, 'review runs are immutable');
    END
    """,
    """
    CREATE TRIGGER review_runs_no_delete
    BEFORE DELETE ON review_runs
    BEGIN
        SELECT RAISE(ABORT, 'review runs are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0028_nvidia_review_runs supports SQLite only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0028_nvidia_review_runs",
    description="Allow immutable NVIDIA reviews without weakening provenance",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop API, provider workers, and publishers; back up both SQLite and "
        "the artifact root. This migration rebuilds only review_runs while "
        "foreign keys are checked before commit. On failure restore the backup "
        "and do not bypass the reviewer-kind or artifact-binding constraints."
    ),
    requires_foreign_keys_disabled=True,
)
