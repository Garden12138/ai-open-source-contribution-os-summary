from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import Database
from app.jobs import JobService, JobTransitionError
from app.models import Job, ProviderInvocation
from app.provenance import canonical_json
from app.providers.budgets import (
    AnalysisBudget,
    AnalysisBudgetLedger,
    BudgetExceededError,
)
from app.providers.contracts import (
    AnalysisProvider,
    AnalysisResult,
    AnalyzeRequest,
    FrozenEvidence,
    InspectionResult,
    InspectRequest,
    ProviderCompletedEvent,
    ProviderContractError,
    ProviderIdentity,
    ProviderLifecycleEvent,
    ProviderRunError,
    ProviderStage,
    ProviderUsage,
    ProviderUsageEvent,
)
from app.providers.invocations import (
    ProviderInvocationService,
    ProviderInvocationStatus,
)
from app.security import contains_sensitive_text


PROVIDER_ANALYSIS_JOB_KIND = "provider_analysis"
PROVIDER_ANALYSIS_SPEC_VERSION = "provider-analysis-job-v1"
MAX_PROVIDER_JOB_PAYLOAD_BYTES = 2_000_000
MAX_PROVIDER_STAGE_OUTPUT_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class ProviderAnalysisJobSpec:
    correlation_id: str
    snapshot_id: str
    score_version_id: str
    evidence: tuple[FrozenEvidence, ...]
    inspect_prompt_version: str
    inspect_policy_version: str
    inspect_output_schema_version: str
    analyze_prompt_version: str
    analyze_policy_version: str
    analyze_output_schema_version: str
    budget: AnalysisBudget
    expected_provider: ProviderIdentity
    frozen_input_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if len(self.evidence) > 64:
            raise ProviderContractError(
                "Provider analysis Job cannot contain more than 64 evidence items"
            )
        InspectRequest.create(
            request_id="provider-job-spec-validation",
            correlation_id=self.correlation_id,
            snapshot_id=self.snapshot_id,
            score_version_id=self.score_version_id,
            evidence=self.evidence,
            prompt_version=self.inspect_prompt_version,
            policy_version=self.inspect_policy_version,
            output_schema_version=self.inspect_output_schema_version,
        )
        for value, name in (
            (self.analyze_prompt_version, "analyze prompt version"),
            (self.analyze_policy_version, "analyze policy version"),
            (
                self.analyze_output_schema_version,
                "analyze output schema version",
            ),
        ):
            _safe_text(value, name, maximum=128)
        if self.frozen_input_hash is not None and not re.fullmatch(
            r"[0-9a-f]{64}",
            self.frozen_input_hash,
        ):
            raise ProviderContractError(
                "Provider analysis frozen input hash must be a SHA-256 hash"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "spec_version": PROVIDER_ANALYSIS_SPEC_VERSION,
            "correlation_id": self.correlation_id,
            "snapshot_id": self.snapshot_id,
            "score_version_id": self.score_version_id,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "kind": item.kind,
                    "source_uri": item.source_uri,
                    "content": item.content,
                    "content_hash": item.content_hash,
                }
                for item in self.evidence
            ],
            "versions": {
                "inspect": {
                    "prompt": self.inspect_prompt_version,
                    "policy": self.inspect_policy_version,
                    "output_schema": self.inspect_output_schema_version,
                },
                "analyze": {
                    "prompt": self.analyze_prompt_version,
                    "policy": self.analyze_policy_version,
                    "output_schema": self.analyze_output_schema_version,
                },
            },
            "budget": asdict(self.budget),
            "expected_provider": self.expected_provider.hash_payload(),
            "frozen_input_hash": self.frozen_input_hash,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProviderAnalysisJobSpec":
        _require_size(
            payload,
            maximum=MAX_PROVIDER_JOB_PAYLOAD_BYTES,
            name="provider analysis Job payload",
        )
        _exact_keys(
            payload,
            {
                "spec_version",
                "correlation_id",
                "snapshot_id",
                "score_version_id",
                "evidence",
                "versions",
                "budget",
                "expected_provider",
                "frozen_input_hash",
            },
            "provider analysis payload",
        )
        if payload["spec_version"] != PROVIDER_ANALYSIS_SPEC_VERSION:
            raise ValueError("Provider analysis Job spec version is unsupported")
        evidence_values = payload["evidence"]
        if not isinstance(evidence_values, list):
            raise ValueError("Provider analysis evidence must be a list")
        evidence: list[FrozenEvidence] = []
        for value in evidence_values:
            item = _mapping(value, "provider analysis evidence")
            _exact_keys(
                item,
                {
                    "evidence_id",
                    "kind",
                    "source_uri",
                    "content",
                    "content_hash",
                },
                "provider analysis evidence",
            )
            evidence.append(
                FrozenEvidence(
                    evidence_id=_string(item["evidence_id"], "evidence ID"),
                    kind=_string(item["kind"], "evidence kind"),
                    source_uri=_string(item["source_uri"], "evidence source URI"),
                    content=_string(item["content"], "evidence content", allow_empty=True),
                    content_hash=_string(item["content_hash"], "evidence hash"),
                )
            )

        versions = _mapping(payload["versions"], "provider analysis versions")
        _exact_keys(versions, {"inspect", "analyze"}, "provider analysis versions")
        inspect_versions = _version_mapping(versions["inspect"], "inspect")
        analyze_versions = _version_mapping(versions["analyze"], "analyze")

        budget_data = _mapping(payload["budget"], "provider analysis budget")
        budget_fields = set(AnalysisBudget.__dataclass_fields__)
        _exact_keys(budget_data, budget_fields, "provider analysis budget")
        budget = AnalysisBudget(
            **{
                name: _integer(
                    budget_data[name],
                    f"provider analysis budget {name}",
                )
                for name in budget_fields
            }
        )

        provider_data = _mapping(
            payload["expected_provider"],
            "expected provider identity",
        )
        _exact_keys(
            provider_data,
            {"provider", "adapter_version", "model", "model_version"},
            "expected provider identity",
        )
        return cls(
            correlation_id=_string(payload["correlation_id"], "correlation ID"),
            snapshot_id=_string(payload["snapshot_id"], "snapshot ID"),
            score_version_id=_string(
                payload["score_version_id"],
                "score version ID",
            ),
            evidence=tuple(evidence),
            inspect_prompt_version=inspect_versions["prompt"],
            inspect_policy_version=inspect_versions["policy"],
            inspect_output_schema_version=inspect_versions["output_schema"],
            analyze_prompt_version=analyze_versions["prompt"],
            analyze_policy_version=analyze_versions["policy"],
            analyze_output_schema_version=analyze_versions["output_schema"],
            budget=budget,
            expected_provider=ProviderIdentity(
                provider=_string(provider_data["provider"], "provider"),
                adapter_version=_string(
                    provider_data["adapter_version"],
                    "adapter version",
                ),
                model=_string(provider_data["model"], "model"),
                model_version=_string(
                    provider_data["model_version"],
                    "model version",
                ),
            ),
            frozen_input_hash=(
                None
                if payload["frozen_input_hash"] is None
                else _string(
                    payload["frozen_input_hash"],
                    "frozen input hash",
                )
            ),
        )


def enqueue_provider_analysis(
    *,
    service: JobService,
    spec: ProviderAnalysisJobSpec,
    idempotency_key: str,
    now: datetime | None = None,
) -> tuple[Job, bool]:
    payload = spec.to_payload()
    _require_size(
        payload,
        maximum=MAX_PROVIDER_JOB_PAYLOAD_BYTES,
        name="provider analysis Job payload",
    )
    return service.enqueue(
        kind=PROVIDER_ANALYSIS_JOB_KIND,
        idempotency_key=idempotency_key,
        payload=payload,
        max_attempts=spec.budget.max_retries + 1,
        timeout_seconds=max(1, math.ceil(spec.budget.max_duration_ms / 1000)),
        now=now,
    )


class _ProviderLifecycleRecorder:
    def __init__(
        self,
        *,
        request_id: str,
        correlation_id: str,
        stage: ProviderStage,
        provider: ProviderIdentity,
    ) -> None:
        self.request_id = request_id
        self.correlation_id = correlation_id
        self.stage = stage
        self.provider = provider
        self.events: list[ProviderLifecycleEvent] = []

    async def __call__(self, event: ProviderLifecycleEvent) -> None:
        if (
            event.request_id != self.request_id
            or event.correlation_id != self.correlation_id
            or event.stage is not self.stage
            or event.provider != self.provider
        ):
            raise ProviderContractError(
                "Provider lifecycle event does not match the active request"
            )
        if event.sequence != len(self.events) + 1:
            raise ProviderContractError(
                "Provider lifecycle event sequence is not contiguous"
            )
        self.events.append(event)

    @property
    def observed_usage(self) -> ProviderUsage:
        for event in reversed(self.events):
            if isinstance(event, (ProviderUsageEvent, ProviderCompletedEvent)):
                return event.usage
        return ProviderUsage()

    def require_completed(self, *, output_hash: str, usage: ProviderUsage) -> None:
        if not self.events or self.events[0].kind != "started":
            raise ProviderContractError("Provider lifecycle did not start")
        completed = self.events[-1]
        if not isinstance(completed, ProviderCompletedEvent):
            raise ProviderContractError("Provider lifecycle did not complete")
        if completed.output_hash != output_hash or completed.usage != usage:
            raise ProviderContractError(
                "Provider completion event does not match the result"
            )


@dataclass(slots=True)
class _ActiveInvocation:
    stage: ProviderStage
    request_id: str
    input_hash: str
    prompt_version: str
    policy_version: str
    output_schema_version: str
    started_at: datetime
    recorder: _ProviderLifecycleRecorder
    recorded: bool = False


class _ProviderJobCancelled(RuntimeError):
    pass


class ProviderAnalysisJobWorker:
    """Run FakeProvider-compatible analysis pipelines through durable Jobs.

    The read-only container runner exists, but the concrete Codex adapter
    remains intentionally unwired until P3-T15–T16 add the internal model
    gateway and credential boundary.
    """

    def __init__(
        self,
        database: Database,
        provider: AnalysisProvider,
        *,
        worker_id: str,
        heartbeat_interval_seconds: float = 15,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        self.database = database
        self.provider = provider
        self.worker_id = worker_id
        self.heartbeat_interval_seconds = max(0.1, heartbeat_interval_seconds)

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        started_at = _aware(now)
        with self.database.session() as session:
            service = JobService(session)
            leased = service.lease_next(
                worker_id=self.worker_id,
                kinds=(PROVIDER_ANALYSIS_JOB_KIND,),
                lease_seconds=60,
                now=started_at,
            )
            if leased is None:
                return None
            running = service.start(
                leased.id,
                worker_id=self.worker_id,
                now=started_at,
            )
            running = service.heartbeat(
                running.id,
                worker_id=self.worker_id,
                lease_seconds=running.timeout_seconds + 30,
                now=started_at,
            )
            job_id = running.id
            attempt_number = running.attempt_count
            timeout_seconds = running.timeout_seconds
            payload = dict(running.payload)

        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                job_id,
                heartbeat_stop,
                lease_seconds=timeout_seconds + 30,
            )
        )
        active: _ActiveInvocation | None = None
        try:
            spec = ProviderAnalysisJobSpec.from_payload(payload)
            if self.provider.identity != spec.expected_provider:
                raise ProviderRunError(
                    "provider_identity_mismatch",
                    "Runtime provider identity does not match the queued Job",
                    retryable=False,
                )
            ledger = AnalysisBudgetLedger(spec.budget)
            ledger.consider_candidates((spec.snapshot_id,))
            self._restore_budget_ledger(
                ledger,
                job_id=job_id,
                attempt_number=attempt_number,
                candidate_id=spec.snapshot_id,
            )

            async with asyncio.timeout(timeout_seconds):
                inspect_request = InspectRequest.create(
                    request_id=f"{job_id}:inspect:{attempt_number}",
                    correlation_id=spec.correlation_id,
                    snapshot_id=spec.snapshot_id,
                    score_version_id=spec.score_version_id,
                    evidence=spec.evidence,
                    prompt_version=spec.inspect_prompt_version,
                    policy_version=spec.inspect_policy_version,
                    output_schema_version=spec.inspect_output_schema_version,
                )
                ledger.start_invocation(
                    invocation_id=inspect_request.request_id,
                    candidate_id=spec.snapshot_id,
                )
                inspect_recorder = _ProviderLifecycleRecorder(
                    request_id=inspect_request.request_id,
                    correlation_id=spec.correlation_id,
                    stage=ProviderStage.INSPECT,
                    provider=spec.expected_provider,
                )
                active = _ActiveInvocation(
                    stage=ProviderStage.INSPECT,
                    request_id=inspect_request.request_id,
                    input_hash=inspect_request.input_hash,
                    prompt_version=inspect_request.prompt_version,
                    policy_version=inspect_request.policy_version,
                    output_schema_version=inspect_request.output_schema_version,
                    started_at=_aware(None),
                    recorder=inspect_recorder,
                )
                inspection = await self.provider.inspect(
                    inspect_request,
                    inspect_recorder,
                )
                self._validate_inspection(
                    inspection,
                    request=inspect_request,
                    provider=spec.expected_provider,
                    recorder=inspect_recorder,
                )
                self._append_invocation(
                    job_id=job_id,
                    attempt_number=attempt_number,
                    active=active,
                    status=ProviderInvocationStatus.SUCCEEDED,
                    output_hash=inspection.output_hash,
                    usage=inspection.usage,
                    error_code=None,
                )
                active.recorded = True
                ledger.record_usage(
                    invocation_id=inspect_request.request_id,
                    usage=inspection.usage,
                )
                self._update_progress(job_id, current=1)
                if self._cancel_requested(job_id):
                    raise _ProviderJobCancelled
                active = None

                analyze_request = AnalyzeRequest.create(
                    request_id=f"{job_id}:analyze:{attempt_number}",
                    correlation_id=spec.correlation_id,
                    snapshot_id=spec.snapshot_id,
                    score_version_id=spec.score_version_id,
                    inspection=inspection,
                    evidence=spec.evidence,
                    prompt_version=spec.analyze_prompt_version,
                    policy_version=spec.analyze_policy_version,
                    output_schema_version=spec.analyze_output_schema_version,
                )
                ledger.start_invocation(
                    invocation_id=analyze_request.request_id,
                    candidate_id=spec.snapshot_id,
                )
                analyze_recorder = _ProviderLifecycleRecorder(
                    request_id=analyze_request.request_id,
                    correlation_id=spec.correlation_id,
                    stage=ProviderStage.ANALYZE,
                    provider=spec.expected_provider,
                )
                active = _ActiveInvocation(
                    stage=ProviderStage.ANALYZE,
                    request_id=analyze_request.request_id,
                    input_hash=analyze_request.input_hash,
                    prompt_version=analyze_request.prompt_version,
                    policy_version=analyze_request.policy_version,
                    output_schema_version=analyze_request.output_schema_version,
                    started_at=_aware(None),
                    recorder=analyze_recorder,
                )
                analysis = await self.provider.analyze(
                    analyze_request,
                    analyze_recorder,
                )
                self._validate_analysis(
                    analysis,
                    request=analyze_request,
                    provider=spec.expected_provider,
                    recorder=analyze_recorder,
                )
                self._append_invocation(
                    job_id=job_id,
                    attempt_number=attempt_number,
                    active=active,
                    status=ProviderInvocationStatus.SUCCEEDED,
                    output_hash=analysis.output_hash,
                    usage=analysis.usage,
                    error_code=None,
                )
                active.recorded = True
                ledger.record_usage(
                    invocation_id=analyze_request.request_id,
                    usage=analysis.usage,
                )
                self._update_progress(job_id, current=2)

            if self._cancel_requested(job_id):
                raise _ProviderJobCancelled
            result_data = self._result_data(
                spec=spec,
                inspection=inspection,
                analysis=analysis,
                ledger=ledger,
                attempt_number=attempt_number,
            )
            with self.database.session() as session:
                completed = JobService(session).succeed(
                    job_id,
                    worker_id=self.worker_id,
                    result_data=result_data,
                    now=_aware(None),
                    commit=False,
                )
                # Import locally to preserve the Provider JobSpec dependency
                # direction while committing Job success and its immutable
                # AnalysisVersion in one transaction.
                from app.providers.versions import AnalysisVersionService

                if spec.frozen_input_hash is not None:
                    AnalysisVersionService(
                        session
                    ).create_from_succeeded_job(
                        job_id=job_id,
                        now=_aware(None),
                    )
                else:
                    session.commit()
                session.refresh(completed)
                return completed
        except _ProviderJobCancelled:
            self._record_unsuccessful(
                job_id=job_id,
                attempt_number=attempt_number,
                active=active,
                status=ProviderInvocationStatus.CANCELLED,
                error_code="provider_job_cancelled",
            )
            return self._finish_cancelled_job(job_id)
        except TimeoutError:
            self._record_unsuccessful(
                job_id=job_id,
                attempt_number=attempt_number,
                active=active,
                status=ProviderInvocationStatus.TIMED_OUT,
                error_code="provider_execution_timeout",
            )
            with self.database.session() as session:
                return JobService(session).time_out(
                    job_id,
                    worker_id=self.worker_id,
                    error_message="Provider analysis Job exceeded its timeout",
                    now=_aware(None),
                )
        except Exception as exc:
            error_code = self._error_code(exc)
            self._record_unsuccessful(
                job_id=job_id,
                attempt_number=attempt_number,
                active=active,
                status=ProviderInvocationStatus.FAILED,
                error_code=error_code,
            )
            with self.database.session() as session:
                current = JobService(session).get(job_id)
                if current.cancel_requested_at is not None:
                    return self._finish_cancelled_job(job_id)
                return JobService(session).fail(
                    job_id,
                    worker_id=self.worker_id,
                    error_code=error_code,
                    error_message="Provider analysis Job failed closed",
                    now=_aware(None),
                )
        finally:
            heartbeat_stop.set()
            with suppress(JobTransitionError):
                await heartbeat_task

    async def _heartbeat_loop(
        self,
        job_id: str,
        stop: asyncio.Event,
        *,
        lease_seconds: int,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.heartbeat_interval_seconds,
                )
                return
            except TimeoutError:
                with self.database.session() as session:
                    JobService(session).heartbeat(
                        job_id,
                        worker_id=self.worker_id,
                        lease_seconds=lease_seconds,
                    )

    def _append_invocation(
        self,
        *,
        job_id: str,
        attempt_number: int,
        active: _ActiveInvocation,
        status: ProviderInvocationStatus,
        output_hash: str | None,
        usage: ProviderUsage,
        error_code: str | None,
    ) -> None:
        with self.database.session() as session:
            ProviderInvocationService(session).append(
                job_id=job_id,
                attempt_number=attempt_number,
                stage=active.stage,
                status=status,
                request_id=active.request_id,
                correlation_id=active.recorder.correlation_id,
                input_hash=active.input_hash,
                output_hash=output_hash,
                provider=self.provider.identity,
                prompt_version=active.prompt_version,
                policy_version=active.policy_version,
                output_schema_version=active.output_schema_version,
                usage=usage,
                error_code=error_code,
                started_at=active.started_at,
                completed_at=_aware(None),
            )

    def _record_unsuccessful(
        self,
        *,
        job_id: str,
        attempt_number: int,
        active: _ActiveInvocation | None,
        status: ProviderInvocationStatus,
        error_code: str,
    ) -> None:
        if active is None or active.recorded:
            return
        self._append_invocation(
            job_id=job_id,
            attempt_number=attempt_number,
            active=active,
            status=status,
            output_hash=None,
            usage=active.recorder.observed_usage,
            error_code=error_code,
        )
        active.recorded = True

    def _update_progress(self, job_id: str, *, current: int) -> None:
        with self.database.session() as session:
            JobService(session).update_progress(
                job_id,
                worker_id=self.worker_id,
                current=current,
                total=2,
                message=f"provider_{'inspect' if current == 1 else 'analyze'}_completed",
            )

    def _cancel_requested(self, job_id: str) -> bool:
        with self.database.session() as session:
            return JobService(session).get(job_id).cancel_requested_at is not None

    def _finish_cancelled_job(self, job_id: str) -> Job:
        """Finalize cancellation safely when recovery races the active worker."""
        with self.database.session() as session:
            service = JobService(session)
            current = service.get(job_id)
            if current.state == "cancelled":
                return current
            try:
                return service.cancel(
                    job_id,
                    worker_id=self.worker_id,
                    now=_aware(None),
                )
            except JobTransitionError:
                current = service.get(job_id)
                if current.state == "cancelled":
                    return current
                raise

    def _restore_budget_ledger(
        self,
        ledger: AnalysisBudgetLedger,
        *,
        job_id: str,
        attempt_number: int,
        candidate_id: str,
    ) -> None:
        with self.database.session() as session:
            previous = list(
                session.scalars(
                    select(ProviderInvocation)
                    .where(
                        ProviderInvocation.job_id == job_id,
                        ProviderInvocation.attempt_number < attempt_number,
                    )
                    .order_by(
                        ProviderInvocation.attempt_number,
                        ProviderInvocation.completed_at,
                        ProviderInvocation.id,
                    )
                )
            )
        for invocation in previous:
            ledger.start_invocation(
                invocation_id=invocation.request_id,
                candidate_id=candidate_id,
            )
            ledger.record_usage(
                invocation_id=invocation.request_id,
                usage=ProviderUsage(
                    input_tokens=invocation.input_tokens,
                    cached_input_tokens=invocation.cached_input_tokens,
                    output_tokens=invocation.output_tokens,
                    estimated_cost_microusd=(
                        invocation.estimated_cost_microusd
                    ),
                    duration_ms=invocation.duration_ms,
                ),
            )
        if previous:
            retry_invocation_id = previous[-1].request_id
            for retry_number in range(1, attempt_number):
                ledger.record_retry(
                    retry_id=f"{job_id}:retry:{retry_number}",
                    invocation_id=retry_invocation_id,
                )

    @staticmethod
    def _validate_inspection(
        result: InspectionResult,
        *,
        request: InspectRequest,
        provider: ProviderIdentity,
        recorder: _ProviderLifecycleRecorder,
    ) -> None:
        if (
            result.request_id != request.request_id
            or result.input_hash != request.input_hash
            or result.provider != provider
        ):
            raise ProviderContractError(
                "Provider inspection result does not match the queued request"
            )
        recorder.require_completed(
            output_hash=result.output_hash,
            usage=result.usage,
        )
        _require_size(
            result.structured_output,
            maximum=MAX_PROVIDER_STAGE_OUTPUT_BYTES,
            name="provider inspection output",
        )

    @staticmethod
    def _validate_analysis(
        result: AnalysisResult,
        *,
        request: AnalyzeRequest,
        provider: ProviderIdentity,
        recorder: _ProviderLifecycleRecorder,
    ) -> None:
        if (
            result.request_id != request.request_id
            or result.input_hash != request.input_hash
            or result.provider != provider
        ):
            raise ProviderContractError(
                "Provider analysis result does not match the queued request"
            )
        recorder.require_completed(
            output_hash=result.output_hash,
            usage=result.usage,
        )
        _require_size(
            result.structured_output,
            maximum=MAX_PROVIDER_STAGE_OUTPUT_BYTES,
            name="provider analysis output",
        )

    @staticmethod
    def _result_data(
        *,
        spec: ProviderAnalysisJobSpec,
        inspection: InspectionResult,
        analysis: AnalysisResult,
        ledger: AnalysisBudgetLedger,
        attempt_number: int,
    ) -> dict[str, object]:
        usage = _sum_usage(inspection.usage, analysis.usage)
        snapshot = ledger.snapshot
        return {
            "provider_run": {
                "snapshot_id": spec.snapshot_id,
                "score_version_id": spec.score_version_id,
                "correlation_id": spec.correlation_id,
                "frozen_input_hash": spec.frozen_input_hash,
                "provider": spec.expected_provider.hash_payload(),
                "versions": {
                    "inspect": {
                        "prompt": spec.inspect_prompt_version,
                        "policy": spec.inspect_policy_version,
                        "output_schema": spec.inspect_output_schema_version,
                    },
                    "analyze": {
                        "prompt": spec.analyze_prompt_version,
                        "policy": spec.analyze_policy_version,
                        "output_schema": spec.analyze_output_schema_version,
                    },
                },
                "usage": usage.hash_payload(),
                "budget": {
                    "candidates_considered": snapshot.candidates_considered,
                    "model_invocations": snapshot.model_invocations,
                    "retries": snapshot.retries,
                    "job_attempt_number": attempt_number,
                    "input_tokens": snapshot.input_tokens,
                    "cached_input_tokens": snapshot.cached_input_tokens,
                    "output_tokens": snapshot.output_tokens,
                    "estimated_cost_microusd": (
                        snapshot.estimated_cost_microusd
                    ),
                    "duration_ms": snapshot.duration_ms,
                    "exhausted_reason": (
                        snapshot.exhausted_reason.value
                        if snapshot.exhausted_reason
                        else None
                    ),
                },
            },
            "inspection": _serialize_result(inspection),
            "analysis": _serialize_result(analysis),
        }

    @staticmethod
    def _error_code(error: Exception) -> str:
        if isinstance(error, BudgetExceededError):
            return f"analysis_budget_{error.reason.value}"[:80]
        if isinstance(error, ProviderRunError):
            return error.code
        if isinstance(error, (ProviderContractError, ValueError, TypeError)):
            return "provider_contract_failure"
        return "provider_runtime_failure"


def _serialize_result(
    result: InspectionResult | AnalysisResult,
) -> dict[str, object]:
    return {
        "request_id": result.request_id,
        "input_hash": result.input_hash,
        "output_hash": result.output_hash,
        "cited_evidence_ids": list(result.cited_evidence_ids),
        "structured_output": json.loads(canonical_json(result.structured_output)),
        "usage": result.usage.hash_payload(),
    }


def _sum_usage(*values: ProviderUsage) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=sum(value.input_tokens for value in values),
        cached_input_tokens=sum(value.cached_input_tokens for value in values),
        output_tokens=sum(value.output_tokens for value in values),
        estimated_cost_microusd=sum(
            value.estimated_cost_microusd for value in values
        ),
        duration_ms=sum(value.duration_ms for value in values),
    )


def _safe_text(value: str, name: str, *, maximum: int) -> None:
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain between 1 and {maximum} characters")
    if contains_sensitive_text(value):
        raise ValueError(f"{name} must not contain credential-like data")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{name} must be an object with string keys")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{name} has missing or unknown fields")


def _string(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{name} must be a string")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _version_mapping(value: Any, stage: str) -> dict[str, str]:
    item = _mapping(value, f"{stage} versions")
    _exact_keys(
        item,
        {"prompt", "policy", "output_schema"},
        f"{stage} versions",
    )
    return {
        key: _string(item[key], f"{stage} {key} version")
        for key in ("prompt", "policy", "output_schema")
    }


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def _require_size(value: Any, *, maximum: int, name: str) -> None:
    try:
        size = len(canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not canonical JSON") from exc
    if size > maximum:
        raise ProviderContractError(
            f"{name} exceeds the {maximum}-byte safety limit"
        )
