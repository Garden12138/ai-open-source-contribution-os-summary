from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE execution_attempts (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        task_id VARCHAR(36) NOT NULL,
        plan_version_id VARCHAR(36) NOT NULL,
        plan_approval_id VARCHAR(36) NOT NULL,
        approved_state_version_id VARCHAR(36) NOT NULL,
        executing_state_version_id VARCHAR(36) NOT NULL UNIQUE,
        schema_version VARCHAR(32) NOT NULL,
        attempt_number BIGINT NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        actor_type VARCHAR(40) NOT NULL,
        actor_id VARCHAR(128) NOT NULL,
        action VARCHAR(40) NOT NULL,
        repository_full_name VARCHAR(255) NOT NULL,
        base_commit_sha VARCHAR(64) NOT NULL,
        repository_archive_hash VARCHAR(64) NOT NULL,
        runner_image_digest VARCHAR(71) NOT NULL,
        sandbox_policy_version VARCHAR(128) NOT NULL,
        sandbox_policy_hash VARCHAR(64) NOT NULL,
        task_record_hash VARCHAR(64) NOT NULL,
        analysis_version_id VARCHAR(36) NOT NULL,
        analysis_record_hash VARCHAR(64) NOT NULL,
        analysis_output_hash VARCHAR(64) NOT NULL,
        snapshot_id VARCHAR(36) NOT NULL,
        snapshot_inputs_hash VARCHAR(64) NOT NULL,
        provider_contract_hash VARCHAR(64) NOT NULL,
        plan_content_hash VARCHAR(64) NOT NULL,
        plan_record_hash VARCHAR(64) NOT NULL,
        approval_hash VARCHAR(64) NOT NULL,
        approved_state_record_hash VARCHAR(64) NOT NULL,
        executing_state_record_hash VARCHAR(64) NOT NULL,
        observed_fingerprint_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_execution_attempt_number
            UNIQUE (task_id, attempt_number),
        CONSTRAINT ck_execution_attempt_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_execution_attempt_number
            CHECK (attempt_number >= 1),
        CONSTRAINT ck_execution_attempt_action
            CHECK (action = 'start_execution'),
        CONSTRAINT ck_execution_attempt_base_sha
            CHECK (
                (length(base_commit_sha) = 40 OR length(base_commit_sha) = 64)
                AND base_commit_sha NOT GLOB '*[^0-9a-f]*'
            ),
        CONSTRAINT ck_execution_attempt_image_digest
            CHECK (
                length(runner_image_digest) = 71
                AND substr(runner_image_digest, 1, 7) = 'sha256:'
                AND substr(runner_image_digest, 8)
                    NOT GLOB '*[^0-9a-f]*'
            ),
        FOREIGN KEY(task_id)
            REFERENCES contribution_tasks (id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_version_id)
            REFERENCES plan_versions (id) ON DELETE RESTRICT,
        FOREIGN KEY(plan_approval_id)
            REFERENCES plan_approvals (id) ON DELETE RESTRICT,
        FOREIGN KEY(approved_state_version_id)
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT,
        FOREIGN KEY(executing_state_version_id)
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_execution_attempts_task_id ON execution_attempts (task_id)",
    (
        "CREATE INDEX ix_execution_attempts_plan_version_id "
        "ON execution_attempts (plan_version_id)"
    ),
    (
        "CREATE INDEX ix_execution_attempts_plan_approval_id "
        "ON execution_attempts (plan_approval_id)"
    ),
    (
        "CREATE INDEX ix_execution_attempts_approved_state_version_id "
        "ON execution_attempts (approved_state_version_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_attempts_executing_state_version_id "
        "ON execution_attempts (executing_state_version_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_attempts_idempotency_key "
        "ON execution_attempts (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_attempts_record_hash "
        "ON execution_attempts (record_hash)"
    ),
    (
        "CREATE INDEX ix_execution_attempts_created_at "
        "ON execution_attempts (created_at)"
    ),
    """
    CREATE TABLE execution_stage_versions (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        execution_attempt_id VARCHAR(36) NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        sequence BIGINT NOT NULL,
        stage VARCHAR(20) NOT NULL,
        status VARCHAR(20) NOT NULL,
        reason_code VARCHAR(100) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        job_spec_hash VARCHAR(64),
        input_hashes JSON NOT NULL,
        result_hash VARCHAR(64),
        workspace_id VARCHAR(128),
        workspace_ref VARCHAR(128),
        workspace_inventory_hash VARCHAR(64),
        attempt_record_hash VARCHAR(64) NOT NULL,
        previous_stage_state_hash VARCHAR(64),
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT uq_execution_stage_sequence
            UNIQUE (execution_attempt_id, sequence),
        CONSTRAINT ck_execution_stage_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_execution_stage_sequence
            CHECK (sequence >= 1),
        CONSTRAINT ck_execution_stage_name
            CHECK (stage IN ('explore', 'implement', 'verify')),
        CONSTRAINT ck_execution_stage_status
            CHECK (
                status IN (
                    'pending', 'running', 'succeeded', 'failed',
                    'cancelled', 'timed_out'
                )
            ),
        CONSTRAINT ck_execution_stage_workspace
            CHECK (
                (
                    workspace_id IS NULL
                    AND workspace_ref IS NULL
                    AND workspace_inventory_hash IS NULL
                )
                OR (
                    workspace_id IS NOT NULL
                    AND workspace_ref IS NOT NULL
                )
            ),
        CONSTRAINT ck_execution_stage_evidence
            CHECK (
                (
                    status = 'pending'
                    AND job_spec_hash IS NULL
                    AND result_hash IS NULL
                )
                OR (
                    status = 'running'
                    AND job_spec_hash IS NOT NULL
                    AND result_hash IS NULL
                )
                OR (
                    status = 'succeeded'
                    AND job_spec_hash IS NOT NULL
                    AND result_hash IS NOT NULL
                )
                OR status IN ('failed', 'cancelled', 'timed_out')
            ),
        FOREIGN KEY(execution_attempt_id)
            REFERENCES execution_attempts (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE INDEX ix_execution_stage_versions_execution_attempt_id "
        "ON execution_stage_versions (execution_attempt_id)"
    ),
    (
        "CREATE INDEX ix_execution_stage_versions_stage "
        "ON execution_stage_versions (stage)"
    ),
    (
        "CREATE INDEX ix_execution_stage_versions_status "
        "ON execution_stage_versions (status)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_stage_versions_idempotency_key "
        "ON execution_stage_versions (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_execution_stage_versions_record_hash "
        "ON execution_stage_versions (record_hash)"
    ),
    (
        "CREATE INDEX ix_execution_stage_versions_created_at "
        "ON execution_stage_versions (created_at)"
    ),
    """
    CREATE TRIGGER execution_attempts_provenance_insert
    BEFORE INSERT ON execution_attempts
    WHEN NOT EXISTS (
        SELECT 1
        FROM plan_approvals AS approval
        JOIN plan_locks AS lock
          ON lock.id = approval.plan_lock_id
        JOIN plan_versions AS plan
          ON plan.id = approval.plan_version_id
        JOIN contribution_tasks AS task
          ON task.id = approval.task_id
        JOIN analysis_versions AS analysis
          ON analysis.id = task.analysis_version_id
        JOIN opportunity_snapshots AS snapshot
          ON snapshot.id = task.snapshot_id
        JOIN contribution_task_state_versions AS approved
          ON approved.id = approval.approved_state_version_id
        JOIN contribution_task_state_versions AS executing
          ON executing.id = NEW.executing_state_version_id
        WHERE approval.id = NEW.plan_approval_id
          AND approval.task_id = NEW.task_id
          AND approval.plan_version_id = NEW.plan_version_id
          AND approval.approved_state_version_id =
              NEW.approved_state_version_id
          AND approval.approval_hash = NEW.approval_hash
          AND approval.plan_content_hash = NEW.plan_content_hash
          AND approval.plan_record_hash = NEW.plan_record_hash
          AND approval.approved_state_record_hash =
              NEW.approved_state_record_hash
          AND lock.plan_version_id = NEW.plan_version_id
          AND lock.task_id = NEW.task_id
          AND lock.analysis_version_id = NEW.analysis_version_id
          AND lock.snapshot_id = NEW.snapshot_id
          AND lock.base_commit_sha = NEW.base_commit_sha
          AND lock.task_record_hash = NEW.task_record_hash
          AND lock.analysis_record_hash = NEW.analysis_record_hash
          AND lock.analysis_output_hash = NEW.analysis_output_hash
          AND lock.snapshot_inputs_hash = NEW.snapshot_inputs_hash
          AND lock.provider_contract_hash = NEW.provider_contract_hash
          AND lock.plan_content_hash = NEW.plan_content_hash
          AND lock.plan_record_hash = NEW.plan_record_hash
          AND plan.task_id = NEW.task_id
          AND plan.content_hash = NEW.plan_content_hash
          AND plan.record_hash = NEW.plan_record_hash
          AND task.record_hash = NEW.task_record_hash
          AND task.analysis_version_id = NEW.analysis_version_id
          AND task.snapshot_id = NEW.snapshot_id
          AND task.analysis_record_hash = NEW.analysis_record_hash
          AND task.analysis_output_hash = NEW.analysis_output_hash
          AND task.snapshot_inputs_hash = NEW.snapshot_inputs_hash
          AND analysis.record_hash = NEW.analysis_record_hash
          AND analysis.analysis_output_hash = NEW.analysis_output_hash
          AND analysis.snapshot_inputs_hash = NEW.snapshot_inputs_hash
          AND snapshot.inputs_hash = NEW.snapshot_inputs_hash
          AND json_extract(snapshot.repository_data, '$.full_name') =
              NEW.repository_full_name
          AND approved.task_id = NEW.task_id
          AND approved.to_state = 'plan_approved'
          AND approved.record_hash = NEW.approved_state_record_hash
          AND executing.task_id = NEW.task_id
          AND executing.sequence = approved.sequence + 1
          AND executing.from_state = 'plan_approved'
          AND executing.to_state = 'executing'
          AND executing.reason_code = 'execution_started'
          AND executing.task_record_hash = NEW.task_record_hash
          AND executing.previous_state_hash = approved.record_hash
          AND executing.record_hash = NEW.executing_state_record_hash
          AND NEW.attempt_number = COALESCE(
              (
                  SELECT MAX(existing.attempt_number) + 1
                  FROM execution_attempts AS existing
                  WHERE existing.task_id = NEW.task_id
              ),
              1
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid execution attempt provenance');
    END
    """,
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
    """
    CREATE TRIGGER execution_attempts_no_update
    BEFORE UPDATE ON execution_attempts
    BEGIN
        SELECT RAISE(ABORT, 'execution attempts are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_attempts_no_delete
    BEFORE DELETE ON execution_attempts
    BEGIN
        SELECT RAISE(ABORT, 'execution attempts are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_stage_versions_no_update
    BEFORE UPDATE ON execution_stage_versions
    BEGIN
        SELECT RAISE(ABORT, 'execution stage versions are immutable');
    END
    """,
    """
    CREATE TRIGGER execution_stage_versions_no_delete
    BEFORE DELETE ON execution_stage_versions
    BEGIN
        SELECT RAISE(ABORT, 'execution stage versions are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0016_execution_attempts supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0016_execution_attempts",
    description=(
        "Add immutable execution roots and restart-safe stage hash chains"
    ),
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds immutable ExecutionAttempt roots and append-only "
        "Explore/Implement/Verify stage versions without rewriting approved "
        "plans or task states. Stop orchestrator and Worker writers and "
        "preserve the SQLite file before upgrade. On failure restore it; "
        "never fabricate a succeeded stage, predecessor hash, workspace "
        "reference, signed JobSpec hash, or execution authorization."
    ),
)
