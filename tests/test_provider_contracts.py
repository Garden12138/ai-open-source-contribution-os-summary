from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.providers import (
    AnalysisProvider,
    AnalysisResult,
    AnalyzeRequest,
    Analyzer,
    FrozenEvidence,
    InspectRequest,
    InspectionResult,
    Inspector,
    ProviderCompletedEvent,
    ProviderContractError,
    ProviderFailedEvent,
    ProviderIdentity,
    ProviderProgressEvent,
    ProviderRunError,
    ProviderStage,
    ProviderStartedEvent,
    ProviderUsage,
    ProviderUsageEvent,
)


NOW = datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc)
IDENTITY = ProviderIdentity(
    provider="contract-fixture",
    adapter_version="adapter-v1",
    model="fixture-model",
    model_version="model-v1",
)


def evidence() -> tuple[FrozenEvidence, ...]:
    return (
        FrozenEvidence.capture(
            evidence_id="issue",
            kind="github_issue",
            source_uri="github://fixture/repository/issues/7",
            content="Expected deterministic provider contracts.",
        ),
        FrozenEvidence.capture(
            evidence_id="repository",
            kind="github_repository",
            source_uri="github://fixture/repository",
            content="Python repository with tests.",
        ),
    )


def inspect_request(*, request_id: str = "inspect-1") -> InspectRequest:
    return InspectRequest.create(
        request_id=request_id,
        correlation_id="analysis-1",
        snapshot_id="snapshot-1",
        score_version_id="score-1",
        evidence=evidence(),
        prompt_version="inspect-prompt-v1",
        policy_version="analysis-policy-v1",
        output_schema_version="inspection-schema-v1",
    )


def inspection_result(
    request: InspectRequest,
    *,
    output: dict[str, Any] | None = None,
) -> InspectionResult:
    return InspectionResult.create(
        request=request,
        provider=IDENTITY,
        structured_output=output
        or {
            "observations": [
                {
                    "summary": "The Issue requests a deterministic contract.",
                    "evidence_ids": ["issue"],
                }
            ]
        },
        cited_evidence_ids=("issue",),
        usage=ProviderUsage(
            input_tokens=100,
            cached_input_tokens=20,
            output_tokens=30,
            estimated_cost_microusd=15,
            duration_ms=250,
        ),
    )


def analyze_request(
    inspection: InspectionResult,
    *,
    request_id: str = "analyze-1",
) -> AnalyzeRequest:
    return AnalyzeRequest.create(
        request_id=request_id,
        correlation_id="analysis-1",
        snapshot_id="snapshot-1",
        score_version_id="score-1",
        inspection=inspection,
        prompt_version="analyze-prompt-v1",
        policy_version="analysis-policy-v1",
        output_schema_version="analysis-schema-v1",
    )


def test_provider_inputs_and_outputs_are_hash_bound_and_replay_stable() -> None:
    first_inspect = inspect_request(request_id="inspect-first")
    replay_inspect = inspect_request(request_id="inspect-replay")

    assert first_inspect.input_hash == replay_inspect.input_hash

    mutable_output: dict[str, Any] = {
        "observations": [{"summary": "Stable", "evidence_ids": ["issue"]}]
    }
    first_result = inspection_result(first_inspect, output=mutable_output)
    replay_result = inspection_result(replay_inspect, output=mutable_output)
    mutable_output["observations"][0]["summary"] = "mutated after creation"

    assert first_result.output_hash == replay_result.output_hash
    assert first_result.structured_output["observations"][0]["summary"] == "Stable"
    with pytest.raises(TypeError):
        first_result.structured_output["new"] = "not mutable"  # type: ignore[index]

    first_analyze = analyze_request(first_result, request_id="analyze-first")
    replay_analyze = analyze_request(replay_result, request_id="analyze-replay")
    assert first_analyze.input_hash == replay_analyze.input_hash

    first_analysis = AnalysisResult.create(
        request=first_analyze,
        provider=IDENTITY,
        structured_output={
            "problem_summary": "Define a provider-neutral contract.",
            "confidence": 0.9,
        },
        cited_evidence_ids=("issue", "repository"),
        usage=ProviderUsage(input_tokens=200, output_tokens=50),
    )
    replay_analysis = AnalysisResult.create(
        request=replay_analyze,
        provider=IDENTITY,
        structured_output={
            "confidence": 0.9,
            "problem_summary": "Define a provider-neutral contract.",
        },
        cited_evidence_ids=("issue", "repository"),
        usage=ProviderUsage(input_tokens=999, output_tokens=999),
    )

    assert first_analysis.output_hash == replay_analysis.output_hash


def test_provider_contract_rejects_stale_hashes_and_invalid_usage() -> None:
    request = inspect_request()
    result = inspection_result(request)

    with pytest.raises(ProviderContractError, match="evidence content hash"):
        replace(evidence()[0], content="changed after freezing")
    with pytest.raises(ProviderContractError, match="inspect input hash"):
        replace(request, input_hash="0" * 64)
    with pytest.raises(ProviderContractError, match="inspection output hash"):
        replace(result, output_hash="0" * 64)
    with pytest.raises(ProviderContractError, match="duplicates"):
        replace(result, cited_evidence_ids=("issue", "issue"))
    with pytest.raises(ProviderContractError, match="cached_input_tokens"):
        ProviderUsage(input_tokens=1, cached_input_tokens=2)


def test_provider_lifecycle_events_are_typed_and_sanitized() -> None:
    request = inspect_request()
    usage = ProviderUsage(input_tokens=10, output_tokens=5, duration_ms=100)
    common = {
        "request_id": request.request_id,
        "correlation_id": request.correlation_id,
        "occurred_at": NOW.astimezone(timezone(timedelta(hours=8))),
        "stage": ProviderStage.INSPECT,
        "provider": IDENTITY,
    }
    events = (
        ProviderStartedEvent(
            event_id="event-1",
            sequence=1,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            policy_version=request.policy_version,
            output_schema_version=request.output_schema_version,
            **common,
        ),
        ProviderProgressEvent(
            event_id="event-2",
            sequence=2,
            message_code="provider.inspecting",
            completed_units=1,
            total_units=2,
            **common,
        ),
        ProviderUsageEvent(
            event_id="event-3",
            sequence=3,
            usage=usage,
            **common,
        ),
        ProviderCompletedEvent(
            event_id="event-4",
            sequence=4,
            output_hash="1" * 64,
            usage=usage,
            **common,
        ),
        ProviderFailedEvent(
            event_id="event-5",
            sequence=5,
            error_code="fixture_failure",
            safe_message="Provider execution failed",
            retryable=True,
            **common,
        ),
    )

    assert [event.kind for event in events] == [
        "started",
        "progress",
        "usage",
        "completed",
        "failed",
    ]
    assert all(event.occurred_at.tzinfo == timezone.utc for event in events)

    with pytest.raises(ProviderContractError, match="sequence"):
        replace(events[0], sequence=0)
    with pytest.raises(ProviderContractError, match="timezone-aware"):
        replace(events[0], occurred_at=NOW.replace(tzinfo=None))

    error = ProviderRunError(
        "fixture_failure",
        "Provider execution failed",
        retryable=True,
    )
    assert error.code == "fixture_failure"
    assert error.safe_message == str(error)
    assert error.retryable is True


def test_inspect_and_analyze_protocols_are_provider_neutral() -> None:
    class ContractProvider:
        identity = IDENTITY

        async def inspect(self, request, emit):  # type: ignore[no-untyped-def]
            return inspection_result(request)

        async def analyze(self, request, emit):  # type: ignore[no-untyped-def]
            return AnalysisResult.create(
                request=request,
                provider=self.identity,
                structured_output={"problem_summary": "Fixture"},
                cited_evidence_ids=("issue",),
                usage=ProviderUsage(),
            )

    provider = ContractProvider()

    assert isinstance(provider, Inspector)
    assert isinstance(provider, Analyzer)
    assert isinstance(provider, AnalysisProvider)
