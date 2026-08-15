from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE execution_artifact_manifests (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL UNIQUE,
        execution_stage_run_id VARCHAR(36) NOT NULL UNIQUE,
        execution_attempt_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        stage VARCHAR(20) NOT NULL,
        job_spec_hash VARCHAR(64) NOT NULL,
        result_hash VARCHAR(64) NOT NULL,
        entry_count INTEGER NOT NULL,
        manifest_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_execution_artifact_manifest_schema
            CHECK (schema_version = 'execution-artifact-manifest-v1'),
        CONSTRAINT ck_execution_artifact_manifest_stage
            CHECK (stage IN ('explore', 'implement', 'verify')),
        CONSTRAINT ck_execution_artifact_manifest_count
            CHECK (
                (stage = 'explore' AND entry_count = 1)
                OR (stage = 'implement' AND entry_count = 3)
                OR (stage = 'verify' AND entry_count = 2)
            ),
        CONSTRAINT ck_execution_artifact_manifest_hashes
            CHECK (
                length(job_spec_hash) = 64
                AND job_spec_hash NOT GLOB '*[^0-9a-f]*'
                AND length(result_hash) = 64
                AND result_hash NOT GLOB '*[^0-9a-f]*'
                AND length(manifest_hash) = 64
                AND manifest_hash NOT GLOB '*[^0-9a-f]*'
            ),
        FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
        FOREIGN KEY(execution_stage_run_id)
            REFERENCES execution_stage_runs (id) ON DELETE RESTRICT,
        FOREIGN KEY(execution_attempt_id)
            REFERENCES execution_attempts (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE UNIQUE INDEX ix_execution_artifact_manifests_job_id "
        "ON execution_artifact_manifests (job_id)"
    ),
    (
        "CREATE UNIQUE INDEX "
        "ix_execution_artifact_manifests_execution_stage_run_id "
        "ON execution_artifact_manifests (execution_stage_run_id)"
    ),
    (
        "CREATE INDEX ix_execution_artifact_manifests_execution_attempt_id "
        "ON execution_artifact_manifests (execution_attempt_id)"
    ),
    (
        "CREATE INDEX ix_execution_artifact_manifests_stage "
        "ON execution_artifact_manifests (stage)"
    ),
    (
        "CREATE INDEX ix_execution_artifact_manifests_result_hash "
        "ON execution_artifact_manifests (result_hash)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_artifact_manifests_manifest_hash "
        "ON execution_artifact_manifests (manifest_hash)"
    ),
    (
        "CREATE INDEX ix_execution_artifact_manifests_created_at "
        "ON execution_artifact_manifests (created_at)"
    ),
    """
    CREATE TABLE execution_artifact_entries (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        manifest_id VARCHAR(36) NOT NULL,
        position INTEGER NOT NULL,
        role VARCHAR(80) NOT NULL,
        artifact_id VARCHAR(64) NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_execution_artifact_entry_position
            UNIQUE (manifest_id, position),
        CONSTRAINT uq_execution_artifact_entry_role
            UNIQUE (manifest_id, role),
        CONSTRAINT ck_execution_artifact_entry_position
            CHECK (position >= 0 AND position <= 2),
        CONSTRAINT ck_execution_artifact_entry_role
            CHECK (
                role IN (
                    'stage-result',
                    'file-inventory',
                    'unified-diff',
                    'normalized-test-results'
                )
            ),
        FOREIGN KEY(manifest_id)
            REFERENCES execution_artifact_manifests (id) ON DELETE RESTRICT,
        FOREIGN KEY(artifact_id)
            REFERENCES artifacts (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_execution_artifact_entries_manifest_id "
        "ON execution_artifact_entries (manifest_id)"
    ),
    (
        "CREATE INDEX ix_execution_artifact_entries_artifact_id "
        "ON execution_artifact_entries (artifact_id)"
    ),
    """
    CREATE TRIGGER execution_artifact_manifests_provenance_insert
    BEFORE INSERT ON execution_artifact_manifests
    WHEN NOT EXISTS (
        SELECT 1
        FROM execution_stage_runs AS stage_run
        JOIN jobs AS job ON job.id = stage_run.job_id
        JOIN execution_attempts AS attempt
          ON attempt.id = stage_run.execution_attempt_id
        WHERE stage_run.id = NEW.execution_stage_run_id
          AND stage_run.job_id = NEW.job_id
          AND stage_run.execution_attempt_id = NEW.execution_attempt_id
          AND stage_run.stage = NEW.stage
          AND stage_run.job_spec_hash = NEW.job_spec_hash
          AND attempt.id = NEW.execution_attempt_id
          AND job.id = NEW.job_id
          AND job.kind = 'sandbox_stage'
          AND job.state = 'running'
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'invalid execution artifact manifest provenance'
        );
    END
    """,
    """
    CREATE TRIGGER execution_artifact_entries_provenance_insert
    BEFORE INSERT ON execution_artifact_entries
    WHEN NOT EXISTS (
        SELECT 1
        FROM execution_artifact_manifests AS manifest
        JOIN artifacts AS artifact ON artifact.id = NEW.artifact_id
        WHERE manifest.id = NEW.manifest_id
          AND (
              (
                  manifest.stage = 'explore'
                  AND NEW.position = 0
                  AND NEW.role = 'stage-result'
                  AND NEW.artifact_id = manifest.result_hash
              )
              OR (
                  manifest.stage = 'implement'
                  AND (
                      (
                          NEW.position = 0
                          AND NEW.role = 'stage-result'
                          AND NEW.artifact_id = manifest.result_hash
                      )
                      OR (
                          NEW.position = 1
                          AND NEW.role = 'file-inventory'
                      )
                      OR (
                          NEW.position = 2
                          AND NEW.role = 'unified-diff'
                      )
                  )
              )
              OR (
                  manifest.stage = 'verify'
                  AND (
                      (
                          NEW.position = 0
                          AND NEW.role = 'stage-result'
                          AND NEW.artifact_id = manifest.result_hash
                      )
                      OR (
                          NEW.position = 1
                          AND NEW.role = 'normalized-test-results'
                      )
                  )
              )
          )
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'invalid execution artifact entry provenance'
        );
    END
    """,
    """
    CREATE TRIGGER sandbox_stage_job_success_requires_artifacts
    BEFORE UPDATE OF state, result_data ON jobs
    WHEN NEW.kind = 'sandbox_stage'
      AND NEW.state = 'succeeded'
      AND NOT EXISTS (
          SELECT 1
          FROM execution_artifact_manifests AS manifest
          JOIN execution_stage_runs AS stage_run
            ON stage_run.id = manifest.execution_stage_run_id
          WHERE manifest.job_id = NEW.id
            AND stage_run.job_id = NEW.id
            AND manifest.stage = stage_run.stage
            AND json_valid(NEW.result_data)
            AND json_type(NEW.result_data) = 'object'
            AND (
                SELECT COUNT(*) FROM json_each(NEW.result_data)
            ) = 2
            AND json_extract(
                NEW.result_data,
                '$.result_hash'
            ) = manifest.result_hash
            AND json_extract(
                NEW.result_data,
                '$.artifact_manifest_hash'
            ) = manifest.manifest_hash
            AND (
                SELECT COUNT(*)
                FROM execution_artifact_entries AS entry
                WHERE entry.manifest_id = manifest.id
            ) = manifest.entry_count
            AND NOT EXISTS (
                SELECT 1
                FROM execution_artifact_entries AS entry
                WHERE entry.manifest_id = manifest.id
                  AND NOT EXISTS (
                      SELECT 1
                      FROM job_artifacts AS link
                      WHERE link.job_id = NEW.id
                        AND link.artifact_id = entry.artifact_id
                        AND link.role = entry.role
                  )
            )
      )
    BEGIN
        SELECT RAISE(
            ABORT,
            'sandbox stage success requires finalized artifacts'
        );
    END
    """,
    """
    CREATE TRIGGER execution_artifact_manifests_no_update
    BEFORE UPDATE ON execution_artifact_manifests
    BEGIN
        SELECT RAISE(ABORT, 'execution artifact manifests are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_artifact_manifests_no_delete
    BEFORE DELETE ON execution_artifact_manifests
    BEGIN
        SELECT RAISE(ABORT, 'execution artifact manifests are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_artifact_entries_no_update
    BEFORE UPDATE ON execution_artifact_entries
    BEGIN
        SELECT RAISE(ABORT, 'execution artifact entries are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_artifact_entries_no_delete
    BEFORE DELETE ON execution_artifact_entries
    BEGIN
        SELECT RAISE(ABORT, 'execution artifact entries are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0019_execution_artifact_manifests supports "
            "the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0019_execution_artifact_manifests",
    description=(
        "Atomically bind execution artifacts before sandbox stage success"
    ),
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop all workers and back up the SQLite database and artifact root "
        "together before upgrade. This additive revision does not rewrite "
        "historical Jobs or artifacts. If it fails, restore both backups "
        "together. Content files finalized before a rolled-back database "
        "transaction are safe hash-addressed orphans and must not be deleted "
        "until a later reconciliation pass proves they are unreferenced."
    ),
)
