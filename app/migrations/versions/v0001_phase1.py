from __future__ import annotations

import json

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Connection,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    inspect,
)

from app.migrations.core import Migration


PHASE1_METADATA = MetaData()

repositories = Table(
    "repositories",
    PHASE1_METADATA,
    Column("id", Integer, primary_key=True),
    Column("github_id", BigInteger, unique=True, nullable=True),
    Column("full_name", String(255), nullable=False),
    Column("description", Text, nullable=True),
    Column("html_url", String(500), nullable=True),
    Column("language", String(80), nullable=True),
    Column("license_spdx", String(80), nullable=True),
    Column("stars", Integer, nullable=False),
    Column("forks", Integer, nullable=False),
    Column("open_issues", Integer, nullable=False),
    Column("archived", Boolean, nullable=False),
    Column("disabled", Boolean, nullable=False),
    Column("default_branch", String(255), nullable=True),
    Column("topics", JSON, nullable=False),
    Column("pushed_at", DateTime(timezone=True), nullable=True),
    Column("has_contributing_guide", Boolean, nullable=False),
    Column("health_percentage", Integer, nullable=True),
    Column("sync_error", Text, nullable=True),
    Column("last_synced_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index("ix_repositories_full_name", repositories.c.full_name, unique=True)
Index("ix_repositories_language", repositories.c.language)

opportunities = Table(
    "opportunities",
    PHASE1_METADATA,
    Column("id", Integer, primary_key=True),
    Column("github_issue_id", BigInteger, nullable=False),
    Column(
        "repository_id",
        Integer,
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("issue_number", Integer, nullable=False),
    Column("title", String(500), nullable=False),
    Column("body", Text, nullable=False),
    Column("html_url", String(500), unique=True, nullable=False),
    Column("state", String(30), nullable=False),
    Column("labels", JSON, nullable=False),
    Column("comments_count", Integer, nullable=False),
    Column("assignees_count", Integer, nullable=False),
    Column("author_association", String(40), nullable=True),
    Column("source_queries", JSON, nullable=False),
    Column("issue_created_at", DateTime(timezone=True), nullable=False),
    Column("issue_updated_at", DateTime(timezone=True), nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("eligible", Boolean, nullable=False),
    Column("filter_reasons", JSON, nullable=False),
    Column("score_total", Float, nullable=False),
    Column("score_components", JSON, nullable=False),
    Column("risk_penalty", Float, nullable=False),
    Column("risk_reasons", JSON, nullable=False),
    Column("has_bounty", Boolean, nullable=False),
    Column("bounty_amount_usd", Float, nullable=True),
    Column("is_strategic", Boolean, nullable=False),
    Column("is_tech_match", Boolean, nullable=False),
    UniqueConstraint("repository_id", "issue_number", name="uq_repository_issue"),
)
Index(
    "ix_opportunities_github_issue_id",
    opportunities.c.github_issue_id,
    unique=True,
)
Index("ix_opportunities_repository_id", opportunities.c.repository_id)
Index("ix_opportunities_eligible", opportunities.c.eligible)
Index("ix_opportunities_score_total", opportunities.c.score_total)
Index("ix_opportunities_has_bounty", opportunities.c.has_bounty)
Index("ix_opportunities_is_strategic", opportunities.c.is_strategic)
Index("ix_opportunities_is_tech_match", opportunities.c.is_tech_match)
Index(
    "ix_opportunity_eligible_score",
    opportunities.c.eligible,
    opportunities.c.score_total,
)

daily_picks = Table(
    "daily_picks",
    PHASE1_METADATA,
    Column("id", Integer, primary_key=True),
    Column("selection_date", Date, nullable=False),
    Column("rank", Integer, nullable=False),
    Column(
        "opportunity_id",
        Integer,
        ForeignKey("opportunities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("selection_reason", String(40), nullable=False),
    Column("score_snapshot", Float, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("selection_date", "rank", name="uq_daily_pick_rank"),
    UniqueConstraint(
        "selection_date",
        "opportunity_id",
        name="uq_daily_pick_opportunity",
    ),
)
Index("ix_daily_picks_selection_date", daily_picks.c.selection_date)
Index("ix_daily_picks_opportunity_id", daily_picks.c.opportunity_id)

scan_runs = Table(
    "scan_runs",
    PHASE1_METADATA,
    Column("id", String(36), primary_key=True),
    Column("status", String(30), nullable=False),
    Column("queries", JSON, nullable=False),
    Column("candidate_count", Integer, nullable=False),
    Column("eligible_count", Integer, nullable=False),
    Column("selected_count", Integer, nullable=False),
    Column("repository_count", Integer, nullable=False),
    Column("error_message", Text, nullable=True),
    Column("rate_limit_remaining", Integer, nullable=True),
    Column("rate_limit_reset_at", DateTime(timezone=True), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
)
Index("ix_scan_runs_status", scan_runs.c.status)


_EXPECTED_COLUMNS = {
    table.name: tuple(column.name for column in table.columns)
    for table in PHASE1_METADATA.sorted_tables
}
_EXPECTED_PRIMARY_KEYS = {
    table.name: tuple(column.name for column in table.primary_key.columns)
    for table in PHASE1_METADATA.sorted_tables
}
_EXPECTED_FOREIGN_KEYS = {
    "opportunities": {("repository_id", "repositories", "id", "CASCADE")},
    "daily_picks": {("opportunity_id", "opportunities", "id", "CASCADE")},
    "repositories": set(),
    "scan_runs": set(),
}
_SCHEMA_MANIFEST = {
    "columns": {
        table.name: [
            {
                "name": column.name,
                "type": str(column.type),
                "nullable": column.nullable,
                "primary_key": column.primary_key,
            }
            for column in table.columns
        ]
        for table in PHASE1_METADATA.sorted_tables
    },
    "primary_keys": _EXPECTED_PRIMARY_KEYS,
    "foreign_keys": {
        table: sorted(values) for table, values in _EXPECTED_FOREIGN_KEYS.items()
    },
    "indexes": sorted(
        [
            {
                "name": index.name,
                "columns": [column.name for column in index.columns],
                "unique": index.unique,
            }
            for table in PHASE1_METADATA.tables.values()
            for index in table.indexes
            if index.name
        ],
        key=lambda item: str(item["name"]),
    ),
}


def upgrade(connection: Connection) -> None:
    PHASE1_METADATA.create_all(connection, checkfirst=False)


def _foreign_keys(
    connection: Connection, table_name: str
) -> set[tuple[str, str, str, str]]:
    if connection.dialect.name == "sqlite":
        rows = connection.exec_driver_sql(
            f'PRAGMA foreign_key_list("{table_name}")'
        ).mappings()
        return {
            (
                str(item["from"]),
                str(item["table"]),
                str(item["to"]),
                str(item["on_delete"] or "").upper(),
            )
            for item in rows
        }

    inspector = inspect(connection)
    return {
        (
            tuple(item.get("constrained_columns") or ("",))[0],
            str(item.get("referred_table") or ""),
            tuple(item.get("referred_columns") or ("",))[0],
            str((item.get("options") or {}).get("ondelete") or "").upper(),
        )
        for item in inspector.get_foreign_keys(table_name)
    }


def matches_existing_schema(connection: Connection) -> tuple[bool, str]:
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names()) - {"_schema_migrations"}
    expected_tables = set(_EXPECTED_COLUMNS)
    if actual_tables != expected_tables:
        missing = sorted(expected_tables - actual_tables)
        extra = sorted(actual_tables - expected_tables)
        return False, f"table mismatch (missing={missing}, extra={extra})"

    for table_name, expected_columns in _EXPECTED_COLUMNS.items():
        actual_columns = tuple(
            column["name"] for column in inspector.get_columns(table_name)
        )
        if actual_columns != expected_columns:
            return (
                False,
                f"{table_name} column mismatch "
                f"(expected={expected_columns}, actual={actual_columns})",
            )

        primary_key = inspector.get_pk_constraint(table_name)
        actual_primary_key = tuple(primary_key.get("constrained_columns") or ())
        if actual_primary_key != _EXPECTED_PRIMARY_KEYS[table_name]:
            return (
                False,
                f"{table_name} primary-key mismatch "
                f"(expected={_EXPECTED_PRIMARY_KEYS[table_name]}, "
                f"actual={actual_primary_key})",
            )

        actual_foreign_keys = _foreign_keys(connection, table_name)
        if actual_foreign_keys != _EXPECTED_FOREIGN_KEYS[table_name]:
            return (
                False,
                f"{table_name} foreign-key mismatch "
                f"(expected={sorted(_EXPECTED_FOREIGN_KEYS[table_name])}, "
                f"actual={sorted(actual_foreign_keys)})",
            )

    return True, "schema matches the Phase 1 baseline"


MIGRATION = Migration(
    revision="0001_phase1_baseline",
    description="Adopt or create the Phase 1 discovery schema",
    signature=json.dumps(_SCHEMA_MANIFEST, sort_keys=True, separators=(",", ":")),
    upgrade=upgrade,
    matches_existing_schema=matches_existing_schema,
    recovery=(
        "This baseline never drops user tables. On an empty database retry after "
        "fixing the reported error. For a pre-migration database, stop the "
        "service, preserve the original SQLite file, and restore that file if "
        "schema adoption cannot be verified."
    ),
)
