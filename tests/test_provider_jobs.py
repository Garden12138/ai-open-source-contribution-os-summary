from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.jobs import JobService
from app.models import Job, ProviderInvocation
from app.providers import (
    AnalysisBudget,
    ANALYSIS_SCHEMA_VERSION,
    FakeFailure,
    FakeProvider,
    FakeProviderScript,
    FrozenEvidence,
    MAX_PROVIDER_JOB_PAYLOAD_BYTES,
    MAX_PROVIDER_STAGE_OUTPUT_BYTES,
    PROVIDER_ANALYSIS_JOB_KIND,
    ProviderAnalysisJobSpec,
    ProviderAnalysisJobWorker,
    ProviderContractError,
    ProviderIdentity,
    ProviderInvocationConflictError,
    ProviderInvocationService,
    ProviderInvocationStatus,
    ProviderStage,
    ProviderUsage,
    enqueue_provider_analysis,
)
from app.security import SensitiveDataError


NOW = datetime.now(timezone.utc) + timedelta(seconds=1)


@pytest.fixture(autouse=True)
def refresh_job_clock() -> None:
    """Keep synthetic lease timestamps fresh in long full-suite runs."""
    global NOW
    NOW = datetime.now(timezone.utc) + timedelta(seconds=1)


def _budget(**overrides: int) -> AnalysisBudget:
    values = {
        "max_candidates": 1,
        "max_model_invocations": 20,
        "max_input_tokens": 1_000,
        "max_output_tokens": 500,
        "max_estimated_cost_microusd": 1_000,
        "max_duration_ms": 10_000,
        "max_retries": 1,
    }
    values.update(overrides)
    return AnalysisBudget(**values)


def _spec(
    provider: FakeProvider,
    *,
    budget: AnalysisBudget | None = None,
    evidence_content: str = "Implement a durable provider Job with citations.",
) -> ProviderAnalysisJobSpec:
    return ProviderAnalysisJobSpec(
        correlation_id="analysis-job-corpus-1",
        snapshot_id="snapshot-job-1",
        score_version_id="score-job-1",
        evidence=(
            FrozenEvidence.capture(
                evidence_id="issue",
                kind="github_issue",
                source_uri="github://fixture/repository/issues/1",
                content=evidence_content,
            ),
            FrozenEvidence.capture(
                evidence_id="repository",
                kind="github_repository",
                source_uri="github://fixture/repository",
                content="A deterministic Python provider fixture.",
            ),
        ),
        inspect_prompt_version="inspect-prompt-v1",
        inspect_policy_version="analysis-policy-v1",
        inspect_output_schema_version="inspection-schema-v1",
        analyze_prompt_version="analyze-prompt-v1",
        analyze_policy_version="analysis-policy-v1",
        analyze_output_schema_version=ANALYSIS_SCHEMA_VERSION,
        budget=budget or _budget(),
        expected_provider=provider.identity,
    )


def _database(tmp_path: Path, name: str) -> Database:
    database = Database(f"sqlite+pysqlite:///{tmp_path / name}")
    database.create_schema()
    return database


def test_provider_pipeline_runs_through_a_durable_job_and_records_versions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-success.db"
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    provider = FakeProvider()
    try:
        with database.session() as session:
            job, created = enqueue_provider_analysis(
                service=JobService(session),
                spec=_spec(provider),
                idempotency_key="provider-success",
                now=NOW,
            )
            replay, replay_created = enqueue_provider_analysis(
                service=JobService(session),
                spec=_spec(provider),
                idempotency_key="provider-success",
                now=NOW,
            )

        assert created is True
        assert replay_created is False
        assert replay.id == job.id
        assert job.kind == PROVIDER_ANALYSIS_JOB_KIND
        assert job.max_attempts == 2
        assert job.timeout_seconds == 10

        completed = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                provider,
                worker_id="provider-worker",
                heartbeat_interval_seconds=0.1,
            ).run_once(now=NOW)
        )

        assert completed is not None
        assert completed.state == "succeeded"
        assert completed.progress_current == 2
        assert completed.progress_total == 2
        assert len(provider.inspect_requests) == 1
        assert len(provider.analyze_requests) == 1

        provider_run = completed.result_data["provider_run"]
        assert provider_run["provider"] == provider.identity.hash_payload()
        assert provider_run["versions"] == {
            "inspect": {
                "prompt": "inspect-prompt-v1",
                "policy": "analysis-policy-v1",
                "output_schema": "inspection-schema-v1",
            },
            "analyze": {
                "prompt": "analyze-prompt-v1",
                "policy": "analysis-policy-v1",
                "output_schema": ANALYSIS_SCHEMA_VERSION,
            },
        }
        assert provider_run["usage"] == {
            "input_tokens": 300,
            "cached_input_tokens": 60,
            "output_tokens": 90,
            "estimated_cost_microusd": 30,
            "duration_ms": 300,
        }
        assert provider_run["budget"]["model_invocations"] == 2
        assert provider_run["budget"]["exhausted_reason"] is None
        assert completed.result_data["inspection"]["output_hash"]
        assert completed.result_data["analysis"]["output_hash"]

        with database.session() as session:
            invocations = list(
                session.scalars(
                    select(ProviderInvocation).order_by(
                        ProviderInvocation.attempt_number,
                        ProviderInvocation.stage.desc(),
                    )
                )
            )
            assert [(item.stage, item.status) for item in invocations] == [
                ("inspect", "succeeded"),
                ("analyze", "succeeded"),
            ]
            assert [item.input_tokens for item in invocations] == [100, 200]
            assert [item.duration_ms for item in invocations] == [100, 200]
            assert all(item.record_hash for item in invocations)

            invocations[0].duration_ms = 0
            with pytest.raises(IntegrityError, match="immutable"):
                session.commit()
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            persisted = JobService(session).get(job.id)
            assert persisted.state == "succeeded"
            assert len(persisted.provider_invocations) == 2
        assert (
            asyncio.run(
                ProviderAnalysisJobWorker(
                    restarted,
                    provider,
                    worker_id="provider-worker-restarted",
                ).run_once()
            )
            is None
        )
    finally:
        restarted.close()


def test_failed_attempt_is_retained_and_retry_creates_new_accounting(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "provider-retry.db")
    provider = FakeProvider(
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
    worker = ProviderAnalysisJobWorker(
        database,
        provider,
        worker_id="retry-worker",
        heartbeat_interval_seconds=0.1,
    )
    try:
        with database.session() as session:
            job, _ = enqueue_provider_analysis(
                service=JobService(session),
                spec=_spec(provider),
                idempotency_key="provider-retry",
                now=NOW,
            )

        first = asyncio.run(worker.run_once(now=NOW))
        assert first is not None
        assert first.state == "failed"
        assert first.error_code == "injected_transient_failure"

        with database.session() as session:
            JobService(session).retry(job.id, now=NOW + timedelta(seconds=1))

        second = asyncio.run(worker.run_once(now=NOW + timedelta(seconds=1)))
        assert second is not None
        assert second.state == "succeeded"
        assert second.attempt_count == 2
        assert second.result_data["provider_run"]["budget"]["model_invocations"] == 3
        assert second.result_data["provider_run"]["budget"]["retries"] == 1

        with database.session() as session:
            invocations = list(
                session.scalars(
                    select(ProviderInvocation).order_by(
                        ProviderInvocation.attempt_number,
                        ProviderInvocation.stage.desc(),
                    )
                )
            )
        assert [
            (item.attempt_number, item.stage, item.status, item.error_code)
            for item in invocations
        ] == [
            (1, "inspect", "failed", "injected_transient_failure"),
            (2, "inspect", "succeeded", None),
            (2, "analyze", "succeeded", None),
        ]
    finally:
        database.close()


def test_provider_worker_cancellation_finalization_is_idempotent(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "provider-cancel-race.db")
    provider = FakeProvider()
    worker = ProviderAnalysisJobWorker(
        database,
        provider,
        worker_id="cancel-race-worker",
    )
    try:
        with database.session() as session:
            job, _ = enqueue_provider_analysis(
                service=JobService(session),
                spec=_spec(provider),
                idempotency_key="provider-cancel-race",
                now=NOW,
            )
            service = JobService(session)
            leased = service.lease_next(
                worker_id="cancel-race-worker",
                now=NOW,
            )
            assert leased is not None
            service.start(
                job.id,
                worker_id="cancel-race-worker",
                now=NOW,
            )
            service.request_cancel(job.id, now=NOW)
            service.cancel(
                job.id,
                worker_id="cancel-race-worker",
                now=NOW,
            )

        completed = worker._finish_cancelled_job(job.id)

        assert completed.state == "cancelled"
    finally:
        database.close()


def test_over_budget_output_is_accounted_but_analysis_fails_closed(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "provider-budget.db")
    provider = FakeProvider()
    try:
        with database.session() as session:
            job, _ = enqueue_provider_analysis(
                service=JobService(session),
                spec=_spec(
                    provider,
                    budget=_budget(
                        max_input_tokens=50,
                        max_retries=0,
                    ),
                ),
                idempotency_key="provider-budget",
                now=NOW,
            )

        completed = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                provider,
                worker_id="budget-worker",
            ).run_once(now=NOW)
        )

        assert completed is not None
        assert completed.state == "failed"
        assert completed.error_code == "analysis_budget_input_token_limit"
        assert completed.result_data == {}
        assert len(provider.inspect_requests) == 1
        assert provider.analyze_requests == ()

        with database.session() as session:
            invocation = session.scalar(select(ProviderInvocation))
            assert invocation is not None
            assert invocation.stage == "inspect"
            assert invocation.status == "succeeded"
            assert invocation.input_tokens == 100
    finally:
        database.close()


class _SlowFakeProvider(FakeProvider):
    async def inspect(self, request, emit):  # type: ignore[no-untyped-def]
        await asyncio.sleep(2)
        return await super().inspect(request, emit)


def test_provider_job_timeout_is_terminal_and_recorded_without_raw_output(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "provider-timeout.db")
    provider = _SlowFakeProvider()
    try:
        with database.session() as session:
            job, _ = enqueue_provider_analysis(
                service=JobService(session),
                spec=_spec(
                    provider,
                    budget=_budget(max_duration_ms=1, max_retries=0),
                ),
                idempotency_key="provider-timeout",
                now=NOW,
            )
            assert job.timeout_seconds == 1

        completed = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                provider,
                worker_id="timeout-worker",
                heartbeat_interval_seconds=0.1,
            ).run_once(now=NOW)
        )

        assert completed is not None
        assert completed.state == "timed_out"
        assert completed.error_code == "execution_timeout"
        assert completed.error_message == (
            "Provider analysis Job exceeded its timeout"
        )
        with database.session() as session:
            invocation = session.scalar(select(ProviderInvocation))
            assert invocation is not None
            assert invocation.status == "timed_out"
            assert invocation.error_code == "provider_execution_timeout"
            assert invocation.output_hash is None
    finally:
        database.close()


def test_malformed_job_and_provider_identity_mismatch_never_invoke_model(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "provider-malformed.db")
    provider = FakeProvider()
    try:
        with database.session() as session:
            malformed, _ = JobService(session).enqueue(
                kind=PROVIDER_ANALYSIS_JOB_KIND,
                idempotency_key="malformed-provider-job",
                payload={},
                now=NOW,
            )

        malformed_result = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                provider,
                worker_id="malformed-worker",
            ).run_once(now=NOW)
        )
        assert malformed_result is not None
        assert malformed_result.id == malformed.id
        assert malformed_result.state == "failed"
        assert malformed_result.error_code == "provider_contract_failure"

        mismatched_provider = FakeProvider(
            identity=ProviderIdentity(
                provider="different-fake",
                adapter_version="fake-adapter-v1",
                model="fake-analysis-model",
                model_version="fake-model-v1",
            )
        )
        with database.session() as session:
            mismatch, _ = enqueue_provider_analysis(
                service=JobService(session),
                spec=_spec(provider),
                idempotency_key="mismatched-provider-job",
                now=NOW,
            )
        mismatch_result = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                mismatched_provider,
                worker_id="mismatch-worker",
            ).run_once(now=NOW)
        )
        assert mismatch_result is not None
        assert mismatch_result.id == mismatch.id
        assert mismatch_result.state == "failed"
        assert mismatch_result.error_code == "provider_identity_mismatch"
        assert provider.inspect_requests == ()
        assert provider.analyze_requests == ()
        assert mismatched_provider.inspect_requests == ()
        assert mismatched_provider.analyze_requests == ()
    finally:
        database.close()


def test_secret_like_inputs_and_job_results_are_rejected_before_persistence(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "provider-secret.db")
    provider = FakeProvider()
    canary = "github_pat_PROVIDER_JOB_CANARY_123456"
    try:
        with database.session() as session:
            with pytest.raises(SensitiveDataError):
                enqueue_provider_analysis(
                    service=JobService(session),
                    spec=_spec(provider, evidence_content=canary),
                    idempotency_key="provider-secret",
                    now=NOW,
                )
            assert session.scalar(select(Job)) is None

            job, _ = JobService(session).enqueue(
                kind="safe-result-check",
                idempotency_key="safe-result-check",
                payload={},
                now=NOW,
            )
            leased = JobService(session).lease_next(
                worker_id="safe-result-worker",
                now=NOW,
            )
            assert leased is not None
            JobService(session).start(
                job.id,
                worker_id="safe-result-worker",
                now=NOW,
            )
            with pytest.raises(SensitiveDataError):
                JobService(session).succeed(
                    job.id,
                    worker_id="safe-result-worker",
                    result_data={"output": canary},
                    now=NOW,
                )
    finally:
        database.close()


def test_provider_job_payload_and_stage_outputs_are_bounded(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "provider-bounds.db")
    provider = FakeProvider()
    try:
        with database.session() as session:
            with pytest.raises(ProviderContractError, match="payload exceeds"):
                enqueue_provider_analysis(
                    service=JobService(session),
                    spec=_spec(
                        provider,
                        evidence_content="x" * (MAX_PROVIDER_JOB_PAYLOAD_BYTES + 1),
                    ),
                    idempotency_key="oversized-provider-payload",
                    now=NOW,
                )

        oversized_provider = FakeProvider(
            FakeProviderScript(
                inspect_output={
                    "blob": "x" * (MAX_PROVIDER_STAGE_OUTPUT_BYTES + 1)
                }
            )
        )
        with database.session() as session:
            job, _ = enqueue_provider_analysis(
                service=JobService(session),
                spec=_spec(oversized_provider),
                idempotency_key="oversized-provider-output",
                now=NOW,
            )

        completed = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                oversized_provider,
                worker_id="bounds-worker",
            ).run_once(now=NOW)
        )
        assert completed is not None
        assert completed.id == job.id
        assert completed.state == "failed"
        assert completed.error_code == "provider_contract_failure"
        assert oversized_provider.analyze_requests == ()
        with database.session() as session:
            invocation = session.scalar(select(ProviderInvocation))
            assert invocation is not None
            assert invocation.status == "failed"
            assert invocation.output_hash is None
    finally:
        database.close()


def test_provider_invocation_replay_is_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "provider-accounting.db")
    provider = FakeProvider()
    try:
        with database.session() as session:
            job, _ = JobService(session).enqueue(
                kind=PROVIDER_ANALYSIS_JOB_KIND,
                idempotency_key="accounting",
                payload={},
                now=NOW,
            )
            service = ProviderInvocationService(session)
            fields = {
                "job_id": job.id,
                "attempt_number": 1,
                "stage": ProviderStage.INSPECT,
                "status": ProviderInvocationStatus.SUCCEEDED,
                "request_id": "accounting-request",
                "correlation_id": "accounting-correlation",
                "input_hash": "1" * 64,
                "output_hash": "2" * 64,
                "provider": provider.identity,
                "prompt_version": "prompt-v1",
                "policy_version": "policy-v1",
                "output_schema_version": "schema-v1",
                "usage": ProviderUsage(input_tokens=10),
                "error_code": None,
                "started_at": NOW,
                "completed_at": NOW + timedelta(milliseconds=1),
            }
            first = service.append(**fields)
            replay = service.append(**fields)
            assert replay.id == first.id

            with pytest.raises(ProviderInvocationConflictError):
                service.append(
                    **{
                        **fields,
                        "usage": ProviderUsage(input_tokens=11),
                    }
                )
    finally:
        database.close()
