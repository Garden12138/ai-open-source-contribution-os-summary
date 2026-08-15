from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.models import ScanRun
from app.worker import DISCOVERY_JOB_KIND, DiscoveryJobWorker


NOW = datetime.now(timezone.utc) + timedelta(seconds=1)


class FailingGitHub:
    rate_limit_remaining = None
    rate_limit_reset_at = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        raise RuntimeError("injected discovery failure")

    async def get_repository_bundle(self, full_name: str) -> dict[str, Any]:
        return {}


class SlowGitHub(FailingGitHub):
    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        await asyncio.sleep(2)
        return []


def test_worker_persists_sanitized_failure(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'failed-worker.db'}")
    database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        github_queries=("offline-query",),
    )
    try:
        with database.session() as session:
            job, _ = JobService(session).enqueue(
                kind=DISCOVERY_JOB_KIND,
                idempotency_key="failed-worker",
                payload={"queries": None, "top_n": None},
                now=NOW,
            )

        worker = DiscoveryJobWorker(
            database,
            settings,
            FailingGitHub,
            worker_id="failing-worker",
            heartbeat_interval_seconds=0.1,
        )
        completed = asyncio.run(worker.run_once(now=NOW))

        assert completed is not None
        assert completed.id == job.id
        assert completed.state == "failed"
        assert completed.error_code == "discovery_runtimeerror"
        assert completed.error_message == (
            "Discovery job failed; inspect the associated scan"
        )
        assert completed.lease_owner is None
    finally:
        database.close()


def test_worker_marks_execution_timeout(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'timeout-worker.db'}")
    database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        github_queries=("offline-query",),
    )
    try:
        with database.session() as session:
            job, _ = JobService(session).enqueue(
                kind=DISCOVERY_JOB_KIND,
                idempotency_key="timeout-worker",
                payload={"queries": None, "top_n": None},
                timeout_seconds=1,
                now=NOW,
            )

        worker = DiscoveryJobWorker(
            database,
            settings,
            SlowGitHub,
            worker_id="slow-worker",
            heartbeat_interval_seconds=0.1,
        )
        completed = asyncio.run(worker.run_once(now=NOW))

        assert completed is not None
        assert completed.id == job.id
        assert completed.state == "timed_out"
        assert completed.error_code == "execution_timeout"
        assert completed.completed_at is not None
        assert completed.lease_owner is None
        with database.session() as session:
            scan_run = session.scalar(select(ScanRun))
            assert scan_run is not None
            assert scan_run.status == "failed"
            assert scan_run.error_message == (
                "Discovery scan cancelled before completion"
            )
            assert scan_run.completed_at is not None
    finally:
        database.close()
