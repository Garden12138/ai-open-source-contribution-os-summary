from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    "DROP TRIGGER review_runs_provenance_insert",
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
            JOIN execution_artifact_manifests AS implement_manifest
              ON implement_manifest.execution_attempt_id = attempt.id
             AND implement_manifest.stage = 'implement'
             AND implement_manifest.result_hash = implement.result_hash
            JOIN execution_artifact_entries AS diff
              ON diff.manifest_id = implement_manifest.id
             AND diff.role = 'unified-diff'
            JOIN execution_stage_versions AS verify
              ON verify.id = NEW.verify_stage_version_id
            JOIN execution_artifact_manifests AS verify_manifest
              ON verify_manifest.execution_attempt_id = attempt.id
             AND verify_manifest.stage = 'verify'
             AND verify_manifest.result_hash = verify.result_hash
            JOIN execution_artifact_entries AS tests
              ON tests.manifest_id = verify_manifest.id
             AND tests.role = 'normalized-test-results'
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
              AND diff.artifact_id = NEW.diff_hash
              AND verify.execution_attempt_id = attempt.id
              AND verify.stage = 'verify'
              AND verify.status = 'succeeded'
              AND verify.result_hash = NEW.verify_result_hash
              AND tests.artifact_id = NEW.test_results_hash
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
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0026_review_artifact_bindings supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0026_review_artifact_bindings",
    description="Bind new reviews to exact diff and test-result artifacts",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop review and publication writers and preserve the SQLite file "
        "before upgrade. This migration replaces only the ReviewRun insert "
        "provenance trigger; existing immutable reviews are retained. On "
        "failure restore the backup and do not weaken artifact binding checks."
    ),
)
