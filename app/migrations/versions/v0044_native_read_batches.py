"""Append-only read batch admission/preflight/child lifecycle guards."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENTS = (
"""CREATE TRIGGER agent_v2_read_batch_admission BEFORE INSERT ON workbench_events
WHEN NEW.kind = 'agent_v2_read_batch'
BEGIN
 SELECT CASE WHEN NEW.actor_id != 'orchestrator'
  OR json_type(NEW.payload,'$.call_count') IS NOT 'integer'
  OR json_extract(NEW.payload,'$.call_count') NOT BETWEEN 5 AND 8
  OR json_extract(NEW.payload,'$.reads_reserved') IS NOT json_extract(NEW.payload,'$.call_count')
  OR NOT EXISTS (SELECT 1 FROM artifacts WHERE id=json_extract(NEW.payload,'$.artifact_id'))
  OR NOT EXISTS (SELECT 1 FROM workbench_events r WHERE r.task_id=NEW.task_id
   AND r.job_id=NEW.job_id AND r.kind='agent_v2_model_response'
   AND json_extract(r.payload,'$.session_id')=json_extract(NEW.payload,'$.session_id')
   AND json_extract(r.payload,'$.artifact_id')=json_extract(NEW.payload,'$.response_artifact')
   AND json_extract(r.payload,'$.binding_hash')=json_extract(NEW.payload,'$.binding_hash'))
  OR EXISTS (SELECT 1 FROM workbench_events r WHERE r.task_id=NEW.task_id
   AND r.job_id=NEW.job_id AND r.kind='agent_v2_read_batch')
 THEN RAISE(ABORT,'Invalid native read batch admission') END;
END""",
"""CREATE TRIGGER agent_v2_read_batch_lifecycle BEFORE INSERT ON workbench_events
WHEN NEW.kind IN ('agent_v2_read_batch_preflight','agent_v2_read_sub_batch_started',
 'agent_v2_read_sub_batch_completed','agent_v2_read_batch_completed','agent_v2_read_batch_stopped')
BEGIN
 SELECT CASE WHEN NEW.actor_id != 'orchestrator'
  OR NOT EXISTS (SELECT 1 FROM workbench_events b WHERE b.task_id=NEW.task_id
   AND b.kind='agent_v2_read_batch'
   AND json_extract(b.payload,'$.session_id')=json_extract(NEW.payload,'$.session_id')
   AND json_extract(b.payload,'$.artifact_id')=json_extract(NEW.payload,'$.batch_artifact'))
  OR EXISTS (SELECT 1 FROM workbench_events b WHERE b.task_id=NEW.task_id
   AND b.kind IN ('agent_v2_read_batch_completed','agent_v2_read_batch_stopped')
   AND json_extract(b.payload,'$.batch_artifact')=json_extract(NEW.payload,'$.batch_artifact'))
  OR (NEW.kind != 'agent_v2_read_sub_batch_started' AND NOT EXISTS (
   SELECT 1 FROM artifacts WHERE id=json_extract(NEW.payload,'$.artifact_id')))
 THEN RAISE(ABORT,'Invalid native read batch lifecycle') END;
 SELECT CASE WHEN NEW.kind='agent_v2_read_batch_preflight' AND (
  json_type(NEW.payload,'$.status') IS NOT 'text'
  OR json_extract(NEW.payload,'$.status') NOT IN ('succeeded','rejected')
  OR EXISTS (SELECT 1 FROM workbench_events b WHERE b.task_id=NEW.task_id
   AND b.kind=NEW.kind AND json_extract(b.payload,'$.batch_artifact')=json_extract(NEW.payload,'$.batch_artifact')))
 THEN RAISE(ABORT,'Duplicate or invalid native read batch preflight') END;
 SELECT CASE WHEN NEW.kind IN ('agent_v2_read_sub_batch_started','agent_v2_read_sub_batch_completed') AND (
  json_type(NEW.payload,'$.index') IS NOT 'integer'
  OR json_extract(NEW.payload,'$.index') NOT BETWEEN 0 AND 1
  OR json_type(NEW.payload,'$.call_ids') IS NOT 'array'
  OR json_array_length(NEW.payload,'$.call_ids') NOT BETWEEN 1 AND 4
  OR NOT EXISTS (SELECT 1 FROM workbench_events p WHERE p.task_id=NEW.task_id
   AND p.kind='agent_v2_read_batch_preflight'
   AND json_extract(p.payload,'$.batch_artifact')=json_extract(NEW.payload,'$.batch_artifact')
   AND json_extract(p.payload,'$.status')='succeeded'
   AND json_extract(p.payload,'$.source_hash')=json_extract(NEW.payload,'$.source_hash'))
  OR EXISTS (SELECT 1 FROM workbench_events p WHERE p.task_id=NEW.task_id AND p.kind=NEW.kind
   AND json_extract(p.payload,'$.sub_batch_id')=json_extract(NEW.payload,'$.sub_batch_id')))
 THEN RAISE(ABORT,'Invalid native read sub batch') END;
 SELECT CASE WHEN NEW.kind='agent_v2_read_sub_batch_started' AND json_extract(NEW.payload,'$.index')=1
  AND NOT EXISTS (SELECT 1 FROM workbench_events p WHERE p.task_id=NEW.task_id
   AND p.kind='agent_v2_read_sub_batch_completed'
   AND json_extract(p.payload,'$.batch_artifact')=json_extract(NEW.payload,'$.batch_artifact')
   AND json_extract(p.payload,'$.index')=0 AND json_extract(p.payload,'$.status')='succeeded')
 THEN RAISE(ABORT,'Native read child predecessor incomplete') END;
 SELECT CASE WHEN NEW.kind='agent_v2_read_sub_batch_completed' AND NOT EXISTS (
  SELECT 1 FROM workbench_events p WHERE p.task_id=NEW.task_id AND p.kind='agent_v2_read_sub_batch_started'
   AND json_extract(p.payload,'$.sub_batch_id')=json_extract(NEW.payload,'$.sub_batch_id'))
 THEN RAISE(ABORT,'Native read child was never started') END;
 SELECT CASE WHEN NEW.kind='agent_v2_read_batch_completed' AND (
  SELECT COUNT(*) FROM workbench_events p WHERE p.task_id=NEW.task_id AND p.kind='agent_v2_read_sub_batch_completed'
   AND json_extract(p.payload,'$.batch_artifact')=json_extract(NEW.payload,'$.batch_artifact')
   AND json_extract(p.payload,'$.status')='succeeded') != 2
 THEN RAISE(ABORT,'Native read batch children incomplete') END;
END""",
)


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(revision="0044_native_read_batches", description="Bounded native read batch lifecycle",
    signature="\n".join(STATEMENTS), upgrade=upgrade,
    recovery="Drain writers; preserve all post-upgrade batch/receipt/usage evidence, then restore matching "
        "pre-upgrade database, artifacts and code backup. Never delete batch events, refund reservations, "
        "resume terminal Plan12, or replay an ambiguous/paid request.")
