"""Additive guard for versioned source-tree scope authorizations."""
from sqlalchemy import Connection
from app.migrations.core import Migration

STATEMENTS = (
    """CREATE TRIGGER workbench_workspace_scope BEFORE INSERT ON workbench_events
    WHEN NEW.kind = 'execution_authorized'
      AND json_type(NEW.payload, '$.workspace_scope_version') IS NOT NULL AND (
        json_extract(NEW.payload, '$.workspace_scope_version') != 'source-tree-scope-v1'
        OR coalesce(json_type(NEW.payload, '$.workspace_paths'), '') != 'array'
        OR json_array_length(NEW.payload, '$.workspace_paths') > 64
        OR length(coalesce(json_extract(NEW.payload, '$.workspace_scope_hash'), '')) != 64
      ) BEGIN SELECT RAISE(ABORT, 'Workspace scope authorization invalid'); END""",
)


def upgrade(connection: Connection) -> None:
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(
    revision="0035_workspace_scopes", description="Bind versioned source-tree scopes to explicit execution authorizations",
    signature="\n".join(" ".join(s.split()) for s in STATEMENTS), upgrade=upgrade,
    recovery="Stop writers and back up SQLite and artifacts. No historical rows or hashes change. Scoped sandbox-job-spec-v2 requires matching new workers; drain or cancel active work before downgrade. Restore the matching pre-upgrade database/artifact backup and code together. Never reinterpret a scoped job as a full-repository v1 job.",
)
