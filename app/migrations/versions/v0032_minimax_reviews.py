"""Allow verified MiniMax reviews without weakening immutable provenance."""

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError
from app.migrations.versions.v0028_nvidia_review_runs import (
    _STATEMENTS as REVIEW_STATEMENTS,
)
from app.migrations.versions.v0031_task_visibility import _inactive


def _statements() -> tuple[str, ...]:
    values: list[str] = []
    for statement in REVIEW_STATEMENTS:
        statement = statement.replace(
            "'fake_blocking', 'nvidia_nim'",
            "'fake_blocking', 'nvidia_nim', 'openai_compatible', 'minimax'",
        )
        statement = statement.replace(
            "NEW.reviewer_kind != 'nvidia_nim'",
            "NEW.reviewer_kind NOT IN "
            "('nvidia_nim', 'openai_compatible', 'minimax')",
        )
        statement = statement.replace(
            "invocation.provider_name = 'nvidia_nim'",
            "invocation.provider_name = NEW.reviewer_kind",
        )
        values.append(statement)
    values.append(
        f"""CREATE TRIGGER review_runs_task_active BEFORE INSERT ON review_runs
        WHEN {_inactive('NEW.task_id')}
        BEGIN SELECT RAISE(ABORT, 'Task is archived or deleted'); END"""
    )
    return tuple(values)


STATEMENTS = _statements()


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError("Migration 0032_minimax_reviews supports SQLite only")
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0032_minimax_reviews",
    description="Allow immutable MiniMax reviews with exact invocation binding",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop API, provider workers, and publishers; restore the pre-upgrade "
        "SQLite and artifact backup together. Do not bypass the reviewer-kind, "
        "task-visibility, or provider-invocation constraints."
    ),
    requires_foreign_keys_disabled=True,
)
