from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api import create_app
from app.archives import RepositoryArchiveStore
from app.artifacts import ArtifactStore
from app.authorizations import UserAction
from app.coding import (
    CodingContextJobWorker,
    CodingContextService,
    CodingConversationService,
    NvidiaCodingJobWorker,
)
from app.config import Settings
from app.models import AgentInvocation, PublishIntent, ReviewRun
from app.providers import (
    NVIDIA_NIM_ADAPTER_VERSION,
    NVIDIA_NIM_MODEL_VERSION,
    NVIDIA_NIM_PROVIDER,
    NvidiaNimCompletion,
    ProviderIdentity,
)
from app.review_provider import NvidiaReviewJobWorker, ProviderReviewService
from app.sandbox_worker.coding_context import CodingContextEntry
from app.sandbox_worker.specs import JobSpecSigner
from app.task_states import ContributionTaskStateService
from app.worker import ContribOSWorker
from tests.test_execution_pipeline import (
    ARCHIVE_BYTES,
    BASE_SHA,
    _complete_verified_execution,
    _prepare_plan,
)
from tests.test_runtime_wiring import SIGNING_KEY_HEX


class FakeContextRuntime:
    async def capture(self, *, paths, **_):
        values = []
        for path in paths:
            content = f"original content for {path}\n"
            values.append(
                CodingContextEntry(
                    path=path,
                    prior_hash=hashlib.sha256(content.encode()).hexdigest(),
                    content=content,
                    executable=False,
                )
            )
        return tuple(values)


class ScriptedRunner:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.invocations = []

    async def complete(self, invocation):
        self.invocations.append(invocation)
        return NvidiaNimCompletion(
            content=self.contents.pop(0),
            input_tokens=100,
            cached_input_tokens=10,
            output_tokens=30,
            duration_ms=25,
        )


def _identity(model: str) -> ProviderIdentity:
    return ProviderIdentity(
        provider=NVIDIA_NIM_PROVIDER,
        adapter_version=NVIDIA_NIM_ADAPTER_VERSION,
        model=model,
        model_version=NVIDIA_NIM_MODEL_VERSION,
    )


def _explored_execution(tmp_path: Path):
    path = tmp_path / "coding.db"
    plan_id = _prepare_plan(path)
    artifact_root = tmp_path / "coding-artifacts"
    source = tmp_path / "coding.tar.gz"
    source.write_bytes(ARCHIVE_BYTES)
    archive_hash = RepositoryArchiveStore(artifact_root).put_file(source)
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{path}",
        artifact_root=str(artifact_root),
        sandbox_job_spec_key_id="local-test-key",
        sandbox_job_spec_signing_key=bytes.fromhex(SIGNING_KEY_HEX),
        sandbox_stage_runtime="fake",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={"base_commit_sha": BASE_SHA, "actor_id": "user-1"},
            headers={"Idempotency-Key": "coding-approval"},
        ).json()
        execution = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                "approval_id": approval["id"],
                "base_commit_sha": BASE_SHA,
                "repository_archive_hash": archive_hash,
                "runner_image_digest": "sha256:" + "c" * 64,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "coding-execution"},
        ).json()
    worker = ContribOSWorker(
        app.state.database,
        settings,
        lambda: None,
        worker_id="coding-explore-worker",
    )
    assert asyncio.run(worker.run_once()).state == "succeeded"
    return app, settings, execution["id"]


def test_nvidia_multi_turn_proposal_requires_exact_user_confirmation(
    tmp_path: Path,
) -> None:
    app, settings, execution_id = _explored_execution(tmp_path)
    with app.state.database.session() as session:
        context_job, created = CodingContextService(session).enqueue(
            execution_id,
            idempotency_key="coding-context",
        )
        assert created is True
    completed_context = asyncio.run(
        CodingContextJobWorker(
            app.state.database,
            artifact_root=settings.artifact_root,
            runtime=FakeContextRuntime(),
            worker_id="context-worker",
        ).run_once()
    )
    assert completed_context.id == context_job.id
    assert completed_context.state == "succeeded"

    with app.state.database.session() as session:
        conversation = CodingConversationService(
            session, artifact_root=settings.artifact_root
        )
        message_job, created = conversation.send_message(
            execution_id,
            content="请最小化修改 app/plans.py，并保留现有 API。",
            idempotency_key="coding-message-1",
        )
        assert created is True

    runner = ScriptedRunner(
        json.dumps(
            {
                "reply": (
                    "我会只改 app/plans.py，先保持接口不变并补齐边界处理。"
                )
            },
            ensure_ascii=False,
        )
    )
    coding_worker = NvidiaCodingJobWorker(
        app.state.database,
        artifact_root=settings.artifact_root,
        runner=runner,
        identity=_identity("deepseek-ai/deepseek-v4-pro-0813"),
        worker_id="nvidia-coding-worker",
    )
    completed_message = asyncio.run(coding_worker.run_once())
    assert completed_message.id == message_job.id
    assert completed_message.state == "succeeded"

    with app.state.database.session() as session:
        conversation = CodingConversationService(
            session, artifact_root=settings.artifact_root
        )
        coding_session = conversation.require_session(execution_id)
        turns = conversation.turns(coding_session.id)
        assert [turn.role for turn in turns] == ["user", "assistant"]
        context = ArtifactStore(session, settings.artifact_root).read_bytes(
            coding_session.context_artifact_id
        )
        context_value = json.loads(context)
        prior_hash = next(
            item["prior_hash"]
            for item in context_value["entries"]
            if item["path"] == "app/plans.py"
        )
        proposal_job, created = conversation.request_proposal(
            execution_id,
            idempotency_key="coding-proposal-1",
        )
        assert created is True

    runner.contents.append(
        json.dumps(
            {
                "summary": "最小化调整计划校验",
                "operations": [
                    {
                        "path": "app/plans.py",
                        "kind": "write",
                        "expected_prior_hash": prior_hash,
                        "content": "def preserved_api():\n    return True\n",
                        "executable": False,
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    completed_proposal = asyncio.run(coding_worker.run_once())
    assert completed_proposal.id == proposal_job.id
    assert completed_proposal.state == "succeeded"
    proposal_id = completed_proposal.result_data["change_set_proposal_id"]
    proposal_hash = completed_proposal.result_data["change_set_hash"]

    signer = JobSpecSigner(
        key_id="local-test-key",
        signing_key=bytes.fromhex(SIGNING_KEY_HEX),
    )
    with app.state.database.session() as session:
        conversation = CodingConversationService(
            session, artifact_root=settings.artifact_root
        )
        try:
            conversation.accept_proposal(
                proposal_id,
                action=UserAction.ACCEPT_CHANGE_SET,
                expected_change_set_hash="0" * 64,
                idempotency_key="accept-wrong-hash",
                signer=signer,
            )
        except Exception as exc:
            assert "does not match" in str(exc)
        else:
            raise AssertionError("mismatched proposal hash was accepted")

        accepted = conversation.accept_proposal(
            proposal_id,
            action=UserAction.ACCEPT_CHANGE_SET,
            expected_change_set_hash=proposal_hash,
            idempotency_key="accept-exact-hash",
            signer=signer,
        )
        assert accepted.change_set_hash == proposal_hash
    with app.state.database.session() as session:
        from app.executions import ExecutionAttemptService

        stage = ExecutionAttemptService(session).current(execution_id)
        assert stage.stage == "implement"
        assert stage.input_hashes[1] == proposal_hash


def test_kimi_review_creates_review_bound_to_provider_invocation(
    tmp_path: Path,
) -> None:
    app, execution_id, task_id, _ = _complete_verified_execution(
        tmp_path,
        key_prefix="nvidia-review",
    )
    identity = _identity("minimaxai/minimax-m3")
    with app.state.database.session() as session:
        job, created = ProviderReviewService(
            session,
            artifacts=ArtifactStore(session, app.state.settings.artifact_root),
        ).enqueue(
            execution_id,
            action=UserAction.START_REVIEW,
            actor_id="user-1",
            expected_provider=identity,
            idempotency_key="nvidia-review-job",
        )
        assert created is True

    runner = ScriptedRunner(
        json.dumps(
            {
                "verdict": "pass",
                "reason_code": "review_passed",
                "findings": [
                    {
                        "severity": "info",
                        "location": "tests",
                        "evidence": "规范化测试产物显示所有批准命令通过。",
                        "recommendation": "保持当前精确 diff 与测试绑定。",
                        "verdict": "pass",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    completed = asyncio.run(
        NvidiaReviewJobWorker(
            app.state.database,
            artifact_root=app.state.settings.artifact_root,
            runner=runner,
            identity=identity,
            worker_id="nvidia-review-worker",
        ).run_once()
    )
    assert completed.id == job.id
    assert completed.state == "succeeded"

    with app.state.database.session() as session:
        review = session.scalar(
            select(ReviewRun).where(
                ReviewRun.execution_attempt_id == execution_id
            )
        )
        invocation = session.get(
            AgentInvocation, completed.result_data["agent_invocation_id"]
        )
        assert review is not None
        assert invocation is not None
        assert review.reviewer_kind == "nvidia_nim"
        assert review.reviewer_invocation_id == invocation.id
        assert review.diff_hash != review.verify_result_hash
        assert review.verdict == "pass"
        assert invocation.model_name == "minimaxai/minimax-m3"
        assert ContributionTaskStateService(session).current(task_id).to_state == "ready"

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/reviews/{review.id}/publish-intents",
            json={
                "actor_id": "user-1",
                "title": "Preserve exact NVIDIA review",
                "body": "Migration recovery must retain this immutable intent.",
            },
            headers={"Idempotency-Key": "nvidia-review-intent"},
        )
        assert response.status_code == 201
        intent_id = response.json()["id"]
        review_id = review.id

    with app.state.database.engine.begin() as connection:
        from tests.migration_fixtures import remove_task_visibility_schema
        remove_task_visibility_schema(connection)
        connection.execute(text("DROP TABLE job_model_bindings"))
        connection.execute(text("DROP TABLE model_config_versions"))
        connection.execute(text("DELETE FROM _schema_migrations WHERE revision = '0030_model_settings'"))
        connection.execute(text("DROP TABLE workbench_events"))
        connection.execute(text("DELETE FROM _schema_migrations WHERE revision = '0029_workbench'"))
        connection.execute(
            text(
                "DELETE FROM _schema_migrations "
                "WHERE revision = '0028_nvidia_review_runs'"
            )
        )
    report = app.state.database.create_schema()
    assert report.applied == (
        "0028_nvidia_review_runs",
        "0029_workbench",
        "0030_model_settings",
        "0031_task_visibility",
        "0032_minimax_reviews",
    )
    with app.state.database.session() as session:
        assert session.get(ReviewRun, review_id) is not None
        intent = session.get(PublishIntent, intent_id)
        assert intent is not None
        assert intent.review_run_id == review_id
    with app.state.database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_malformed_nvidia_review_fails_closed_without_business_state(
    tmp_path: Path,
) -> None:
    app, execution_id, task_id, _ = _complete_verified_execution(
        tmp_path,
        key_prefix="nvidia-review-malformed",
    )
    identity = _identity("minimaxai/minimax-m3")
    with app.state.database.session() as session:
        job, created = ProviderReviewService(
            session,
            artifacts=ArtifactStore(session, app.state.settings.artifact_root),
        ).enqueue(
            execution_id,
            action=UserAction.START_REVIEW,
            actor_id="user-1",
            expected_provider=identity,
            idempotency_key="nvidia-review-malformed-job",
        )
        assert created is True

    completed = asyncio.run(
        NvidiaReviewJobWorker(
            app.state.database,
            artifact_root=app.state.settings.artifact_root,
            runner=ScriptedRunner('{"verdict":"pass","findings":[]}'),
            identity=identity,
            worker_id="nvidia-review-malformed-worker",
        ).run_once()
    )
    assert completed.id == job.id
    assert completed.state == "failed"
    assert completed.error_code == "nvidia_review_failed"
    assert completed.error_message == "NVIDIA Review failed validation or execution"
    with app.state.database.session() as session:
        assert session.scalar(
            select(AgentInvocation).where(AgentInvocation.job_id == job.id)
        ) is None
        assert session.scalar(
            select(ReviewRun).where(
                ReviewRun.execution_attempt_id == execution_id
            )
        ) is None
        assert (
            ContributionTaskStateService(session).current(task_id).to_state
            == "executing"
        )
