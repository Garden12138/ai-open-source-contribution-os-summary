from __future__ import annotations

import importlib
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import Settings


class StubGitHub:
    rate_limit_remaining = 4_998
    rate_limit_reset_at = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def search_issues(self, query: str, limit: int):
        return [
            {
                "id": 501,
                "number": 12,
                "title": "$300 bounty: improve MCP diagnostics",
                "body": (
                    "Steps to reproduce and expected behavior are documented with "
                    "a concrete acceptance checklist for this contribution."
                ),
                "html_url": "https://github.com/acme/tool/issues/12",
                "repository_url": "https://api.github.com/repos/acme/tool",
                "state": "open",
                "labels": [{"name": "bounty"}],
                "comments": 1,
                "assignees": [],
                "author_association": "MEMBER",
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-16T00:00:00Z",
            }
        ]

    async def get_repository_bundle(self, full_name: str):
        return {
            "repository": {
                "id": 601,
                "full_name": full_name,
                "description": "MCP developer tooling",
                "html_url": f"https://github.com/{full_name}",
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "stargazers_count": 2_000,
                "forks_count": 80,
                "open_issues_count": 10,
                "archived": False,
                "disabled": False,
                "default_branch": "main",
                "topics": ["mcp", "ai"],
                "pushed_at": "2026-07-16T00:00:00Z",
            },
            "community": {
                "health_percentage": 90,
                "files": {"contributing": {"url": "https://example.test"}},
            },
        }


def test_health_and_empty_daily_leaderboard(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'api.db'}"
    # app.api exposes a default application at import time. Point that instance at
    # the test directory too, so importing this module never touches the real DB.
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(database_url=database_url, github_queries=("offline-test-query",))
    )

    with TestClient(app) as client:
        health = client.get("/health")
        dashboard = client.get("/")
        javascript = client.get("/static/app.js")
        leaderboard = client.get(
            "/api/v1/opportunities/daily", params={"on": "2026-07-17"}
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "ok"}
    assert dashboard.status_code == 200
    assert "今日机会榜" in dashboard.text
    assert javascript.status_code == 200
    assert "runScan" in javascript.text
    assert leaderboard.status_code == 200
    assert leaderboard.json() == {
        "selection_date": "2026-07-17",
        "generated_at": None,
        "total_candidates": 0,
        "total_eligible": 0,
        "picks": [],
    }


def test_scan_endpoint_populates_the_dashboard_contract(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'scan-api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(database_url=database_url, github_queries=("offline-query",))
    )
    app.state.github_client_factory = StubGitHub

    with TestClient(app) as client:
        scan = client.post("/api/v1/scans", json={})
        leaderboard = client.get("/api/v1/opportunities/daily")

    assert scan.status_code == 201
    assert scan.json()["candidate_count"] == 1
    assert scan.json()["selected_count"] == 1
    assert leaderboard.status_code == 200
    pick = leaderboard.json()["picks"][0]
    assert pick["selection_reason"] == "bounty"
    assert pick["opportunity"]["repository"]["full_name"] == "acme/tool"
    assert pick["opportunity"]["bounty_amount_usd"] == 300.0
    assert "body" not in pick["opportunity"]
