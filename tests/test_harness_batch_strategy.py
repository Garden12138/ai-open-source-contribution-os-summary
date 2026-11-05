"""Strategy B: whole rejection and bounded correction, never automatic splitting."""
import asyncio
import copy
import json

import pytest

from app.agent_v2.journal import AgentJournal
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_protocol_repairs import fixture, overflow


@pytest.mark.parametrize('member', ['read_file', 'apply_patch', 'run_approved_checks', 'contribos_agent_finish', 'shell'])
def test_five_calls_of_any_composition_are_stored_then_rejected_without_partial_execution(tmp_path, member):
    raw = overflow()
    calls = raw['choices'][0]['message']['tool_calls']
    calls[2]['function'] = {'name': member, 'arguments': json.dumps({'path': 'calc.py'} if member == 'read_file' else {})}
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, raw)
    assert asyncio.run(model.run_once()).state == 'succeeded'
    assert asyncio.run(tool.run_once()) is None and gateway.calls == []
    assert len(runner.prompts) == 1
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        cp = j.checkpoint()
        capture = next(e for e in j.events() if e.kind == 'agent_v2_model_response')
        assert j.wb.read(capture.payload['artifact_id'])['choices'][0]['tool_calls'] == calls
        rejected = next(e for e in j.events() if e.kind == 'agent_v2_model_rejected')
        assert rejected.payload['protocol_rejection_receipt']['executed_calls'] == 0
        assert cp.pending_calls == [] and cp.usage.get('writes', 0) == cp.usage.get('checks', 0) == 0
        assert not any(e.kind in {'agent_v2_turn', 'agent_v2_tool_started'} for e in j.events())


def test_three_whole_rejections_stop_with_two_repairs_and_all_reservations_preserved(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, overflow(), overflow(), overflow())
    for _ in range(3):
        assert asyncio.run(model.run_once()).state == 'succeeded'
    assert asyncio.run(model.run_once()) is None and asyncio.run(tool.run_once()) is None
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        cp = j.checkpoint()
        assert cp.state == 'blocked' and cp.usage['model_calls'] == 3
        assert cp.usage['protocol_repairs'] == 2 and cp.usage['output_tokens'] == 12288
        assert cp.usage.get('provider_retries', 0) == 0
        assert len([e for e in j.events() if e.kind == 'agent_v2_model_response']) == 3
        assert j.spec().limits.model_calls == 24 and j.spec().limits.protocol_repairs == 2


def test_corrected_four_calls_keep_original_ids_order_and_one_result_per_call(tmp_path):
    batch = overflow(4)
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, overflow(), batch)
    assert asyncio.run(model.run_once()).state == 'succeeded'
    assert asyncio.run(tool.run_once()) is None
    assert asyncio.run(model.run_once()).state == 'succeeded'
    correction = runner.prompts[-1]['native_protocol_correction']
    assert correction['remaining_protocol_repairs'] == 1
    assert correction['rejection']['max_allowed_calls'] == 4 and correction['rejection']['executed_calls'] == 0
    for _ in range(4):
        assert asyncio.run(tool.run_once()).state == 'succeeded'
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        results = [e for e in j.events() if e.kind == 'agent_v2_tool_result']
        assert [e.payload['provider_call_id'] for e in results] == ['0', '1', '2', '3']
        assert [e.payload['provider_call_index'] for e in results] == [0, 1, 2, 3]
        assert len({e.payload['call_id'] for e in results}) == 4
        assert len({e.payload['response_artifact'] for e in results}) == 1
        assert j.checkpoint().usage['model_calls'] == 2 and j.checkpoint().usage['reads'] == 4
        assert len(runner.prompts) == 2  # The platform never asked for regenerated sub-batches.
