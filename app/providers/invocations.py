from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, ProviderInvocation
from app.provenance import content_hash
from app.providers.contracts import ProviderIdentity, ProviderStage, ProviderUsage
from app.security import ensure_no_sensitive_data


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProviderInvocationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ProviderInvocationError(RuntimeError):
    pass


class ProviderInvocationConflictError(ProviderInvocationError):
    pass


class ProviderInvocationService:
    """Append immutable, sanitized provider accounting for each Job attempt."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        job_id: str,
        attempt_number: int,
        stage: ProviderStage,
        status: ProviderInvocationStatus,
        request_id: str,
        correlation_id: str,
        input_hash: str,
        output_hash: str | None,
        provider: ProviderIdentity,
        prompt_version: str,
        policy_version: str,
        output_schema_version: str,
        usage: ProviderUsage,
        error_code: str | None,
        started_at: datetime,
        completed_at: datetime,
    ) -> ProviderInvocation:
        started_at = self._aware(started_at)
        completed_at = self._aware(completed_at)
        if completed_at < started_at:
            raise ValueError("Provider invocation completed before it started")
        if self.session.get(Job, job_id) is None:
            raise ValueError("Provider invocation references an unknown Job")
        if attempt_number < 1:
            raise ValueError("Provider invocation attempt must be at least 1")
        for value, name, maximum in (
            (request_id, "request ID", 128),
            (correlation_id, "correlation ID", 128),
            (prompt_version, "prompt version", 128),
            (policy_version, "policy version", 128),
            (output_schema_version, "output schema version", 128),
        ):
            if not value.strip() or len(value) > maximum:
                raise ValueError(
                    f"Provider invocation {name} must contain between "
                    f"1 and {maximum} characters"
                )
        self._require_hash(input_hash, "input hash")
        if status is ProviderInvocationStatus.SUCCEEDED:
            if output_hash is None or error_code is not None:
                raise ValueError(
                    "Succeeded provider invocation requires output without error"
                )
            self._require_hash(output_hash, "output hash")
        elif output_hash is not None or not error_code:
            raise ValueError(
                "Unsuccessful provider invocation requires an error without output"
            )
        if error_code is not None and len(error_code) > 80:
            raise ValueError("Provider invocation error code is too long")

        payload: dict[str, object] = {
            "job_id": job_id,
            "attempt_number": attempt_number,
            "stage": stage.value,
            "status": status.value,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "provider": provider.hash_payload(),
            "prompt_version": prompt_version,
            "policy_version": policy_version,
            "output_schema_version": output_schema_version,
            "usage": usage.hash_payload(),
            "error_code": error_code,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        ensure_no_sensitive_data(
            payload,
            context="provider invocation accounting",
        )
        record_hash = content_hash(payload)
        existing = self.session.scalar(
            select(ProviderInvocation).where(
                ProviderInvocation.job_id == job_id,
                ProviderInvocation.attempt_number == attempt_number,
                ProviderInvocation.stage == stage.value,
            )
        )
        if existing is not None:
            if existing.record_hash != record_hash:
                raise ProviderInvocationConflictError(
                    "Provider invocation attempt was replayed with different values"
                )
            return existing

        invocation = ProviderInvocation(
            id=str(uuid4()),
            job_id=job_id,
            attempt_number=attempt_number,
            stage=stage.value,
            status=status.value,
            request_id=request_id,
            correlation_id=correlation_id,
            input_hash=input_hash,
            output_hash=output_hash,
            provider_name=provider.provider,
            adapter_version=provider.adapter_version,
            model_name=provider.model,
            model_version=provider.model_version,
            prompt_version=prompt_version,
            policy_version=policy_version,
            output_schema_version=output_schema_version,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_microusd=usage.estimated_cost_microusd,
            duration_ms=usage.duration_ms,
            error_code=error_code,
            record_hash=record_hash,
            started_at=started_at,
            completed_at=completed_at,
        )
        self.session.add(invocation)
        self.session.commit()
        return invocation

    @staticmethod
    def _require_hash(value: str, name: str) -> None:
        if not _SHA256.fullmatch(value):
            raise ValueError(
                f"Provider invocation {name} must be a lowercase SHA-256 hash"
            )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Provider invocation timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)
