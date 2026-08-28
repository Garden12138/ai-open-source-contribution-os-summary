from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TypeAlias
from uuid import uuid4

from app.provenance import canonical_json
from app.providers.analysis_schema import (
    ANALYSIS_SCHEMA_VERSION,
    validate_structured_analysis,
)
from app.providers.contracts import (
    AnalysisResult,
    AnalyzeRequest,
    EventSink,
    InspectRequest,
    InspectionResult,
    JSONValue,
    ProviderCompletedEvent,
    ProviderContractError,
    ProviderFailedEvent,
    ProviderIdentity,
    ProviderLifecycleEvent,
    ProviderProgressEvent,
    ProviderRunError,
    ProviderStage,
    ProviderStartedEvent,
    ProviderUsage,
    ProviderUsageEvent,
)


Clock: TypeAlias = Callable[[], datetime]
IdFactory: TypeAlias = Callable[[], str]


@dataclass(frozen=True, slots=True)
class FakeFailure:
    stage: ProviderStage
    code: str = "fake_provider_failure"
    safe_message: str = "Fake provider execution failed"
    retryable: bool = True
    times: int = 1

    def __post_init__(self) -> None:
        if self.times < 1:
            raise ProviderContractError("fake failure times must be at least 1")
        # Reuse the public failure validation for safe fields and limits.
        ProviderRunError(
            self.code,
            self.safe_message,
            retryable=self.retryable,
        )


@dataclass(frozen=True, slots=True)
class FakeProviderScript:
    inspect_output: Mapping[str, JSONValue] | None = None
    inspect_citations: tuple[str, ...] | None = None
    analyze_output: Mapping[str, JSONValue] | None = None
    analyze_citations: tuple[str, ...] | None = None
    inspect_usage: ProviderUsage = field(
        default_factory=lambda: ProviderUsage(
            input_tokens=100,
            cached_input_tokens=20,
            output_tokens=30,
            estimated_cost_microusd=10,
            duration_ms=100,
        )
    )
    analyze_usage: ProviderUsage = field(
        default_factory=lambda: ProviderUsage(
            input_tokens=200,
            cached_input_tokens=40,
            output_tokens=60,
            estimated_cost_microusd=20,
            duration_ms=200,
        )
    )
    failure: FakeFailure | None = None

    def __post_init__(self) -> None:
        if self.inspect_output is not None:
            canonical_json(self.inspect_output)
        if self.analyze_output is not None:
            canonical_json(self.analyze_output)
        for citations, name in (
            (self.inspect_citations, "fake inspect citations"),
            (self.analyze_citations, "fake analyze citations"),
        ):
            if citations is not None and len(citations) != len(set(citations)):
                raise ProviderContractError(f"{name} must not contain duplicates")


class CollectingEventSink:
    def __init__(self) -> None:
        self.events: list[ProviderLifecycleEvent] = []

    async def __call__(self, event: ProviderLifecycleEvent) -> None:
        self.events.append(event)


class FakeProvider:
    """Deterministic provider implementation for offline contract tests."""

    def __init__(
        self,
        script: FakeProviderScript | None = None,
        *,
        identity: ProviderIdentity | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self.script = script or FakeProviderScript()
        self._identity = identity or ProviderIdentity(
            provider="fake",
            adapter_version="fake-adapter-v1",
            model="fake-analysis-model",
            model_version="fake-model-v1",
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._inspect_requests: list[InspectRequest] = []
        self._analyze_requests: list[AnalyzeRequest] = []
        self._failure_count: dict[ProviderStage, int] = {
            ProviderStage.INSPECT: 0,
            ProviderStage.ANALYZE: 0,
        }

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    @property
    def inspect_requests(self) -> tuple[InspectRequest, ...]:
        return tuple(self._inspect_requests)

    @property
    def analyze_requests(self) -> tuple[AnalyzeRequest, ...]:
        return tuple(self._analyze_requests)

    async def inspect(
        self,
        request: InspectRequest,
        emit: EventSink,
    ) -> InspectionResult:
        self._inspect_requests.append(request)
        await self._started(
            emit,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            stage=ProviderStage.INSPECT,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            policy_version=request.policy_version,
            output_schema_version=request.output_schema_version,
        )
        await self._fail_if_scripted(
            emit,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            stage=ProviderStage.INSPECT,
        )

        all_evidence_ids = tuple(item.evidence_id for item in request.evidence)
        citations = (
            all_evidence_ids
            if self.script.inspect_citations is None
            else tuple(self.script.inspect_citations)
        )
        self._require_known_citations(
            citations,
            allowed=all_evidence_ids,
            stage=ProviderStage.INSPECT,
        )
        output = (
            {
                "observations": [
                    {
                        "code": "fake.inspect.observation",
                        "summary": "Deterministic inspection fixture",
                        "evidence_ids": list(citations),
                    }
                ]
            }
            if self.script.inspect_output is None
            else self._copy_output(self.script.inspect_output)
        )
        result = InspectionResult.create(
            request=request,
            provider=self.identity,
            structured_output=output,
            cited_evidence_ids=citations,
            usage=self.script.inspect_usage,
        )
        await self._succeed(
            emit,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            stage=ProviderStage.INSPECT,
            output_hash=result.output_hash,
            usage=result.usage,
        )
        return result

    async def analyze(
        self,
        request: AnalyzeRequest,
        emit: EventSink,
    ) -> AnalysisResult:
        if request.output_schema_version != ANALYSIS_SCHEMA_VERSION:
            raise ProviderContractError(
                "Unsupported FakeProvider analysis output schema version"
            )
        self._analyze_requests.append(request)
        await self._started(
            emit,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            stage=ProviderStage.ANALYZE,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            policy_version=request.policy_version,
            output_schema_version=request.output_schema_version,
        )
        await self._fail_if_scripted(
            emit,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            stage=ProviderStage.ANALYZE,
        )

        allowed_citations = request.inspection.cited_evidence_ids
        citations = (
            allowed_citations
            if self.script.analyze_citations is None
            else tuple(self.script.analyze_citations)
        )
        self._require_known_citations(
            citations,
            allowed=allowed_citations,
            stage=ProviderStage.ANALYZE,
        )
        output = (
            {
                "problem_summary": "Deterministic fake analysis",
                "current_behavior": (
                    "The frozen Issue describes behavior requiring a change."
                ),
                "expected_behavior": (
                    "The requested behavior is implemented and verified."
                ),
                "acceptance_criteria": [
                    "The requested behavior passes deterministic tests."
                ],
                "missing_information": [],
                "similar_issue_pr_evidence": [],
                "competition": {
                    "level": "unknown",
                    "summary": "No additional competition evidence was frozen.",
                    "signals": [],
                },
                "estimated_effort": {
                    "size": "unknown",
                    "hours_min": None,
                    "hours_max": None,
                    "rationale": (
                        "The deterministic fake does not estimate real effort."
                    ),
                },
                "bounty_basis": {
                    "has_bounty": False,
                    "amount_usd": None,
                    "basis": "The fake Provider makes no bounty claim.",
                },
                "risks": [],
                "confidence": 1.0,
                "recommendation": "consider",
                "recommendation_summary": (
                    "建议先确认任务范围，再决定是否投入。"
                ),
                "fit_reasons": [
                    "任务与当前冻结的 Issue 和仓库证据一致。"
                ],
                "next_steps": [
                    "阅读贡献指南并向维护者确认任务仍可接手。"
                ],
                "maintainer_questions": [
                    "当前是否仍欢迎新的贡献者处理这个 Issue？"
                ],
                "cited_evidence_ids": list(citations),
                "citation_map": {
                    "problem_summary": list(citations),
                    "current_behavior": list(citations),
                    "expected_behavior": list(citations),
                    "acceptance_criteria": [list(citations)],
                    "missing_information": [],
                    "similar_issue_pr_evidence": [],
                    "competition": list(citations),
                    "estimated_effort": list(citations),
                    "bounty_basis": list(citations),
                    "risks": [],
                    "confidence": list(citations),
                    "recommendation": list(citations),
                    "recommendation_summary": list(citations),
                    "fit_reasons": [list(citations)],
                    "next_steps": [list(citations)],
                    "maintainer_questions": [list(citations)],
                },
            }
            if self.script.analyze_output is None
            else self._copy_output(self.script.analyze_output)
        )
        validate_structured_analysis(
            output,
            allowed_evidence_ids=allowed_citations,
        )
        result = AnalysisResult.create(
            request=request,
            provider=self.identity,
            structured_output=output,
            cited_evidence_ids=citations,
            usage=self.script.analyze_usage,
        )
        await self._succeed(
            emit,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            stage=ProviderStage.ANALYZE,
            output_hash=result.output_hash,
            usage=result.usage,
        )
        return result

    async def _started(
        self,
        emit: EventSink,
        *,
        request_id: str,
        correlation_id: str,
        stage: ProviderStage,
        input_hash: str,
        prompt_version: str,
        policy_version: str,
        output_schema_version: str,
    ) -> None:
        await emit(
            ProviderStartedEvent(
                event_id=self._id_factory(),
                request_id=request_id,
                correlation_id=correlation_id,
                sequence=1,
                occurred_at=self._clock(),
                stage=stage,
                provider=self.identity,
                input_hash=input_hash,
                prompt_version=prompt_version,
                policy_version=policy_version,
                output_schema_version=output_schema_version,
            )
        )

    async def _succeed(
        self,
        emit: EventSink,
        *,
        request_id: str,
        correlation_id: str,
        stage: ProviderStage,
        output_hash: str,
        usage: ProviderUsage,
    ) -> None:
        common = {
            "request_id": request_id,
            "correlation_id": correlation_id,
            "occurred_at": self._clock(),
            "stage": stage,
            "provider": self.identity,
        }
        await emit(
            ProviderProgressEvent(
                event_id=self._id_factory(),
                sequence=2,
                message_code=f"provider.fake.{stage.value}.completed",
                completed_units=1,
                total_units=1,
                **common,
            )
        )
        await emit(
            ProviderUsageEvent(
                event_id=self._id_factory(),
                sequence=3,
                usage=usage,
                **common,
            )
        )
        await emit(
            ProviderCompletedEvent(
                event_id=self._id_factory(),
                sequence=4,
                output_hash=output_hash,
                usage=usage,
                **common,
            )
        )

    async def _fail_if_scripted(
        self,
        emit: EventSink,
        *,
        request_id: str,
        correlation_id: str,
        stage: ProviderStage,
    ) -> None:
        failure = self.script.failure
        if (
            failure is None
            or failure.stage != stage
            or self._failure_count[stage] >= failure.times
        ):
            return
        self._failure_count[stage] += 1
        await emit(
            ProviderFailedEvent(
                event_id=self._id_factory(),
                request_id=request_id,
                correlation_id=correlation_id,
                sequence=2,
                occurred_at=self._clock(),
                stage=stage,
                provider=self.identity,
                error_code=failure.code,
                safe_message=failure.safe_message,
                retryable=failure.retryable,
            )
        )
        raise ProviderRunError(
            failure.code,
            failure.safe_message,
            retryable=failure.retryable,
        )

    @staticmethod
    def _copy_output(
        value: Mapping[str, JSONValue],
    ) -> dict[str, JSONValue]:
        copied = json.loads(canonical_json(value))
        if not isinstance(copied, dict):
            raise ProviderContractError("fake provider output must be an object")
        return copied

    @staticmethod
    def _require_known_citations(
        citations: tuple[str, ...],
        *,
        allowed: tuple[str, ...],
        stage: ProviderStage,
    ) -> None:
        unknown = sorted(set(citations) - set(allowed))
        if unknown:
            raise ProviderContractError(
                f"fake {stage.value} citations reference unknown evidence: "
                f"{', '.join(unknown)}"
            )
