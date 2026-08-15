from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    """
    ALTER TABLE plan_versions
    ADD COLUMN parent_version_id VARCHAR(36)
    REFERENCES plan_versions (id) ON DELETE RESTRICT
    """,
    """
    ALTER TABLE plan_versions
    ADD COLUMN parent_record_hash VARCHAR(64)
    """,
    (
        "CREATE INDEX ix_plan_versions_parent_version_id "
        "ON plan_versions (parent_version_id)"
    ),
    """
    CREATE TRIGGER plan_versions_revision_insert
    BEFORE INSERT ON plan_versions
    WHEN NOT (
        (
            NEW.version_number = 1
            AND NEW.parent_version_id IS NULL
            AND NEW.parent_record_hash IS NULL
        )
        OR (
            NEW.version_number > 1
            AND EXISTS (
                SELECT 1
                FROM plan_versions AS parent
                WHERE parent.id = NEW.parent_version_id
                  AND parent.task_id = NEW.task_id
                  AND parent.version_number = NEW.version_number - 1
                  AND parent.record_hash = NEW.parent_record_hash
            )
        )
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid plan revision parent');
    END
    """,
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0012_plan_revision_links supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0012_plan_revision_links",
    description="Link immutable PlanVersion revisions to exact parents",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in _STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "This revision adds nullable parent provenance to existing immutable "
        "plans and a trigger for future revisions. Stop plan writers and "
        "preserve the SQLite file before upgrade. On failure restore it; never "
        "rewrite an existing plan, skip a version number, or fabricate a parent "
        "record hash."
    ),
)
