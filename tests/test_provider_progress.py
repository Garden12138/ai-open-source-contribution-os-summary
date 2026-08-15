from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.database import Database
from app.jobs import JobNotFoundError, JobService
from app.providers import JobProgressService


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def test_job_progress_feed_tracks_failure_retry_and_revision(tmp_path) -> None:
    database = Database(
        f"sqlite+pysqlite:///{tmp_path / 'job-progress.db'}"
    )
    database.create_schema()
    try:
        with database.session() as session:
            job, _ = JobService(session).enqueue(
                kind="progress-fixture",
                idempotency_key="progress-fixture",
                payload={},
                max_attempts=2,
                now=NOW,
            )
            leased = JobService(session).lease_next(
                worker_id="progress-worker",
                now=NOW,
            )
            assert leased is not None
            JobService(session).start(
                job.id,
                worker_id="progress-worker",
                now=NOW,
            )
            JobService(session).fail(
                job.id,
                worker_id="progress-worker",
                error_code="fixture_failure",
                error_message="Safe fixture failure",
                now=NOW + timedelta(seconds=1),
            )

        with database.session() as session:
            failed = JobProgressService(session).get(job.id)
            assert [event.event_type for event in failed.events] == [
                "job.queued",
                "job.failed",
            ]
            assert failed.events[-1].data["error_code"] == "fixture_failure"
            failed_revision = failed.revision
            unchanged = JobProgressService(session).get(
                job.id,
                after_revision=failed_revision,
            )
            assert unchanged.unchanged is True
            assert unchanged.events == ()
            JobService(session).retry(
                job.id,
                now=NOW + timedelta(seconds=2),
            )

        with database.session() as session:
            requeued = JobProgressService(session).get(
                job.id,
                after_revision=failed_revision,
            )
            assert requeued.unchanged is False
            assert requeued.revision != failed_revision
            assert [event.event_type for event in requeued.events] == [
                "job.queued",
                "job.requeued",
            ]
            assert requeued.events[-1].data["attempt_count"] == 1
            with pytest.raises(JobNotFoundError):
                JobProgressService(session).get("missing")
    finally:
        database.close()
