"""Forward guard for new expectation-bearing command JSON; no old row rewrite."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENT = """CREATE TRIGGER environment_plan_expectation BEFORE INSERT ON plan_versions
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM json_each(NEW.commands_to_run) AS command
        WHERE json_type(command.value, '$.baseline_expectation') IS NOT NULL
        AND (
            json_type(command.value, '$.baseline_expectation') IS NOT 'object'
            OR json_extract(command.value, '$.baseline_expectation.version') IS NOT 'expected-issue-reproduction-v1'
            OR json_type(command.value, '$.baseline_expectation.issue_url') IS NOT 'text'
            OR json_type(command.value, '$.baseline_expectation.test_node') IS NOT 'text'
            OR json_type(command.value, '$.baseline_expectation.failure_literal') IS NOT 'text'
            OR json_extract(command.value, '$.baseline_expectation.exit_code') IS NOT 1
        )
    ) THEN RAISE(ABORT, 'Invalid baseline expectation') END;
END
"""


def upgrade(connection: Connection) -> None:
    connection.exec_driver_sql(STATEMENT)


MIGRATION = Migration(revision="0039_environment_contracts", description="Guard exact baseline expectation JSON",
    signature=STATEMENT, upgrade=upgrade,
    recovery="Drain writers and restore matched SQLite/artifacts/code backup. Preserve all later evidence and usage; do not reinterpret new contracts with old verifiers.")
