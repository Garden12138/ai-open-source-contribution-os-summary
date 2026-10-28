"""Additive invariants for the v2 event protocol. Historical rows stay exact."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENTS = ("""CREATE TRIGGER agent_v2_event_protocol BEFORE INSERT ON workbench_events
WHEN NEW.kind LIKE 'agent_v2_%'
BEGIN
    SELECT CASE WHEN NEW.actor_id != 'orchestrator'
        OR json_extract(NEW.payload, '$.version') IS NOT 'agent-runtime-v2'
        OR json_type(NEW.payload, '$.session_id') IS NOT 'text'
        THEN RAISE(ABORT, 'Invalid v2 event authority') END;
    SELECT CASE WHEN NEW.kind != 'agent_v2_session' AND NOT EXISTS (
        SELECT 1 FROM workbench_events AS root WHERE root.task_id = NEW.task_id
          AND root.kind = 'agent_v2_session'
          AND json_extract(root.payload, '$.session_id') = json_extract(NEW.payload, '$.session_id')
    ) THEN RAISE(ABORT, 'Missing v2 session') END;
    SELECT CASE WHEN NEW.kind = 'agent_v2_session' AND EXISTS (
        SELECT 1 FROM workbench_events AS root WHERE root.task_id = NEW.task_id
          AND root.kind = 'agent_v2_session'
          AND json_extract(root.payload, '$.session_id') = json_extract(NEW.payload, '$.session_id')
    ) THEN RAISE(ABORT, 'Duplicate v2 session') END;
    SELECT CASE WHEN NEW.kind = 'agent_v2_checkpoint' AND EXISTS (
        SELECT 1 FROM workbench_events AS cp WHERE cp.task_id = NEW.task_id
          AND cp.kind = 'agent_v2_checkpoint'
          AND json_extract(cp.payload, '$.session_id') = json_extract(NEW.payload, '$.session_id')
    ) AND json_extract(NEW.payload, '$.checkpoint.revision') IS NOT (
        SELECT cp.record_hash FROM workbench_events AS cp WHERE cp.task_id = NEW.task_id
          AND cp.kind = 'agent_v2_checkpoint'
          AND json_extract(cp.payload, '$.session_id') = json_extract(NEW.payload, '$.session_id')
        ORDER BY cp.sequence DESC LIMIT 1
    ) THEN RAISE(ABORT, 'Stale v2 checkpoint') END;
END
""",)


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(
    revision="0038_agent_runtime_v2", description="Versioned CAS agent journal protocol",
    signature="\n".join(STATEMENTS), upgrade=upgrade,
    recovery="Drain writers; restore a matched pre-upgrade SQLite/artifact/code backup. "
             "Preserve all later events and incurred usage. Never downgrade an active v2 session in place.",
)
