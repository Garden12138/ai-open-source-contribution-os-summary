from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import Settings
from app.api import _batch_analysis_budget
from app.models import Job
from app.providers import (
    AnalysisBudget,
    FakeProvider,
    ProviderAnalysisJobWorker,
)
from app.task_states import (
    TaskStateConflictError,
    TaskStateTransitionError,
)
from app.worker import DiscoveryJobWorker


def _selection_date(value: datetime) -> str:
    aware = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return aware.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def test_recommendation_analysis_budget_keeps_one_item_probe_bounded() -> None:
    budget = AnalysisBudget(max_duration_ms=4_200_000)

    assert _batch_analysis_budget(budget, 1).max_duration_ms == 840_000
    assert _batch_analysis_budget(budget, 5).max_duration_ms == 840_000


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


def test_task_state_conflicts_have_stable_redacted_409_contract(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'state-conflict-api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(Settings(database_url=database_url))

    def stale_transition() -> None:
        raise TaskStateConflictError(
            "stale compare-and-swap ghp_stateapicanary12345678"
        )

    def illegal_transition() -> None:
        raise TaskStateTransitionError(
            "Illegal ContributionTask transition"
        )

    app.add_api_route("/_test/stale-transition", stale_transition)
    app.add_api_route("/_test/illegal-transition", illegal_transition)
    with TestClient(app) as client:
        stale = client.get("/_test/stale-transition")
        illegal = client.get("/_test/illegal-transition")

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "task_state_conflict"
    assert "ghp_" not in stale.text
    assert "[REDACTED]" in stale.text
    assert illegal.status_code == 409
    assert illegal.json() == {
        "error": {
            "code": "illegal_task_state_transition",
            "message": "Illegal ContributionTask transition",
        }
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
        api_module_asset = client.get("/static/api.js")
        stylesheet = client.get("/static/styles.css")
        leaderboard = client.get(
            "/api/v1/opportunities/daily", params={"on": "2026-07-17"}
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "ok"}
    assert dashboard.status_code == 200
    assert dashboard.headers["cache-control"] == "no-cache"
    assert "今日机会榜" in dashboard.text
    assert "/static/styles.css?v=analysis-context-v2" in dashboard.text
    assert "/static/app.js?v=analysis-context-v2" in dashboard.text
    assert javascript.status_code == 200
    assert "runScan" in javascript.text
    assert "runAnalysis" in javascript.text
    assert "/analyses" in javascript.text
    assert "cited_evidence_ids" not in javascript.text
    assert "estimated_cost_microusd" not in javascript.text
    assert "基础评分 · 深入评估未开启" in javascript.text
    assert "版本差异" in javascript.text
    assert "创建贡献任务" in javascript.text
    assert "execution-readiness" in javascript.text
    assert "buildExecutionWorkbench" in javascript.text
    assert 'executions: "/api/v1/executions"' in javascript.text
    assert "/archives" in javascript.text
    assert "采集仓库归档" in javascript.text
    assert "/change-sets" in javascript.text
    assert "提交变更方案" in javascript.text
    assert "/reviews" in javascript.text
    assert "启动独立 Review" in javascript.text
    assert "启动有界修复" in javascript.text
    assert "/publish-intents" in javascript.text
    assert "创建发布意图" in javascript.text
    assert "确认发布 Draft PR" in javascript.text
    assert "贡献漏斗" in dashboard.text
    assert "发现机会" in dashboard.text
    assert "我的候选" in dashboard.text
    assert "贡献进度" in dashboard.text
    assert "调整偏好" in dashboard.text
    assert "先告诉我们，你想获得什么" in dashboard.text
    assert 'recommendations: "/api/v1/recommendations"' in javascript.text
    assert '`${API.recommendations}/analyses`' in javascript.text
    assert "AI 筛选前 5 个" in dashboard.text
    assert "开始 Vibe Coding" in javascript.text
    assert "openContributionTask" in javascript.text
    assert "contribution-workbench" in dashboard.text
    assert "Draft PR 仅写本地 Fake 记录" in javascript.text
    assert 'shortlist: "/api/v1/shortlist"' in javascript.text
    assert 'notifications: "/api/v1/notifications"' in javascript.text
    assert "togglePreferenceEditor" in javascript.text
    assert "compareSelectedOpportunities" in javascript.text
    assert "请求取消" in javascript.text
    assert "查看结果" in javascript.text
    assert "requestArtifact" in javascript.text
    assert "查看分析报告" in javascript.text
    assert "下载 Markdown" in javascript.text
    assert "renderSafeMarkdown" in javascript.text
    assert "旧版分析缺少项目—需求关联" in javascript.text
    assert "重新生成关联分析" in javascript.text
    assert "appendMarkdownInline" in javascript.text
    assert '"./api.js?v=analysis-context-v2"' in javascript.text
    assert '"compare/document"' in javascript.text
    assert "analysisObjectBlock" not in javascript.text
    assert "分析依据与技术详情" not in javascript.text
    assert ".innerHTML" not in javascript.text
    assert "insertAdjacentHTML" not in javascript.text
    assert "保存为新修订" in javascript.text
    assert api_module_asset.status_code == 200
    assert "requestJSON" in api_module_asset.text
    assert "requestArtifact" in api_module_asset.text
    assert stylesheet.status_code == 200
    assert ".analysis-workbench" in stylesheet.text
    assert ".analysis-markdown-document" in stylesheet.text
    assert ".analysis-summary-grid" in stylesheet.text
    assert ".difference-item" in stylesheet.text
    assert ".planning-layout" in stylesheet.text
    assert ".planning-stale-alert" in stylesheet.text
    assert ".execution-workbench" in stylesheet.text
    assert ".execution-stage-timeline" in stylesheet.text
    assert ".execution-artifact-grid" in stylesheet.text
    assert ".primary-nav" in stylesheet.text
    assert ".decision-summary" in stylesheet.text
    assert ".comparison-grid" in stylesheet.text
    assert ".notification-drawer" in stylesheet.text
    assert 'type="module"' in dashboard.text
    assert "精选机会 · 清晰决策" in dashboard.text
    assert leaderboard.status_code == 200
    assert leaderboard.json() == {
        "selection_date": "2026-07-17",
        "scan_run_id": None,
        "provenance_status": "not_generated",
        "generated_at": None,
        "total_candidates": 0,
        "total_eligible": 0,
        "analysis": {
            "mode": "rule_only_fallback",
            "automatic_model_invocation_enabled": False,
            "rule_leaderboard_preserved": True,
            "fallback_reasons": [
                "provider_not_configured",
                "budget_not_configured",
            ],
        },
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
        scan = client.post(
            "/api/v1/scans",
            json={},
            headers={"Idempotency-Key": "api-scan-test"},
        )
        replay = client.post(
            "/api/v1/scans",
            json={},
            headers={"Idempotency-Key": "api-scan-test"},
        )
        queued = client.get(f"/api/v1/jobs/{scan.json()['id']}")
        worker_now = (
            datetime.fromisoformat(queued.json()["run_after"])
            + timedelta(seconds=1)
        )
        worker = DiscoveryJobWorker(
            app.state.database,
            app.state.settings,
            app.state.github_client_factory,
            worker_id="api-test-worker",
        )
        completed = asyncio.run(
            worker.run_once(now=worker_now)
        )
        job = client.get(f"/api/v1/jobs/{scan.json()['id']}")
        completed_replay = client.post(
            "/api/v1/scans",
            json={},
            headers={"Idempotency-Key": "api-scan-test"},
        )
        idle_worker_result = asyncio.run(
            worker.run_once(now=worker_now + timedelta(minutes=1))
        )
        scan_history = client.get("/api/v1/scans")
        leaderboard = client.get(
            "/api/v1/opportunities/daily",
            params={"on": _selection_date(worker_now)},
        )

    assert scan.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == scan.json()["id"]
    assert queued.status_code == 200
    assert queued.json()["state"] == "queued"
    assert completed is not None
    assert completed.id == scan.json()["id"]
    assert job.status_code == 200
    assert job.json()["state"] == "succeeded"
    assert job.json()["result_data"]["candidate_count"] == 1
    assert job.json()["result_data"]["selected_count"] == 1
    assert completed_replay.status_code == 202
    assert completed_replay.json()["id"] == scan.json()["id"]
    assert completed_replay.json()["state"] == "succeeded"
    assert idle_worker_result is None
    assert len(scan_history.json()) == 1
    assert leaderboard.json()["scan_run_id"] == job.json()["scan_run_id"]
    assert leaderboard.json()["provenance_status"] == "verified"
    assert leaderboard.json()["analysis"] == {
        "mode": "rule_only_fallback",
        "automatic_model_invocation_enabled": False,
        "rule_leaderboard_preserved": True,
        "fallback_reasons": [
            "provider_not_configured",
            "budget_not_configured",
        ],
    }
    assert leaderboard.status_code == 200
    pick = leaderboard.json()["picks"][0]
    assert pick["selection_reason"] == "bounty"
    assert pick["provenance_status"] == "verified"
    assert pick["scan_run_id"] == job.json()["scan_run_id"]
    assert pick["snapshot_id"]
    assert pick["score_version_id"]
    assert pick["opportunity"]["repository"]["full_name"] == "acme/tool"
    assert pick["opportunity"]["bounty_amount_usd"] == 300.0
    assert "body" not in pick["opportunity"]


def test_job_cancel_retry_and_idempotency_conflict(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'job-api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(database_url=database_url, github_queries=("offline-query",))
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/scans",
            json={"top_n": 2},
            headers={"Idempotency-Key": "cancel-test"},
        )
        conflict = client.post(
            "/api/v1/scans",
            json={"top_n": 3},
            headers={"Idempotency-Key": "cancel-test"},
        )
        cancelled = client.post(
            f"/api/v1/jobs/{created.json()['id']}/cancel"
        )
        retried = client.post(
            f"/api/v1/jobs/{created.json()['id']}/retry"
        )
        missing = client.get("/api/v1/jobs/missing")

    assert created.status_code == 202
    assert conflict.status_code == 409
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert retried.status_code == 200
    assert retried.json()["state"] == "queued"
    assert missing.status_code == 404


def test_create_analysis_api_is_exact_protected_and_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'analysis-api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(database_url=database_url, github_queries=("offline-query",))
    )
    app.state.github_client_factory = StubGitHub
    provider = FakeProvider()

    with TestClient(app) as client:
        discovery = client.post(
            "/api/v1/scans",
            json={},
            headers={"Idempotency-Key": "analysis-api-discovery"},
        )
        worker_now = (
            datetime.fromisoformat(discovery.json()["run_after"])
            + timedelta(seconds=1)
        )
        asyncio.run(
            DiscoveryJobWorker(
                app.state.database,
                app.state.settings,
                app.state.github_client_factory,
                worker_id="analysis-api-discovery-worker",
            ).run_once(now=worker_now)
        )
        board = client.get(
            "/api/v1/opportunities/daily",
            params={"on": _selection_date(worker_now)},
        ).json()
        pick = board["picks"][0]
        opportunity_id = pick["opportunity"]["id"]
        snapshot_id = pick["snapshot_id"]
        endpoint = (
            f"/api/v1/opportunities/{opportunity_id}/analyses"
        )

        unavailable = client.post(
            endpoint,
            json={"snapshot_id": snapshot_id},
            headers={"Idempotency-Key": "analysis-api-job"},
        )
        app.state.analysis_provider = provider
        app.state.analysis_budget = AnalysisBudget()
        created = client.post(
            endpoint,
            json={"snapshot_id": snapshot_id},
            headers={"Idempotency-Key": "analysis-api-job"},
        )
        replay = client.post(
            endpoint,
            json={"snapshot_id": snapshot_id},
            headers={"Idempotency-Key": "analysis-api-job"},
        )
        detail = client.get(f"/api/v1/jobs/{created.json()['id']}")
        queued_events = client.get(
            f"/api/v1/jobs/{created.json()['id']}/events"
        )
        missing_opportunity = client.post(
            "/api/v1/opportunities/999999/analyses",
            json={"snapshot_id": snapshot_id},
            headers={"Idempotency-Key": "analysis-api-missing-opportunity"},
        )
        missing_snapshot = client.post(
            endpoint,
            json={"snapshot_id": "missing-snapshot"},
            headers={"Idempotency-Key": "analysis-api-missing-snapshot"},
        )
        app.state.analysis_budget = AnalysisBudget(
            max_model_invocations=3,
        )
        conflict = client.post(
            endpoint,
            json={"snapshot_id": snapshot_id},
            headers={"Idempotency-Key": "analysis-api-job"},
        )
        assert provider.inspect_requests == ()
        assert provider.analyze_requests == ()
        completed = asyncio.run(
            ProviderAnalysisJobWorker(
                app.state.database,
                provider,
                worker_id="analysis-api-provider-worker",
            ).run_once(
                now=(
                    datetime.fromisoformat(created.json()["run_after"])
                    + timedelta(seconds=1)
                )
            )
        )
        history = client.get(endpoint)
        completed_events = client.get(
            f"/api/v1/jobs/{created.json()['id']}/events"
        )
        unchanged_events = client.get(
            f"/api/v1/jobs/{created.json()['id']}/events",
            params={
                "after_revision": completed_events.json()["revision"],
            },
        )
        invalid_event_cursor = client.get(
            f"/api/v1/jobs/{created.json()['id']}/events",
            params={"after_revision": "not-a-hash"},
        )
        version_id = history.json()["versions"][0]["id"]
        version_detail = client.get(f"{endpoint}/{version_id}")
        version_document = client.get(f"{endpoint}/{version_id}/document")
        downloaded_document = client.get(
            f"{endpoint}/{version_id}/document",
            params={"download": True},
        )
        cached_document = client.get(
            f"{endpoint}/{version_id}/document",
            headers={"If-None-Match": version_document.headers["etag"]},
        )
        comparison = client.get(
            f"{endpoint}/compare",
            params={
                "left_version_id": version_id,
                "right_version_id": version_id,
            },
        )
        comparison_document = client.get(
            f"{endpoint}/compare/document",
            params={
                "left_version_id": version_id,
                "right_version_id": version_id,
            },
        )
        missing_version = client.get(f"{endpoint}/missing-version")
        missing_document = client.get(f"{endpoint}/missing-version/document")
        wrong_opportunity_document = client.get(
            f"/api/v1/opportunities/999999/analyses/{version_id}/document"
        )

    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["message"] == (
        "Analysis is unavailable: provider_not_configured, "
        "budget_not_configured"
    )
    assert created.status_code == 202
    assert created.json()["kind"] == "provider_analysis"
    assert created.json()["state"] == "queued"
    assert created.json()["idempotency_key"] == "analysis-api-job"
    assert created.json()["attempt_count"] == 0
    assert created.json()["max_attempts"] == 3
    assert created.json()["timeout_seconds"] == 1_200
    assert replay.status_code == 202
    assert replay.json()["id"] == created.json()["id"]
    assert detail.status_code == 200
    assert detail.json()["id"] == created.json()["id"]
    assert detail.json()["kind"] == created.json()["kind"]
    assert detail.json()["state"] == created.json()["state"]
    assert detail.json()["idempotency_key"] == (
        created.json()["idempotency_key"]
    )
    assert queued_events.status_code == 200
    assert queued_events.json()["state"] == "queued"
    assert queued_events.json()["unchanged"] is False
    assert [
        event["event_type"]
        for event in queued_events.json()["events"]
    ] == ["job.queued"]
    assert missing_opportunity.status_code == 404
    assert missing_snapshot.status_code == 404
    assert conflict.status_code == 409
    assert completed is not None
    assert completed.state == "succeeded"
    assert completed_events.status_code == 200
    assert completed_events.json()["state"] == "succeeded"
    assert [
        event["event_type"]
        for event in completed_events.json()["events"]
    ] == [
        "job.queued",
        "provider.inspect.succeeded",
        "provider.analyze.succeeded",
        "job.succeeded",
    ]
    assert completed_events.json()["events"][1]["data"][
        "input_tokens"
    ] == 100
    assert len(completed_events.json()["revision"]) == 64
    assert "确定性的演示分析" not in completed_events.text
    assert unchanged_events.status_code == 200
    assert unchanged_events.json()["unchanged"] is True
    assert unchanged_events.json()["events"] == []
    assert invalid_event_cursor.status_code == 422
    assert history.status_code == 200
    assert history.json()["opportunity_id"] == opportunity_id
    assert len(history.json()["versions"]) == 1
    assert history.json()["versions"][0]["job_id"] == created.json()["id"]
    assert history.json()["versions"][0]["snapshot_id"] == snapshot_id
    assert version_detail.status_code == 200
    assert version_detail.json()["id"] == version_id
    assert version_detail.json()["content"]["analysis"][
        "problem_summary"
    ] == "确定性的演示分析。"
    assert version_detail.json()["content"]["provider"]["name"] == "fake"
    assert version_detail.json()["content"]["contracts"][
        "inspect_prompt"
    ] == "inspect-prompt-v2"
    assert version_document.status_code == 200
    assert version_document.headers["content-type"].startswith("text/markdown")
    assert version_document.headers[
        "x-contribos-analysis-document-version"
    ] == "analysis-document-v2"
    assert version_document.text.startswith("# 开源贡献机会分析\n")
    assert "## 项目介绍" in version_document.text
    assert "## 需求内容" in version_document.text
    assert "## 综合分析" in version_document.text
    assert "## 行动建议" in version_document.text
    assert "provider" not in version_document.text.lower()
    assert "prompt" not in version_document.text.lower()
    assert "cited_evidence_ids" not in version_document.text
    assert "{" not in version_document.text
    assert downloaded_document.status_code == 200
    assert downloaded_document.headers["content-disposition"].startswith(
        "attachment;"
    )
    assert downloaded_document.text == version_document.text
    assert cached_document.status_code == 304
    assert comparison.status_code == 200
    assert comparison.json()["left_version_id"] == version_id
    assert comparison.json()["right_version_id"] == version_id
    assert comparison.json()["differences"] == []
    assert comparison_document.status_code == 200
    assert comparison_document.text == (
        "# 分析版本对比\n\n重要分析内容没有变化。\n"
    )
    assert missing_version.status_code == 404
    assert missing_document.status_code == 404
    assert wrong_opportunity_document.status_code == 409
    assert len(provider.inspect_requests) == 1
    assert len(provider.analyze_requests) == 1

    with app.state.database.session() as session:
        job = session.get(Job, created.json()["id"])
        assert job is not None
        assert job.payload["snapshot_id"] == snapshot_id
        assert job.payload["versions"]["inspect"] == {
            "prompt": "inspect-prompt-v2",
            "policy": "analysis-policy-v3",
            "output_schema": "inspection-schema-v1",
        }
        assert job.payload["versions"]["analyze"]["prompt"] == (
            "analyze-prompt-v11"
        )
        assert job.payload["versions"]["analyze"]["policy"] == (
            "analysis-policy-v3"
        )
        assert job.payload["expected_provider"] == (
            provider.identity.hash_payload()
        )


def test_historical_leaderboard_uses_its_own_scan(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'historical-api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    api_module = importlib.import_module("app.api")
    app = api_module.create_app(
        Settings(database_url=database_url, github_queries=("offline-query",))
    )
    app.state.github_client_factory = StubGitHub
    worker = DiscoveryJobWorker(
        app.state.database,
        app.state.settings,
        app.state.github_client_factory,
        worker_id="history-worker",
    )

    with TestClient(app) as client:
        first_job = client.post(
            "/api/v1/scans",
            json={},
            headers={"Idempotency-Key": "history-day-one"},
        )
        first_worker_now = (
            datetime.fromisoformat(first_job.json()["run_after"])
            + timedelta(seconds=1)
        )
        asyncio.run(
            worker.run_once(now=first_worker_now)
        )
        first_completed = client.get(
            f"/api/v1/jobs/{first_job.json()['id']}"
        ).json()

        second_job = client.post(
            "/api/v1/scans",
            json={},
            headers={"Idempotency-Key": "history-day-two"},
        )
        second_worker_now = max(
            datetime.fromisoformat(second_job.json()["run_after"]),
            first_worker_now + timedelta(days=1),
        ) + timedelta(seconds=1)
        asyncio.run(
            worker.run_once(now=second_worker_now)
        )
        second_completed = client.get(
            f"/api/v1/jobs/{second_job.json()['id']}"
        ).json()

        first_board = client.get(
            "/api/v1/opportunities/daily",
            params={"on": _selection_date(first_worker_now)},
        ).json()
        second_board = client.get(
            "/api/v1/opportunities/daily",
            params={"on": _selection_date(second_worker_now)},
        ).json()

    assert first_board["scan_run_id"] == first_completed["scan_run_id"]
    assert second_board["scan_run_id"] == second_completed["scan_run_id"]
    assert first_board["scan_run_id"] != second_board["scan_run_id"]
    assert first_board["total_candidates"] == (
        first_completed["result_data"]["candidate_count"]
    )
    assert second_board["total_candidates"] == (
        second_completed["result_data"]["candidate_count"]
    )
    assert first_board["provenance_status"] == "verified"
    assert second_board["provenance_status"] == "verified"
