from __future__ import annotations

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError


_STATEMENTS = (
    "ALTER TABLE scan_runs ADD COLUMN selection_date DATE",
    "CREATE INDEX ix_scan_runs_selection_date ON scan_runs (selection_date)",
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError(
            "Migration 0005_scan_selection_date supports the SQLite MVP only"
        )
    for statement in _STATEMENTS:
        connection.exec_driver_sql(statement)


MIGRATION = Migration(
    revision="0005_scan_selection_date",
    description="Bind new scan runs to their local leaderboard date",
    signature="\n-- statement --\n".join(_STATEMENTS),
    upgrade=upgrade,
    recovery=(
        "This revision adds a nullable date and index. Existing Phase 1/early "
        "Phase 2 rows remain unverified with a null date. Preserve the SQLite "
        "file before upgrade and restore it if column/index creation fails."
    ),
)
