from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.models import (
    AnalysisVersion,
    FinalScoreVersion,
    Job,
    Opportunity,
    OpportunitySnapshot,
    ScanRun,
    ScoreVersion,
)
from app.providers import (
    AnalysisHistoryNotFoundError,
    AnalysisHistoryService,
    AnalysisBudget,
    ANALYSIS_SCHEMA_VERSION,
    AnalysisInputFreezer,
    AnalysisInputIntegrityError,
    AnalysisInputNotFoundError,
    AnalysisInputSafetyError,
    AnalysisVersionIntegrityError,
    AnalysisVersionService,
    FakeFailure,
    FakeProvider,
    FakeProviderScript,
    FinalScoreConflictError,
    FinalScoreVersionService,
    FrozenCandidateInput,
    FrozenEvidence,
    ManualAnalysisConflictError,
    ManualAnalysisRequest,
    ManualAnalysisService,
    ProviderAnalysisJobSpec,
    ProviderAnalysisJobWorker,
    ProviderStage,
    ScoreCalibration,
    enqueue_provider_analysis,
    freeze_candidate_input,
)
from app.service import DiscoveryService


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
WORKER_NOW = datetime.now(timezone.utc) + timedelta(seconds=1)


class _CandidateGitHub:
    rate_limit_remaining = 4_999
    rate_limit_reset_at = NOW + timedelta(hours=1)

    def __init__(
        self,
        count: int,
        *,
        body: str | None = None,
    ) -> None:
        self.count = count
        self.body = body or (
            "Steps to reproduce, current behavior, expected behavior, and an "
            "acceptance checklist are included for deterministic analysis."
        )

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "id": 10_000 + index,
                "number": index,
                "title": f"Candidate {index:02d} improve analysis",
                "body": self.body,
                "html_url": (
                    f"https://github.com/fixture/evidence/issues/{index}"
                ),
                "repository_url": (
                    "https://api.github.com/repos/fixture/evidence"
                ),
                "state": "open",
                "labels": [{"name": "help wanted"}],
                "comments": index % 5,
                "assignees": [],
                "author_association": "MEMBER",
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-07-29T00:00:00Z",
            }
            for index in range(1, self.count + 1)
        ]

    async def get_repository_bundle(self, full_name: str) -> dict[str, Any]:
        assert full_name == "fixture/evidence"
        return {
            "repository": {
                "id": 9_001,
                "full_name": full_name,
                "description": "Evidence-safe Python analysis fixture",
                "html_url": f"https://github.com/{full_name}",
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "stargazers_count": 2_000,
                "forks_count": 100,
                "open_issues_count": 40,
                "archived": False,
                "disabled": False,
                "default_branch": "main",
                "topics": ["ai", "analysis"],
                "pushed_at": "2026-07-29T00:00:00Z",
            },
            "community": {
                "health_percentage": 95,
                "files": {
                    "contributing": {
                        "url": "https://github.com/fixture/evidence/CONTRIBUTING.md"
                    }
                },
            },
        }


def _database(tmp_path: Path, name: str) -> Database:
    database = Database(f"sqlite+pysqlite:///{tmp_path / name}")
    database.create_schema()
    return database


def _scan(database: Database, *, count: int, body: str | None = None) -> str:
    settings = Settings(
        database_url=str(database.engine.url),
        github_queries=("evidence-query",),
        candidates_per_query=50,
        daily_pick_count=min(10, count),
    )
    with database.session() as session:
        run = asyncio.run(
            DiscoveryService(
                session,
                _CandidateGitHub(count, body=body),
                settings,
            ).scan(
                now=NOW,
                top_n=min(10, count),
            )
        )
        return run.id


def test_top_rule_ranked_inputs_are_deterministic_bounded_and_snapshot_based(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "evidence-top.db")
    try:
        scan_run_id = _scan(database, count=35)
        with database.session() as session:
            first = AnalysisInputFreezer(session).freeze_top_candidates(
                scan_run_id=scan_run_id,
            )
            assert len(first) == 30
            assert [item.rule_rank for item in first] == list(range(1, 31))
            assert all(len(item.evidence) == 3 for item in first)
            assert all(item.input_hash for item in first)

            rows = list(
                session.execute(
                    select(OpportunitySnapshot, ScoreVersion)
                    .join(
                        ScoreVersion,
                        ScoreVersion.snapshot_id == OpportunitySnapshot.id,
                    )
                    .where(
                        OpportunitySnapshot.scan_run_id == scan_run_id,
                        OpportunitySnapshot.filter_eligible.is_(True),
                    )
                )
            )
            expected = sorted(
                rows,
                key=lambda pair: (
                    -pair[1].score_total,
                    -float(
                        pair[1].score_components.get("project_impact", 0)
                    ),
                    pair[0].opportunity_id,
                    pair[1].id,
                ),
            )[:30]
            assert [item.snapshot_id for item in first] == [
                snapshot.id for snapshot, _ in expected
            ]

            live_opportunity = session.get(
                Opportunity,
                expected[0][0].opportunity_id,
            )
            assert live_opportunity is not None
            live_opportunity.title = "Mutable current title must not affect evidence"
            live_opportunity.body = "Mutable current body must not affect evidence"
            session.commit()

            replay = AnalysisInputFreezer(session).freeze_top_candidates(
                scan_run_id=scan_run_id,
            )
            assert replay == first

            issue = json.loads(first[0].evidence[0].content)
            repository = json.loads(first[0].evidence[1].content)
            rule_score = json.loads(first[0].evidence[2].content)
            assert issue["title"] != live_opportunity.title
            assert issue["body_truncated"] is False
            assert repository["full_name"] == "fixture/evidence"
            assert set(repository).isdisjoint(
                {"github_id", "sync_error", "last_synced_at"}
            )
            assert rule_score["snapshot_inputs_hash"] == (
                first[0].snapshot_inputs_hash
            )
            assert rule_score["score_output_hash"] == first[0].score_output_hash

            provider = FakeProvider()
            spec = first[0].to_provider_job_spec(
                correlation_id="frozen-evidence-job",
                budget=AnalysisBudget(),
                expected_provider=provider.identity,
                inspect_prompt_version="inspect-prompt-v1",
                inspect_policy_version="analysis-policy-v1",
                inspect_output_schema_version="inspection-schema-v1",
                analyze_prompt_version="analyze-prompt-v1",
                analyze_policy_version="analysis-policy-v1",
                analyze_output_schema_version=ANALYSIS_SCHEMA_VERSION,
            )
            assert spec.snapshot_id == first[0].snapshot_id
            assert spec.score_version_id == first[0].score_version_id
            assert spec.evidence == first[0].evidence
            assert spec.frozen_input_hash == first[0].input_hash
            assert provider.inspect_requests == ()
            assert provider.analyze_requests == ()

            with pytest.raises(FrozenInstanceError):
                first[0].rule_rank = 2  # type: ignore[misc]
    finally:
        database.close()


def test_long_issue_body_is_deterministically_truncated_and_hash_bound(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "evidence-truncated.db")
    body = "expected behavior " + ("x" * 70_000)
    try:
        scan_run_id = _scan(database, count=1, body=body)
        with database.session() as session:
            frozen = AnalysisInputFreezer(session).freeze_top_candidates(
                scan_run_id=scan_run_id,
                limit=1,
            )[0]
        issue = json.loads(frozen.evidence[0].content)
        assert issue["body_truncated"] is True
        assert len(issue["body"]) == 65_536
        assert issue["body_content_hash"]
        assert body not in frozen.evidence[0].content
    finally:
        database.close()


def test_frozen_input_hash_and_evidence_survive_the_durable_job_boundary(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "evidence-job.db")
    provider = FakeProvider()
    try:
        scan_run_id = _scan(database, count=1)
        with database.session() as session:
            frozen = AnalysisInputFreezer(session).freeze_top_candidates(
                scan_run_id=scan_run_id,
                limit=1,
            )[0]
            spec = frozen.to_provider_job_spec(
                correlation_id="frozen-job-boundary",
                budget=AnalysisBudget(),
                expected_provider=provider.identity,
                inspect_prompt_version="inspect-prompt-v1",
                inspect_policy_version="analysis-policy-v1",
                inspect_output_schema_version="inspection-schema-v1",
                analyze_prompt_version="analyze-prompt-v1",
                analyze_policy_version="analysis-policy-v1",
                analyze_output_schema_version=ANALYSIS_SCHEMA_VERSION,
            )
            job, _ = enqueue_provider_analysis(
                service=JobService(session),
                spec=spec,
                idempotency_key="frozen-job-boundary",
                now=NOW,
            )

        completed = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                provider,
                worker_id="frozen-input-worker",
            ).run_once(now=WORKER_NOW)
        )
        assert completed is not None
        assert completed.id == job.id
        assert completed.state == "succeeded"
        assert completed.result_data["provider_run"]["frozen_input_hash"] == (
            frozen.input_hash
        )
        assert provider.inspect_requests[0].evidence == frozen.evidence
        assert provider.inspect_requests[0].snapshot_id == frozen.snapshot_id
        assert provider.inspect_requests[0].score_version_id == (
            frozen.score_version_id
        )
    finally:
        database.close()


def test_stale_snapshot_or_score_hash_is_rejected_before_evidence_is_created(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "evidence-stale.db")
    try:
        scan_run_id = _scan(database, count=1)
        with database.session() as session:
            snapshot = session.scalar(
                select(OpportunitySnapshot).where(
                    OpportunitySnapshot.scan_run_id == scan_run_id
                )
            )
            score = session.scalar(
                select(ScoreVersion).where(
                    ScoreVersion.snapshot_id == snapshot.id
                )
            )
            assert snapshot is not None
            assert score is not None
            original_snapshot_hash = snapshot.inputs_hash
            snapshot.inputs_hash = "0" * 64
            with pytest.raises(
                AnalysisInputIntegrityError,
                match="OpportunitySnapshot inputs hash",
            ):
                freeze_candidate_input(
                    snapshot=snapshot,
                    score=score,
                    rule_rank=1,
                )

            snapshot.inputs_hash = original_snapshot_hash
            score.output_hash = "0" * 64
            with pytest.raises(
                AnalysisInputIntegrityError,
                match="ScoreVersion output hash",
            ):
                freeze_candidate_input(
                    snapshot=snapshot,
                    score=score,
                    rule_rank=1,
                )
            session.rollback()
    finally:
        database.close()


def test_freezer_rejects_missing_legacy_and_out_of_range_requests(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "evidence-invalid.db")
    try:
        with database.session() as session:
            freezer = AnalysisInputFreezer(session)
            with pytest.raises(AnalysisInputNotFoundError):
                freezer.freeze_top_candidates(scan_run_id="missing")
            with pytest.raises(ValueError, match="between 1 and 30"):
                freezer.freeze_top_candidates(scan_run_id="missing", limit=31)

            legacy = ScanRun(
                id="legacy-analysis-scan",
                status="completed",
                provenance_status="legacy_unverified",
                selection_date=None,
                queries=[],
                candidate_count=0,
                eligible_count=0,
                selected_count=0,
                repository_count=0,
                started_at=NOW,
                completed_at=NOW,
            )
            session.add(legacy)
            session.commit()
            with pytest.raises(
                AnalysisInputIntegrityError,
                match="completed verified",
            ):
                freezer.freeze_top_candidates(scan_run_id=legacy.id)
    finally:
        database.close()


def test_credential_like_content_cannot_become_frozen_analysis_evidence() -> None:
    canary = "github_pat_FROZEN_EVIDENCE_CANARY_123456"
    unsafe = FrozenEvidence.capture(
        evidence_id="issue",
        kind="github_issue_snapshot",
        source_uri="github://fixture/evidence/issues/1",
        content=canary,
    )

    with pytest.raises(AnalysisInputSafetyError, match="credential-like"):
        FrozenCandidateInput.create(
            rule_rank=1,
            scan_run_id="scan-1",
            snapshot_id="snapshot-1",
            snapshot_inputs_hash="1" * 64,
            score_version_id="score-1",
            score_output_hash="2" * 64,
            evidence=(unsafe,),
        )


def _run_frozen_provider_job(
    database: Database,
    *,
    idempotency_key: str,
) -> tuple[str, FrozenCandidateInput]:
    provider = FakeProvider()
    scan_run_id = _scan(database, count=1)
    with database.session() as session:
        frozen = AnalysisInputFreezer(session).freeze_top_candidates(
            scan_run_id=scan_run_id,
            limit=1,
        )[0]
        spec = frozen.to_provider_job_spec(
            correlation_id=idempotency_key,
            budget=AnalysisBudget(),
            expected_provider=provider.identity,
            inspect_prompt_version="inspect-prompt-v1",
            inspect_policy_version="analysis-policy-v1",
            inspect_output_schema_version="inspection-schema-v1",
            analyze_prompt_version="analyze-prompt-v1",
            analyze_policy_version="analysis-policy-v1",
            analyze_output_schema_version=ANALYSIS_SCHEMA_VERSION,
        )
        job, _ = enqueue_provider_analysis(
            service=JobService(session),
            spec=spec,
            idempotency_key=idempotency_key,
            now=NOW,
        )
    completed = asyncio.run(
        ProviderAnalysisJobWorker(
            database,
            provider,
            worker_id=f"{idempotency_key}-worker",
        ).run_once(now=WORKER_NOW)
    )
    assert completed is not None
    assert completed.state == "succeeded"
    return job.id, frozen


def test_succeeded_provider_job_creates_one_immutable_analysis_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "analysis-version.db"
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        job_id, frozen = _run_frozen_provider_job(
            database,
            idempotency_key="analysis-version-success",
        )
        with database.session() as session:
            service = AnalysisVersionService(session)
            version = service.create_from_succeeded_job(
                job_id=job_id,
                now=NOW,
            )
            replay = service.create_from_succeeded_job(
                job_id=job_id,
                now=NOW + timedelta(seconds=1),
            )

            assert replay.id == version.id
            assert version.snapshot_id == frozen.snapshot_id
            assert version.score_version_id == frozen.score_version_id
            assert version.frozen_input_hash == frozen.input_hash
            assert version.snapshot_inputs_hash == frozen.snapshot_inputs_hash
            assert version.score_output_hash == frozen.score_output_hash
            assert version.provider_name == "fake"
            assert version.model_name == "fake-analysis-model"
            assert version.inspect_prompt_version == "inspect-prompt-v1"
            assert (
                version.analyze_output_schema_version
                == ANALYSIS_SCHEMA_VERSION
            )
            assert version.input_tokens == 300
            assert version.cached_input_tokens == 60
            assert version.output_tokens == 90
            assert version.estimated_cost_microusd == 30
            assert version.duration_ms == 300
            assert version.record_hash
            assert version.inspection_structured_output["observations"]
            assert set(version.inspection_cited_evidence_ids) == {
                "issue",
                "repository",
                "rule_score",
            }
            assert version.structured_output["problem_summary"] == (
                "Deterministic fake analysis"
            )
            assert set(version.structured_output) == {
                "problem_summary",
                "current_behavior",
                "expected_behavior",
                "acceptance_criteria",
                "missing_information",
                "similar_issue_pr_evidence",
                "competition",
                "estimated_effort",
                "bounty_basis",
                "risks",
                "confidence",
                "cited_evidence_ids",
                "citation_map",
            }
            assert set(version.cited_evidence_ids) == {
                "issue",
                "repository",
                "rule_score",
            }
            assert version.inspect_invocation.stage == "inspect"
            assert version.analyze_invocation.stage == "analyze"
            assert version.inspect_invocation.job_id == job_id
            assert version.analyze_invocation.job_id == job_id
            assert version.inspect_input_hash == (
                version.inspect_invocation.input_hash
            )
            assert version.inspect_output_hash == (
                version.inspect_invocation.output_hash
            )
            assert version.analysis_input_hash == (
                version.analyze_invocation.input_hash
            )
            assert version.analysis_output_hash == (
                version.analyze_invocation.output_hash
            )

            version.structured_output = {"tampered": True}
            with pytest.raises(IntegrityError, match="immutable"):
                session.commit()
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            persisted = session.scalar(
                select(AnalysisVersion).where(AnalysisVersion.job_id == job_id)
            )
            assert persisted is not None
            assert persisted.frozen_input_hash == frozen.input_hash
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "invalid_kind",
    ("schema_version", "evidence_reference", "structured_shape"),
)
def test_invalid_schema_or_evidence_never_produces_an_analysis_version(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    database = _database(tmp_path, f"analysis-invalid-{invalid_kind}.db")
    if invalid_kind == "evidence_reference":
        provider = FakeProvider(
            FakeProviderScript(analyze_citations=("not-frozen",))
        )
    elif invalid_kind == "structured_shape":
        provider = FakeProvider(
            FakeProviderScript(
                analyze_output={
                    "problem_summary": "Missing required structured fields",
                    "cited_evidence_ids": ["issue"],
                }
            )
        )
    else:
        provider = FakeProvider()
    try:
        scan_run_id = _scan(database, count=1)
        with database.session() as session:
            frozen = AnalysisInputFreezer(session).freeze_top_candidates(
                scan_run_id=scan_run_id,
                limit=1,
            )[0]
            spec = frozen.to_provider_job_spec(
                correlation_id=f"invalid-{invalid_kind}",
                budget=AnalysisBudget(),
                expected_provider=provider.identity,
                inspect_prompt_version="inspect-prompt-v1",
                inspect_policy_version="analysis-policy-v1",
                inspect_output_schema_version="inspection-schema-v1",
                analyze_prompt_version="analyze-prompt-v1",
                analyze_policy_version="analysis-policy-v1",
                analyze_output_schema_version=(
                    "analysis-schema-v999"
                    if invalid_kind == "schema_version"
                    else ANALYSIS_SCHEMA_VERSION
                ),
            )
            job, _ = enqueue_provider_analysis(
                service=JobService(session),
                spec=spec,
                idempotency_key=f"invalid-{invalid_kind}",
                now=NOW,
            )

        completed = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                provider,
                worker_id=f"invalid-{invalid_kind}-worker",
            ).run_once(now=WORKER_NOW)
        )
        assert completed is not None
        assert completed.state == "failed"
        assert completed.error_code == "provider_contract_failure"
        with database.session() as session:
            assert session.scalar(select(AnalysisVersion)) is None
            with pytest.raises(
                AnalysisVersionIntegrityError,
                match="requires a succeeded",
            ):
                AnalysisVersionService(session).create_from_succeeded_job(
                    job_id=job.id,
                    now=NOW,
                )
    finally:
        database.close()


@pytest.mark.parametrize("tamper_target", ("payload", "result"))
def test_analysis_version_rejects_tampered_job_state(
    tmp_path: Path,
    tamper_target: str,
) -> None:
    database = _database(
        tmp_path,
        f"analysis-version-{tamper_target}.db",
    )
    try:
        job_id, _ = _run_frozen_provider_job(
            database,
            idempotency_key=f"analysis-version-{tamper_target}",
        )
        with database.session() as session:
            job = session.get(Job, job_id)
            existing_version = session.scalar(
                select(AnalysisVersion).where(
                    AnalysisVersion.job_id == job_id
                )
            )
            assert job is not None
            assert existing_version is not None
            existing_version_id = existing_version.id
            existing_record_hash = existing_version.record_hash
            if tamper_target == "payload":
                payload = copy.deepcopy(job.payload)
                payload["correlation_id"] = "tampered-correlation"
                job.payload = payload
                expected = "payload hash"
            else:
                result = copy.deepcopy(job.result_data)
                result["analysis"]["structured_output"] = {
                    "problem_summary": "tampered after Provider completion"
                }
                job.result_data = result
                expected = "results are invalid"
            session.commit()

        with database.session() as session:
            with pytest.raises(AnalysisVersionIntegrityError, match=expected):
                AnalysisVersionService(session).create_from_succeeded_job(
                    job_id=job_id,
                    now=NOW,
                )
            versions = list(session.scalars(select(AnalysisVersion)))
            assert len(versions) == 1
            assert versions[0].id == existing_version_id
            assert versions[0].record_hash == existing_record_hash
    finally:
        database.close()


def _calibration(
    *,
    score_total: float = 75.0,
    component_value: float = 80.0,
    risk_penalty: float = 5.0,
    citations: tuple[str, ...] = ("issue", "repository"),
) -> ScoreCalibration:
    return ScoreCalibration(
        score_total=score_total,
        score_components={
            "reward_reliability": component_value,
            "acceptance_probability": component_value,
            "tech_match": component_value,
            "project_impact": component_value,
            "issue_clarity": component_value,
            "competition": component_value,
            "learning_value": component_value,
        },
        risk_penalty=risk_penalty,
        risk_reasons=("ai_scope_uncertainty",),
        rationale=(
            "The frozen evidence supports a calibrated score while retaining "
            "the deterministic rule score as its immutable parent."
        ),
        cited_evidence_ids=citations,
    )


def test_ai_calibration_creates_a_new_final_score_without_overwriting_rules(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "final-score.db")
    try:
        job_id, _ = _run_frozen_provider_job(
            database,
            idempotency_key="final-score",
        )
        with database.session() as session:
            analysis = AnalysisVersionService(
                session
            ).create_from_succeeded_job(
                job_id=job_id,
                now=NOW,
            )
            rule_score = session.get(ScoreVersion, analysis.score_version_id)
            assert rule_score is not None
            rule_before = {
                "id": rule_score.id,
                "output_hash": rule_score.output_hash,
                "score_total": rule_score.score_total,
                "score_components": copy.deepcopy(rule_score.score_components),
                "risk_penalty": rule_score.risk_penalty,
                "risk_reasons": copy.deepcopy(rule_score.risk_reasons),
            }

            service = FinalScoreVersionService(session)
            final = service.create(
                analysis_version_id=analysis.id,
                calibration=_calibration(),
                now=NOW,
            )
            replay = service.create(
                analysis_version_id=analysis.id,
                calibration=_calibration(),
                now=NOW + timedelta(seconds=1),
            )

            assert replay.id == final.id
            assert final.snapshot_id == analysis.snapshot_id
            assert final.rule_score_version_id == rule_score.id
            assert final.analysis_version_id == analysis.id
            assert final.score_total == 75.0
            assert set(final.score_components) == set(rule_score.score_components)
            assert final.input_hash
            assert final.calibration_hash
            assert final.output_hash
            assert final.cited_evidence_ids == ["issue", "repository"]
            final_id = final.id
            rule_score_id = rule_score.id

            session.refresh(rule_score)
            rule_after = {
                "id": rule_score.id,
                "output_hash": rule_score.output_hash,
                "score_total": rule_score.score_total,
                "score_components": copy.deepcopy(rule_score.score_components),
                "risk_penalty": rule_score.risk_penalty,
                "risk_reasons": copy.deepcopy(rule_score.risk_reasons),
            }
            assert rule_after == rule_before

            with pytest.raises(FinalScoreConflictError, match="different"):
                service.create(
                    analysis_version_id=analysis.id,
                    calibration=_calibration(
                        score_total=76.0,
                        component_value=81.0,
                    ),
                    now=NOW,
                )

            final.rationale = "tampered"
            with pytest.raises(IntegrityError, match="immutable"):
                session.commit()
            session.rollback()

        with database.session() as session:
            stored_final = session.get(FinalScoreVersion, final_id)
            stored_rule = session.get(ScoreVersion, rule_score_id)
            assert stored_final is not None
            assert stored_final.rationale == _calibration().rationale
            assert stored_rule is not None
            assert {
                "id": stored_rule.id,
                "output_hash": stored_rule.output_hash,
                "score_total": stored_rule.score_total,
                "score_components": stored_rule.score_components,
                "risk_penalty": stored_rule.risk_penalty,
                "risk_reasons": stored_rule.risk_reasons,
            } == rule_before
    finally:
        database.close()


def test_ai_calibration_rejects_invalid_math_or_non_frozen_citations(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="total does not match"):
        _calibration(score_total=74.9)

    database = _database(tmp_path, "final-score-invalid.db")
    try:
        job_id, _ = _run_frozen_provider_job(
            database,
            idempotency_key="final-score-invalid",
        )
        with database.session() as session:
            analysis = AnalysisVersionService(
                session
            ).create_from_succeeded_job(
                job_id=job_id,
                now=NOW,
            )
            with pytest.raises(ValueError, match="outside"):
                FinalScoreVersionService(session).create(
                    analysis_version_id=analysis.id,
                    calibration=_calibration(citations=("not-frozen",)),
                    now=NOW,
                )
            assert session.scalar(select(FinalScoreVersion)) is None
    finally:
        database.close()


def test_manual_analysis_is_idempotent_and_limited_to_the_frozen_top_30(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "manual-analysis.db")
    provider = FakeProvider()
    try:
        scan_run_id = _scan(database, count=31)
        with database.session() as session:
            frozen = AnalysisInputFreezer(session).freeze_top_candidates(
                scan_run_id=scan_run_id,
            )
            included_ids = {item.snapshot_id for item in frozen}
            excluded = session.scalar(
                select(OpportunitySnapshot).where(
                    OpportunitySnapshot.scan_run_id == scan_run_id,
                    OpportunitySnapshot.id.not_in(included_ids),
                )
            )
            assert len(frozen) == 30
            assert excluded is not None

            request = ManualAnalysisRequest(
                snapshot_id=frozen[0].snapshot_id,
                correlation_id="manual-fixed-corpus",
                idempotency_key="manual-fixed-corpus-run-1",
                budget=AnalysisBudget(max_retries=1),
                expected_provider=provider.identity,
            )
            service = ManualAnalysisService(session)
            job, created = service.request(request, now=NOW)
            replay, replay_created = service.request(request, now=NOW)
            spec = ProviderAnalysisJobSpec.from_payload(job.payload)

            assert created is True
            assert replay_created is False
            assert replay.id == job.id
            assert spec.snapshot_id == frozen[0].snapshot_id
            assert spec.score_version_id == frozen[0].score_version_id
            assert spec.frozen_input_hash == frozen[0].input_hash
            assert job.max_attempts == 2

            with pytest.raises(
                ManualAnalysisConflictError,
                match="top-30",
            ):
                service.request(
                    ManualAnalysisRequest(
                        snapshot_id=excluded.id,
                        correlation_id="manual-outside-corpus",
                        idempotency_key="manual-outside-corpus",
                        budget=AnalysisBudget(),
                        expected_provider=provider.identity,
                    ),
                    now=NOW,
                )
            with pytest.raises(
                ManualAnalysisConflictError,
                match="inspect and analyze",
            ):
                service.request(
                    ManualAnalysisRequest(
                        snapshot_id=frozen[0].snapshot_id,
                        correlation_id="manual-insufficient-budget",
                        idempotency_key="manual-insufficient-budget",
                        budget=AnalysisBudget(max_model_invocations=1),
                        expected_provider=provider.identity,
                    ),
                    now=NOW,
                )
    finally:
        database.close()


def test_manual_analysis_retry_is_preflighted_against_durable_budget(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path, "manual-retry.db")
    provider = FakeProvider(
        FakeProviderScript(
            failure=FakeFailure(
                stage=ProviderStage.INSPECT,
                code="manual_transient_failure",
                safe_message="Offline retry fixture failed",
                retryable=True,
                times=2,
            )
        )
    )
    try:
        scan_run_id = _scan(database, count=1)
        with database.session() as session:
            frozen = AnalysisInputFreezer(session).freeze_top_candidates(
                scan_run_id=scan_run_id,
                limit=1,
            )[0]
            job, _ = ManualAnalysisService(session).request(
                ManualAnalysisRequest(
                    snapshot_id=frozen.snapshot_id,
                    correlation_id="manual-retry",
                    idempotency_key="manual-retry",
                    budget=AnalysisBudget(
                        max_candidates=1,
                        max_model_invocations=4,
                        max_retries=1,
                    ),
                    expected_provider=provider.identity,
                ),
                now=NOW,
            )
            job_id = job.id

        worker = ProviderAnalysisJobWorker(
            database,
            provider,
            worker_id="manual-retry-worker",
        )
        first = asyncio.run(worker.run_once(now=WORKER_NOW))
        assert first is not None
        assert first.state == "failed"

        with database.session() as session:
            service = ManualAnalysisService(session)
            allowance = service.retry_allowance(job_id)
            assert allowance.attempts_remaining == 1
            assert allowance.retries_remaining == 1
            assert allowance.model_invocations_used == 1
            retried = service.retry(
                job_id,
                now=WORKER_NOW + timedelta(seconds=1),
            )
            assert retried.state == "queued"

        second = asyncio.run(
            worker.run_once(now=WORKER_NOW + timedelta(seconds=1))
        )
        assert second is not None
        assert second.state == "failed"
        assert second.attempt_count == 2

        with database.session() as session:
            service = ManualAnalysisService(session)
            allowance = service.retry_allowance(job_id)
            assert allowance.attempts_remaining == 0
            assert allowance.retries_remaining == 0
            assert allowance.model_invocations_used == 2
            with pytest.raises(
                ManualAnalysisConflictError,
                match="retry budget",
            ):
                service.retry(
                    job_id,
                    now=WORKER_NOW + timedelta(seconds=2),
                )
    finally:
        database.close()


def test_fixed_analysis_corpus_can_be_rerun_listed_and_compared_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "analysis-history.db"
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    first_provider = FakeProvider()
    try:
        scan_run_id = _scan(database, count=1)
        with database.session() as session:
            frozen = AnalysisInputFreezer(session).freeze_top_candidates(
                scan_run_id=scan_run_id,
                limit=1,
            )[0]
            snapshot = session.get(
                OpportunitySnapshot,
                frozen.snapshot_id,
            )
            assert snapshot is not None
            opportunity_id = snapshot.opportunity_id
            first_job, _ = ManualAnalysisService(session).request(
                ManualAnalysisRequest(
                    snapshot_id=frozen.snapshot_id,
                    correlation_id="fixed-corpus-rerun",
                    idempotency_key="fixed-corpus-rerun-1",
                    budget=AnalysisBudget(max_retries=0),
                    expected_provider=first_provider.identity,
                ),
                now=NOW,
            )
            first_job_id = first_job.id

        first_completed = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                first_provider,
                worker_id="fixed-corpus-worker-1",
            ).run_once(now=WORKER_NOW)
        )
        assert first_completed is not None
        assert first_completed.state == "succeeded"
        with database.session() as session:
            first_version = AnalysisVersionService(
                session
            ).create_from_succeeded_job(
                job_id=first_job_id,
                now=NOW,
            )
            first_version_id = first_version.id
            changed_output = copy.deepcopy(first_version.structured_output)
            changed_output["problem_summary"] = (
                "The rerun identifies a deliberately changed bounded summary."
            )

        second_provider = FakeProvider(
            FakeProviderScript(analyze_output=changed_output)
        )
        with database.session() as session:
            second_job, _ = ManualAnalysisService(session).request(
                ManualAnalysisRequest(
                    snapshot_id=frozen.snapshot_id,
                    correlation_id="fixed-corpus-rerun",
                    idempotency_key="fixed-corpus-rerun-2",
                    budget=AnalysisBudget(max_retries=0),
                    expected_provider=second_provider.identity,
                ),
                now=NOW + timedelta(seconds=1),
            )
            second_job_id = second_job.id

        second_completed = asyncio.run(
            ProviderAnalysisJobWorker(
                database,
                second_provider,
                worker_id="fixed-corpus-worker-2",
            ).run_once(now=WORKER_NOW + timedelta(seconds=1))
        )
        assert second_completed is not None
        assert second_completed.state == "succeeded"
        with database.session() as session:
            second_version = AnalysisVersionService(
                session
            ).create_from_succeeded_job(
                job_id=second_job_id,
                now=NOW + timedelta(seconds=1),
            )
            second_version_id = second_version.id
    finally:
        database.close()

    restarted = Database(f"sqlite+pysqlite:///{path}")
    restarted.create_schema()
    try:
        with restarted.session() as session:
            history = AnalysisHistoryService(session)
            by_snapshot = history.list_for_snapshot(frozen.snapshot_id)
            by_opportunity = history.list_for_opportunity(opportunity_id)
            comparison = history.compare(
                first_version_id,
                second_version_id,
            )
            identical = history.compare(
                first_version_id,
                first_version_id,
            )

            assert [item.id for item in by_snapshot] == [
                second_version_id,
                first_version_id,
            ]
            assert [item.id for item in by_opportunity] == [
                second_version_id,
                first_version_id,
            ]
            assert all(
                item.opportunity_id == opportunity_id
                for item in by_opportunity
            )
            assert comparison.same_snapshot is True
            assert comparison.same_rule_score is True
            assert comparison.same_frozen_input is True
            problem_change = next(
                item
                for item in comparison.differences
                if item.path == "/analysis/problem_summary"
            )
            assert problem_change.left != problem_change.right
            assert (
                comparison.left["analysis"]["problem_summary"]
                == problem_change.left
            )
            assert (
                comparison.right["analysis"]["problem_summary"]
                == problem_change.right
            )
            assert identical.differences == ()

            with pytest.raises(AnalysisHistoryNotFoundError):
                history.compare("missing", second_version_id)
    finally:
        restarted.close()
