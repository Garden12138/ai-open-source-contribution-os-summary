"""Allow a failed Verify to consume a newly proven baseline, preserving history."""
from sqlalchemy import Connection
from app.migrations.core import Migration
from app.migrations.versions.v0018_dependency_verify_inputs import _STATEMENTS as PREVIOUS

_BOUNDARY = """                          OR (
                              previous.stage = NEW.stage
                              AND previous.status IN ('failed', 'timed_out')"""
_RECOVERY = """                          OR (
                              previous.stage = 'verify' AND NEW.stage = 'verify'
                              AND previous.status = 'failed' AND NEW.status = 'pending'
                              AND NEW.reason_code = 'dependency_environment_restored'
                              AND NEW.job_spec_hash IS NULL AND NEW.result_hash IS NULL
                              AND json_array_length(previous.input_hashes) = 2
                              AND json_array_length(NEW.input_hashes) = 2
                              AND json_extract(previous.input_hashes, '$[0]') = json_extract(NEW.input_hashes, '$[0]')
                              AND NEW.workspace_id = previous.workspace_id
                              AND NEW.workspace_ref = previous.workspace_ref
                              AND NEW.workspace_inventory_hash = previous.workspace_inventory_hash
                              AND EXISTS (
                                SELECT 1 FROM workbench_events AS receipt JOIN jobs AS preparation ON preparation.id = receipt.job_id
                                WHERE receipt.kind = 'verification_environment_restored' AND receipt.actor_id = 'system'
                                  AND receipt.task_id = (SELECT task_id FROM execution_attempts WHERE id = NEW.execution_attempt_id)
                                  AND json_extract(receipt.payload, '$.attempt_id') = NEW.execution_attempt_id
                                  AND json_extract(receipt.payload, '$.failed_stage_hash') = previous.record_hash
                                  AND json_extract(receipt.payload, '$.implement_result_hash') = json_extract(NEW.input_hashes, '$[0]')
                                  AND json_extract(receipt.payload, '$.old_dependency_hash') = json_extract(previous.input_hashes, '$[1]')
                                  AND json_extract(receipt.payload, '$.dependency_result_hash') = json_extract(NEW.input_hashes, '$[1]')
                                  AND preparation.kind = 'execution_prepare' AND preparation.state = 'succeeded'
                                  AND json_extract(preparation.payload, '$.recovery_stage_hash') = previous.record_hash
                                  AND json_extract(preparation.payload, '$.execution_attempt_id') = NEW.execution_attempt_id
                                  AND json_extract(preparation.result_data, '$.result_hash') = json_extract(receipt.payload, '$.baseline_hash')
                              )
                          )
"""
assert PREVIOUS[1].count(_BOUNDARY) == 1
STATEMENTS = (PREVIOUS[0], PREVIOUS[1].replace(_BOUNDARY, _RECOVERY + _BOUNDARY))


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(
    revision="0036_verification_environment_recovery",
    description="Bind append-only Verify environment replacement to a successful recovery baseline job",
    signature="\n".join(" ".join(s.split()) for s in STATEMENTS), upgrade=upgrade,
    recovery="Stop all writers and back up SQLite and artifacts before upgrade. No historical rows or hashes change. Drain recovery preparation and Verify jobs before rollback. Restore the complete pre-upgrade database/artifact backup with matching code; old workers cannot interpret recovery input edges. Never rewrite an old dependency result or failure to fabricate restoration.",
)
