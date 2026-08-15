from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs import JobConflictError, JobService, JobTransitionError
from app.models import Job, OpportunitySnapshot, ProviderInvocation
from app.provenance import content_hash
from app.providers.analysis_schema import ANALYSIS_SCHEMA_VERSION
from app.providers.budgets import AnalysisBudget
from app.providers.codex_cli import (
    ANALYSIS_POLICY_VERSION,
    ANALYZE_PROMPT_VERSION,
    INSPECTION_SCHEMA_VERSION,
    INSPECT_PROMPT_VERSION,
)
from app.providers.contracts import ProviderContractError, ProviderIdentity
from app.providers.evidence import (
    MAX_ANALYSIS_CANDIDATES,
    AnalysisInputError,
    AnalysisInputFreezer,
    FrozenCandidateInput,
)
from app.providers.jobs import (
    PROVIDER_ANALYSIS_JOB_KIND,
    ProviderAnalysisJobSpec,
    enqueue_provider_analysis,
)


DEFAULT_INSPECT_PROMPT_VERSION = INSPECT_PROMPT_VERSION
DEFAULT_ANALYZE_PROMPT_VERSION = ANALYZE_PROMPT_VERSION
DEFAULT_ANALYSIS_POLICY_VERSION = ANALYSIS_POLICY_VERSION


class ManualAnalysisError(RuntimeError):
    pass


class ManualAnalysisNotFoundError(ManualAnalysisError):
    pass


class ManualAnalysisConflictError(ManualAnalysisError):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisRunVersions:
    inspect_prompt: str = DEFAULT_INSPECT_PROMPT_VERSION
    inspect_policy: str = DEFAULT_ANALYSIS_POLICY_VERSION
    inspect_output_schema: str = INSPECTION_SCHEMA_VERSION
    analyze_prompt: str = DEFAULT_ANALYZE_PROMPT_VERSION
    analyze_policy: str = DEFAULT_ANALYSIS_POLICY_VERSION
    analyze_output_schema: str = ANALYSIS_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ManualAnalysisRequest:
    snapshot_id: str
    correlation_id: str
    idempotency_key: str
    budget: AnalysisBudget
    expected_provider: ProviderIdentity
    versions: AnalysisRunVersions = AnalysisRunVersions()


@dataclass(frozen=True, slots=True)
class AnalysisRetryAllowance:
    job_id: str
    attempts_started: int
    attempts_remaining: int
    retries_remaining: int
    model_invocations_used: int
    model_invocations_remaining: int
    input_tokens_used: int
    output_tokens_used: int
    estimated_cost_microusd_used: int
    duration_ms_used: int


class ManualAnalysisService:
    """Queue explicit analysis and retry only within its frozen Job budget."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def request(
        self,
        request: ManualAnalysisRequest,
        *,
        now: datetime | None = None,
    ) -> tuple[Job, bool]:
        if request.budget.max_model_invocations < 2:
            raise ManualAnalysisConflictError(
                "Manual analysis requires budget for inspect and analyze"
            )
        frozen = self._frozen_input(request.snapshot_id)
        try:
            spec = frozen.to_provider_job_spec(
                correlation_id=request.correlation_id,
                budget=request.budget,
                expected_provider=request.expected_provider,
                inspect_prompt_version=request.versions.inspect_prompt,
                inspect_policy_version=request.versions.inspect_policy,
                inspect_output_schema_version=(
                    request.versions.inspect_output_schema
                ),
                analyze_prompt_version=request.versions.analyze_prompt,
                analyze_policy_version=request.versions.analyze_policy,
                analyze_output_schema_version=(
                    request.versions.analyze_output_schema
                ),
            )
            return enqueue_provider_analysis(
                service=JobService(self.session),
                spec=spec,
                idempotency_key=request.idempotency_key,
                now=now,
            )
        except (JobConflictError, ProviderContractError, ValueError) as exc:
            raise ManualAnalysisConflictError(str(exc)) from exc

    def retry_allowance(self, job_id: str) -> AnalysisRetryAllowance:
        job = self.session.get(Job, job_id)
        if job is None:
            raise ManualAnalysisNotFoundError(
                "Provider analysis Job was not found"
            )
        if job.kind != PROVIDER_ANALYSIS_JOB_KIND:
            raise ManualAnalysisConflictError(
                "Only Provider analysis Jobs can use analysis retry"
            )
        if content_hash(job.payload) != job.payload_hash:
            raise ManualAnalysisConflictError(
                "Provider analysis Job payload hash does not match"
            )
        try:
            spec = ProviderAnalysisJobSpec.from_payload(job.payload)
        except (ProviderContractError, TypeError, ValueError) as exc:
            raise ManualAnalysisConflictError(
                "Provider analysis Job payload is invalid"
            ) from exc
        if (
            job.max_attempts != spec.budget.max_retries + 1
            or job.timeout_seconds
            != max(1, (spec.budget.max_duration_ms + 999) // 1000)
        ):
            raise ManualAnalysisConflictError(
                "Provider analysis Job limits do not match its frozen budget"
            )
        invocations = tuple(
            self.session.scalars(
                select(ProviderInvocation).where(
                    ProviderInvocation.job_id == job.id
                )
            )
        )
        model_invocations_used = len(invocations)
        return AnalysisRetryAllowance(
            job_id=job.id,
            attempts_started=job.attempt_count,
            attempts_remaining=max(0, job.max_attempts - job.attempt_count),
            retries_remaining=max(
                0,
                spec.budget.max_retries - max(0, job.attempt_count - 1),
            ),
            model_invocations_used=model_invocations_used,
            model_invocations_remaining=max(
                0,
                spec.budget.max_model_invocations - model_invocations_used,
            ),
            input_tokens_used=sum(item.input_tokens for item in invocations),
            output_tokens_used=sum(item.output_tokens for item in invocations),
            estimated_cost_microusd_used=sum(
                item.estimated_cost_microusd for item in invocations
            ),
            duration_ms_used=sum(item.duration_ms for item in invocations),
        )

    def retry(
        self,
        job_id: str,
        *,
        run_after: datetime | None = None,
        now: datetime | None = None,
    ) -> Job:
        allowance = self.retry_allowance(job_id)
        job = self.session.get(Job, job_id)
        if job is None:
            raise ManualAnalysisNotFoundError(
                "Provider analysis Job was not found"
            )
        spec = ProviderAnalysisJobSpec.from_payload(job.payload)
        if job.attempt_count > 0 and allowance.retries_remaining == 0:
            raise ManualAnalysisConflictError(
                "Provider analysis retry budget is exhausted"
            )
        if allowance.attempts_remaining == 0:
            raise ManualAnalysisConflictError(
                "Provider analysis Job exhausted its attempts"
            )
        if (
            job.attempt_count > 0
            and allowance.model_invocations_remaining < 2
        ):
            raise ManualAnalysisConflictError(
                "Provider analysis retry lacks budget for inspect and analyze"
            )
        for used, maximum, name in (
            (
                allowance.input_tokens_used,
                spec.budget.max_input_tokens,
                "input Token",
            ),
            (
                allowance.output_tokens_used,
                spec.budget.max_output_tokens,
                "output Token",
            ),
            (
                allowance.estimated_cost_microusd_used,
                spec.budget.max_estimated_cost_microusd,
                "estimated cost",
            ),
            (
                allowance.duration_ms_used,
                spec.budget.max_duration_ms,
                "duration",
            ),
        ):
            if used > maximum:
                raise ManualAnalysisConflictError(
                    f"Provider analysis {name} budget is already exhausted"
                )
        try:
            return JobService(self.session).retry(
                job_id,
                run_after=run_after,
                now=now,
            )
        except JobTransitionError as exc:
            raise ManualAnalysisConflictError(str(exc)) from exc

    def _frozen_input(self, snapshot_id: str) -> FrozenCandidateInput:
        snapshot = self.session.get(OpportunitySnapshot, snapshot_id)
        if snapshot is None:
            raise ManualAnalysisNotFoundError(
                "OpportunitySnapshot was not found"
            )
        try:
            candidates = AnalysisInputFreezer(
                self.session
            ).freeze_top_candidates(
                scan_run_id=snapshot.scan_run_id,
                limit=MAX_ANALYSIS_CANDIDATES,
            )
        except AnalysisInputError as exc:
            raise ManualAnalysisConflictError(str(exc)) from exc
        frozen = next(
            (item for item in candidates if item.snapshot_id == snapshot_id),
            None,
        )
        if frozen is None:
            raise ManualAnalysisConflictError(
                "Manual analysis is limited to the frozen top-30 rule corpus"
            )
        return frozen
