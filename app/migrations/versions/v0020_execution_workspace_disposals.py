from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE execution_workspace_disposals (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        execution_attempt_id VARCHAR(36) NOT NULL UNIQUE,
        verify_stage_run_id VARCHAR(36) NOT NULL UNIQUE,
        artifact_manifest_id VARCHAR(36) NOT NULL UNIQUE,
        schema_version VARCHAR(32) NOT NULL,
        workspace_id VARCHAR(128) NOT NULL,
        workspace_ref VARCHAR(128) NOT NULL,
        workspace_inventory_hash VARCHAR(64) NOT NULL,
        runner_image_digest VARCHAR(71) NOT NULL,
        sandbox_policy_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_execution_workspace_disposal_schema
            CHECK (schema_version = 'execution-workspace-disposal-v1'),
        CONSTRAINT ck_execution_workspace_disposal_hashes
            CHECK (
                length(workspace_inventory_hash) = 64
                AND workspace_inventory_hash NOT GLOB '*[^0-9a-f]*'
                AND length(runner_image_digest) = 71
                AND substr(runner_image_digest, 1, 7) = 'sha256:'
                AND substr(runner_image_digest, 8)
                    NOT GLOB '*[^0-9a-f]*'
                AND length(sandbox_policy_hash) = 64
                AND sandbox_policy_hash NOT GLOB '*[^0-9a-f]*'
                AND length(record_hash) = 64
                AND record_hash NOT GLOB '*[^0-9a-f]*'
            ),
        FOREIGN KEY(execution_attempt_id)
            REFERENCES execution_attempts (id) ON DELETE RESTRICT,
        FOREIGN KEY(verify_stage_run_id)
            REFERENCES execution_stage_runs (id) ON DELETE RESTRICT,
        FOREIGN KEY(artifact_manifest_id)
            REFERENCES execution_artifact_manifests (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE UNIQUE INDEX "
        "ix_execution_workspace_disposals_execution_attempt_id "
        "ON execution_workspace_disposals (execution_attempt_id)"
    ),
    (
        "CREATE UNIQUE INDEX "
        "ix_execution_workspace_disposals_verify_stage_run_id "
        "ON execution_workspace_disposals (verify_stage_run_id)"
    ),
    (
        "CREATE UNIQUE INDEX "
        "ix_execution_workspace_disposals_artifact_manifest_id "
        "ON execution_workspace_disposals (artifact_manifest_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_workspace_disposals_record_hash "
        "ON execution_workspace_disposals (record_hash)"
    ),
    (
        "CREATE INDEX ix_execution_workspace_disposals_created_at "
        "ON execution_workspace_disposals (created_at)"
    ),
    """
    CREATE TABLE execution_workspace_disposal_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        disposal_id VARCHAR(36) NOT NULL,
        sequence INTEGER NOT NULL,
        status VARCHAR(20) NOT NULL,
        reason_code VARCHAR(100) NOT NULL,
        worker_id VARCHAR(128),
        disposal_record_hash VARCHAR(64) NOT NULL,
        previous_version_hash VARCHAR(64),
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_execution_workspace_disposal_sequence
            UNIQUE (disposal_id, sequence),
        CONSTRAINT ck_execution_workspace_disposal_sequence
            CHECK (sequence >= 1),
        CONSTRAINT ck_execution_workspace_disposal_status
            CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
        CONSTRAINT ck_execution_workspace_disposal_worker
            CHECK (
                (status = 'pending' AND worker_id IS NULL)
                OR (
                    status IN ('running', 'succeeded', 'failed')
                    AND worker_id IS NOT NULL
                )
            ),
        FOREIGN KEY(disposal_id)
            REFERENCES execution_workspace_disposals (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX "
        "ix_execution_workspace_disposal_versions_disposal_id "
        "ON execution_workspace_disposal_versions (disposal_id)"
    ),
    (
        "CREATE INDEX ix_execution_workspace_disposal_versions_status "
        "ON execution_workspace_disposal_versions (status)"
    ),
    (
        "CREATE UNIQUE INDEX "
        "ix_execution_workspace_disposal_versions_record_hash "
        "ON execution_workspace_disposal_versions (record_hash)"
    ),
    (
        "CREATE INDEX ix_execution_workspace_disposal_versions_created_at "
        "ON execution_workspace_disposal_versions (created_at)"
    ),
    """
    CREATE TRIGGER execution_workspace_disposals_provenance_insert
    BEFORE INSERT ON execution_workspace_disposals
    WHEN NOT EXISTS (
        SELECT 1
        FROM execution_stage_runs AS stage_run
        JOIN execution_attempts AS attempt
          ON attempt.id = stage_run.execution_attempt_id
        JOIN execution_artifact_manifests AS manifest
          ON manifest.execution_stage_run_id = stage_run.id
        JOIN execution_stage_versions AS running
          ON running.execution_attempt_id = attempt.id
        WHERE stage_run.id = NEW.verify_stage_run_id
          AND stage_run.stage = 'verify'
          AND stage_run.execution_attempt_id = NEW.execution_attempt_id
          AND attempt.id = NEW.execution_attempt_id
          AND manifest.id = NEW.artifact_manifest_id
          AND manifest.stage = 'verify'
          AND running.stage = 'verify'
          AND running.status = 'running'
          AND running.previous_stage_state_hash =
              stage_run.pending_stage_record_hash
          AND running.workspace_id = NEW.workspace_id
          AND running.workspace_ref = NEW.workspace_ref
          AND running.workspace_inventory_hash =
              NEW.workspace_inventory_hash
          AND attempt.runner_image_digest = NEW.runner_image_digest
          AND attempt.sandbox_policy_hash = NEW.sandbox_policy_hash
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'invalid execution workspace disposal provenance'
        );
    END
    """,
    """
    CREATE TRIGGER execution_workspace_disposal_versions_provenance_insert
    BEFORE INSERT ON execution_workspace_disposal_versions
    WHEN NOT (
        EXISTS (
            SELECT 1
            FROM execution_workspace_disposals AS disposal
            WHERE disposal.id = NEW.disposal_id
              AND disposal.record_hash = NEW.disposal_record_hash
        )
        AND (
            (
                NEW.sequence = 1
                AND NEW.status = 'pending'
                AND NEW.reason_code = 'artifacts_finalized'
                AND NEW.worker_id IS NULL
                AND NEW.previous_version_hash IS NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM execution_workspace_disposal_versions AS existing
                    WHERE existing.disposal_id = NEW.disposal_id
                )
            )
            OR (
                NEW.sequence > 1
                AND EXISTS (
                    SELECT 1
                    FROM execution_workspace_disposal_versions AS previous
                    WHERE previous.disposal_id = NEW.disposal_id
                      AND previous.sequence = NEW.sequence - 1
                      AND previous.record_hash =
                          NEW.previous_version_hash
                      AND (
                          (
                              previous.status = 'pending'
                              AND NEW.status = 'running'
                          )
                          OR (
                              previous.status = 'running'
                              AND NEW.status IN ('succeeded', 'failed')
                          )
                          OR (
                              previous.status = 'failed'
                              AND NEW.status = 'pending'
                              AND NEW.worker_id IS NULL
                          )
                      )
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'invalid execution workspace disposal transition'
        );
    END
    """,
    """
    CREATE TRIGGER execution_workspace_disposals_no_update
    BEFORE UPDATE ON execution_workspace_disposals
    BEGIN
        SELECT RAISE(ABORT, 'execution workspace disposals are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_workspace_disposals_no_delete
    BEFORE DELETE ON execution_workspace_disposals
    BEGIN
        SELECT RAISE(ABORT, 'execution workspace disposals are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_workspace_disposal_versions_no_update
    BEFORE UPDATE ON execution_workspace_disposal_versions
    BEGIN
        SELECT RAISE(
            ABORT,
            'execution workspace disposal versions are immutable'
        );
    END
    """,
    """
    CREATE TRIGGER execution_workspace_disposal_versions_no_delete
    BEFORE DELETE ON execution_workspace_disposal_versions
    BEGIN
        SELECT RAISE(
            ABORT,
            'execution workspace disposal versions are immutable'
        );
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0020_execution_workspace_disposals supports "
            "the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0020_execution_workspace_disposals",
    description=(
        "Persist post-artifact disposable workspace destruction"
    ),
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop orchestrator and Sandbox Worker cleanup writers before upgrade "
        "and preserve the SQLite file. This additive revision does not delete "
        "any workspace. If it fails, restore the database backup and leave "
        "all labelled volumes untouched. After recovery, verify artifact "
        "manifests before resuming pending cleanup; never mark a disposal "
        "successful without an idempotent Worker confirmation."
    ),
)
