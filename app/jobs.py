from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data, redact_text


class JobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.TIMED_OUT,
    }
)


class JobError(RuntimeError):
    pass


class JobNotFoundError(JobError):
    pass


class JobConflictError(JobError):
    pass


class JobTransitionError(JobError):
    pass


class JobService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self,
        *,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
        max_attempts: int = 3,
        timeout_seconds: int = 600,
        run_after: datetime | None = None,
        now: datetime | None = None,
        commit: bool = True,
    ) -> tuple[Job, bool]:
        now = self._aware(now)
        run_after = self._aware(run_after or now)
        kind = kind.strip()
        idempotency_key = idempotency_key.strip()
        if not kind or len(kind) > 64:
            raise ValueError("Job kind must contain between 1 and 64 characters")
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError(
                "Idempotency key must contain between 1 and 128 characters"
            )
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")
        ensure_no_sensitive_data(kind, context="job kind")
        ensure_no_sensitive_data(
            idempotency_key,
            context="job idempotency key",
        )
        ensure_no_sensitive_data(payload, context="job payload")
        from app.task_visibility import require_active_payload_task
        from app.planning import ContributionTaskError

        try:
            require_active_payload_task(self.session, payload)
        except ContributionTaskError as exc:
            raise JobConflictError("任务已归档或删除，不能启动新处理") from exc

        payload_hash = content_hash(payload)
        existing = self.session.scalar(
            select(Job).where(
                Job.kind == kind,
                Job.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise JobConflictError(
                    "Idempotency key was already used with a different payload"
                )
            return existing, False

        job = Job(
            id=str(uuid4()),
            kind=kind,
            state=JobState.QUEUED.value,
            idempotency_key=idempotency_key,
            payload=payload,
            payload_hash=payload_hash,
            result_data={},
            attempt_count=0,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            run_after=run_after,
            progress_current=0,
            created_at=now,
            updated_at=now,
        )
        self.session.add(job)
        try:
            self.session.flush()
            from app.model_settings import bind_job_model
            bind_job_model(self.session, job)
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self.session.scalar(
                select(Job).where(
                    Job.kind == kind,
                    Job.idempotency_key == idempotency_key,
                )
            )
            if existing is None:
                raise
            if existing.payload_hash != payload_hash:
                raise JobConflictError(
                    "Idempotency key was already used with a different payload"
                ) from exc
            return existing, False
        return job, True

    def lease_next(
        self,
        *,
        worker_id: str,
        kinds: tuple[str, ...] | None = None,
        exclude_model_bound: bool = False,
        exclude_fake_provider: bool = False,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> Job | None:
        now = self._aware(now)
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")

        self.recover_expired(now=now)
        query = (
            select(Job.id)
            .where(
                Job.state == JobState.QUEUED.value,
                Job.run_after <= now,
                Job.cancel_requested_at.is_(None),
            )
            .order_by(Job.run_after, Job.created_at, Job.id)
            .limit(1)
        )
        if kinds:
            query = query.where(Job.kind.in_(kinds))
        if exclude_model_bound:
            from app.models import JobModelBinding
            query = query.where(Job.id.not_in(select(JobModelBinding.job_id)))
        if exclude_fake_provider:
            provider_name = Job.payload["expected_provider"]["provider"].as_string()
            query = query.where(or_(provider_name.is_(None), provider_name != "fake"))
        job_id = self.session.scalar(query)
        if job_id is None:
            return None

        lease_expires_at = now + timedelta(seconds=lease_seconds)
        result = self.session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.state == JobState.QUEUED.value,
                Job.run_after <= now,
                Job.cancel_requested_at.is_(None),
            )
            .values(
                state=JobState.LEASED.value,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                heartbeat_at=now,
                attempt_count=Job.attempt_count + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.session.rollback()
            return None
        self.session.commit()
        self.session.expire_all()
        return self._get(job_id)

    def start(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
        commit: bool = True,
    ) -> Job:
        now = self._aware(now)
        return self._transition(
            job_id,
            worker_id=worker_id,
            allowed=(JobState.LEASED,),
            now=now,
            require_live_lease=True,
            values={
                "state": JobState.RUNNING.value,
                "started_at": now,
                "updated_at": now,
            },
            commit=commit,
        )

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> Job:
        now = self._aware(now)
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        return self._transition(
            job_id,
            worker_id=worker_id,
            allowed=(JobState.LEASED, JobState.RUNNING),
            now=now,
            require_live_lease=True,
            values={
                "heartbeat_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            },
        )

    def update_progress(
        self,
        job_id: str,
        *,
        worker_id: str,
        current: int,
        total: int | None = None,
        message: str | None = None,
        now: datetime | None = None,
    ) -> Job:
        now = self._aware(now)
        if current < 0 or (total is not None and total < current):
            raise ValueError("Job progress is out of range")
        return self._transition(
            job_id,
            worker_id=worker_id,
            allowed=(JobState.RUNNING,),
            now=now,
            require_live_lease=True,
            values={
                "progress_current": current,
                "progress_total": total,
                "progress_message": (
                    redact_text(message)[:500] if message else None
                ),
                "updated_at": now,
            },
        )

    def succeed(
        self,
        job_id: str,
        *,
        worker_id: str,
        result_data: dict[str, Any] | None = None,
        scan_run_id: str | None = None,
        now: datetime | None = None,
        commit: bool = True,
    ) -> Job:
        now = self._aware(now)
        safe_result = result_data or {}
        ensure_no_sensitive_data(
            safe_result,
            context="job result",
        )
        return self._transition(
            job_id,
            worker_id=worker_id,
            allowed=(JobState.RUNNING,),
            now=now,
            require_live_lease=True,
            require_no_cancel=True,
            values={
                "state": JobState.SUCCEEDED.value,
                "result_data": safe_result,
                "scan_run_id": scan_run_id,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": now,
                "completed_at": now,
                "updated_at": now,
            },
            commit=commit,
        )

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        error_code: str,
        error_message: str,
        now: datetime | None = None,
        commit: bool = True,
    ) -> Job:
        now = self._aware(now)
        return self._transition(
            job_id,
            worker_id=worker_id,
            allowed=(JobState.LEASED, JobState.RUNNING),
            now=now,
            values={
                "state": JobState.FAILED.value,
                "error_code": redact_text(error_code)[:80],
                "error_message": redact_text(error_message)[:2000],
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": now,
                "completed_at": now,
                "updated_at": now,
            },
            commit=commit,
        )

    def time_out(
        self,
        job_id: str,
        *,
        worker_id: str,
        error_message: str = "Job exceeded its execution timeout",
        now: datetime | None = None,
        commit: bool = True,
    ) -> Job:
        now = self._aware(now)
        return self._transition(
            job_id,
            worker_id=worker_id,
            allowed=(JobState.LEASED, JobState.RUNNING),
            now=now,
            values={
                "state": JobState.TIMED_OUT.value,
                "error_code": "execution_timeout",
                "error_message": error_message[:2000],
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": now,
                "completed_at": now,
                "updated_at": now,
            },
            commit=commit,
        )

    def request_cancel(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> Job:
        now = self._aware(now)
        job = self._get(job_id)
        state = JobState(job.state)
        if state in TERMINAL_STATES:
            return job
        if state == JobState.QUEUED:
            job.state = JobState.CANCELLED.value
            job.cancel_requested_at = now
            job.completed_at = now
        else:
            job.cancel_requested_at = job.cancel_requested_at or now
        job.updated_at = now
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return job

    def cancel(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
        commit: bool = True,
    ) -> Job:
        now = self._aware(now)
        return self._transition(
            job_id,
            worker_id=worker_id,
            allowed=(JobState.LEASED, JobState.RUNNING),
            now=now,
            values={
                "state": JobState.CANCELLED.value,
                "cancel_requested_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": now,
                "completed_at": now,
                "updated_at": now,
            },
            commit=commit,
        )

    def retry(
        self,
        job_id: str,
        *,
        run_after: datetime | None = None,
        now: datetime | None = None,
    ) -> Job:
        now = self._aware(now)
        job = self._get(job_id)
        state = JobState(job.state)
        if state not in {
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMED_OUT,
        }:
            raise JobTransitionError(
                f"Job {job_id} cannot retry from state {job.state}"
            )
        from app.task_visibility import require_active_job_task
        from app.planning import ContributionTaskError

        try:
            require_active_job_task(self.session, job)
        except ContributionTaskError as exc:
            raise JobTransitionError("任务已归档或删除，不能重试") from exc
        if job.attempt_count >= job.max_attempts:
            raise JobTransitionError(f"Job {job_id} exhausted its attempts")
        job.state = JobState.QUEUED.value
        job.run_after = self._aware(run_after or now)
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.cancel_requested_at = None
        job.error_code = None
        job.error_message = None
        job.started_at = None
        job.completed_at = None
        job.progress_current = 0
        job.progress_total = None
        job.progress_message = None
        job.result_data = {}
        job.scan_run_id = None
        job.updated_at = now
        self.session.commit()
        return job

    def recover_expired(self, *, now: datetime | None = None) -> tuple[str, ...]:
        now = self._aware(now)
        expired = list(
            self.session.scalars(
                select(Job).where(
                    Job.state.in_(
                        (JobState.LEASED.value, JobState.RUNNING.value)
                    ),
                    Job.lease_expires_at <= now,
                )
            )
        )
        recovered: list[str] = []
        for job in expired:
            recovered.append(job.id)
            if job.cancel_requested_at is not None:
                job.state = JobState.CANCELLED.value
                job.completed_at = now
                job.error_code = "cancelled_after_lease_expiry"
            elif job.attempt_count < job.max_attempts:
                job.state = JobState.QUEUED.value
                job.run_after = now
                job.started_at = None
                job.error_code = "lease_expired_retry"
                job.error_message = "Previous worker lease expired; job requeued"
            else:
                job.state = JobState.TIMED_OUT.value
                job.completed_at = now
                job.error_code = "lease_expired"
                job.error_message = "Worker lease expired after the final attempt"
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.updated_at = now
        if recovered:
            self.session.commit()
        return tuple(recovered)

    def get(self, job_id: str) -> Job:
        return self._get(job_id)

    def _transition(
        self,
        job_id: str,
        *,
        worker_id: str,
        allowed: tuple[JobState, ...],
        now: datetime,
        values: dict[str, Any],
        require_live_lease: bool = False,
        require_no_cancel: bool = False,
        commit: bool = True,
    ) -> Job:
        conditions = [
            Job.id == job_id,
            Job.state.in_(tuple(state.value for state in allowed)),
            Job.lease_owner == worker_id,
        ]
        if require_live_lease:
            conditions.append(Job.lease_expires_at > now)
        if require_no_cancel:
            conditions.append(Job.cancel_requested_at.is_(None))
        result = self.session.execute(
            update(Job)
            .where(*conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self.session.rollback()
            if self.session.get(Job, job_id) is None:
                raise JobNotFoundError(f"Job {job_id} was not found")
            raise JobTransitionError(
                f"Job {job_id} transition was rejected for worker {worker_id}"
            )
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        self.session.expire_all()
        return self._get(job_id)

    def _get(self, job_id: str) -> Job:
        job = self.session.get(Job, job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} was not found")
        return job

    @staticmethod
    def _aware(value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
