from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from itertools import count

import pytest

from app.providers import (
    ANALYSIS_SCHEMA_VERSION,
    AnalysisProvider,
    AnalyzeRequest,
    CollectingEventSink,
    FakeFailure,
    FakeProvider,
    FakeProviderScript,
    FrozenEvidence,
    InspectRequest,
    ProviderContractError,
    ProviderRunError,
    ProviderStage,
)


NOW = datetime(2026, 7, 30, 7, 0, tzinfo=timezone.utc)


def inspect_request(request_id: str) -> InspectRequest:
    return InspectRequest.create(
        request_id=request_id,
        correlation_id="analysis-corpus-1",
        snapshot_id="snapshot-corpus-1",
        score_version_id="score-corpus-1",
        evidence=(
            FrozenEvidence.capture(
                evidence_id="issue",
                kind="github_issue",
                source_uri="github://fixture/repository/issues/1",
                content="Add a deterministic provider fake.",
            ),
            FrozenEvidence.capture(
                evidence_id="repository",
                kind="github_repository",
                source_uri="github://fixture/repository",
                content="A Python repository with an offline test suite.",
            ),
        ),
        prompt_version="inspect-prompt-v1",
        policy_version="analysis-policy-v1",
        output_schema_version="inspection-schema-v1",
    )


def analyze_request(
    request_id: str,
    inspection,
) -> AnalyzeRequest:  # type: ignore[no-untyped-def]
    return AnalyzeRequest.create(
        request_id=request_id,
        correlation_id="analysis-corpus-1",
        snapshot_id="snapshot-corpus-1",
        score_version_id="score-corpus-1",
        inspection=inspection,
        prompt_version="analyze-prompt-v1",
        policy_version="analysis-policy-v1",
        output_schema_version=ANALYSIS_SCHEMA_VERSION,
    )


def deterministic_provider(
    script: FakeProviderScript | None = None,
) -> FakeProvider:
    event_ids = count(1)
    return FakeProvider(
        script,
        clock=lambda: NOW,
        id_factory=lambda: f"fake-event-{next(event_ids)}",
    )


def test_fake_provider_is_deterministic_across_contract_replays() -> None:
    provider = deterministic_provider()

    async def run():
        first_inspect_sink = CollectingEventSink()
        replay_inspect_sink = CollectingEventSink()
        first_inspection = await provider.inspect(
            inspect_request("inspect-first"),
            first_inspect_sink,
        )
        replay_inspection = await provider.inspect(
            inspect_request("inspect-replay"),
            replay_inspect_sink,
        )

        first_analyze_sink = CollectingEventSink()
        replay_analyze_sink = CollectingEventSink()
        first_analysis = await provider.analyze(
            analyze_request("analyze-first", first_inspection),
            first_analyze_sink,
        )
        replay_analysis = await provider.analyze(
            analyze_request("analyze-replay", replay_inspection),
            replay_analyze_sink,
        )
        return (
            first_inspection,
            replay_inspection,
            first_analysis,
            replay_analysis,
            first_inspect_sink,
            replay_inspect_sink,
            first_analyze_sink,
            replay_analyze_sink,
        )

    (
        first_inspection,
        replay_inspection,
        first_analysis,
        replay_analysis,
        *sinks,
    ) = asyncio.run(run())

    assert isinstance(provider, AnalysisProvider)
    assert first_inspection.input_hash == replay_inspection.input_hash
    assert first_inspection.output_hash == replay_inspection.output_hash
    assert first_analysis.input_hash == replay_analysis.input_hash
    assert first_analysis.output_hash == replay_analysis.output_hash
    assert len(provider.inspect_requests) == 2
    assert len(provider.analyze_requests) == 2

    for sink, expected_stage in zip(
        sinks,
        (
            ProviderStage.INSPECT,
            ProviderStage.INSPECT,
            ProviderStage.ANALYZE,
            ProviderStage.ANALYZE,
        ),
        strict=True,
    ):
        assert [event.kind for event in sink.events] == [
            "started",
            "progress",
            "usage",
            "completed",
        ]
        assert [event.sequence for event in sink.events] == [1, 2, 3, 4]
        assert all(event.stage == expected_stage for event in sink.events)
        assert sink.events[-1].output_hash in {
            first_inspection.output_hash,
            first_analysis.output_hash,
        }


def test_fake_provider_emits_safe_failure_then_replays_successfully() -> None:
    provider = deterministic_provider(
        FakeProviderScript(
            failure=FakeFailure(
                stage=ProviderStage.INSPECT,
                code="injected_transient_failure",
                safe_message="Injected offline provider failure",
                retryable=True,
                times=1,
            )
        )
    )

    async def run():
        failed_sink = CollectingEventSink()
        with pytest.raises(ProviderRunError) as captured:
            await provider.inspect(
                inspect_request("inspect-failed"),
                failed_sink,
            )
        replay_sink = CollectingEventSink()
        replay_result = await provider.inspect(
            inspect_request("inspect-retry"),
            replay_sink,
        )
        return captured.value, failed_sink, replay_sink, replay_result

    error, failed_sink, replay_sink, replay_result = asyncio.run(run())

    assert error.code == "injected_transient_failure"
    assert error.retryable is True
    assert str(error) == "Injected offline provider failure"
    assert [event.kind for event in failed_sink.events] == ["started", "failed"]
    assert failed_sink.events[-1].safe_message == str(error)
    assert [event.kind for event in replay_sink.events] == [
        "started",
        "progress",
        "usage",
        "completed",
    ]
    assert replay_result.output_hash == replay_sink.events[-1].output_hash


def test_fake_provider_fails_closed_on_unknown_citations_and_secret_errors() -> None:
    provider = deterministic_provider(
        FakeProviderScript(inspect_citations=("not-frozen",))
    )

    async def run() -> None:
        with pytest.raises(ProviderContractError, match="unknown evidence"):
            await provider.inspect(
                inspect_request("inspect-unknown-citation"),
                CollectingEventSink(),
            )

    asyncio.run(run())

    with pytest.raises(ProviderContractError, match="credential-like"):
        FakeFailure(
            stage=ProviderStage.ANALYZE,
            safe_message="Bearer github_pat_SECRET_CANARY_123456",
        )
