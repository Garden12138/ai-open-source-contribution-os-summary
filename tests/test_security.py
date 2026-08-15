from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.artifacts import ArtifactStore
from app.audit import AuditService
from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.models import Artifact, AuditEvent, Job
from app.security import REDACTED, SensitiveDataError, redact_text
from app.worker import DiscoveryJobWorker


TOKEN_CANARY = "github_pat_SECURITY_CANARY_123456"


class CanaryFailingGitHub:
    rate_limit_remaining = None
    rate_limit_reset_at = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        raise RuntimeError(f"upstream reflected {TOKEN_CANARY}")

    async def get_repository_bundle(self, full_name: str) -> dict[str, Any]:
        return {}


def test_redact_text_removes_explicit_and_credential_shaped_values() -> None:
    assert (
        redact_text(
            f"failure token={TOKEN_CANARY}",
            secrets=(TOKEN_CANARY,),
        )
        == f"failure token={REDACTED}"
    )
    assert TOKEN_CANARY not in redact_text(f"failure {TOKEN_CANARY}")
    assert "secret-value" not in redact_text(
        "Authorization: Bearer secret-value-123456"
    )
    assert "query-secret" not in redact_text(
        "https://example.test?access_token=query-secret"
    )


def test_persistence_boundaries_reject_secret_content(tmp_path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'boundaries.db'}")
    database.create_schema()
    artifact_root = tmp_path / "artifacts"
    try:
        with database.session() as session:
            with pytest.raises(SensitiveDataError):
                JobService(session).enqueue(
                    kind="test",
                    idempotency_key="secret-job",
                    payload={"credential": TOKEN_CANARY},
                )
            assert session.scalar(select(func.count()).select_from(Job)) == 0

            with pytest.raises(SensitiveDataError):
                ArtifactStore(
                    session,
                    artifact_root,
                    secrets=(TOKEN_CANARY,),
                ).store_bytes(f"result={TOKEN_CANARY}".encode())
            assert session.scalar(select(func.count()).select_from(Artifact)) == 0

            with pytest.raises(SensitiveDataError):
                AuditService(
                    session,
                    secrets=(TOKEN_CANARY,),
                ).append(
                    event_type="security.test",
                    actor_type="system",
                    actor_id="test",
                    correlation_id="canary",
                    payload={"credential": TOKEN_CANARY},
                )
            assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0

        assert not list(artifact_root.rglob("*")) if artifact_root.exists() else True
    finally:
        database.close()


def test_token_canary_never_reaches_response_database_or_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_path = tmp_path / "canary-api.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(
            database_url=database_url,
            github_token=TOKEN_CANARY,
            github_queries=("offline-query",),
        )
    )
    app.state.github_client_factory = CanaryFailingGitHub

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scans",
            json={},
            headers={"Idempotency-Key": "canary-failure"},
        )
        worker = DiscoveryJobWorker(
            app.state.database,
            app.state.settings,
            app.state.github_client_factory,
            worker_id="canary-worker",
        )
        completed = asyncio.run(worker.run_once())
        job_response = client.get(f"/api/v1/jobs/{created.json()['id']}")
        scan_response = client.get("/api/v1/scans")

        response_text = "\n".join(
            (created.text, job_response.text, scan_response.text)
        )
        assert completed is not None
        assert completed.state == "failed"
        assert TOKEN_CANARY not in response_text

        with app.state.database.engine.connect() as connection:
            raw = connection.connection.driver_connection
            database_dump = "\n".join(raw.iterdump())

    assert TOKEN_CANARY not in database_dump
    assert TOKEN_CANARY not in caplog.text
