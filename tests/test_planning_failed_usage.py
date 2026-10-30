"""Failed planning calls consume reported usage or retain an explicit allowance."""
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.database import Database
from app.jobs import JobService
from app.planner import PlanningService, PlanningTurnWorker
from app.planning_budget import planning_budget_view, planning_usage, require_planning_budget
from app.providers.contracts import ProviderRunError, ProviderUsage
from app.workbench import WorkbenchError
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_planning_budget import update
from tests.test_workbench import result, seeded


@pytest.mark.parametrize("known", [False, True])
def test_failed_receipt_survives_restart_without_rewriting_call_or_granting_execution(tmp_path, known):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(task, text="Prepare the plan", parent_id=None, expected_hash=None, key="fail")
        original = [(e.id, e.record_hash) for e in wb.history(task)]
    class FailingRunner:
        profile = {"max_tokens": 8192}
        async def complete(self, invocation):
            assert invocation.output_token_limit == 8192
            raise ProviderRunError("minimax_completion_truncated", "Output budget exhausted",
                retryable=False, usage=ProviderUsage(input_tokens=123, output_tokens=8192) if known else None)
    worker = PlanningTurnWorker(db, settings, FailingRunner(), _identity(settings.implementation_model), worker_id="receipt")
    failed = asyncio.run(worker.run_once())
    assert failed.state == "failed" and failed.error_code == "planning_provider_failed"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        view = planning_budget_view(wb, task)
        assert view["usage"] == {"calls": 1, "input_tokens": 123 if known else 0, "output_tokens": 8192 if known else 0}
        assert view["reserved"] == {"input_tokens": 0 if known else 450000, "output_tokens": 0 if known else 8192}
        assert view["unknown_calls"] == int(not known)
        assert view["remaining"]["output_tokens"] == 91808
        assert wb.latest(task, "model_failed_usage").payload["usage_unknown"] is (not known)
        assert wb.latest(task, "model_failed_usage").payload["reason_code"] == "minimax_completion_truncated"
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(original)]] == original
        assert wb.latest_plan(task) is None and wb.latest(task, "execution_authorized") is None
        call = wb.latest(task, "model_call")
        frozen_call = call.id, call.record_hash
    db.close()
    db = Database(settings.database_url)
    assert db.create_schema().applied == ()
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert planning_budget_view(wb, task) == view
        assert (wb.latest(task, "model_call").id, wb.latest(task, "model_call").record_hash) == frozen_call
    db.close()


def terminal_call(wb, task, *, count=1, bound=True):
    job = wb.enqueue(task, "planning_turn", {}, key="historical-call")
    jobs = JobService(wb.session)
    assert jobs.lease_next(worker_id="historical", kinds=("planning_turn",)).id == job.id
    jobs.start(job.id, worker_id="historical")
    for index in range(count):
        wb.append(task, "model_call", {"output_token_limit": 8192} if bound else {}, key=f"historical-{index}", job_id=job.id)
    jobs.fail(job.id, worker_id="historical", error_code="completion_truncated", error_message="No receipt")
    return job


def test_legacy_unknown_calls_are_separate_from_known_usage_and_block_underfunded_updates(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        job = terminal_call(wb, task, count=2)
        wb.append(task, "conversation_usage", {"usage": {"input_tokens": 10, "output_tokens": 20}},
            key="one-of-two", job_id=job.id)
        before = [(e.id, e.record_hash) for e in wb.history(task)]
        view = planning_budget_view(wb, task)
        assert view["usage"] == {"calls": 2, "input_tokens": 10, "output_tokens": 20}
        assert view["reserved"] == {"input_tokens": 450000, "output_tokens": 8192}
        assert view["unknown_calls"] == 1
        with pytest.raises(WorkbenchError) as caught:
            update(wb, task, limits={"calls":32, "input_tokens":1000, "output_tokens":100000})
        assert caught.value.code == "planning_budget_below_usage"
        assert [(e.id, e.record_hash) for e in wb.history(task)] == before
    db.close()


def test_legacy_missing_output_bound_holds_full_allowance_instead_of_free_retry(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        terminal_call(wb, task, bound=False)
        view = planning_budget_view(wb, task)
        assert view["remaining"]["output_tokens"] == 0
        assert view["usage"]["output_tokens"] == 0
        assert view["reserved"]["output_tokens"] == 100000
        with pytest.raises(WorkbenchError) as caught:
            require_planning_budget(wb, task)
        assert caught.value.code == "planning_budget_exceeded"
        assert "未知预留" in str(caught.value)
    db.close()


def test_provider_receives_remaining_output_cap_and_next_call_is_blocked(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        update(wb, task, limits={"calls":32, "input_tokens":2000000, "output_tokens":1000})
        wb.append(task, "assistant_message", {"usage":{"input_tokens":1,"output_tokens":970}}, key="spent")
        wb.request(task, text="Prepare the plan", parent_id=None, expected_hash=None, key="last")
    runner = ScriptedRunner(result(content))
    runner.profile = {"max_tokens":8192}
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id="cap")
    completed = asyncio.run(worker.run_once())
    assert completed.state == "succeeded", completed.error_message
    assert runner.invocations[0].output_token_limit == 30
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert planning_usage(wb, task)["output_tokens"] == 1000
        assert wb.latest(task, "model_call").payload["output_token_limit"] == 30
        with pytest.raises(WorkbenchError) as caught:
            require_planning_budget(wb, task)
        assert caught.value.code == "planning_budget_exceeded"
        assert wb.latest(task, "execution_authorized") is None
    db.close()


def test_api_exposes_reservations_without_profile_or_credential_mutation(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        terminal_call(wb, task)
    with TestClient(create_app(replace(settings, local_access_token="test-local-token"))) as client:
        route = f"/api/v1/tasks/{task}/workbench"
        view = client.get(route).json()["execution"]["planning_budget"]
        assert view["reserved"] == {"input_tokens":450000,"output_tokens":8192}
        response = client.post(route+"/budget", json={"expected_hash":view["revision"],"limits":view["limits"]},
            headers={"X-ContribOS-Token":"test-local-token","Idempotency-Key":"retain-allowance"})
        assert response.status_code == 200, response.text
        assert response.json()["reserved"] == view["reserved"]
        assert response.json()["unknown_calls"] == 1
        schema = client.get("/openapi.json").json()["components"]["schemas"]["PlanningBudgetResponse"]
        assert {"reserved","unknown_calls"} <= schema["properties"].keys()
    db.close()
