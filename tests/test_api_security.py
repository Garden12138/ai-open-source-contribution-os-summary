from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.cli import build_parser
from app.config import Settings


LOCAL_TOKEN = "local-access-token-for-tests"
SECRET_CANARY = "github_pat_API_ERROR_CANARY_123456"


def test_mutations_require_local_token_and_browser_csrf(
    tmp_path: Path, monkeypatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'access.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(
            database_url=database_url,
            local_access_token=LOCAL_TOKEN,
            github_queries=("offline-query",),
        )
    )

    with TestClient(app, base_url="http://testserver") as client:
        meta = client.get("/api/v1/meta")
        csrf = meta.json()["csrf_token"]

        no_token = client.post("/api/v1/scans", json={})
        no_token_analysis = client.post(
            "/api/v1/opportunities/1/analyses",
            json={"snapshot_id": "snapshot-1"},
        )
        no_token_task = client.post(
            "/api/v1/tasks",
            json={"analysis_version_id": "analysis-1"},
        )
        no_token_plan = client.post(
            "/api/v1/tasks/task-1/plan-versions",
            json={},
        )
        no_token_approval = client.post(
            "/api/v1/plan-versions/plan-1/approve",
            json={},
        )
        no_token_message = client.post(
            "/api/v1/tasks/task-1/conversation/messages",
            json={},
        )
        no_token_readiness = client.post(
            "/api/v1/plan-approvals/approval-1/execution-readiness",
            json={},
        )
        no_token_execution = client.post(
            "/api/v1/plan-versions/plan-1/executions",
            json={},
        )
        no_token_archive = client.post(
            "/api/v1/plan-versions/plan-1/archives",
            json={},
        )
        no_token_changeset = client.post(
            "/api/v1/executions/attempt-1/change-sets",
            json={},
        )
        no_token_review = client.post(
            "/api/v1/executions/attempt-1/reviews",
            json={},
        )
        no_token_repair = client.post(
            "/api/v1/reviews/review-1/repair",
            json={},
        )
        no_token_intent = client.post(
            "/api/v1/reviews/review-1/publish-intents",
            json={},
        )
        no_token_confirm = client.post(
            "/api/v1/publish-intents/intent-1/confirm",
            json={},
        )
        no_token_event = client.post(
            "/api/v1/draft-pull-requests/pr-1/events",
            json={},
        )
        no_token_lifecycle = client.post(
            "/api/v1/tasks/task-1/lifecycle",
            json={},
        )
        no_token_preferences = client.post(
            "/api/v1/preferences/current",
            json={},
        )
        no_token_disposition = client.post(
            "/api/v1/opportunities/1/dispositions",
            json={"state": "shortlisted"},
        )
        no_token_notification = client.post(
            "/api/v1/notifications/read-all",
            json={},
        )
        no_csrf = client.post(
            "/api/v1/scans",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_analysis = client.post(
            "/api/v1/opportunities/1/analyses",
            json={"snapshot_id": "snapshot-1"},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_task = client.post(
            "/api/v1/tasks",
            json={"analysis_version_id": "analysis-1"},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_plan = client.post(
            "/api/v1/tasks/task-1/plan-versions",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_approval = client.post(
            "/api/v1/plan-versions/plan-1/approve",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_message = client.post(
            "/api/v1/tasks/task-1/conversation/messages",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_readiness = client.post(
            "/api/v1/plan-approvals/approval-1/execution-readiness",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_execution = client.post(
            "/api/v1/plan-versions/plan-1/executions",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_archive = client.post(
            "/api/v1/plan-versions/plan-1/archives",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_changeset = client.post(
            "/api/v1/executions/attempt-1/change-sets",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_review = client.post(
            "/api/v1/executions/attempt-1/reviews",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_repair = client.post(
            "/api/v1/reviews/review-1/repair",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_intent = client.post(
            "/api/v1/reviews/review-1/publish-intents",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_confirm = client.post(
            "/api/v1/publish-intents/intent-1/confirm",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_event = client.post(
            "/api/v1/draft-pull-requests/pr-1/events",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_lifecycle = client.post(
            "/api/v1/tasks/task-1/lifecycle",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_preferences = client.post(
            "/api/v1/preferences/current",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_disposition = client.post(
            "/api/v1/opportunities/1/dispositions",
            json={"state": "shortlisted"},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        no_csrf_notification = client.post(
            "/api/v1/notifications/read-all",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
            },
        )
        cross_origin = client.post(
            "/api/v1/scans",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "https://evil.example",
                "X-CSRF-Token": csrf,
            },
        )
        browser_allowed = client.post(
            "/api/v1/scans",
            json={},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "browser-auth",
            },
        )
        secret_preference = client.post(
            "/api/v1/preferences/current",
            json={"preferred_languages": [LOCAL_TOKEN]},
            headers={
                "Authorization": f"Bearer {LOCAL_TOKEN}",
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        cli_allowed = client.post(
            "/api/v1/scans",
            json={},
            headers={
                "X-ContribOS-Token": LOCAL_TOKEN,
                "Idempotency-Key": "cli-auth",
            },
        )

    assert meta.status_code == 200
    assert meta.json()["local_access_token_required"] is True
    assert LOCAL_TOKEN not in meta.text
    assert no_token.status_code == 401
    assert no_token_analysis.status_code == 401
    assert no_token_task.status_code == 401
    assert no_token_plan.status_code == 401
    assert no_token_approval.status_code == 401
    assert no_token_message.status_code == 401
    assert no_token_readiness.status_code == 401
    assert no_token_execution.status_code == 401
    assert no_token_archive.status_code == 401
    assert no_token_changeset.status_code == 401
    assert no_token_review.status_code == 401
    assert no_token_repair.status_code == 401
    assert no_token_intent.status_code == 401
    assert no_token_confirm.status_code == 401
    assert no_token_event.status_code == 401
    assert no_token_lifecycle.status_code == 401
    assert no_token_preferences.status_code == 401
    assert no_token_disposition.status_code == 401
    assert no_token_notification.status_code == 401
    assert no_token.headers["www-authenticate"] == "Bearer"
    assert no_csrf.status_code == 403
    assert no_csrf_analysis.status_code == 403
    assert no_csrf_task.status_code == 403
    assert no_csrf_plan.status_code == 403
    assert no_csrf_approval.status_code == 403
    assert no_csrf_message.status_code == 403
    assert no_csrf_readiness.status_code == 403
    assert no_csrf_execution.status_code == 403
    assert no_csrf_archive.status_code == 403
    assert no_csrf_changeset.status_code == 403
    assert no_csrf_review.status_code == 403
    assert no_csrf_repair.status_code == 403
    assert no_csrf_intent.status_code == 403
    assert no_csrf_confirm.status_code == 403
    assert no_csrf_event.status_code == 403
    assert no_csrf_lifecycle.status_code == 403
    assert no_csrf_preferences.status_code == 403
    assert no_csrf_disposition.status_code == 403
    assert no_csrf_notification.status_code == 403
    assert cross_origin.status_code == 403
    assert browser_allowed.status_code == 202
    assert secret_preference.status_code == 422
    assert LOCAL_TOKEN not in secret_preference.text
    assert cli_allowed.status_code == 202


def test_same_origin_browser_requires_csrf_even_without_access_token(
    tmp_path: Path, monkeypatch
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'csrf.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(database_url=database_url, github_queries=("offline-query",))
    )

    with TestClient(app, base_url="http://testserver") as client:
        meta = client.get("/api/v1/meta").json()
        rejected = client.post(
            "/api/v1/scans",
            json={},
            headers={"Origin": "http://testserver"},
        )
        accepted = client.post(
            "/api/v1/scans",
            json={},
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": meta["csrf_token"],
                "Idempotency-Key": "csrf-accepted",
            },
        )

    assert meta["local_access_token_required"] is False
    assert rejected.status_code == 403
    assert accepted.status_code == 202


def test_errors_are_structured_and_redacted(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'errors.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(
            database_url=database_url,
            github_token=SECRET_CANARY,
            github_queries=("offline-query",),
        )
    )

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError(f"internal failure {SECRET_CANARY}")

    with TestClient(
        app,
        base_url="http://testserver",
        raise_server_exceptions=False,
    ) as client:
        missing = client.get("/api/v1/jobs/missing")
        invalid = client.post("/api/v1/scans", json={"top_n": 0})
        internal = client.get("/explode")

    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "http_404",
            "message": "Job missing was not found",
        }
    }
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert internal.status_code == 500
    assert internal.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal request failure",
        }
    }
    assert SECRET_CANARY not in internal.text
    assert SECRET_CANARY not in caplog.text


def test_serve_defaults_to_loopback() -> None:
    args = build_parser().parse_args(["serve"])

    assert args.host == "127.0.0.1"
