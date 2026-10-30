"""Oversized context intent is rejected before reads and repaired within old caps."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from app.planner import PlanningEnvelope, PlanningService, PlanningTurnWorker
from app.planning_budget import planning_usage
from app.planning_diagnostics import planning_validation_details
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import seeded, result


def oversized_reads():
    return {"read_context":[{"reference_id":str(i)*64} for i in range(5)]}


def test_read_count_diagnostics_name_only_known_field_without_request_values():
    with pytest.raises(ValidationError) as caught:
        PlanningEnvelope.model_validate(oversized_reads())
    details = planning_validation_details(caught.value)
    assert details == {"reason_code":"planning_output_invalid", "validation_types":["too_long"], "fields":["read_context"]}
    assert "reference_id" not in json.dumps(details) and "0"*64 not in json.dumps(details)
    canary = "ghp_readcountdiagnosticcanary123456789012345678"
    with pytest.raises(ValidationError) as caught:
        PlanningEnvelope.model_validate({"read_context":[{"reference_id":canary}]})
    details = planning_validation_details(caught.value)
    assert details["fields"] == ["read_context.reference_id"] and canary not in json.dumps(details)


@pytest.mark.parametrize("no_more_reads", [False, True])
def test_v2_count_error_can_recover_without_running_any_rejected_read(tmp_path, monkeypatch, no_more_reads):
    db, settings, task, content, _ = seeded(tmp_path)
    if no_more_reads:
        monkeypatch.setattr("app.planner.MAX_READ_ROUNDS",0)
    def reject_execution(*a, **kw):
        raise AssertionError("An invalid read intent must not execute")
    monkeypatch.setattr("app.planner.read_context", reject_execution)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(task, text="Prepare the exact plan", parent_id=None, expected_hash=None, key="read-count",
            runtime_version="agent-runtime-v2")
        original = [(e.id,e.record_hash) for e in wb.history(task)]
    runner = ScriptedRunner(json.dumps(oversized_reads()), result(content))
    worker = PlanningTurnWorker(db,settings,runner,_identity(settings.implementation_model),worker_id="read-count")
    first = asyncio.run(worker.run_once())
    assert first.state == "succeeded" and first.result_data["repair_scheduled"]
    second = asyncio.run(worker.run_once())
    assert second.state == "succeeded", second.error_message
    prompt = json.loads(runner.invocations[1].prompt)
    repair = prompt["plan_repair"]
    assert repair["fields"] == ["read_context"]
    assert repair["maximum_context_requests"] == (0 if no_more_reads else 4)
    assert repair["read_rounds_remaining"] == (0 if no_more_reads else 4)
    assert repair["constraint"] == ("read_round_limit" if no_more_reads else "context_read_request_limit")
    if no_more_reads:
        assert runner.invocations[1].output_schema["properties"]["read_context"]["maxItems"] == 0
        assert runner.invocations[1].output_schema["properties"]["read_paths"]["maxItems"] == 0
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        assert wb.latest_plan(task).content_hash == content.content_hash
        assert [(e.id,e.record_hash) for e in wb.history(task)[:len(original)]] == original
        assert planning_usage(wb,task)["calls"] == 2
        assert planning_usage(wb,task)["input_tokens"] == 200
        assert wb.latest(task,"execution_authorized") is None
    db.close()


@pytest.mark.parametrize("v2", [False, True])
def test_read_count_repairs_are_bounded_and_do_not_change_legacy_retry_policy(tmp_path, monkeypatch, v2):
    db, settings, task, *_ = seeded(tmp_path)
    def reject_execution(*a, **kw):
        raise AssertionError("Overlong requests cannot execute reads")
    monkeypatch.setattr("app.planner.read_context", reject_execution)
    with db.session() as session:
        PlanningService(session,settings.artifact_root).request(task,text="Prepare plan",parent_id=None,
            expected_hash=None,key="bounded-read-count",**({"runtime_version":"agent-runtime-v2"} if v2 else {}))
    count = 3 if v2 else 1
    runner = ScriptedRunner(*[json.dumps(oversized_reads())]*count)
    worker = PlanningTurnWorker(db,settings,runner,_identity(settings.implementation_model),worker_id="bounded-read-count")
    jobs = [asyncio.run(worker.run_once()) for _ in range(count)]
    assert jobs[-1].state == "failed" and jobs[-1].error_code == "planning_output_invalid"
    assert asyncio.run(worker.run_once()) is None and len(runner.invocations)==count
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        usage = planning_usage(wb,task)
        assert usage["calls"]==count and usage["input_tokens"]==100*count and usage["output_tokens"]==30*count
        assert usage["unknown_calls"]==0
        assert wb.latest_plan(task) is None and wb.latest(task,"execution_authorized") is None
        assert len([e for e in wb.history(task) if e.kind=="model_output_rejected"])==count
    db.close()


def test_other_oversized_fields_do_not_gain_automatic_repairs(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session,settings.artifact_root).request(task,text="Prepare plan",parent_id=None,
            expected_hash=None,key="other-too-long",runtime_version="agent-runtime-v2")
    runner = ScriptedRunner(json.dumps({"read_paths":[f"file_{i}.py" for i in range(33)]}))
    worker = PlanningTurnWorker(db,settings,runner,_identity(settings.implementation_model),worker_id="other-too-long")
    job = asyncio.run(worker.run_once())
    assert job.state=="failed" and job.error_code=="planning_output_invalid"
    assert asyncio.run(worker.run_once()) is None and len(runner.invocations)==1
    db.close()
