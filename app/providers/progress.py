from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs import JobNotFoundError
from app.models import Job, ProviderInvocation
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data


@dataclass(frozen=True, slots=True)
class JobProgressEvent:
    sequence: int
    event_id: str
    event_type: str
    occurred_at: datetime
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class JobProgressFeed:
    job_id: str
    state: str
    revision: str
    unchanged: bool
    events: tuple[JobProgressEvent, ...]


class JobProgressService:
    """Build a bounded, sanitized progress feed from durable Job records."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self,
        job_id: str,
        *,
        after_revision: str | None = None,
    ) -> JobProgressFeed:
        if after_revision is not None and (
            len(after_revision) != 64
            or any(
                character not in "0123456789abcdef"
                for character in after_revision
            )
        ):
            raise ValueError("Job progress revision must be a SHA-256 hash")
        job = self.session.get(Job, job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} was not found")
        invocations = tuple(
            self.session.scalars(
                select(ProviderInvocation)
                .where(ProviderInvocation.job_id == job.id)
                .order_by(
                    ProviderInvocation.attempt_number,
                    ProviderInvocation.completed_at,
                    ProviderInvocation.id,
                )
            )
        )
        raw_events: list[tuple[str, datetime, dict[str, Any]]] = [
            (
                "job.queued",
                job.created_at,
                {
                    "state": "queued",
                    "attempt_count": 0,
                    "progress_current": 0,
                    "progress_total": None,
                    "message_code": "job.queued",
                    "error_code": None,
                },
            )
        ]
        for invocation in invocations:
            raw_events.append(
                (
                    f"provider.{invocation.stage}.{invocation.status}",
                    invocation.completed_at,
                    {
                        "attempt_number": invocation.attempt_number,
                        "stage": invocation.stage,
                        "status": invocation.status,
                        "message_code": (
                            f"provider.{invocation.stage}.{invocation.status}"
                        ),
                        "input_tokens": invocation.input_tokens,
                        "cached_input_tokens": (
                            invocation.cached_input_tokens
                        ),
                        "output_tokens": invocation.output_tokens,
                        "estimated_cost_microusd": (
                            invocation.estimated_cost_microusd
                        ),
                        "duration_ms": invocation.duration_ms,
                        "error_code": invocation.error_code,
                        "output_hash": invocation.output_hash,
                        "record_hash": invocation.record_hash,
                    },
                )
            )
        if not (
            job.state == "queued"
            and job.attempt_count == 0
            and not invocations
        ):
            current_event_type = (
                "job.requeued"
                if job.state == "queued"
                else f"job.{job.state}"
            )
            raw_events.append(
                (
                    current_event_type,
                    (
                        job.completed_at
                        or job.started_at
                        or job.updated_at
                    ),
                    {
                        "state": job.state,
                        "attempt_count": job.attempt_count,
                        "progress_current": job.progress_current,
                        "progress_total": job.progress_total,
                        "message_code": (
                            job.progress_message
                            or f"job.{job.state}"
                        ),
                        "error_code": job.error_code,
                    },
                )
            )
        event_payloads: list[dict[str, Any]] = []
        events: list[JobProgressEvent] = []
        for sequence, (event_type, occurred_at, data) in enumerate(
            raw_events,
            start=1,
        ):
            payload = {
                "job_id": job.id,
                "sequence": sequence,
                "event_type": event_type,
                "occurred_at": occurred_at,
                "data": data,
            }
            ensure_no_sensitive_data(
                payload,
                context="Job progress event",
            )
            event_id = content_hash(payload)
            event_payloads.append({**payload, "event_id": event_id})
            events.append(
                JobProgressEvent(
                    sequence=sequence,
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at=occurred_at,
                    data=MappingProxyType(data),
                )
            )
        revision = content_hash(
            {
                "job_id": job.id,
                "state": job.state,
                "updated_at": job.updated_at,
                "events": event_payloads,
            }
        )
        unchanged = after_revision == revision
        return JobProgressFeed(
            job_id=job.id,
            state=job.state,
            revision=revision,
            unchanged=unchanged,
            events=() if unchanged else tuple(events),
        )
