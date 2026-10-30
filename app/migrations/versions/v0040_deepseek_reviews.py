"""Add DeepSeek to verified reviewers while retaining every provenance guard."""

from sqlalchemy import Connection

from app.migrations.core import Migration, MigrationError
from app.migrations.versions.v0032_minimax_reviews import STATEMENTS as PREVIOUS_STATEMENTS


STATEMENTS = tuple(
    statement.replace("'minimax'", "'minimax', 'deepseek'")
    for statement in PREVIOUS_STATEMENTS
)


def upgrade(connection: Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise MigrationError("Migration 0040_deepseek_reviews supports SQLite only")
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement.strip())


MIGRATION = Migration(
    revision="0040_deepseek_reviews",
    description="Allow immutable DeepSeek reviews with exact invocation binding",
    signature="\n-- statement --\n".join(
        " ".join(statement.split()) for statement in STATEMENTS
    ),
    upgrade=upgrade,
    recovery=(
        "Stop API, workers and Publisher; restore the pre-upgrade SQLite and "
        "artifact backup together. Never edit immutable reviews or remove "
        "reviewer-kind, active-task or provider-invocation guards to downgrade."
    ),
    requires_foreign_keys_disabled=True,
)
