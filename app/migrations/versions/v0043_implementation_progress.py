"""Versioned progress CAS on the existing journal, without rewriting checkpoints."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENT = """CREATE TRIGGER agent_v2_progress_cas BEFORE INSERT ON workbench_events
WHEN NEW.kind = 'agent_v2_progress'
BEGIN
    SELECT CASE WHEN json_extract(NEW.payload, '$.progress.version') IS NOT 'implementation-progress-v1'
        OR NOT EXISTS (SELECT 1 FROM artifacts WHERE id = json_extract(NEW.payload, '$.artifact_id'))
        OR json_type(NEW.payload, '$.progress.revision') IS NOT 'integer'
        OR json_extract(NEW.payload, '$.progress.revision') != 1 + COALESCE((
            SELECT json_extract(prior.payload, '$.progress.revision') FROM workbench_events AS prior
            WHERE prior.task_id = NEW.task_id AND prior.kind = 'agent_v2_progress'
            AND json_extract(prior.payload, '$.session_id') = json_extract(NEW.payload, '$.session_id')
            ORDER BY prior.sequence DESC LIMIT 1), 0)
        OR json_extract(NEW.payload, '$.expected_progress_hash') IS NOT (
            SELECT prior.record_hash FROM workbench_events AS prior
            WHERE prior.task_id = NEW.task_id AND prior.kind = 'agent_v2_progress'
            AND json_extract(prior.payload, '$.session_id') = json_extract(NEW.payload, '$.session_id')
            ORDER BY prior.sequence DESC LIMIT 1)
        OR json_extract(NEW.payload, '$.checkpoint_hash') IS NOT (
            SELECT prior.record_hash FROM workbench_events AS prior
            WHERE prior.task_id = NEW.task_id AND prior.kind = 'agent_v2_checkpoint'
            AND json_extract(prior.payload, '$.session_id') = json_extract(NEW.payload, '$.session_id')
            ORDER BY prior.sequence DESC LIMIT 1)
        THEN RAISE(ABORT, 'Invalid or stale implementation progress') END;
END
"""


def upgrade(connection: Connection) -> None:
    connection.exec_driver_sql(STATEMENT)


MIGRATION = Migration(revision="0043_implementation_progress", description="CAS implementation progress journal guard",
    signature=STATEMENT, upgrade=upgrade,
    recovery="Drain writers; restore matched pre-upgrade SQLite, artifacts and code backup. "
        "Preserve later progress/response events and usage separately. Legacy checkpoints remain unmodified; "
        "never remove a frozen harness policy to resume an old terminal session.")
