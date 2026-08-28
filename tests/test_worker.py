from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.models import Job, ScanRun
from app.product_experience import ProductExperienceService
from app.worker import ContribOSWorker, DISCOVERY_JOB_KIND, DiscoveryJobWorker


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


class EmptyGitHub(FailingGitHub):
    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
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


def test_unified_worker_runs_one_due_daily_scan_from_local_preferences(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'scheduled-worker.db'}")
    database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        artifact_root=str(tmp_path / "artifacts"),
        github_queries=("offline-query",),
        timezone="Asia/Shanghai",
    )
    due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    local_date = due_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    try:
        with database.session() as session:
            ProductExperienceService(session).create_preference(
                primary_goal="balanced",
                preferred_languages=["Python"],
                weekly_hours=5,
                minimum_bounty_usd=0,
                auto_scan_enabled=True,
                auto_scan_local_time="00:00",
            )

        worker = ContribOSWorker(
            database,
            settings,
            EmptyGitHub,
            worker_id="scheduled-worker",
            heartbeat_interval_seconds=0.1,
        )
        completed = asyncio.run(worker.run_once(now=due_at))
        replay = asyncio.run(worker.run_once(now=due_at + timedelta(minutes=5)))

        assert completed is not None
        assert completed.state == "succeeded", (
            completed.error_code,
            completed.error_message,
        )
        assert completed.idempotency_key == f"scheduled-scan:{local_date.isoformat()}"
        assert replay is None
        with database.session() as session:
            jobs = list(session.scalars(select(Job)))
            scans = list(session.scalars(select(ScanRun)))
            assert len(jobs) == 1
            assert len(scans) == 1
            assert scans[0].selection_date == local_date
    finally:
        database.close()
