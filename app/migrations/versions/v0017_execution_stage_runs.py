from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE execution_stage_runs (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL UNIQUE,
        execution_attempt_id VARCHAR(36) NOT NULL,
        pending_stage_version_id VARCHAR(36) NOT NULL UNIQUE,
        schema_version VARCHAR(32) NOT NULL,
        stage VARCHAR(20) NOT NULL,
        stage_run_number BIGINT NOT NULL,
        max_stage_runs BIGINT NOT NULL,
        timeout_seconds INTEGER NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        job_spec_hash VARCHAR(64) NOT NULL,
        input_hashes JSON NOT NULL,
        attempt_record_hash VARCHAR(64) NOT NULL,
        pending_stage_record_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_execution_stage_run_number
            UNIQUE (
                execution_attempt_id,
                stage,
                stage_run_number
            ),
        CONSTRAINT ck_execution_stage_run_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_execution_stage_run_stage
            CHECK (stage IN ('explore', 'implement', 'verify')),
        CONSTRAINT ck_execution_stage_run_budget
            CHECK (
                stage_run_number >= 1
                AND max_stage_runs >= 1
                AND max_stage_runs <= 3
                AND stage_run_number <= max_stage_runs
            ),
        CONSTRAINT ck_execution_stage_run_timeout
            CHECK (timeout_seconds >= 1 AND timeout_seconds <= 600),
        FOREIGN KEY(job_id)
            REFERENCES jobs (id) ON DELETE RESTRICT,
        FOREIGN KEY(execution_attempt_id)
            REFERENCES execution_attempts (id) ON DELETE RESTRICT,
        FOREIGN KEY(pending_stage_version_id)
            REFERENCES execution_stage_versions (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE UNIQUE INDEX ix_execution_stage_runs_job_id "
        "ON execution_stage_runs (job_id)"
    ),
    (
        "CREATE INDEX ix_execution_stage_runs_execution_attempt_id "
        "ON execution_stage_runs (execution_attempt_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_stage_runs_pending_stage_version_id "
        "ON execution_stage_runs (pending_stage_version_id)"
    ),
    (
        "CREATE INDEX ix_execution_stage_runs_stage "
        "ON execution_stage_runs (stage)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_stage_runs_idempotency_key "
        "ON execution_stage_runs (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_stage_runs_record_hash "
        "ON execution_stage_runs (record_hash)"
    ),
    (
        "CREATE INDEX ix_execution_stage_runs_created_at "
        "ON execution_stage_runs (created_at)"
    ),
    """
    CREATE TRIGGER execution_stage_runs_provenance_insert
    BEFORE INSERT ON execution_stage_runs
    WHEN NOT EXISTS (
        SELECT 1
        FROM execution_attempts AS attempt
        JOIN execution_stage_versions AS pending
          ON pending.id = NEW.pending_stage_version_id
        JOIN jobs AS job
          ON job.id = NEW.job_id
        WHERE attempt.id = NEW.execution_attempt_id
          AND attempt.record_hash = NEW.attempt_record_hash
          AND pending.execution_attempt_id = attempt.id
          AND pending.attempt_record_hash = attempt.record_hash
          AND pending.stage = NEW.stage
          AND pending.status = 'pending'
          AND pending.record_hash = NEW.pending_stage_record_hash
          AND pending.input_hashes = NEW.input_hashes
          AND job.kind = 'sandbox_stage'
          AND job.idempotency_key = NEW.idempotency_key
          AND job.max_attempts = 1
          AND job.timeout_seconds = NEW.timeout_seconds
          AND json_extract(
              job.payload,
              '$.schema_version'
          ) = 'execution-stage-job-v1'
          AND json_extract(
              job.payload,
              '$.execution_attempt_id'
          ) = NEW.execution_attempt_id
          AND json_extract(
              job.payload,
              '$.attempt_record_hash'
          ) = NEW.attempt_record_hash
          AND json_extract(
              job.payload,
              '$.pending_stage_version_id'
          ) = NEW.pending_stage_version_id
          AND json_extract(
              job.payload,
              '$.pending_stage_record_hash'
          ) = NEW.pending_stage_record_hash
          AND json_extract(job.payload, '$.stage') = NEW.stage
          AND json_extract(
              job.payload,
              '$.stage_run_number'
          ) = NEW.stage_run_number
          AND json_extract(
              job.payload,
              '$.max_stage_runs'
          ) = NEW.max_stage_runs
          AND json_extract(
              job.payload,
              '$.timeout_seconds'
          ) = NEW.timeout_seconds
          AND json_extract(
              job.payload,
              '$.job_spec_hash'
          ) = NEW.job_spec_hash
          AND json(
              json_extract(job.payload, '$.input_hashes')
          ) = json(NEW.input_hashes)
          AND NEW.stage_run_number = COALESCE(
              (
                  SELECT MAX(prior.stage_run_number) + 1
                  FROM execution_stage_runs AS prior
                  WHERE prior.execution_attempt_id =
                      NEW.execution_attempt_id
                    AND prior.stage = NEW.stage
              ),
              1
          )
          AND NOT EXISTS (
              SELECT 1
              FROM execution_stage_runs AS prior
              WHERE prior.execution_attempt_id =
                  NEW.execution_attempt_id
                AND prior.stage = NEW.stage
                AND prior.max_stage_runs != NEW.max_stage_runs
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid execution stage run provenance');
    END
    """,
    """
    CREATE TRIGGER execution_stage_runs_no_update
    BEFORE UPDATE ON execution_stage_runs
    BEGIN
        SELECT RAISE(ABORT, 'execution stage runs are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_stage_runs_no_delete
    BEFORE DELETE ON execution_stage_runs
    BEGIN
        SELECT RAISE(ABORT, 'execution stage runs are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_stage_jobs_payload_no_update
    BEFORE UPDATE OF
        kind,
        idempotency_key,
        payload,
        payload_hash,
        max_attempts,
        timeout_seconds
    ON jobs
    WHEN EXISTS (
        SELECT 1
        FROM execution_stage_runs AS stage_run
        WHERE stage_run.job_id = OLD.id
    )
    AND (
        NEW.kind IS NOT OLD.kind
        OR NEW.idempotency_key IS NOT OLD.idempotency_key
        OR NEW.payload IS NOT OLD.payload
        OR NEW.payload_hash IS NOT OLD.payload_hash
        OR NEW.max_attempts IS NOT OLD.max_attempts
        OR NEW.timeout_seconds IS NOT OLD.timeout_seconds
    )
    BEGIN
        SELECT RAISE(ABORT, 'execution stage job inputs are immutable');
    END
    """,
    "DROP TRIGGER execution_stage_versions_provenance_insert",
    """
    CREATE TRIGGER execution_stage_versions_provenance_insert
    BEFORE INSERT ON execution_stage_versions
    WHEN NOT (
        EXISTS (
            SELECT 1
            FROM execution_attempts AS attempt
            WHERE attempt.id = NEW.execution_attempt_id
              AND attempt.record_hash = NEW.attempt_record_hash
        )
        AND json_valid(NEW.input_hashes)
        AND json_type(NEW.input_hashes) = 'array'
        AND (
            (
                NEW.sequence = 1
                AND NEW.stage = 'explore'
                AND NEW.status = 'pending'
                AND NEW.reason_code = 'execution_started'
                AND NEW.job_spec_hash IS NULL
                AND json_array_length(NEW.input_hashes) = 0
                AND NEW.result_hash IS NULL
                AND NEW.workspace_id IS NULL
                AND NEW.workspace_ref IS NULL
                AND NEW.workspace_inventory_hash IS NULL
                AND NEW.previous_stage_state_hash IS NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM execution_stage_versions AS existing
                    WHERE existing.execution_attempt_id =
                        NEW.execution_attempt_id
                )
            )
            OR (
                NEW.sequence > 1
                AND EXISTS (
                    SELECT 1
                    FROM execution_stage_versions AS previous
                    WHERE previous.execution_attempt_id =
                        NEW.execution_attempt_id
                      AND previous.sequence = NEW.sequence - 1
                      AND previous.record_hash =
                          NEW.previous_stage_state_hash
                      AND (
                          (
                              previous.stage = NEW.stage
                              AND previous.status = 'pending'
                              AND NEW.status = 'running'
                              AND previous.input_hashes = NEW.input_hashes
                              AND (
                                  NEW.stage != 'verify'
                                  OR (
                                      previous.workspace_id =
                                          NEW.workspace_id
                                      AND previous.workspace_ref =
                                          NEW.workspace_ref
                                      AND previous.workspace_inventory_hash =
                                          NEW.workspace_inventory_hash
                                  )
                              )
                          )
                          OR (
                              previous.stage = NEW.stage
                              AND previous.status = 'pending'
                              AND NEW.status IN (
                                  'failed', 'cancelled', 'timed_out'
                              )
                              AND NEW.job_spec_hash IS NULL
                              AND previous.input_hashes = NEW.input_hashes
                              AND previous.workspace_id IS NEW.workspace_id
                              AND previous.workspace_ref IS NEW.workspace_ref
                              AND previous.workspace_inventory_hash
                                  IS NEW.workspace_inventory_hash
                          )
                          OR (
                              previous.stage = NEW.stage
                              AND previous.status = 'running'
                              AND NEW.status IN (
                                  'succeeded', 'failed',
                                  'cancelled', 'timed_out'
                              )
                              AND previous.job_spec_hash =
                                  NEW.job_spec_hash
                              AND previous.input_hashes = NEW.input_hashes
                          )
                          OR (
                              previous.stage = 'explore'
                              AND previous.status = 'succeeded'
                              AND NEW.stage = 'implement'
                              AND NEW.status = 'pending'
                              AND json_array_length(NEW.input_hashes) = 2
                              AND json_extract(
                                  NEW.input_hashes,
                                  '$[0]'
                              ) = previous.result_hash
                          )
                          OR (
                              previous.stage = 'implement'
                              AND previous.status = 'succeeded'
                              AND NEW.stage = 'verify'
                              AND NEW.status = 'pending'
                              AND json_array_length(NEW.input_hashes) = 1
                              AND json_extract(
                                  NEW.input_hashes,
                                  '$[0]'
                              ) = previous.result_hash
                              AND NEW.workspace_id =
                                  previous.workspace_id
                              AND NEW.workspace_ref =
                                  previous.workspace_ref
                              AND NEW.workspace_inventory_hash =
                                  previous.workspace_inventory_hash
                          )
                          OR (
                              previous.stage = NEW.stage
                              AND previous.status IN ('failed', 'timed_out')
                              AND NEW.status = 'pending'
                              AND NEW.job_spec_hash IS NULL
                              AND NEW.result_hash IS NULL
                              AND previous.input_hashes = NEW.input_hashes
                              AND (
                                  (
                                      NEW.stage = 'implement'
                                      AND NEW.workspace_id IS NULL
                                      AND NEW.workspace_ref IS NULL
                                      AND NEW.workspace_inventory_hash IS NULL
                                  )
                                  OR (
                                      NEW.stage != 'implement'
                                      AND previous.workspace_id
                                          IS NEW.workspace_id
                                      AND previous.workspace_ref
                                          IS NEW.workspace_ref
                                      AND previous.workspace_inventory_hash
                                          IS NEW.workspace_inventory_hash
                                  )
                              )
                          )
                      )
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM execution_stage_versions AS later
                    WHERE later.execution_attempt_id =
                        NEW.execution_attempt_id
                      AND later.sequence >= NEW.sequence
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid execution stage provenance');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0017_execution_stage_runs supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0017_execution_stage_runs",
    description=(
        "Add bounded leased stage runs and explicit loss/retry transitions"
    ),
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds immutable ExecutionStageRun-to-Job bindings and "
        "replaces only the execution-stage provenance trigger to permit "
        "explicit pre-start terminal outcomes and bounded retry states. Stop "
        "orchestrator and Worker writers and preserve the SQLite file before "
        "upgrade. On failure restore it; never edit a Job payload, lease, "
        "attempt budget, terminal stage, or retry sequence to force recovery."
    ),
)
