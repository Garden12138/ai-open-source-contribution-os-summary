from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
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
        CONSTRAINT uq_review_run_number
            UNIQUE (task_id, review_number),
        CONSTRAINT ck_review_run_schema
            CHECK (schema_version = '1'),
        CONSTRAINT ck_review_run_number
            CHECK (review_number >= 1),
        CONSTRAINT ck_review_run_kind
            CHECK (reviewer_kind IN ('fake', 'fake_blocking')),
        CONSTRAINT ck_review_run_verdict
            CHECK (verdict IN ('pass', 'block')),
        CONSTRAINT ck_review_run_status
            CHECK (status IN ('succeeded', 'failed')),
        CONSTRAINT ck_review_run_ready
            CHECK (
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
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT,
        FOREIGN KEY(ready_state_version_id)
            REFERENCES contribution_task_state_versions (id)
            ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX ix_review_runs_task_id ON review_runs (task_id)",
    (
        "CREATE INDEX ix_review_runs_plan_version_id "
        "ON review_runs (plan_version_id)"
    ),
    (
        "CREATE INDEX ix_review_runs_created_at "
        "ON review_runs (created_at)"
    ),
    (
        "CREATE UNIQUE INDEX ix_review_runs_execution_attempt_id "
        "ON review_runs (execution_attempt_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_review_runs_idempotency_key "
        "ON review_runs (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_review_runs_reviewing_state_version_id "
        "ON review_runs (reviewing_state_version_id)"
    ),
    "CREATE UNIQUE INDEX ix_review_runs_record_hash ON review_runs (record_hash)",
    """
    CREATE TRIGGER review_runs_provenance_insert
    BEFORE INSERT ON review_runs
    WHEN NOT (
        json_valid(NEW.findings)
        AND json_type(NEW.findings) = 'array'
        AND EXISTS (
            SELECT 1
            FROM execution_attempts AS attempt
            JOIN execution_stage_versions AS implement
              ON implement.id = NEW.implement_stage_version_id
            JOIN execution_stage_versions AS verify
              ON verify.id = NEW.verify_stage_version_id
            JOIN contribution_task_state_versions AS reviewing
              ON reviewing.id = NEW.reviewing_state_version_id
            WHERE attempt.id = NEW.execution_attempt_id
              AND attempt.task_id = NEW.task_id
              AND attempt.plan_version_id = NEW.plan_version_id
              AND attempt.record_hash = NEW.attempt_record_hash
              AND attempt.plan_content_hash = NEW.plan_content_hash
              AND attempt.plan_record_hash = NEW.plan_record_hash
              AND attempt.base_commit_sha = NEW.base_commit_sha
              AND attempt.repository_archive_hash =
                  NEW.repository_archive_hash
              AND attempt.sandbox_policy_hash = NEW.sandbox_policy_hash
              AND implement.execution_attempt_id = attempt.id
              AND implement.stage = 'implement'
              AND implement.status = 'succeeded'
              AND implement.result_hash = NEW.diff_hash
              AND verify.execution_attempt_id = attempt.id
              AND verify.stage = 'verify'
              AND verify.status = 'succeeded'
              AND verify.result_hash = NEW.verify_result_hash
              AND reviewing.task_id = NEW.task_id
              AND reviewing.from_state = 'executing'
              AND reviewing.to_state = 'reviewing'
              AND reviewing.record_hash = NEW.reviewing_state_record_hash
        )
        AND (
            (
                NEW.verdict = 'block'
                AND NEW.ready_state_version_id IS NULL
            )
            OR (
                NEW.verdict = 'pass'
                AND EXISTS (
                    SELECT 1
                    FROM contribution_task_state_versions AS reviewing
                    JOIN contribution_task_state_versions AS ready
                      ON ready.id = NEW.ready_state_version_id
                    WHERE reviewing.id = NEW.reviewing_state_version_id
                      AND ready.task_id = NEW.task_id
                      AND ready.from_state = 'reviewing'
                      AND ready.to_state = 'ready'
                      AND ready.sequence = reviewing.sequence + 1
                      AND ready.previous_state_hash = reviewing.record_hash
                      AND ready.record_hash = NEW.ready_state_record_hash
                )
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid review run provenance');
    END
    """,
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
    "DROP TRIGGER execution_attempts_provenance_insert",
    """
    CREATE TRIGGER execution_attempts_provenance_insert
    BEFORE INSERT ON execution_attempts
    WHEN NOT (
        EXISTS (
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
              AND NEW.attempt_number = COALESCE(
                  (
                      SELECT MAX(existing.attempt_number) + 1
                      FROM execution_attempts AS existing
                      WHERE existing.task_id = NEW.task_id
                  ),
                  1
              )
              AND (
                  (
                      executing.sequence = approved.sequence + 1
                      AND executing.from_state = 'plan_approved'
                      AND executing.to_state = 'executing'
                      AND executing.reason_code = 'execution_started'
                      AND executing.previous_state_hash = approved.record_hash
                      AND executing.record_hash =
                          NEW.executing_state_record_hash
                      AND executing.task_record_hash = NEW.task_record_hash
                      AND NEW.attempt_number = 1
                  )
                  OR (
                      executing.from_state = 'reviewing'
                      AND executing.to_state = 'executing'
                      AND executing.reason_code = 'repair_started'
                      AND executing.record_hash =
                          NEW.executing_state_record_hash
                      AND executing.task_record_hash = NEW.task_record_hash
                      AND NEW.attempt_number BETWEEN 2 AND 4
                      AND EXISTS (
                          SELECT 1
                          FROM review_runs AS review
                          JOIN contribution_task_state_versions AS reviewing
                            ON reviewing.id = review.reviewing_state_version_id
                          WHERE review.task_id = NEW.task_id
                            AND reviewing.to_state = 'reviewing'
                            AND executing.previous_state_hash =
                                reviewing.record_hash
                            AND executing.sequence = reviewing.sequence + 1
                      )
                  )
              )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid execution attempt provenance');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0021_review_runs supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0021_review_runs",
    description="Add immutable ReviewRun and allow bounded repair attempts",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop review and execution writers and preserve the SQLite file "
        "before upgrade. This additive revision creates review_runs and "
        "replaces the execution-attempt provenance trigger so a later "
        "repair can start from reviewing. On failure restore the backup; "
        "never edit a ReviewRun, weaken the hash binding, or fabricate a "
        "repair attempt without a prior review."
    ),
)
