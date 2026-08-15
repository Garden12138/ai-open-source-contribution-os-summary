from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    CREATE TABLE contribution_tasks (
        id VARCHAR(36) NOT NULL PRIMARY KEY,
        analysis_version_id VARCHAR(36) NOT NULL UNIQUE,
        snapshot_id VARCHAR(36) NOT NULL,
        opportunity_id INTEGER NOT NULL,
        schema_version VARCHAR(32) NOT NULL,
        idempotency_key VARCHAR(128) NOT NULL UNIQUE,
        analysis_record_hash VARCHAR(64) NOT NULL,
        analysis_output_hash VARCHAR(64) NOT NULL,
        snapshot_inputs_hash VARCHAR(64) NOT NULL,
        record_hash VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_contribution_task_schema
            CHECK (schema_version = '1'),
        FOREIGN KEY(analysis_version_id)
            REFERENCES analysis_versions (id) ON DELETE RESTRICT,
        FOREIGN KEY(snapshot_id)
            REFERENCES opportunity_snapshots (id) ON DELETE RESTRICT,
        FOREIGN KEY(opportunity_id)
            REFERENCES opportunities (id) ON DELETE RESTRICT
    )
    """,
    (
        "CREATE UNIQUE INDEX ix_contribution_tasks_analysis_version_id "
        "ON contribution_tasks (analysis_version_id)"
    ),
    (
        "CREATE INDEX ix_contribution_tasks_snapshot_id "
        "ON contribution_tasks (snapshot_id)"
    ),
    (
        "CREATE INDEX ix_contribution_tasks_opportunity_id "
        "ON contribution_tasks (opportunity_id)"
    ),
    (
        "CREATE UNIQUE INDEX ix_contribution_tasks_idempotency_key "
        "ON contribution_tasks (idempotency_key)"
    ),
    (
        "CREATE UNIQUE INDEX ix_contribution_tasks_record_hash "
        "ON contribution_tasks (record_hash)"
    ),
    (
        "CREATE INDEX ix_contribution_tasks_created_at "
        "ON contribution_tasks (created_at)"
    ),
    """
    CREATE TRIGGER contribution_tasks_provenance_insert
    BEFORE INSERT ON contribution_tasks
    WHEN NOT EXISTS (
        SELECT 1
        FROM analysis_versions AS analysis
        JOIN opportunity_snapshots AS snapshot
          ON snapshot.id = analysis.snapshot_id
        WHERE analysis.id = NEW.analysis_version_id
          AND analysis.snapshot_id = NEW.snapshot_id
          AND analysis.record_hash = NEW.analysis_record_hash
          AND analysis.analysis_output_hash = NEW.analysis_output_hash
          AND analysis.snapshot_inputs_hash = NEW.snapshot_inputs_hash
          AND snapshot.opportunity_id = NEW.opportunity_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid contribution task provenance');
    END
    """,
    """
    CREATE TRIGGER contribution_tasks_no_update
    BEFORE UPDATE ON contribution_tasks
    BEGIN
        SELECT RAISE(ABORT, 'contribution tasks are immutable');
    END
    """,
    """
    CREATE TRIGGER contribution_tasks_no_delete
    BEFORE DELETE ON contribution_tasks
    BEGIN
        SELECT RAISE(ABORT, 'contribution tasks are immutable');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0009_contribution_tasks supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0009_contribution_tasks",
    description="Add immutable AnalysisVersion-bound ContributionTask roots",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds an immutable task-root table and provenance "
        "triggers without changing AnalysisVersion records. Stop writers and "
        "preserve the SQLite file before upgrade. On failure restore it; never "
        "drop the triggers, rewrite an AnalysisVersion, or fabricate hashes to "
        "force a task insert."
    ),
)
