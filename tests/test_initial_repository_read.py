"""A newly created task waits for an explicit read request, never silently runs."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.conversation import advance_conversations, receive_message
from app.database import Database
from app.execution_environment import task_environment
from app.jobs import JobService
from app.planning import ContributionTaskService
from app.workbench import Workbench
from tests.test_planning import _seed_analysis


@pytest.fixture
def fresh_task(tmp_path):
    path = tmp_path / "fresh.db"
    analysis_id, *_ = _seed_analysis(path)
    settings = Settings(database_url=f"sqlite:///{path}", artifact_root=str(tmp_path / "artifacts"),
                        implementation_provider="nvidia_nim")
    database = Database(settings.database_url)
    database.create_schema()
    with database.session() as session:
        task = ContributionTaskService(session).create(analysis_version_id=analysis_id, idempotency_key="fresh")
    yield database, settings, task.id
    database.close()


def test_fresh_task_reports_not_started_and_get_does_not_enqueue(fresh_task):
    database, settings, task_id = fresh_task
    with TestClient(create_app(settings)) as client:
        for _ in range(2):
            view = client.get(f"/api/v1/tasks/{task_id}/workbench").json()
            assert "尚未读取仓库" in view["execution"]["environment"]["reason"]
            assert "读取仓库并准备方案" in view["execution"]["environment"]["reason"]
            assert not view["jobs"] and not view["busy"]
    with database.session() as session:
        assert not Workbench(session, settings.artifact_root).history(task_id)


def test_explicit_start_enqueues_one_read_without_classifier_or_authorization(fresh_task):
    database, settings, task_id = fresh_task
    settings = replace(settings, local_access_token="test-initial-read")
    endpoint = f"/api/v1/tasks/{task_id}/workbench/conversation/messages"
    headers = {"X-ContribOS-Token": settings.local_access_token, "Idempotency-Key": "start"}
    with TestClient(create_app(settings)) as client:
        assert client.post(endpoint, json={"text": "读取仓库并准备方案"}).status_code == 401
        for _ in range(2):
            assert client.post(endpoint, json={"text": "读取仓库并准备方案"}, headers=headers).status_code == 202
    database.create_schema()
    for _ in range(2):
        advance_conversations(database, settings)
    with database.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert [(j.kind, j.state) for j in wb.jobs(task_id)] == [("planning_archive", "queued")]
        assert "已排队" in task_environment(wb, task_id)["reason"]
        assert not wb.latest(task_id, "execution_authorized")
        assert not wb.latest(task_id, "publication_confirmed")


@pytest.mark.parametrize("state, text", [("running", "正在读取"), ("failed", "失败"),
                                        ("timed_out", "超时"), ("cancelled", "已停止")])
def test_repository_read_status_and_retry_preserve_history(fresh_task, state, text):
    database, settings, task_id = fresh_task
    with database.session() as session:
        wb = Workbench(session, settings.artifact_root)
        receive_message(wb, task_id, text="继续", key="start", settings=settings)
        jobs = JobService(session)
        job = jobs.lease_next(worker_id="reader", kinds=("planning_archive",))
        jobs.start(job.id, worker_id="reader")
        if state == "failed":
            jobs.fail(job.id, worker_id="reader", error_code="read_failed", error_message="读取失败")
        elif state == "timed_out":
            jobs.time_out(job.id, worker_id="reader")
        elif state == "cancelled":
            jobs.request_cancel(job.id)
            jobs.cancel(job.id, worker_id="reader")
        assert text in task_environment(wb, task_id)["reason"]
        if state != "running":
            before = [(e.id, e.record_hash) for e in wb.history(task_id)]
            receive_message(wb, task_id, text="继续", key="retry", settings=settings)
            assert [(j.kind, j.state) for j in wb.jobs(task_id)] == [("planning_archive", state), ("planning_archive", "queued")]
            assert [(e.id, e.record_hash) for e in wb.history(task_id)][:len(before)] == before
            assert not wb.latest(task_id, "execution_authorized")
