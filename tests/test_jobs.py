from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.jobs import JobConflictError, JobService, JobTransitionError


NOW = datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    current = Database(f"sqlite+pysqlite:///{tmp_path / 'jobs.db'}")
    current.create_schema()
    try:
        yield current
    finally:
        current.close()


def test_enqueue_is_idempotent_and_rejects_payload_reuse(database: Database) -> None:
    with database.session() as session:
        service = JobService(session)
        first, created = service.enqueue(
            kind="discovery_scan",
            idempotency_key="scan-2026-07-30",
            payload={"queries": ["one"], "top_n": 10},
            now=NOW,
        )
        repeated, repeated_created = service.enqueue(
            kind="discovery_scan",
            idempotency_key="scan-2026-07-30",
            payload={"top_n": 10, "queries": ["one"]},
            now=NOW + timedelta(seconds=1),
        )

        assert created is True
        assert repeated_created is False
        assert repeated.id == first.id
        assert repeated.payload_hash == first.payload_hash
        assert repeated.state == "queued"

        with pytest.raises(JobConflictError, match="different payload"):
            service.enqueue(
                kind="discovery_scan",
                idempotency_key="scan-2026-07-30",
                payload={"queries": ["different"], "top_n": 10},
                now=NOW,
            )


def test_job_lease_heartbeat_progress_and_success(database: Database) -> None:
    with database.session() as session:
        service = JobService(session)
        queued, _ = service.enqueue(
            kind="discovery_scan",
            idempotency_key="successful-scan",
            payload={},
            now=NOW,
        )
        leased = service.lease_next(
            worker_id="worker-a",
            lease_seconds=30,
            now=NOW,
        )

        assert leased is not None
        assert leased.id == queued.id
        assert leased.state == "leased"
        assert leased.attempt_count == 1
        assert leased.lease_owner == "worker-a"

        with pytest.raises(JobTransitionError):
            service.start(leased.id, worker_id="worker-b", now=NOW)

        running = service.start(
            leased.id,
            worker_id="worker-a",
            now=NOW + timedelta(seconds=1),
        )
        assert running.state == "running"

        progressed = service.update_progress(
            running.id,
            worker_id="worker-a",
            current=2,
            total=5,
            message="scoring candidates",
            now=NOW + timedelta(seconds=2),
        )
        assert progressed.progress_current == 2
        assert progressed.progress_total == 5

        heartbeat = service.heartbeat(
            running.id,
            worker_id="worker-a",
            lease_seconds=60,
            now=NOW + timedelta(seconds=3),
        )
        assert heartbeat.heartbeat_at is not None

        completed = service.succeed(
            running.id,
            worker_id="worker-a",
            result_data={"selected": 3},
            now=NOW + timedelta(seconds=4),
        )
        assert completed.state == "succeeded"
        assert completed.result_data == {"selected": 3}
        assert completed.completed_at is not None
        assert completed.lease_owner is None
        assert completed.lease_expires_at is None


def test_cancellation_is_durable_and_blocks_success(database: Database) -> None:
    with database.session() as session:
        service = JobService(session)
        queued, _ = service.enqueue(
            kind="discovery_scan",
            idempotency_key="queued-cancel",
            payload={},
            now=NOW,
        )
        cancelled = service.request_cancel(queued.id, now=NOW)
        assert cancelled.state == "cancelled"
        assert cancelled.completed_at is not None

        retried = service.retry(
            queued.id,
            now=NOW + timedelta(seconds=1),
        )
        assert retried.state == "queued"

        leased = service.lease_next(
            worker_id="worker-a",
            now=NOW + timedelta(seconds=1),
        )
        assert leased is not None
        service.start(
            leased.id,
            worker_id="worker-a",
            now=NOW + timedelta(seconds=2),
        )
        requested = service.request_cancel(
            leased.id,
            now=NOW + timedelta(seconds=3),
        )
        assert requested.state == "running"
        assert requested.cancel_requested_at is not None

        with pytest.raises(JobTransitionError):
            service.succeed(
                leased.id,
                worker_id="worker-a",
                now=NOW + timedelta(seconds=4),
            )
        cancelled = service.cancel(
            leased.id,
            worker_id="worker-a",
            now=NOW + timedelta(seconds=4),
        )
        assert cancelled.state == "cancelled"


def test_expired_lease_recovers_after_database_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.db"
    database = Database(f"sqlite+pysqlite:///{database_path}")
    database.create_schema()
    with database.session() as session:
        service = JobService(session)
        job, _ = service.enqueue(
            kind="discovery_scan",
            idempotency_key="restart-recovery",
            payload={},
            max_attempts=2,
            now=NOW,
        )
        leased = service.lease_next(
            worker_id="lost-worker",
            lease_seconds=10,
            now=NOW,
        )
        assert leased is not None
        service.start(leased.id, worker_id="lost-worker", now=NOW)
    database.close()

    restarted = Database(f"sqlite+pysqlite:///{database_path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            service = JobService(session)
            recovered = service.recover_expired(
                now=NOW + timedelta(seconds=11)
            )
            assert recovered == (job.id,)
            requeued = service.get(job.id)
            assert requeued.state == "queued"
            assert requeued.attempt_count == 1
            assert requeued.error_code == "lease_expired_retry"

            final_lease = service.lease_next(
                worker_id="second-lost-worker",
                lease_seconds=10,
                now=NOW + timedelta(seconds=11),
            )
            assert final_lease is not None
            service.start(
                final_lease.id,
                worker_id="second-lost-worker",
                now=NOW + timedelta(seconds=11),
            )
        restarted.close()

        final_restart = Database(f"sqlite+pysqlite:///{database_path}")
        final_restart.create_schema()
        try:
            with final_restart.session() as session:
                service = JobService(session)
                service.recover_expired(now=NOW + timedelta(seconds=22))
                timed_out = service.get(job.id)
                assert timed_out.state == "timed_out"
                assert timed_out.attempt_count == 2
                assert timed_out.error_code == "lease_expired"
                assert timed_out.completed_at is not None
        finally:
            final_restart.close()
    finally:
        restarted.close()


def test_database_rejects_inconsistent_job_state_fields(
    database: Database,
) -> None:
    with database.session() as session:
        service = JobService(session)
        job, _ = service.enqueue(
            kind="discovery_scan",
            idempotency_key="constraint-check",
            payload={},
            now=NOW,
        )

        with pytest.raises(IntegrityError, match="ck_job_state_fields"):
            session.execute(
                text(
                    "UPDATE jobs SET state = 'running' "
                    "WHERE id = :job_id"
                ),
                {"job_id": job.id},
            )
            session.commit()
