from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.execution_acceptance import accept_execution, acceptance_view
from app.workbench import Workbench, WorkbenchError
from app.workbench_view import workbench_view
from tests.test_workbench import automatic_fixture, populate_round, drive_round


@pytest.fixture
def ready(tmp_path):
    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    populate_round(code, review)
    drive_round(db, settings, task_id, workers)
    return db, settings, task_id


def test_acceptance_without_publisher_is_bound_idempotent_and_creates_no_jobs(ready):
    db, settings, task_id = ready
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        view = workbench_view(wb, task_id)
        assert settings.publisher_mode == "none"
        assert view["actions"]["can_accept"]
        jobs = [j.id for j in wb.jobs(task_id)]
        receipt = accept_execution(wb, task_id, binding_hash=view["acceptance"]["binding_hash"],
                                   text="验收通过", key="one")
        assert receipt.payload["review_hash"] == view["acceptance"]["binding"]["review_hash"]
        assert receipt.actor_id == "local-user"
        assert accept_execution(wb, task_id, binding_hash=receipt.payload["binding_hash"],
                                text="验收通过", key="one").id == receipt.id
        with pytest.raises(WorkbenchError, match="内容不同"):
            accept_execution(wb, task_id, binding_hash=receipt.payload["binding_hash"],
                             text="不同内容", key="one")
        accepted = workbench_view(wb, task_id)
        assert accepted["acceptance"]["accepted"]["record_hash"] == receipt.record_hash
        assert not accepted["actions"]["can_accept"]
        assert accepted["state"] == "ready"
        assert jobs == [j.id for j in wb.jobs(task_id)]
        assert not wb.latest(task_id, "publication_confirmed")
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert acceptance_view(wb, task_id)["accepted"]["record_hash"] == receipt.record_hash


def test_wrong_binding_and_replanning_cannot_reuse_acceptance(ready):
    from app.contribution_workflow import stop_workflow, _advance
    from app.planner import PlanningService
    db, settings, task_id = ready
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        binding = acceptance_view(wb, task_id)["binding_hash"]
        with pytest.raises(WorkbenchError, match="结果已变化"):
            accept_execution(wb, task_id, binding_hash="f" * 64, text="确认", key="wrong")
        receipt = accept_execution(wb, task_id, binding_hash=binding, text="确认", key="valid")
        stop_workflow(wb, task_id, key="change")
        _advance(wb, task_id, settings)
        assert not acceptance_view(wb, task_id)["available"]
        assert acceptance_view(wb, task_id)["accepted"] is None
        assert wb.latest(task_id, "execution_accepted").record_hash == receipt.record_hash
        with pytest.raises(WorkbenchError, match="结果已变化"):
            accept_execution(wb, task_id, binding_hash=binding, text="确认", key="stale")


def test_failed_review_cannot_be_accepted(tmp_path):
    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    for _ in range(3):
        populate_round(code, review, verdict="block")
    drive_round(db, settings, task_id, workers)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert not acceptance_view(wb, task_id)["available"]
        with pytest.raises(WorkbenchError):
            accept_execution(wb, task_id, binding_hash="f" * 64, text="确认", key="blocked")
        assert wb.latest(task_id, "execution_accepted") is None


def test_acceptance_api_access_timeline_and_model_independence(ready):
    db, settings, task_id = ready
    settings = replace(settings, local_access_token="test-local-token", implementation_provider="none")
    route = f"/api/v1/tasks/{task_id}/workbench"
    with TestClient(create_app(settings)) as client:
        initial = client.get(route).json()
        assert not initial["planner_available"]
        payload = {"binding_hash": initial["execution"]["acceptance"]["binding_hash"], "text": "确认验收"}
        headers = {"Idempotency-Key": "api", "X-ContribOS-Token": "test-local-token"}
        assert client.post(route + "/acceptance", json=payload, headers={"Idempotency-Key": "api"}).status_code == 401
        assert client.post(route + "/acceptance", json=payload,
                           headers={**headers, "Origin": "http://testserver"}).status_code == 403
        canary = "ghp_ACCEPTANCECANARY12345678901234567890"
        rejected = client.post(route + "/acceptance", json={**payload, "text": canary}, headers=headers)
        assert rejected.status_code == 422 and canary not in rejected.text
        response = client.post(route + "/acceptance", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert client.post(route + "/acceptance", json=payload, headers=headers).json() == response.json()
        timeline = client.get(route + f"/timeline?after={initial['execution']['event_cursor']}").json()
        assert [e["kind"] for e in timeline["events"]] == ["execution_accepted"]
        assert client.get(route).json()["execution"]["acceptance"]["accepted"]
        assert client.post(route + "/acceptance", json={**payload, "binding_hash": "f" * 64},
                           headers={**headers, "Idempotency-Key": "stale"}).status_code == 409


def test_acceptance_rejects_sensitive_message_without_persistence(ready):
    db, settings, task_id = ready
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        with pytest.raises(ValueError):
            accept_execution(wb, task_id, binding_hash=acceptance_view(wb, task_id)["binding_hash"],
                             text="ghp_ACCEPTANCECANARY12345678901234567890", key="secret")
        assert wb.latest(task_id, "execution_accepted") is None
