"""Offline regressions: raw call envelopes survive validation, never private reasoning."""
import asyncio
import copy
import json

import pytest

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import schedule
from app.provenance import content_hash
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_protocol_repairs import fixture, overflow
from tests.test_model_settings import CANARY


def journal(db, settings, task, sid):
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        return j.checkpoint(), [(e, j.wb.read(e.payload['artifact_id'])) for e in j.events()
            if e.kind == 'agent_v2_model_response'], j.events()


@pytest.mark.parametrize('mixed', [False, True])
def test_overflow_is_durably_captured_before_whole_batch_rejection(tmp_path, monkeypatch, mixed):
    value = overflow(5 if mixed else 9)
    for index, call in enumerate(value['choices'][0]['message']['tool_calls']):
        call['id'] = f'original-vendor-{index}'
        call['function'] = {'name': 'read_file', 'arguments': json.dumps({'path': 'calc.py', 'offset': index * 128})}
    if mixed:
        value['choices'][0]['message']['tool_calls'][2]['function'] = {
            'name': 'apply_patch', 'arguments': '{"path":"calc.py","expected_hash":null,"old_text":"","new_text":"x"}'}
    original_calls = copy.deepcopy(value['choices'][0]['message']['tool_calls'])
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, value)
    import app.agent_v2.native_tools as native
    parser = native.parse_native_decision
    observed = []
    def checked_parser(raw, **kwargs):
        # A second connection sees the committed envelope before parser rejection.
        cp, receipts, events = journal(db, settings, task, sid)
        assert len(receipts) == 1
        assert not any(e.kind == 'agent_v2_model_rejected' for e in events)
        observed.append(receipts[0][1])
        return parser(raw, **kwargs)
    monkeypatch.setattr(native, 'parse_native_decision', checked_parser)
    result = asyncio.run(model.run_once())
    assert result.state == 'succeeded', result.error_message
    cp, receipts, events = journal(db, settings, task, sid)
    capture, raw = receipts[0]
    assert len(observed) == 1 and raw['choices'][0]['tool_calls'] == original_calls
    assert raw['choices'][0]['finish_reason'] == 'tool_calls'
    assert raw['usage'] == value['usage']
    assert raw['request_id'] == capture.job_id
    rejected = next(e for e in events if e.kind == 'agent_v2_model_rejected')
    assert capture.sequence < rejected.sequence
    assert rejected.payload['response_artifact'] == capture.payload['artifact_id']
    assert rejected.payload['failure']['safe_reason_code'] == 'agent_native_call_count_invalid'
    assert 'PRIVATE_REASONING_SENTINEL' not in json.dumps(raw)
    assert cp.pending_calls == [] and cp.usage['model_calls'] == 1
    assert cp.usage['output_tokens'] == 4096 and cp.usage['protocol_repairs'] == 1
    assert asyncio.run(tool.run_once()) is None and gateway.calls == []


def test_accepted_native_ids_are_traceable_from_raw_response_to_tool_result(tmp_path):
    value = response('list_paths', {})
    value['choices'][0]['message']['tool_calls'][0]['id'] = 'original-call-with-punctuation:42'
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, value)
    assert asyncio.run(model.run_once()).state == 'succeeded'
    assert asyncio.run(tool.run_once()).state == 'succeeded'
    cp, receipts, events = journal(db, settings, task, sid)
    assert len(receipts) == 1
    capture, raw = receipts[0]
    turn = next(e for e in events if e.kind == 'agent_v2_turn')
    result = next(e for e in events if e.kind == 'agent_v2_tool_result')
    assert turn.payload['response_artifact'] == result.payload['response_artifact'] == capture.payload['artifact_id']
    assert result.payload['provider_call_id'] == raw['choices'][0]['tool_calls'][0]['id']
    assert result.payload['provider_call_index'] == 0
    assert result.payload['call_id'] == gateway.calls[0].call_id


def test_capture_survives_parser_crash_without_repeating_model_or_tools(tmp_path, monkeypatch):
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, overflow())
    import app.agent_v2.native_tools as native
    def crash(*args, **kwargs):
        raise RuntimeError('simulated parser interruption')
    monkeypatch.setattr(native, 'parse_native_decision', crash)
    assert asyncio.run(model.run_once()).state == 'failed'
    cp, receipts, _ = journal(db, settings, task, sid)
    assert len(receipts) == 1 and cp.usage['model_calls'] == 1
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert schedule(j) is None
        assert j.checkpoint().state == 'blocked'
        session.commit()
    assert len(runner.prompts) == 1 and asyncio.run(tool.run_once()) is None


def test_response_larger_than_remaining_output_budget_blocks_without_scope_or_budget_change(tmp_path):
    value = overflow()
    value['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = json.dumps({'query': 'x' * 500000})
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, value)
    result = asyncio.run(model.run_once())
    assert result.state == 'failed' and result.error_code == 'agent_budget_exceeded'
    cp, receipts, events = journal(db, settings, task, sid)
    assert cp.state == 'blocked' and not receipts and not gateway.calls
    assert cp.usage['model_calls'] == 1 and cp.usage['output_tokens'] == 4096
    missing = next(e for e in events if e.kind == 'agent_v2_model_response_unavailable')
    assert missing.payload['reason'] == 'response_evidence_output_budget'
    assert missing.payload['received_call_count'] == 5
    assert asyncio.run(tool.run_once()) is None


def test_sensitive_overflow_is_redacted_and_never_receives_protocol_retry(tmp_path):
    value = overflow()
    value['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = json.dumps({'query': CANARY})
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, value)
    assert asyncio.run(model.run_once()).state == 'succeeded'
    cp, receipts, events = journal(db, settings, task, sid)
    assert len(receipts) == 1 and receipts[0][1]['sensitive_redacted']
    assert cp.state == 'blocked' and cp.usage.get('protocol_repairs', 0) == 0
    assert CANARY not in json.dumps([e.payload for e in events])
    from pathlib import Path
    assert all(CANARY.encode() not in p.read_bytes() for p in Path(settings.artifact_root).rglob('*') if p.is_file())
    assert not gateway.calls and asyncio.run(tool.run_once()) is None


@pytest.mark.parametrize('kind', ['duplicate_ids', 'malformed_arguments', 'unknown_tool', 'finish_length', 'missing_usage'])
def test_invalid_envelopes_remain_diagnosable_without_becoming_executable(tmp_path, kind):
    value = overflow()
    calls = value['choices'][0]['message']['tool_calls']
    if kind == 'duplicate_ids': calls[1]['id'] = calls[0]['id']
    elif kind == 'malformed_arguments': calls[2]['function']['arguments'] = '{invalid'
    elif kind == 'unknown_tool': calls[2]['function']['name'] = 'shell'
    elif kind == 'finish_length': value['choices'][0]['finish_reason'] = 'length'
    elif kind == 'missing_usage': value.pop('usage')
    expected = copy.deepcopy(calls)
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, value)
    assert asyncio.run(model.run_once()).state == 'succeeded'
    cp, receipts, events = journal(db, settings, task, sid)
    assert len(receipts) == 1 and receipts[0][1]['choices'][0]['tool_calls'] == expected
    assert not cp.pending_calls and not gateway.calls
    assert asyncio.run(tool.run_once()) is None
