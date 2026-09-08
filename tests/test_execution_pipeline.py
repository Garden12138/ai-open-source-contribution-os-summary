from __future__ import annotations

import asyncio
import gzip
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.archives import RepositoryArchiveStore
from app.authorizations import UserAction
from app.config import Settings
from app.database import Database
from app.planning import ContributionTaskService
from app.plans import PlanVersionService
from app.reviews import ReviewConflictError, ReviewRunService
from app.worker import ContribOSWorker
from tests.test_planning import _plan_content, _seed_analysis
from tests.test_runtime_wiring import SIGNING_KEY_HEX


BASE_SHA = "a" * 40
ARCHIVE_BYTES = gzip.compress(b"trusted-repository-archive")


def _prepare_plan(path: Path) -> str:
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="pipeline-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="pipeline-plan",
            )
            return plan.id
    finally:
        database.close()


def test_fake_runtime_completes_explore_implement_and_verify(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pipeline.db"
    plan_id = _prepare_plan(path)
    artifact_root = tmp_path / "artifacts"
    source = tmp_path / "incoming.tar.gz"
    source.write_bytes(ARCHIVE_BYTES)
    archive_hash = RepositoryArchiveStore(artifact_root).put_file(source)
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{path}",
        artifact_root=str(artifact_root),
        sandbox_job_spec_key_id="local-test-key",
        sandbox_job_spec_signing_key=bytes.fromhex(SIGNING_KEY_HEX),
        sandbox_stage_runtime="fake",
        publisher_mode="fake",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={"base_commit_sha": BASE_SHA, "actor_id": "user-1"},
            headers={"Idempotency-Key": "pipeline-approval"},
        )
        created = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": BASE_SHA,
                "repository_archive_hash": archive_hash,
                "runner_image_digest": "sha256:" + "c" * 64,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "pipeline-execution"},
        )
        execution_id = created.json()["id"]

    worker = ContribOSWorker(
        app.state.database,
        settings,
        lambda: None,
        worker_id="pipeline-worker",
    )
    explored = asyncio.run(worker.run_once())
    assert explored is not None
    assert explored.state == "succeeded"
    assert explored.error_code is None

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/executions/{execution_id}")
        assert detail.json()["current_stage"]["stage"] == "explore"
        assert detail.json()["current_stage"]["status"] == "succeeded"
        accepted = client.post(
            f"/api/v1/executions/{execution_id}/change-sets",
            json={"source": "fake"},
            headers={"Idempotency-Key": "pipeline-changeset"},
        )
        replay = client.post(
            f"/api/v1/executions/{execution_id}/change-sets",
            json={"source": "fake"},
            headers={"Idempotency-Key": "pipeline-changeset"},
        )
        assert accepted.status_code == 201
        assert replay.status_code == 201
        assert accepted.json()["change_set_hash"] == replay.json()["change_set_hash"]
        assert accepted.json()["paths"] == ["app/plans.py"]
        queued = client.get(f"/api/v1/executions/{execution_id}")
        assert queued.json()["current_stage"]["stage"] == "implement"
        assert queued.json()["current_stage"]["status"] == "pending"

    implemented = asyncio.run(worker.run_once())
    assert implemented is not None
    assert implemented.state == "succeeded"

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/executions/{execution_id}")
        assert detail.json()["current_stage"]["stage"] == "verify"
        assert detail.json()["current_stage"]["status"] == "pending"

    verified = asyncio.run(worker.run_once())
    assert verified is not None
    assert verified.state == "succeeded"

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/executions/{execution_id}")
        body = detail.json()
        assert body["current_stage"]["stage"] == "verify"
        assert body["current_stage"]["status"] == "succeeded"
        assert body["current_stage"]["reason_code"] == "verify_succeeded"
        assert len(body["artifact_manifests"]) == 3
        assert body["reviews"] == []


def _complete_verified_execution(tmp_path: Path, *, key_prefix: str = "pub"):
    path = tmp_path / f"{key_prefix}.db"
    plan_id = _prepare_plan(path)
    artifact_root = tmp_path / f"{key_prefix}-artifacts"
    source = tmp_path / f"{key_prefix}.tar.gz"
    source.write_bytes(ARCHIVE_BYTES)
    archive_hash = RepositoryArchiveStore(artifact_root).put_file(source)
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{path}",
        artifact_root=str(artifact_root),
        sandbox_job_spec_key_id="local-test-key",
        sandbox_job_spec_signing_key=bytes.fromhex(SIGNING_KEY_HEX),
        sandbox_stage_runtime="fake",
        publisher_mode="fake",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={"base_commit_sha": BASE_SHA, "actor_id": "user-1"},
            headers={"Idempotency-Key": f"{key_prefix}-approval"},
        )
        created = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": BASE_SHA,
                "repository_archive_hash": archive_hash,
                "runner_image_digest": "sha256:" + "c" * 64,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": f"{key_prefix}-execution"},
        )
        execution_id = created.json()["id"]
        task_id = created.json()["task_id"]
    worker = ContribOSWorker(
        app.state.database,
        settings,
        lambda: None,
        worker_id=f"{key_prefix}-worker",
    )
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with TestClient(app) as client:
        accepted = client.post(
            f"/api/v1/executions/{execution_id}/change-sets",
            json={"source": "fake"},
            headers={"Idempotency-Key": f"{key_prefix}-changeset"},
        )
        assert accepted.status_code == 201
    assert asyncio.run(worker.run_once()).state == "succeeded"
    assert asyncio.run(worker.run_once()).state == "succeeded"
    return app, execution_id, task_id, worker


def test_review_service_requires_verified_artifact_storage(
    tmp_path: Path,
) -> None:
    app, execution_id, _task_id, _worker = _complete_verified_execution(
        tmp_path,
        key_prefix="review-artifact-store",
    )
    with app.state.database.session() as session:
        with pytest.raises(
            ReviewConflictError,
            match="requires verified artifact storage",
        ):
            ReviewRunService(session).create(
                execution_attempt_id=execution_id,
                action=UserAction.START_REVIEW,
                idempotency_key="review-without-artifact-store",
                actor_type="local_user",
                actor_id="user-1",
            )


def test_fake_review_publish_events_and_dashboard(tmp_path: Path) -> None:
    app, execution_id, task_id, _worker = _complete_verified_execution(tmp_path)
    with TestClient(app) as client:
        blocked = client.post(
            f"/api/v1/executions/{execution_id}/reviews",
            json={"actor_id": "user-1", "reviewer": "fake_blocking"},
            headers={"Idempotency-Key": "review-block"},
        )
        assert blocked.status_code == 201
        assert blocked.json()["verdict"] == "block"
        assert blocked.json()["stale"] is False
        task = client.get(f"/api/v1/tasks/{task_id}")
        assert task.json()["current_state"]["to_state"] == "reviewing"
        forbidden = client.post(
            f"/api/v1/reviews/{blocked.json()['id']}/publish-intents",
            json={
                "actor_id": "user-1",
                "title": "should not publish",
                "body": "blocked review cannot create an intent",
            },
            headers={"Idempotency-Key": "intent-blocked"},
        )
        assert forbidden.status_code == 409
        repaired = client.post(
            f"/api/v1/reviews/{blocked.json()['id']}/repair",
            json={"actor_id": "user-1"},
            headers={"Idempotency-Key": "repair-1"},
        )
        assert repaired.status_code == 201
        assert repaired.json()["attempt_number"] == 2
        repair_id = repaired.json()["id"]

    worker = ContribOSWorker(
        app.state.database,
        app.state.settings,
        lambda: None,
        worker_id="repair-worker",
    )
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with TestClient(app) as client:
        accepted = client.post(
            f"/api/v1/executions/{repair_id}/change-sets",
            json={"source": "fake"},
            headers={"Idempotency-Key": "repair-changeset"},
        )
        assert accepted.status_code == 201
    assert asyncio.run(worker.run_once()).state == "succeeded"
    assert asyncio.run(worker.run_once()).state == "succeeded"

    with TestClient(app) as client:
        passed = client.post(
            f"/api/v1/executions/{repair_id}/reviews",
            json={"actor_id": "user-1", "reviewer": "fake"},
            headers={"Idempotency-Key": "review-pass"},
        )
        assert passed.status_code == 201
        assert passed.json()["verdict"] == "pass"
        assert passed.json()["stale"] is False
        execution = client.get(f"/api/v1/executions/{repair_id}")
        verify_manifest = next(
            item
            for item in execution.json()["artifact_manifests"]
            if item["stage"] == "verify"
        )
        test_results_artifact = next(
            item
            for item in verify_manifest["entries"]
            if item["role"] == "normalized-test-results"
        )
        assert (
            passed.json()["test_results_hash"]
            == test_results_artifact["artifact_id"]
        )
        assert (
            passed.json()["test_results_hash"]
            != passed.json()["verify_result_hash"]
        )
        implement_manifest = next(
            item
            for item in execution.json()["artifact_manifests"]
            if item["stage"] == "implement"
        )
        diff_artifact = next(
            item
            for item in implement_manifest["entries"]
            if item["role"] == "unified-diff"
        )
        assert passed.json()["diff_hash"] == diff_artifact["artifact_id"]
        assert passed.json()["diff_hash"] != implement_manifest["result_hash"]
        fetched = client.get(f"/api/v1/reviews/{passed.json()['id']}")
        assert fetched.json()["binding_hash"] == passed.json()["binding_hash"]
        task = client.get(f"/api/v1/tasks/{task_id}")
        assert task.json()["current_state"]["to_state"] == "ready"
        intent = client.post(
            f"/api/v1/reviews/{passed.json()['id']}/publish-intents",
            json={
                "actor_id": "user-1",
                "title": "Apply reviewed change",
                "body": "Exact reviewed diff and tests.",
            },
            headers={"Idempotency-Key": "intent-1"},
        )
        assert intent.status_code == 201
        assert intent.json()["status"] == "pending"
        assert intent.json()["allowed_actions"] == ["create_draft_pr"]
        missing_nonce = client.post(
            f"/api/v1/publish-intents/{intent.json()['id']}/confirm",
            json={"actor_id": "user-1", "confirmation_nonce": "0" * 64},
        )
        assert missing_nonce.status_code == 403
        confirmed = client.post(
            f"/api/v1/publish-intents/{intent.json()['id']}/confirm",
            json={
                "actor_id": "user-1",
                "confirmation_nonce": intent.json()["confirmation_nonce"],
            },
        )
        assert confirmed.status_code == 200
        draft_id = confirmed.json()["draft_pull_request"]["id"]
        assert confirmed.json()["draft_pull_request"]["provider"] == "fake"
        assert confirmed.json()["draft_pull_request"]["html_url"].startswith(
            "https://local.contribos.invalid/"
        )
        replay_confirm = client.post(
            f"/api/v1/publish-intents/{intent.json()['id']}/confirm",
            json={
                "actor_id": "user-1",
                "confirmation_nonce": intent.json()["confirmation_nonce"],
            },
        )
        assert replay_confirm.status_code == 409
        opened = client.post(
            f"/api/v1/draft-pull-requests/{draft_id}/events",
            json={
                "remote_event_id": "evt-open",
                "event_type": "opened",
                "payload": {"state": "open"},
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "evt-open"},
        )
        replay_opened = client.post(
            f"/api/v1/draft-pull-requests/{draft_id}/events",
            json={
                "remote_event_id": "evt-open",
                "event_type": "opened",
                "payload": {"state": "open"},
                "actor_id": "user-1",
            },
        )
        assert opened.status_code == 201
        assert replay_opened.status_code == 201
        assert opened.json()["id"] == replay_opened.json()["id"]
        requested = client.post(
            f"/api/v1/draft-pull-requests/{draft_id}/events",
            json={
                "remote_event_id": "evt-changes",
                "event_type": "changes_requested",
                "payload": {"review": "please revise"},
                "actor_id": "user-1",
            },
        )
        assert requested.status_code == 201
        task = client.get(f"/api/v1/tasks/{task_id}")
        assert task.json()["current_state"]["to_state"] == "changes_requested"
        merged = client.post(
            f"/api/v1/draft-pull-requests/{draft_id}/events",
            json={
                "remote_event_id": "evt-merged",
                "event_type": "merged",
                "payload": {"merged": True},
                "actor_id": "user-1",
            },
        )
        assert merged.status_code == 201
        task = client.get(f"/api/v1/tasks/{task_id}")
        assert task.json()["current_state"]["to_state"] == "changes_requested"
        revised = client.post(
            f"/api/v1/tasks/{task_id}/lifecycle",
            json={"action": "revise", "actor_id": "user-1"},
        )
        assert revised.status_code == 200
        assert revised.json()["to_state"] == "planning"
        dashboard = client.get("/api/v1/contributions/dashboard")
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["funnel"]["executed"] >= 2
        assert body["funnel"]["reviewed"] >= 2
        assert body["funnel"]["submitted"] == 1
        assert body["metrics"]["pull_request_event_count"] == 3
        tasks = client.get("/api/v1/tasks")
        assert tasks.status_code == 200
        assert any(item["id"] == task_id for item in tasks.json())
