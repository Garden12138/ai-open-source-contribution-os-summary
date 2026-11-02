"""Goal revision preserves the existing runtime and explicit planning Profile."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.contribution_workflow import advance_workflows
from app.contribution_workflow import stop_workflow, _advance
from app.model_settings import ModelSettingsService
from app.models import Job
from app.planner import PlanningService
from app.workbench import WorkbenchError
from tests.test_model_settings import configure
from tests.test_workbench import automatic_fixture


@pytest.mark.parametrize("runtime_v2,bound_profile", [(True, True), (False, True), (False, False)])
def test_replan_inherits_only_explicit_task_model_and_keeps_runtime(tmp_path, monkeypatch, runtime_v2, bound_profile):
    db, settings, task, *_ = automatic_fixture(tmp_path, runtime_v2=runtime_v2)
    settings = replace(settings, local_access_token="test-local-token")
    with db.session() as session:
        # This also changes defaults; the unbound legacy case must ignore them.
        profile = configure(session)
        if bound_profile:
            ModelSettingsService(session).append("task:" + task,
                {"analysis": None, "planning": profile.id, "implementation": None, "review": None}, None)
        wb = PlanningService(session, settings.artifact_root)
        old_plan = wb.latest_plan(task).record_hash
        prior = [(e.id, e.record_hash) for e in wb.history(task)]
        calls = sum(e.kind == "model_call" for e in wb.history(task))
    monkeypatch.setenv("UPEVE_ENABLED", "1")
    monkeypatch.setenv("UPEVE_TARGET_PLATFORM", "linux/arm64")
    route = f"/api/v1/tasks/{task}/workbench"
    headers = {"X-ContribOS-Token": "test-local-token", "Idempotency-Key": "exact-replan"}
    with TestClient(create_app(settings)) as client:
        view = client.get(route).json()
        payload = {"text": "Keep the same change scope and revise the frozen environment.", "expected_revision": view["execution"]["revision"]}
        result = client.post(route + "/replan", json=payload, headers=headers)
        assert result.status_code == 202, result.text
        assert client.post(route + "/replan", json=payload, headers=headers).status_code == 202
    for _ in range(2):
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        messages = [e for e in wb.history(task) if e.kind == "user_message" and e.payload["text"] == payload["text"]]
        assert len(messages) == 1
        job = session.get(Job, messages[0].job_id)
        assert job.payload.get("model_profile_id") == (profile.id if bound_profile else None)
        assert job.payload.get("agent_runtime_version") == ("agent-runtime-v2" if runtime_v2 else None)
        assert bool(job.payload.get("environment_engine")) == runtime_v2
        assert wb.latest_plan(task).record_hash == old_plan
        now = {e.id: e.record_hash for e in wb.history(task)}
        assert all(now.get(i) == h for i, h in prior)
        assert sum(e.kind == "model_call" for e in wb.history(task)) == calls
        assert job.cancel_requested_at is None
    db.close()


def test_disabled_v2_replan_stops_instead_of_downgrading(tmp_path):
    db, settings, task, *_ = automatic_fixture(tmp_path, runtime_v2=True)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.append(task, "execution_change_requested", {"text": "Revise only the environment.", "stop_key": "stop:disabled"},
                  key="change:disabled", actor="local-user")
        stop_workflow(wb, task, key="disabled")
        with pytest.raises(WorkbenchError) as error:
            _advance(wb, task, replace(settings, agent_runtime_v2_enabled=False))
        assert error.value.code == "planning_runtime_version"
        assert not any(e.kind == "user_message" and e.payload.get("text") == "Revise only the environment." for e in wb.history(task))
    db.close()
