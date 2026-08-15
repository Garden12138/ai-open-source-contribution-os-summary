from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
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
                              AND json_array_length(NEW.input_hashes)
                                  IN (1, 2)
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
            "Migration 0018_dependency_verify_inputs supports SQLite only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0018_dependency_verify_inputs",
    description=(
        "Allow one signed dependency result as an optional Verify input"
    ),
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision replaces only the execution-stage provenance trigger "
        "so Verify may bind one optional dependency result after the exact "
        "Implement result. Stop stage writers and preserve SQLite before "
        "upgrade. On failure restore it; never add an unverified second input "
        "or reorder the required Implement result."
    ),
)
