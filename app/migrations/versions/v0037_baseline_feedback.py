"""Authorize read-only replanning only from a completed baseline diagnosis."""
from sqlalchemy import Connection
from app.migrations.core import Migration
from app.migrations.versions.v0029_workbench import STATEMENTS as PREVIOUS

_OLD = "OR (NEW.from_state IN ('executing', 'reviewing') AND NEW.to_state = 'planning' AND NEW.reason_code = 'user_replan')"
_NEW = _OLD + """
        OR (NEW.from_state = 'executing' AND NEW.to_state = 'planning'
            AND NEW.reason_code = 'baseline_requires_replan'
            AND EXISTS (
                SELECT 1 FROM workbench_events AS request
                JOIN workbench_events AS diagnosis ON diagnosis.record_hash = json_extract(request.payload, '$.diagnosis_hash')
                JOIN jobs AS job ON job.id = diagnosis.job_id
                JOIN execution_attempts AS attempt ON attempt.id = json_extract(request.payload, '$.attempt_id')
                WHERE request.task_id = NEW.task_id AND request.kind = 'baseline_replan_requested'
                  AND request.actor_id = 'orchestrator' AND json_extract(request.payload, '$.policy') = 'baseline-feedback-v1'
                  AND diagnosis.task_id = NEW.task_id AND diagnosis.kind = 'baseline_diagnosed'
                  AND diagnosis.actor_id = 'planner' AND request.job_id = job.id
                  AND json_extract(diagnosis.payload, '$.policy') = 'baseline-feedback-v1'
                  AND json_extract(diagnosis.payload, '$.artifact_id') = json_extract(request.payload, '$.diagnostic_hash')
                  AND job.kind = 'baseline_diagnosis' AND job.state = 'succeeded'
                  AND json_extract(job.payload, '$.execution_attempt_id') = attempt.id
                  AND json_extract(job.payload, '$.authorization_hash') = json_extract(request.payload, '$.authorization_hash')
                  AND json_extract(diagnosis.payload, '$.baseline_hash') = json_extract(request.payload, '$.baseline_hash')
                  AND attempt.task_id = NEW.task_id AND request.plan_version_id = attempt.plan_version_id
                  AND attempt.executing_state_record_hash = NEW.previous_state_hash
            ))
"""
_CREATE = next(s for s in PREVIOUS if s.startswith('CREATE TRIGGER contribution_task_state_legal_insert'))
assert _CREATE.count(_OLD) == 1
STATEMENTS = ('DROP TRIGGER contribution_task_state_legal_insert', _CREATE.replace(_OLD, _NEW))


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(revision='0037_baseline_feedback',
    description='Bind automatic baseline replanning to a completed read-only diagnosis',
    signature='\n'.join(' '.join(s.split()) for s in STATEMENTS), upgrade=upgrade,
    recovery='Stop writers and back up SQLite/artifacts before upgrading. Historical records and hashes are unchanged. Drain baseline diagnosis/planning jobs before rollback, then restore the complete pre-upgrade database/artifacts with matching code. Never rewrite failed baselines, approvals, or completed diagnosis records.')
