from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.github import GitHubClient
from app.providers import (
    AnalysisAvailabilityMode,
    AnalysisBudget,
    FakeProvider,
    resolve_analysis_runtime,
)
from app.providers.nvidia_nim import NVIDIA_ANALYSIS_JOB_TIMEOUT_SECONDS
from app.sandbox_worker.runtime import resolve_stage_runtimes
from app.sandbox_worker.fake import FakeExploreRuntime
from app.sandbox_worker.specs import JobSpecSigner
from app.worker import ContribOSWorker, DiscoveryJobWorker
from tests.test_planning import _plan_content, _seed_analysis
from app.planning import ContributionTaskService
from app.plans import PlanVersionService


SIGNING_KEY_HEX = "ab" * 32


def test_default_settings_keep_analysis_and_signing_unconfigured() -> None:
    settings = Settings.from_env()

    assert settings.analysis_provider == "none"
    assert settings.sandbox_job_spec_signing_key is None
    assert settings.sandbox_stage_runtime == "none"


def test_env_can_enable_fake_analysis_and_job_spec_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANALYSIS_PROVIDER", "fake")
    monkeypatch.setenv("SANDBOX_JOB_SPEC_KEY_ID", "local-test-key")
    monkeypatch.setenv("SANDBOX_JOB_SPEC_SIGNING_KEY", SIGNING_KEY_HEX)

    settings = Settings.from_env()

    assert settings.analysis_provider == "fake"
    assert settings.sandbox_job_spec_key_id == "local-test-key"
    assert settings.sandbox_job_spec_signing_key == bytes.fromhex(SIGNING_KEY_HEX)


def test_api_can_report_discovery_token_capability_without_receiving_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_DISCOVERY_TOKEN_CONFIGURED", "true")
    settings = Settings.from_env()
    assert settings.github_token is None
    assert settings.github_discovery_token_configured is True

    app = create_app(
        Settings(
            database_url=(
                f"sqlite+pysqlite:///{tmp_path / 'token-capability.db'}"
            ),
            github_discovery_token_configured=True,
        )
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/meta").json()["token_configured"] is True


@pytest.mark.parametrize("value", ("unknown", "FAKE"))
def test_unsupported_stage_runtime_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("SANDBOX_STAGE_RUNTIME", value)

    with pytest.raises(ValueError, match="SANDBOX_STAGE_RUNTIME"):
        Settings.from_env()


@pytest.mark.parametrize("value", ("codex", "unknown", "FAKE"))
def test_unsupported_analysis_provider_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("ANALYSIS_PROVIDER", value)

    with pytest.raises(ValueError, match="ANALYSIS_PROVIDER"):
        Settings.from_env()


def test_resolve_stage_runtimes_stay_none_until_fake_is_selected() -> None:
    assert resolve_stage_runtimes(Settings()) is None
    runtimes = resolve_stage_runtimes(Settings(sandbox_stage_runtime="fake"))
    assert runtimes is not None
    assert isinstance(runtimes.explore, FakeExploreRuntime)


def test_resolve_analysis_runtime_is_none_until_fake_is_selected() -> None:
    provider, budget = resolve_analysis_runtime(Settings())
    assert provider is None
    assert budget is None

    provider, budget = resolve_analysis_runtime(
        Settings(analysis_provider="fake")
    )
    assert isinstance(provider, FakeProvider)
    assert isinstance(budget, AnalysisBudget)
    assert provider.identity.provider == "fake"

    provider, budget = resolve_analysis_runtime(
        Settings(analysis_provider="nvidia_nim")
    )
    assert provider is not None
    assert budget is not None
    assert budget.max_duration_ms == NVIDIA_ANALYSIS_JOB_TIMEOUT_SECONDS * 5_000


def test_create_app_wires_fake_provider_from_settings(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'wired-provider.db'}"
    default_app = create_app(Settings(database_url=database_url))
    wired_app = create_app(
        Settings(database_url=database_url, analysis_provider="fake")
    )

    assert default_app.state.analysis_provider is None
    assert default_app.state.analysis_budget is None
    assert isinstance(wired_app.state.analysis_provider, FakeProvider)
    assert isinstance(wired_app.state.analysis_budget, AnalysisBudget)

    with TestClient(wired_app) as client:
        board = client.get("/api/v1/opportunities/daily").json()

    assert board["analysis"]["mode"] == AnalysisAvailabilityMode.PROVIDER_READY
    assert board["analysis"]["automatic_model_invocation_enabled"] is True
    assert board["analysis"]["fallback_reasons"] == []


def test_contribos_worker_processes_discovery_and_analysis_jobs(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'unified-worker.db'}")
    database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        analysis_provider="fake",
        github_queries=("offline-query",),
    )
    worker = ContribOSWorker(
        database,
        settings,
        lambda: GitHubClient(settings),
        worker_id="unified-worker",
    )

    assert isinstance(worker.discovery, DiscoveryJobWorker)
    assert worker.analysis is not None
    assert worker.analysis.provider.identity.provider == "fake"
    assert asyncio.run(worker.run_once()) is None
    database.close()


def test_create_execution_schedules_explore_when_signer_is_configured(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-schedule-api.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="wired-execution-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="wired-execution-plan",
            )
            plan_id = plan.id
    finally:
        database.close()

    app = create_app(
        Settings(
            database_url=f"sqlite+pysqlite:///{path}",
            sandbox_job_spec_key_id="local-test-key",
            sandbox_job_spec_signing_key=bytes.fromhex(SIGNING_KEY_HEX),
        )
    )
    assert isinstance(app.state.job_spec_signer, JobSpecSigner)

    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={"base_commit_sha": "a" * 40, "actor_id": "user-1"},
            headers={"Idempotency-Key": "wired-execution-approval"},
        )
        created = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": "a" * 40,
                "repository_archive_hash": "b" * 64,
                "runner_image_digest": "sha256:" + "c" * 64,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "wired-execution-create"},
        )
        replay = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": "a" * 40,
                "repository_archive_hash": "b" * 64,
                "runner_image_digest": "sha256:" + "c" * 64,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "wired-execution-create"},
        )
        detail = client.get(f"/api/v1/executions/{created.json()['id']}")

    assert created.status_code == 201
    assert replay.status_code == 201
    assert created.json()["id"] == replay.json()["id"]
    assert detail.status_code == 200
    runs = detail.json()["stage_runs"]
    assert len(runs) == 1
    assert runs[0]["stage"] == "explore"
    assert runs[0]["job"]["state"] == "queued"


def test_execution_worker_fails_closed_when_archive_is_unavailable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-worker-archive.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="wired-archive-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="wired-archive-plan",
            )
            plan_id = plan.id
    finally:
        database.close()

    settings = Settings(
        database_url=f"sqlite+pysqlite:///{path}",
        sandbox_job_spec_key_id="local-test-key",
        sandbox_job_spec_signing_key=bytes.fromhex(SIGNING_KEY_HEX),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={"base_commit_sha": "a" * 40, "actor_id": "user-1"},
            headers={"Idempotency-Key": "wired-archive-approval"},
        )
        created = client.post(
            f"/api/v1/plan-versions/{plan_id}/executions",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": "a" * 40,
                "repository_archive_hash": "b" * 64,
                "runner_image_digest": "sha256:" + "c" * 64,
                "actor_id": "user-1",
            },
            headers={"Idempotency-Key": "wired-archive-create"},
        )
        execution_id = created.json()["id"]

    worker = ContribOSWorker(
        app.state.database,
        settings,
        lambda: GitHubClient(settings),
        worker_id="archive-missing-worker",
    )
    assert worker.execution is not None
    completed = asyncio.run(worker.run_once())
    assert completed is not None
    assert completed.state == "failed"
    assert completed.error_code == "repository_archive_unavailable"

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/executions/{execution_id}")
    assert detail.status_code == 200
    assert detail.json()["current_stage"]["status"] == "failed"
    assert (
        detail.json()["current_stage"]["reason_code"]
        == "repository_archive_unavailable"
    )
