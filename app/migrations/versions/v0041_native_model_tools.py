"""Guard new frozen model-tool contracts without rewriting legacy events."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENT = """CREATE TRIGGER workbench_native_model_transport BEFORE INSERT ON workbench_events
WHEN json_type(NEW.payload, '$.agent_model_transport') IS NOT NULL
BEGIN
    SELECT CASE WHEN NEW.kind NOT IN ('plan_bound', 'execution_authorized') OR NEW.plan_version_id IS NULL
        OR json_type(NEW.payload, '$.agent_model_transport') IS NOT 'object'
        OR (SELECT count(*) FROM json_each(NEW.payload, '$.agent_model_transport')) != 2
        OR json_extract(NEW.payload, '$.agent_model_transport.version') IS NOT 'agent-native-tools-v1'
        OR json_type(NEW.payload, '$.agent_model_transport.tools_schema_hash') IS NOT 'text'
        OR length(json_extract(NEW.payload, '$.agent_model_transport.tools_schema_hash')) != 64
        OR json_extract(NEW.payload, '$.agent_model_transport.tools_schema_hash') GLOB '*[^0-9a-f]*'
        OR (NEW.kind = 'execution_authorized' AND (
            json_extract(NEW.payload, '$.agent_runtime_version') IS NOT 'agent-runtime-v2'
            OR json_extract(NEW.payload, '$.implementation.provider') IS NOT 'deepseek'
            OR NOT EXISTS (SELECT 1 FROM workbench_events AS binding
                WHERE binding.task_id = NEW.task_id AND binding.kind = 'plan_bound'
                AND binding.plan_version_id = NEW.plan_version_id
                AND binding.record_hash = json_extract(NEW.payload, '$.binding_hash')
                AND json_extract(binding.payload, '$.agent_model_transport.version') = 'agent-native-tools-v1'
                AND json_extract(binding.payload, '$.agent_model_transport.tools_schema_hash') =
                    json_extract(NEW.payload, '$.agent_model_transport.tools_schema_hash'))))
    THEN RAISE(ABORT, 'Invalid frozen native model transport') END;
END
"""

NO_DOWNGRADE = """CREATE TRIGGER workbench_native_model_transport_no_downgrade BEFORE INSERT ON workbench_events
WHEN NEW.kind = 'execution_authorized' AND EXISTS (
    SELECT 1 FROM workbench_events AS binding
    WHERE binding.task_id = NEW.task_id AND binding.kind = 'plan_bound'
    AND binding.plan_version_id = NEW.plan_version_id
    AND binding.record_hash = json_extract(NEW.payload, '$.binding_hash')
    AND json_type(binding.payload, '$.agent_model_transport') IS NOT NULL)
    AND json_type(NEW.payload, '$.agent_model_transport') IS NULL
BEGIN SELECT RAISE(ABORT, 'Frozen native model transport cannot be removed'); END
"""


def upgrade(connection: Connection) -> None:
    connection.exec_driver_sql(STATEMENT)
    connection.exec_driver_sql(NO_DOWNGRADE)


MIGRATION = Migration(revision="0041_native_model_tools", description="Guard frozen native model tools on new plans and authorizations",
    signature=STATEMENT + NO_DOWNGRADE, upgrade=upgrade,
    recovery="Drain all writers; restore matched pre-upgrade SQLite, artifacts and code backup. Preserve later events and usage separately. Never strip native transport from a frozen plan, approval or TaskSpec to run it with the legacy adapter.")
