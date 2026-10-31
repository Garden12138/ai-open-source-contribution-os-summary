"""Out-of-range read parameters fail closed and use only existing correction slots."""
import asyncio
import json

import pytest

from app.planner import PlanningService, PlanningTurnWorker
from app.planning_budget import planning_usage
from app.planning_continuation import resume_failed_model_planning
from app.workbench import WorkbenchError
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_planning_continuation import failed_model_step
from tests.test_workbench import seeded, result


def invalid(field='max_lines', value=161):
    return json.dumps({'read_context': [{'reference_id': 'a' * 64, field: value}]})


@pytest.mark.parametrize('field,value', [('max_lines', 161), ('max_bytes', 16001), ('max_bytes', 255),
    ('offset', 2000001), ('start_line', 0)])
def test_read_bounds_use_existing_repair_without_executing_rejected_requests(tmp_path, monkeypatch, field, value):
    db, settings, task, content, _ = seeded(tmp_path)
    monkeypatch.setattr('app.planner.read_context', lambda *a, **kw: pytest.fail('Rejected read must not execute'))
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text='Prepare plan', parent_id=None,
            expected_hash=None, key='bounds', runtime_version='agent-runtime-v2')
    runner = ScriptedRunner(invalid(field, value), result(content))
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='bounds')
    first = asyncio.run(worker.run_once())
    assert first.state == 'succeeded' and first.result_data['repair_scheduled']
    second = asyncio.run(worker.run_once())
    assert second.state == 'succeeded', second.error_message
    repair = json.loads(runner.invocations[1].prompt)['plan_repair']
    assert repair['constraint'] == 'context_read_parameter_bounds'
    assert repair['context_read_bounds']['max_lines']['maximum'] == 160
    assert repair['context_read_bounds']['max_bytes']['maximum'] == 16000
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task) and not wb.latest(task, 'execution_authorized')
        assert planning_usage(wb, task)['calls'] == 2
        assert wb.jobs(task)[-1].payload['repair_round'] == 1 and wb.jobs(task)[-1].payload['round'] == 0
    db.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_bounds_repair_cannot_grow_repair_count_or_change_legacy_behavior(tmp_path, legacy):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text='Prepare plan', parent_id=None,
            expected_hash=None, key='bounded', **({} if legacy else {'runtime_version': 'agent-runtime-v2'}))
    count = 1 if legacy else 3
    runner = ScriptedRunner(*[invalid() for _ in range(count)])
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='bounded')
    jobs = [asyncio.run(worker.run_once()) for _ in range(count)]
    assert jobs[-1].state == 'failed' and jobs[-1].error_code == 'planning_output_invalid'
    assert asyncio.run(worker.run_once()) is None and len(runner.invocations) == count
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert planning_usage(wb, task)['calls'] == count and wb.latest_plan(task) is None
    db.close()


def range_failure(tmp_path):
    db, settings, task, content, failed_id, frozen = failed_model_step(tmp_path, error_code='planning_output_invalid')
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.append(task, 'model_output_rejected', {'reason_code': 'planning_output_invalid',
            'fields': ['read_context.max_lines'], 'validation_types': ['less_than_equal'],
            'policy': 'planning-v5-independent-actions'}, key='range-rejection', actor='planner', job_id=failed_id)
    return db, settings, task, content, failed_id, frozen


def test_no_read_rounds_range_correction_does_not_restore_read_permission(tmp_path, monkeypatch):
    db, settings, task, content, _ = seeded(tmp_path)
    monkeypatch.setattr('app.planner.MAX_READ_ROUNDS', 0)
    monkeypatch.setattr('app.planner.read_context', lambda *a, **kw: pytest.fail('Rejected read must not execute'))
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text='Prepare plan', parent_id=None,
            expected_hash=None, key='no-reads', runtime_version='agent-runtime-v2')
    runner = ScriptedRunner(invalid(), result(content))
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='no-reads')
    assert asyncio.run(worker.run_once()).state == 'succeeded'
    assert asyncio.run(worker.run_once()).state == 'succeeded'
    second = runner.invocations[1]
    assert second.output_schema['properties']['read_context']['maxItems'] == 0
    assert json.loads(second.prompt)['plan_repair']['read_rounds_remaining'] == 0
    db.close()


def test_native_continue_repairs_only_known_range_failure_and_preserves_frozen_counters(tmp_path):
    db, settings, task, _, failed_id, frozen = range_failure(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        source = wb.append(task, 'conversation_received', {'version': 'conversation-v1', 'text': '继续'},
            key='range-continue', actor='local-user')
        before = planning_usage(wb, task)
        retry = resume_failed_model_planning(wb, task, source)
        assert retry is not None
        assert retry.payload['round'] == frozen['round'] == 2
        assert retry.payload['repair_round'] == frozen['repair_round'] + 1 == 2
        assert retry.payload['text'] == frozen['text'] and retry.payload['pending_plan'] == frozen['pending_plan']
        assert retry.payload['plan_repair']['context_read_bounds']['max_lines']['maximum'] == 160
        assert planning_usage(wb, task) == before
        assert wb.latest(task, 'planning_model_retry_requested').payload['retry_reason'] == 'planning_read_bounds_invalid'
        assert next(j for j in wb.jobs(task) if j.id == failed_id).payload == frozen
        assert not wb.latest(task, 'execution_authorized')
    db.close()


@pytest.mark.parametrize('blocker', ['budget', 'context', 'model', 'pause', 'foreign_rejection'])
def test_range_recovery_requires_budget_and_same_frozen_source_not_chat_permission(tmp_path, blocker):
    db, settings, task, _, _, _ = range_failure(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        if blocker == 'context':
            wb.append(task, 'context_ready', wb.latest(task, 'context_ready').payload, key='changed-context')
        elif blocker == 'model':
            wb.append(task, 'model_changed', {'planning_profile_id': 'new-profile'}, key='changed-model')
        elif blocker == 'pause':
            wb.append(task, 'conversation_paused', {'version': 'conversation-v1'}, key='pause')
        elif blocker == 'foreign_rejection':
            wb.append(task, 'model_output_rejected', {'reason_code': 'planning_output_invalid',
                'fields': ['read_context.max_lines'], 'validation_types': ['less_than_equal'],
                'policy': 'planning-v5-independent-actions'}, key='foreign', actor='planner')
        else:
            for i in range(31):
                wb.append(task, 'model_call', {'input_hash': 'b' * 64}, key=f'exhaust-{i}', commit=False)
        source = wb.append(task, 'conversation_received', {'version': 'conversation-v1', 'text': '继续'},
            key='blocked-continue', actor='local-user')
        jobs = len(wb.jobs(task))
        if blocker == 'budget':
            with pytest.raises(WorkbenchError) as caught:
                resume_failed_model_planning(wb, task, source)
            assert caught.value.code == 'planning_budget_exceeded'
        else:
            assert resume_failed_model_planning(wb, task, source) is None
        assert len(wb.jobs(task)) == jobs and not wb.latest(task, 'execution_authorized')
    db.close()
