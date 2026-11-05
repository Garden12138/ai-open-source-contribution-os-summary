"""Additive guards for response receipts; no historical row is rewritten."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENT = """CREATE TRIGGER agent_v2_response_evidence BEFORE INSERT ON workbench_events
WHEN NEW.kind = 'agent_v2_model_response'
BEGIN
    SELECT CASE WHEN json_type(NEW.payload, '$.artifact_id') IS NOT 'text'
        OR NOT EXISTS (SELECT 1 FROM artifacts WHERE id = json_extract(NEW.payload, '$.artifact_id'))
        OR NOT EXISTS (SELECT 1 FROM workbench_events AS start
            WHERE start.task_id = NEW.task_id AND start.job_id = NEW.job_id
            AND start.kind = 'agent_v2_model_started'
            AND json_extract(start.payload, '$.session_id') = json_extract(NEW.payload, '$.session_id')
            AND json_extract(start.payload, '$.prompt_artifact') = json_extract(NEW.payload, '$.prompt_artifact'))
        OR EXISTS (SELECT 1 FROM workbench_events AS prior
            WHERE prior.task_id = NEW.task_id AND prior.job_id = NEW.job_id
            AND prior.kind = 'agent_v2_model_response')
        THEN RAISE(ABORT, 'Invalid agent response evidence') END;
END
"""


def upgrade(connection: Connection) -> None:
    connection.exec_driver_sql(STATEMENT)


MIGRATION = Migration(revision="0042_agent_response_evidence", description="Durable native response evidence guards",
    signature=STATEMENT, upgrade=upgrade,
    recovery="Drain writers; restore matched pre-upgrade SQLite, artifacts and code backup. "
        "Preserve later response events and all usage separately. Never delete response receipts or replay paid calls.")
