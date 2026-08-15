from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.migrations import MigrationReport, MigrationRunner


class Database:
    def __init__(self, url: str) -> None:
        parsed = make_url(url)
        engine_options: dict[str, object] = {"pool_pre_ping": True}

        if parsed.drivername.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False}
            if parsed.database in (None, "", ":memory:"):
                engine_options["poolclass"] = StaticPool
            else:
                Path(parsed.database).expanduser().parent.mkdir(
                    parents=True, exist_ok=True
                )

        self.engine: Engine = create_engine(url, **engine_options)
        if parsed.drivername.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_schema(self) -> MigrationReport:
        return MigrationRunner(self.engine).upgrade()

    def current_revision(self) -> str | None:
        return MigrationRunner(self.engine).current_revision()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
        finally:
            session.close()

    def close(self) -> None:
        self.engine.dispose()
