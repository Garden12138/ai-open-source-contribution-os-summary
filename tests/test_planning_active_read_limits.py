"""Every v2 planning turn carries current enforced read limits, including repairs."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from app.context_compaction import ContextRead
from app.planner import PlanningService, PlanningTurnWorker
from app.provenance import content_hash
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import seeded, result


@pytest.mark.parametrize('legacy', [False, True])
def test_initial_turn_reports_bounds_before_any_invalid_response_and_keeps_legacy(tmp_path, legacy):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(task, text='Prepare a source-based plan', parent_id=None, expected_hash=None,
            key='initial', **({} if legacy else {'runtime_version': 'agent-runtime-v2'}))
    runner = ScriptedRunner(result(content))
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='active-limits')
    assert asyncio.run(worker.run_once()).state == 'succeeded'
    body = json.loads(runner.invocations[0].prompt)
    if legacy:
        assert 'planning_state' not in body
    else:
        active = body['planning_state']['context_read_limits']
        assert active['maximum_requests'] == 4
        assert active['parameters']['max_lines'] == {'minimum': 1, 'maximum': 160}
        assert active['parameters']['max_bytes'] == {'minimum': 256, 'maximum': 16000}
        assert body['planning_state']['repair_attempts_remaining'] == 2
        ContextRead(reference_id='a' * 64, max_lines=active['parameters']['max_lines']['maximum'])
        with pytest.raises(ValidationError):
            ContextRead(reference_id='a' * 64, max_lines=active['parameters']['max_lines']['maximum'] + 1)
    db.close()


def test_json_repair_retains_active_bounds_without_an_extra_repair_slot(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text='Prepare plan', parent_id=None,
            expected_hash=None, key='json-repair', runtime_version='agent-runtime-v2')
    runner = ScriptedRunner('{"reply": truE, "padding":"' + 'x' * 300 + '"}', result(content))
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='json-bounds')
    assert asyncio.run(worker.run_once()).result_data['repair_scheduled']
    assert asyncio.run(worker.run_once()).state == 'succeeded'
    body = json.loads(runner.invocations[1].prompt)
    assert body['plan_repair']['validation_types'] == ['json_invalid']
    assert body['planning_state']['context_read_limits']['parameters']['max_lines']['maximum'] == 160
    assert body['planning_state']['repair_attempts_remaining'] == 1
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.jobs(task)[-1].payload['repair_round'] == 1
        assert wb.latest(task, 'execution_authorized') is None
    db.close()


@pytest.mark.parametrize('round_number', [2, 4])
def test_current_limits_correct_old_repair_guidance_only_in_prompt_view(tmp_path, round_number):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        original = wb.request(task, text='Prepare plan', parent_id=None, expected_hash=None,
            key='frozen', runtime_version='agent-runtime-v2')
        # Supersede only the fixture Job with an ordinary frozen turn. The
        # production recovery always preserves the actual original payload.
        from app.jobs import JobService
        JobService(session).request_cancel(original.id)
        data = {**original.payload, 'round': round_number, 'repair_round': 1,
            'plan_repair': {'constraint': 'context_read_parameter_bounds', 'read_rounds_remaining': 4}}
        job = wb.enqueue(task, 'planning_turn', data, key='later-turn')
        frozen_id, frozen_hash, frozen_payload = job.id, job.payload_hash, json.loads(json.dumps(job.payload))
    runner = ScriptedRunner(result(content))
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='later-limits')
    observed = asyncio.run(worker.run_once())
    assert observed.state == 'succeeded', observed.error_message
    body = json.loads(runner.invocations[0].prompt)
    assert body['planning_state']['read_rounds_remaining'] == 4 - round_number
    assert body['plan_repair']['read_rounds_remaining'] == 4 - round_number
    assert body['planning_state']['context_read_limits']['maximum_requests'] == (4 if round_number < 4 else 0)
    assert body['planning_state']['repair_attempts_remaining'] == 1
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        frozen = next(j for j in wb.jobs(task) if j.id == frozen_id)
        assert frozen.payload == frozen_payload and frozen.payload_hash == frozen_hash == content_hash(frozen.payload)
        assert not wb.latest(task, 'execution_authorized')
    db.close()
