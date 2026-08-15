from __future__ import annotations

import asyncio
import gzip
import hashlib
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.archives import (
    REPOSITORY_ARCHIVE_JOB_KIND,
    RepositoryArchiveJobWorker,
    RepositoryArchiveService,
    RepositoryArchiveStore,
)
from app.config import Settings
from app.database import Database
from app.github import GitHubAPIError, GitHubClient
from app.jobs import JobService
from app.planning import ContributionTaskService
from app.plans import PlanVersionService
from app.worker import ContribOSWorker
from tests.test_github import TOKEN_CANARY
from tests.test_planning import _plan_content, _seed_analysis
from tests.test_runtime_wiring import SIGNING_KEY_HEX


BASE_SHA = "a" * 40
ARCHIVE_HOST = "codeload.github.test"
ARCHIVE_BYTES = gzip.compress(b"trusted-repository-archive")
ARCHIVE_HASH = hashlib.sha256(ARCHIVE_BYTES).hexdigest()


def _archive_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "github_api_url": "https://api.github.test",
        "github_allowed_hosts": ("api.github.test",),
        "github_archive_hosts": (ARCHIVE_HOST,),
        "github_token": TOKEN_CANARY,
        "github_max_retries": 0,
    }
    values.update(overrides)
    return Settings(**values)


def _gzip_handler(
    requests: list[httpx.Request],
    *,
    body: bytes = ARCHIVE_BYTES,
    redirect_host: str = ARCHIVE_HOST,
) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.github.test":
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        f"https://{redirect_host}/fixture/planning/"
                        f"legacy.tar.gz/{BASE_SHA}"
                    )
                },
                request=request,
            )
        return httpx.Response(200, content=body, request=request)

    return handler


def test_archive_hosts_are_configured_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ARCHIVE_HOSTS", "codeload.example.test")
    settings = Settings.from_env()
    assert settings.github_archive_hosts == ("codeload.example.test",)


def test_github_archive_download_follows_codeload_without_credentials(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    destination = tmp_path / "repository.tar.gz"

    async def run() -> int:
        async with GitHubClient(
            _archive_settings(),
            transport=httpx.MockTransport(_gzip_handler(requests)),
        ) as client:
            return await client.download_repository_archive(
                "fixture/planning",
                BASE_SHA,
                destination,
            )

    size = asyncio.run(run())

    assert size == len(ARCHIVE_BYTES)
    assert destination.read_bytes() == ARCHIVE_BYTES
    assert requests[0].url.host == "api.github.test"
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN_CANARY}"
    assert requests[1].url.host == ARCHIVE_HOST
    assert "authorization" not in {
        key.lower() for key in requests[1].headers
    }


def test_github_archive_download_rejects_unallowlisted_redirect(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    destination = tmp_path / "rejected.tar.gz"

    async def run() -> None:
        async with GitHubClient(
            _archive_settings(),
            transport=httpx.MockTransport(
                _gzip_handler(requests, redirect_host="evil.example")
            ),
        ) as client:
            await client.download_repository_archive(
                "fixture/planning",
                BASE_SHA,
                destination,
            )

    with pytest.raises(GitHubAPIError, match="archive redirect"):
        asyncio.run(run())

    assert len(requests) == 1
    assert not destination.exists()


def test_github_archive_download_does_not_echo_token_canary(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": f"upstream reflected {TOKEN_CANARY}"},
            request=request,
        )

    async def run() -> str:
        async with GitHubClient(
            _archive_settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(GitHubAPIError) as captured:
                await client.download_repository_archive(
                    "fixture/planning",
                    BASE_SHA,
                    tmp_path / "secret.tar.gz",
                )
            return str(captured.value)

    message = asyncio.run(run())
    assert TOKEN_CANARY not in message
    assert "HTTP 500" in message


def test_github_archive_download_rejects_oversized_payload(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "huge.tar.gz"
    requests: list[httpx.Request] = []
    oversized = gzip.compress(b"x" * 64)

    async def run() -> None:
        async with GitHubClient(
            _archive_settings(),
            transport=httpx.MockTransport(
                _gzip_handler(requests, body=oversized)
            ),
        ) as client:
            await client.download_repository_archive(
                "fixture/planning",
                BASE_SHA,
                destination,
                max_bytes=16,
            )

    with pytest.raises(GitHubAPIError, match="size limit"):
        asyncio.run(run())
    assert not destination.exists()


def test_repository_archive_store_is_content_addressed_and_lookupable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incoming.tar.gz"
    source.write_bytes(ARCHIVE_BYTES)
    store = RepositoryArchiveStore(tmp_path / "artifacts")

    first = store.put_file(source)
    second = store.put_file(source)
    found = store.lookup(
        first,
        repository_full_name="fixture/planning",
        base_commit_sha=BASE_SHA,
    )

    assert first == ARCHIVE_HASH
    assert second == ARCHIVE_HASH
    assert found is not None
    assert found.archive_hash == ARCHIVE_HASH
    assert found.size_bytes == len(ARCHIVE_BYTES)
    assert found.path.is_file()
    assert found.path.read_bytes() == ARCHIVE_BYTES
    assert store.lookup(
        "0" * 64,
        repository_full_name="fixture/planning",
        base_commit_sha=BASE_SHA,
    ) is None


def test_archive_job_worker_captures_without_extracting(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'archive-job.db'}")
    database.create_schema()
    artifact_root = tmp_path / "artifacts"
    settings = _archive_settings(artifact_root=str(artifact_root))
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(_gzip_handler(requests))

    with database.session() as session:
        job, created = RepositoryArchiveService(session).enqueue(
            plan_version_id="plan-1",
            plan_approval_id="approval-1",
            repository_full_name="fixture/planning",
            base_commit_sha=BASE_SHA,
            idempotency_key="archive-job-1",
        )
        assert created is True
        assert job.kind == REPOSITORY_ARCHIVE_JOB_KIND

    worker = RepositoryArchiveJobWorker(
        database,
        settings,
        lambda: GitHubClient(settings, transport=transport),
        worker_id="archive-worker",
    )
    completed = asyncio.run(worker.run_once())

    assert completed is not None
    assert completed.state == "succeeded"
    assert completed.result_data["archive_hash"] == ARCHIVE_HASH
    assert completed.result_data["size_bytes"] == len(ARCHIVE_BYTES)
    assert TOKEN_CANARY not in str(completed.result_data)
    stored = RepositoryArchiveStore(artifact_root).lookup(
        ARCHIVE_HASH,
        repository_full_name="fixture/planning",
        base_commit_sha=BASE_SHA,
    )
    assert stored is not None
    assert stored.path.read_bytes() == ARCHIVE_BYTES
    assert not any(artifact_root.rglob("*.extracted"))
    assert requests[1].url.host == ARCHIVE_HOST
    assert "authorization" not in {key.lower() for key in requests[1].headers}
    database.close()


def test_create_archive_api_is_exact_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "archive-api.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="archive-api-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="archive-api-plan",
            )
            plan_id = plan.id
    finally:
        database.close()

    app = create_app(Settings(database_url=f"sqlite+pysqlite:///{path}"))
    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={"base_commit_sha": BASE_SHA, "actor_id": "user-1"},
            headers={"Idempotency-Key": "archive-api-approval"},
        )
        created = client.post(
            f"/api/v1/plan-versions/{plan_id}/archives",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": BASE_SHA,
            },
            headers={"Idempotency-Key": "archive-api-create"},
        )
        replay = client.post(
            f"/api/v1/plan-versions/{plan_id}/archives",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": BASE_SHA,
            },
            headers={"Idempotency-Key": "archive-api-create"},
        )
        conflict = client.post(
            f"/api/v1/plan-versions/{plan_id}/archives",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": "b" * 40,
            },
            headers={"Idempotency-Key": "archive-api-create"},
        )
        missing = client.post(
            "/api/v1/plan-versions/missing-plan/archives",
            json={
                "approval_id": approval.json()["id"],
                "base_commit_sha": BASE_SHA,
            },
            headers={"Idempotency-Key": "archive-api-missing"},
        )

    assert created.status_code == 202
    assert replay.status_code == 202
    assert created.json()["id"] == replay.json()["id"]
    assert created.json()["kind"] == REPOSITORY_ARCHIVE_JOB_KIND
    assert created.json()["state"] == "queued"
    assert conflict.status_code == 409
    assert missing.status_code == 404

    database = Database(f"sqlite+pysqlite:///{path}")
    try:
        with database.session() as session:
            job = JobService(session).get(created.json()["id"])
            assert job.payload["repository_full_name"] == "fixture/planning"
            assert job.payload["base_commit_sha"] == BASE_SHA
    finally:
        database.close()


def test_execution_worker_uses_local_archive_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive-lookup.db"
    analysis_id, _, _ = _seed_analysis(path)
    database = Database(f"sqlite+pysqlite:///{path}")
    database.create_schema()
    try:
        with database.session() as session:
            task = ContributionTaskService(session).create(
                analysis_version_id=analysis_id,
                idempotency_key="archive-lookup-task",
            )
            plan = PlanVersionService(session).create_initial(
                task_id=task.id,
                content=_plan_content(),
                idempotency_key="archive-lookup-plan",
            )
            plan_id = plan.id
    finally:
        database.close()

    artifact_root = tmp_path / "artifacts"
    source = tmp_path / "incoming.tar.gz"
    source.write_bytes(ARCHIVE_BYTES)
    store = RepositoryArchiveStore(artifact_root)
    archive_hash = store.put_file(source)
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{path}",
        artifact_root=str(artifact_root),
        sandbox_job_spec_key_id="local-test-key",
        sandbox_job_spec_signing_key=bytes.fromhex(SIGNING_KEY_HEX),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        approval = client.post(
            f"/api/v1/plan-versions/{plan_id}/approve",
            json={"base_commit_sha": BASE_SHA, "actor_id": "user-1"},
            headers={"Idempotency-Key": "archive-lookup-approval"},
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
            headers={"Idempotency-Key": "archive-lookup-create"},
        )
        execution_id = created.json()["id"]

    worker = ContribOSWorker(
        app.state.database,
        settings,
        lambda: GitHubClient(settings),
        worker_id="archive-present-worker",
    )
    completed = asyncio.run(worker.run_once())
    assert completed is not None
    assert completed.state == "failed"
    assert completed.error_code == "explore_runtime_unavailable"

    with TestClient(app) as client:
        detail = client.get(f"/api/v1/executions/{execution_id}")
    assert detail.json()["current_stage"]["reason_code"] == (
        "explore_runtime_unavailable"
    )


def test_contribos_worker_leases_archive_jobs_before_execution(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'order.db'}",
        analysis_provider="fake",
        artifact_root=str(tmp_path / "artifacts"),
        sandbox_job_spec_signing_key=bytes.fromhex(SIGNING_KEY_HEX),
    )
    database = Database(settings.database_url)
    database.create_schema()
    worker = ContribOSWorker(
        database,
        settings,
        lambda: GitHubClient(settings),
        worker_id="order-worker",
    )
    assert worker.archives is not None
    assert worker.execution is not None
    database.close()
