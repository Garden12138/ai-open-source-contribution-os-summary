from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Engine,
    MetaData,
    String,
    Table,
    inspect,
    select,
)


class MigrationError(RuntimeError):
    """Raised when a database cannot be upgraded without risking its data."""


Upgrade = Callable[[Connection], None]
ExistingSchemaCheck = Callable[[Connection], tuple[bool, str]]


@dataclass(frozen=True, slots=True)
class Migration:
    revision: str
    description: str
    signature: str
    upgrade: Upgrade
    recovery: str
    matches_existing_schema: ExistingSchemaCheck | None = None

    @property
    def checksum(self) -> str:
        payload = "\n".join((self.revision, self.description, self.signature))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MigrationReport:
    previous_revision: str | None
    current_revision: str | None
    applied: tuple[str, ...]
    stamped: tuple[str, ...]


_MIGRATION_METADATA = MetaData()
SCHEMA_MIGRATIONS = Table(
    "_schema_migrations",
    _MIGRATION_METADATA,
    Column("revision", String(64), primary_key=True),
    Column("description", String(255), nullable=False),
    Column("checksum", String(64), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)


class MigrationRunner:
    """Apply an immutable, ordered migration chain.

    The runner can adopt a pre-migration Phase 1 database only when the first
    migration explicitly recognizes the complete existing schema. Unknown or
    partial schemas fail closed.
    """

    def __init__(
        self, engine: Engine, migrations: Sequence[Migration] | None = None
    ) -> None:
        if migrations is None:
            from app.migrations.versions import MIGRATIONS

            migrations = MIGRATIONS
        self.engine = engine
        self.migrations = tuple(migrations)
        self._validate_chain()

    def _validate_chain(self) -> None:
        revisions = [migration.revision for migration in self.migrations]
        if not revisions:
            raise MigrationError("At least one database migration is required")
        if revisions != sorted(revisions):
            raise MigrationError("Database migrations must be ordered by revision")
        if len(revisions) != len(set(revisions)):
            raise MigrationError("Database migration revisions must be unique")
        for migration in self.migrations:
            if not migration.recovery.strip():
                raise MigrationError(
                    f"Migration {migration.revision} has no recovery instructions"
                )

    def upgrade(self) -> MigrationReport:
        applied_now: list[str] = []
        stamped_now: list[str] = []

        with self.engine.begin() as connection:
            tables_before = set(inspect(connection).get_table_names())
            business_tables = tables_before - {SCHEMA_MIGRATIONS.name}

            SCHEMA_MIGRATIONS.create(connection, checkfirst=True)
            applied = self._read_applied(connection)
            self._validate_applied(applied)
            previous_revision = next(reversed(applied), None)

            if not applied and business_tables:
                first = self.migrations[0]
                check = first.matches_existing_schema
                if check is None:
                    raise MigrationError(
                        "Existing unversioned database cannot be adopted safely"
                    )
                matches, reason = check(connection)
                if not matches:
                    raise MigrationError(
                        "Existing unversioned database does not match the "
                        f"{first.revision} baseline: {reason}"
                    )
                self._record(connection, first)
                applied[first.revision] = first.checksum
                stamped_now.append(first.revision)

            for migration in self.migrations:
                if migration.revision in applied:
                    continue
                migration.upgrade(connection)
                self._record(connection, migration)
                applied[migration.revision] = migration.checksum
                applied_now.append(migration.revision)

            current_revision = next(reversed(applied), None)

        return MigrationReport(
            previous_revision=previous_revision,
            current_revision=current_revision,
            applied=tuple(applied_now),
            stamped=tuple(stamped_now),
        )

    def current_revision(self) -> str | None:
        with self.engine.connect() as connection:
            if SCHEMA_MIGRATIONS.name not in inspect(connection).get_table_names():
                return None
            applied = self._read_applied(connection)
            self._validate_applied(applied)
            return next(reversed(applied), None)

    def _read_applied(self, connection: Connection) -> dict[str, str]:
        rows = connection.execute(
            select(
                SCHEMA_MIGRATIONS.c.revision,
                SCHEMA_MIGRATIONS.c.checksum,
            ).order_by(SCHEMA_MIGRATIONS.c.revision)
        )
        return {str(row.revision): str(row.checksum) for row in rows}

    def _validate_applied(self, applied: dict[str, str]) -> None:
        known = {migration.revision: migration for migration in self.migrations}
        unknown = sorted(set(applied) - set(known))
        if unknown:
            raise MigrationError(
                f"Database contains unknown migration revisions: {', '.join(unknown)}"
            )

        seen_gap = False
        for migration in self.migrations:
            checksum = applied.get(migration.revision)
            if checksum is None:
                seen_gap = True
                continue
            if seen_gap:
                raise MigrationError(
                    "Database migration history is not a contiguous revision chain"
                )
            if checksum != migration.checksum:
                raise MigrationError(
                    f"Checksum mismatch for migration {migration.revision}; "
                    "the applied migration or source has changed"
                )

    @staticmethod
    def _record(connection: Connection, migration: Migration) -> None:
        connection.execute(
            SCHEMA_MIGRATIONS.insert().values(
                revision=migration.revision,
                description=migration.description,
                checksum=migration.checksum,
                applied_at=datetime.now(timezone.utc),
            )
        )
