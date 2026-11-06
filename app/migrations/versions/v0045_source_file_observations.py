"""Additive guard for non-content, approved-path absence receipts."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENT = """CREATE TRIGGER agent_v2_missing_source_receipt BEFORE INSERT ON workbench_events
WHEN NEW.kind='agent_v2_tool_result' AND json_extract(NEW.payload,'$.status')='not_found'
BEGIN
 SELECT CASE WHEN NEW.actor_id!='orchestrator'
  OR json_extract(NEW.payload,'$.name') IS NOT 'read_file'
  OR json_type(NEW.payload,'$.failure') IS NOT 'null'
  OR json_type(NEW.payload,'$.request_arguments.expected_hash') IS NOT 'null'
  OR NOT EXISTS (SELECT 1 FROM artifacts WHERE id=json_extract(NEW.payload,'$.artifact_id'))
  OR NOT EXISTS (SELECT 1 FROM workbench_events s
   JOIN execution_attempts a ON a.id=json_extract(s.payload,'$.attempt_id')
   JOIN plan_versions p ON p.id=a.plan_version_id
   JOIN json_each(p.files_likely_to_change) w
   WHERE s.task_id=NEW.task_id AND s.kind='agent_v2_session'
    AND json_extract(s.payload,'$.session_id')=json_extract(NEW.payload,'$.session_id')
    AND w.value=json_extract(NEW.payload,'$.request_arguments.path'))
  OR NOT EXISTS (SELECT 1 FROM workbench_events c WHERE c.task_id=NEW.task_id AND c.job_id=NEW.job_id
   AND c.kind IN ('agent_v2_tool_started','agent_v2_tool_reused')
   AND json_extract(c.payload,'$.call_id')=json_extract(NEW.payload,'$.call_id')
   AND json_extract(c.payload,'$.session_id')=json_extract(NEW.payload,'$.session_id'))
 THEN RAISE(ABORT,'Invalid approved missing source receipt') END;
END"""


def upgrade(connection: Connection) -> None:
    connection.exec_driver_sql(STATEMENT)


MIGRATION = Migration(revision="0045_source_file_observations", description="Approved source absence receipt guard",
    signature=STATEMENT, upgrade=upgrade,
    recovery="Drain writers and retain all new absence/patch/checkpoint/usage artifacts. Restore matched SQLite, "
        "artifact and code backup; never delete observation events, refund reservations or resume terminal Plan13.")
