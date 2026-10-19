"""Helpers for reconstructing older schemas in disposable migration fixtures."""

import sqlite3
from sqlalchemy import Connection


def remove_task_visibility_schema(connection: Connection | sqlite3.Connection) -> None:
    execute = getattr(connection, "exec_driver_sql", connection.execute)
    from app.migrations.versions.v0018_dependency_verify_inputs import _STATEMENTS
    for statement in _STATEMENTS:
        execute(statement)
    execute("DELETE FROM _schema_migrations WHERE revision='0036_verification_environment_recovery'")
    execute("DROP TRIGGER IF EXISTS workbench_workspace_scope")
    execute("DELETE FROM _schema_migrations WHERE revision='0035_workspace_scopes'")
    execute("DROP TRIGGER IF EXISTS workbench_baseline_reference")
    execute("DELETE FROM _schema_migrations WHERE revision='0034_execution_baselines'")
    execute("DELETE FROM _schema_migrations WHERE revision='0033_snapshot_assessments'")
    execute("DELETE FROM _schema_migrations WHERE revision='0032_minimax_reviews'")
    triggers = execute("SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE '%task_visibility_versions%'").fetchall()
    for (name,) in triggers:
        execute(f'DROP TRIGGER "{name}"')
    execute("DROP TABLE task_visibility_versions")
    execute("DELETE FROM _schema_migrations WHERE revision='0031_task_visibility'")
