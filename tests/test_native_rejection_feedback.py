"""A rejected native batch needs exact evidence and current, bounded recovery."""
import asyncio
import copy

import pytest

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import prompt_for
from app.workbench import Workbench, WorkbenchError
from tests.test_agent_native_tools import response
from tests.test_native_protocol_repairs import fixture, overflow


def test_exact_rejection_and_current_budget_reach_retry_then_clear_after_valid_turn(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = fixture(
        tmp_path, overflow(), response('list_paths', {}), response('list_paths', {}))
    assert asyncio.run(model.run_once()).state == 'succeeded'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        journal = AgentJournal(wb, task, sid)
        rejection = next(e for e in journal.events() if e.kind == 'agent_v2_model_rejected')
        raw = copy.deepcopy(rejection.payload)
        assert rejection.payload['failure']['remaining_budget']['protocol_repairs'] == 2
        prompt = prompt_for(journal)
        correction = prompt['native_protocol_correction']
        assert correction['rejection'] == raw['protocol_rejection_receipt']
        assert correction['rejection_event_hash'] == rejection.record_hash
        assert correction['model_job_id'] == rejection.job_id
        assert correction['remaining_protocol_repairs'] == 1
        assert correction['rejection']['received_call_count'] == 5
        assert correction['rejection']['max_allowed_calls'] == 4
        assert correction['rejection']['executed_calls'] == 0
        assert prompt['protocol_feedback']['remaining_budget'] == journal.remaining()
        assert rejection.payload == raw
    assert asyncio.run(tool.run_once()) is None and gateway.calls == []
    assert asyncio.run(model.run_once()).state == 'succeeded'
    assert runner.prompts[-1]['native_protocol_correction']['rejection']['whole_batch_rejected'] is True
    assert asyncio.run(tool.run_once()).state == 'succeeded'
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        prompt = prompt_for(journal)
        assert prompt['protocol_feedback'] is None and 'native_protocol_correction' not in prompt
        assert journal.checkpoint().usage['protocol_repairs'] == 1
        assert journal.checkpoint().usage['model_calls'] == 2 and len(gateway.calls) == 1


def test_final_permitted_correction_has_zero_additional_repairs_without_refund(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, overflow(), overflow(), overflow())
    for _ in range(3):
        assert asyncio.run(model.run_once()).state == 'succeeded'
    correction = runner.prompts[-1]['native_protocol_correction']
    assert correction['remaining_protocol_repairs'] == 0
    assert runner.prompts[-1]['protocol_feedback']['remaining_budget']['protocol_repairs'] == 0
    assert asyncio.run(model.run_once()) is None and asyncio.run(tool.run_once()) is None
    with db.session() as session:
        cp = AgentJournal(Workbench(session, settings.artifact_root), task, sid).checkpoint()
        assert cp.state == 'blocked' and cp.usage['model_calls'] == 3
        assert cp.usage['protocol_repairs'] == 2 and cp.usage['output_tokens'] == 12288
    assert not gateway.calls


@pytest.mark.parametrize('field,value', [('max_allowed_calls', 5), ('executed_calls', 1),
    ('whole_batch_rejected', False), ('received_call_count', 4), ('count_exceeds_diagnostic_bound', True)])
def test_invalid_stored_receipt_cannot_be_projected_as_trusted_correction(tmp_path, field, value):
    db, settings, task, sid, model, *_ = fixture(tmp_path, overflow())
    assert asyncio.run(model.run_once()).state == 'succeeded'
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        last = next(e for e in journal.events() if e.kind == 'agent_v2_model_rejected')
        receipt = {**last.payload['protocol_rejection_receipt'], field: value}
        journal.append('model_rejected', {**last.payload, 'protocol_rejection_receipt': receipt},
            key='malformed-correction', job_id=last.job_id)
        with pytest.raises(WorkbenchError) as error:
            prompt_for(journal)
        assert error.value.code == 'agent_native_rejection_evidence_invalid'


def test_provider_error_code_does_not_invent_a_native_count_receipt(tmp_path):
    from app.providers.contracts import ProviderRunError
    db, settings, task, sid, model, *_ = fixture(tmp_path,
        ProviderRunError('agent_native_call_count_invalid', 'upstream error', retryable=False))
    assert asyncio.run(model.run_once()).state == 'succeeded'
    with db.session() as session:
        prompt = prompt_for(AgentJournal(Workbench(session, settings.artifact_root), task, sid))
        assert 'native_protocol_correction' not in prompt


def test_large_rejected_batch_projects_only_bounded_cardinality_facts(tmp_path):
    db, settings, task, sid, model, *_ = fixture(tmp_path, overflow(1025))
    assert asyncio.run(model.run_once()).state == 'succeeded'
    with db.session() as session:
        prompt = prompt_for(AgentJournal(Workbench(session, settings.artifact_root), task, sid))
        correction = prompt['native_protocol_correction']
        assert correction['rejection']['received_call_count'] is None
        assert correction['rejection']['count_exceeds_diagnostic_bound'] is True
        assert correction['rejection']['max_allowed_calls'] == 4
        assert correction['rejection']['executed_calls'] == 0
        import json
        assert len(json.dumps(correction)) < 1200
        assert 'PRIVATE_REASONING_SENTINEL' not in json.dumps(correction)
