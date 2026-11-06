"""Additive metered output and capacity-before-child evidence guards."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENTS = (
"""CREATE TRIGGER agent_v2_output_charge_guard BEFORE INSERT ON workbench_events
WHEN NEW.kind='agent_v2_output_charge'
BEGIN
 SELECT CASE WHEN NEW.actor_id!='orchestrator'
  OR json_extract(NEW.payload,'$.contract') IS NOT 'native-read-capacity-v1'
  OR json_type(NEW.payload,'$.bytes') IS NOT 'integer'
  OR json_type(NEW.payload,'$.role') IS NOT 'text'
  OR json_extract(NEW.payload,'$.role') NOT IN ('read_batch','read_preflight','read_capacity','tool_result',
   'observed_result','source_page','read_child_result','read_batch_result','read_batch_stopped')
  OR json_extract(NEW.payload,'$.bytes') < 0
  OR NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.id=json_extract(NEW.payload,'$.artifact_id')
   AND a.size_bytes=json_extract(NEW.payload,'$.bytes'))
  OR NOT EXISTS (SELECT 1 FROM workbench_events s WHERE s.task_id=NEW.task_id
   AND s.kind='agent_v2_session' AND json_extract(s.payload,'$.session_id')=json_extract(NEW.payload,'$.session_id'))
 THEN RAISE(ABORT,'Invalid exact output charge') END;
END""",
"""CREATE TRIGGER agent_v2_read_capacity_guard BEFORE INSERT ON workbench_events
WHEN NEW.kind='agent_v2_read_capacity'
BEGIN
 SELECT CASE WHEN NEW.actor_id!='orchestrator'
  OR json_type(NEW.payload,'$.status') IS NOT 'text'
  OR json_extract(NEW.payload,'$.status') NOT IN ('succeeded','rejected')
  OR NOT EXISTS (SELECT 1 FROM artifacts WHERE id=json_extract(NEW.payload,'$.artifact_id'))
  OR NOT EXISTS (SELECT 1 FROM workbench_events p WHERE p.task_id=NEW.task_id
   AND p.kind='agent_v2_read_batch_preflight' AND json_extract(p.payload,'$.status')='succeeded'
   AND json_extract(p.payload,'$.session_id')=json_extract(NEW.payload,'$.session_id')
   AND json_extract(p.payload,'$.batch_artifact')=json_extract(NEW.payload,'$.batch_artifact')
   AND json_extract(p.payload,'$.source_hash')=json_extract(NEW.payload,'$.source_hash'))
  OR EXISTS (SELECT 1 FROM workbench_events p WHERE p.task_id=NEW.task_id AND p.kind=NEW.kind
   AND json_extract(p.payload,'$.batch_artifact')=json_extract(NEW.payload,'$.batch_artifact'))
 THEN RAISE(ABORT,'Invalid read capacity evidence') END;
END""",
"""CREATE TRIGGER agent_v2_read_capacity_before_child BEFORE INSERT ON workbench_events
WHEN NEW.kind='agent_v2_read_sub_batch_started' AND EXISTS (
 SELECT 1 FROM workbench_events b WHERE b.task_id=NEW.task_id AND b.kind='agent_v2_read_batch'
 AND json_extract(b.payload,'$.artifact_id')=json_extract(NEW.payload,'$.batch_artifact')
 AND json_extract(b.payload,'$.capacity_contract')='native-read-capacity-v1')
BEGIN
 SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM workbench_events p WHERE p.task_id=NEW.task_id
  AND p.kind='agent_v2_read_capacity' AND json_extract(p.payload,'$.status')='succeeded'
  AND json_extract(p.payload,'$.batch_artifact')=json_extract(NEW.payload,'$.batch_artifact')
  AND json_extract(p.payload,'$.source_hash')=json_extract(NEW.payload,'$.source_hash'))
 THEN RAISE(ABORT,'Read capacity proof required') END;
END""",
)


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(revision="0046_read_capacity", description="Source-attested read capacity and exact output charges",
    signature="\n".join(STATEMENTS), upgrade=upgrade,
    recovery="Drain writers, preserve capacity/charge/checkpoint artifacts and restore matched database/artifact/code backup. "
        "Never delete history, refund reservations, resume terminal Plan14 or reuse an old binding.")
