"""SQLite connections and atomic, ordered forward migrations."""

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

MIGRATIONS = Path(__file__).with_name("migrations")
MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _statements(script: str) -> Iterator[str]:
    statement = ""
    for char in script:
        statement += char
        if char == ";" and sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        yield statement


def migrate(connection: sqlite3.Connection, directory: Path = MIGRATIONS) -> None:
    """Apply each new SQL file and its migration record in one transaction."""
    connection.execute(MIGRATION_TABLE)
    for path in sorted(directory.glob("*.sql")):
        if not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", path.name):
            raise ValueError("Invalid migration filename")
        script = path.read_text(encoding="utf-8")
        connection.execute("BEGIN IMMEDIATE")
        try:
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (path.name,)
            ).fetchone()
            if not applied:
                for statement in _statements(script):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                    (path.name,),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
