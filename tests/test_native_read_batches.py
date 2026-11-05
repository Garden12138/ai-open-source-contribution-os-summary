"""Recorded Plan12 envelopes under synthetic authority; zero provider/network use."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.agent_v2.contracts import ToolResult
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentToolWorker, prompt_for, schedule
from app.agent_v2.native_tools import parse_native_decision
from app.agent_v2.progress import current_progress
from app.agent_v2.read_batches import NativeReadBatch
from app.agent_v2.tools import artifact_page
from app.provenance import content_hash
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_protocol_repairs import fixture

SAVED = json.loads((Path(__file__).parent / 'fixtures/plan12_native_read_batches.json').read_text())


def saved_response(index):
    envelope = SAVED['batches'][index]['envelope']
    assert content_hash(envelope) == SAVED['batches'][index]['response_artifact']
    return {'choices': [{'finish_reason': c['finish_reason'], 'message': {
        'role': c['role'], 'tool_calls': copy.deepcopy(c['tool_calls'])}} for c in envelope['choices']],
        'usage': copy.deepcopy(envelope['usage'])}


def reads(count=6, **arguments):
    value = response('read_file', {'path': 'calc.py', **arguments})
    c = value['choices'][0]['message']['tool_calls'][0]
    value['choices'][0]['message']['tool_calls'] = [{**copy.deepcopy(c), 'id': 'vendor-' + str(i)} for i in range(count)]
    return value


class ReadGateway:
    def __init__(self, *, fail_at=None, denied=None):
        self.calls = []
        self.preflights = 0
        self.text = 'source line\n' * 1200
        self.fail_at, self.denied = fail_at, denied

    async def preflight_read_batch(self, spec, calls, *, source_hash, snapshot, input_hash):
        self.preflights += 1
        args = [c.arguments for c in calls]
        from app.agent_v2.tools import RestrictedToolRegistry
        args = [RestrictedToolRegistry().validate(c, spec).model_dump() for c in calls]
        denied = any(a['path'] == self.denied for a in args)
        return {'version': 'native-read-batch-preflight-v1', 'input_hash': input_hash,
            'binding_hash': content_hash(spec.binding.model_dump()), 'source_hash': source_hash,
            'after_hash': source_hash, 'requests_hash': content_hash(args),
            'status': 'rejected' if denied else 'succeeded',
            'failure_code': 'path_denied' if denied else None,
            'checked': [] if denied else [{'path': a['path'], 'content_hash': hashlib.sha256(self.text.encode()).hexdigest(),
                'offset': a['offset'], 'max_bytes': a['max_bytes'], 'total_bytes': len(self.text.encode())} for a in args],
            'execution_backend': 'fixture'}

    async def execute(self, spec, call, arguments, *, source_hash, snapshot, input_hash):
        self.calls.append(call)
        data = {'tool': call.name, 'execution_backend': 'docker', 'path': arguments.path,
            'content_hash': hashlib.sha256(self.text.encode()).hexdigest(),
            **artifact_page(self.text.encode(), arguments.offset, arguments.max_bytes)}
        failure = None
        if len(self.calls) == self.fail_at:
            from app.agent_v2.contracts import Failure
            failure = Failure(category='protocol', safe_reason_code='source_unavailable', retryable=False,
                remaining_budget={}, next_allowed_action='inspect_diagnostic', blocking_scope='tool')
        return {'result': ToolResult(call_id=call.call_id, input_hash=input_hash, source_hash=source_hash,
            output_source_hash=source_hash, status='rejected' if failure else 'succeeded', data=data, failure=failure).model_dump()}


def setup(tmp_path, *values, gateway=None):
    db, settings, task, sid, model, _, runner, _ = fixture(tmp_path, *values)
    gateway = gateway or ReadGateway()
    tool = AgentToolWorker(db, settings, gateway, worker_id='read-batch-tools')
    return db, settings, task, sid, model, tool, runner, gateway


def inspect(values):
    db, settings, task, sid = values[:4]
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        return j.checkpoint(), j.events(), current_progress(j)


def drain(tool, count=9):
    for _ in range(count):
        if asyncio.run(tool.run_once()) is None:
            break


@pytest.mark.parametrize('index,count', [(0, 6), (1, 5), (2, 5)])
def test_real_saved_envelopes_split_and_aggregate_original_order(tmp_path, index, count):
    values = setup(tmp_path, saved_response(index))
    db, settings, task, sid, model, tool, runner, gateway = values
    assert asyncio.run(model.run_once()).state == 'succeeded'
    cp, events, _ = inspect(values)
    assert cp.state == 'tools' and len(cp.pending_calls) == 4
    assert cp.usage['reads'] == count and not gateway.calls
    assert asyncio.run(tool.run_once()).state == 'succeeded'  # Entire-batch OCI preflight, no read yet.
    assert gateway.preflights == 1 and not gateway.calls
    for _ in range(4):
        assert asyncio.run(tool.run_once()).state == 'succeeded'
    cp, events, _ = inspect(values)
    assert cp.state == 'tools' and len(cp.pending_calls) == count - 4
    assert asyncio.run(model.run_once()) is None  # No Decision between children.
    drain(tool)
    cp, events, ledger = inspect(values)
    assert cp.state == 'ready' and cp.usage['reads'] == count and cp.usage['model_calls'] == 1
    results = [e for e in events if e.kind == 'agent_v2_tool_result']
    original = saved_response(index)['choices'][0]['message']['tool_calls']
    assert [e.payload['provider_call_id'] for e in results] == [c['id'] for c in original]
    assert [e.payload['provider_call_index'] for e in results] == list(range(count))
    children = [e for e in events if e.kind == 'agent_v2_read_sub_batch_completed']
    assert [len(e.payload['call_ids']) for e in children] == [4, count - 4]
    assert len({e.payload['checkpoint_hash'] for e in events if e.kind == 'agent_v2_read_sub_batch_started'}) == 2
    aggregate = next(e for e in events if e.kind == 'agent_v2_read_batch_completed')
    assert aggregate.payload['receipts'] == [e.payload['artifact_id'] for e in results]
    assert ledger.steps[3].status == 'pending' and ledger.phase == 'investigating'
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        prompt = prompt_for(j)
        assert len(prompt['feedback']) == count and prompt['read_batch_observation']['status'] == 'completed'
        assert [f['receipt_artifact'] for f in prompt['feedback']] == aggregate.payload['receipts']
        for f in prompt['feedback']:
            text = f['data']['text'];d=f['data'];end=d['offset']+len(text.encode())
            assert d['next_offset'] == (end if end < d['total_bytes'] else None)
    assert len(runner.prompts) == 1


@pytest.mark.parametrize('kind', ['write', 'check', 'terminal', 'unknown', 'bad_last_argument', 'escape', 'duplicate_id', 'too_many'])
def test_entire_invalid_or_mixed_batch_rejected_before_any_read(tmp_path, kind):
    value = reads(9 if kind == 'too_many' else 5)
    calls = value['choices'][0]['message']['tool_calls']
    if kind in ('write','check','terminal','unknown'):
        calls[-1]['function']['name'] = {'write':'apply_patch','check':'run_approved_checks',
            'terminal':'contribos_agent_finish','unknown':'shell'}[kind]
    elif kind == 'bad_last_argument': calls[-1]['function']['arguments'] = '{"path":"calc.py","offset":-1}'
    elif kind == 'escape': calls[-1]['function']['arguments'] = '{"path":"../secret"}'
    elif kind == 'duplicate_id': calls[-1]['id'] = calls[0]['id']
    values = setup(tmp_path, value)
    assert asyncio.run(values[4].run_once()).state == 'succeeded'
    assert asyncio.run(values[5].run_once()) is None
    cp, events, _ = inspect(values)
    assert not values[-1].calls and values[-1].preflights == 0
    assert not any(e.kind == 'agent_v2_read_batch' for e in events)
    assert cp.usage.get('reads', 0) == 0 and cp.usage.get('writes', 0) == 0


def test_last_path_is_denied_by_whole_sandbox_preflight_zero_application_reads(tmp_path):
    value = reads(6);value['choices'][0]['message']['tool_calls'][-1]['function']['arguments'] = '{"path":"linked/secret"}'
    values = setup(tmp_path, value, gateway=ReadGateway(denied='linked/secret'))
    asyncio.run(values[4].run_once());drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == 'blocked' and cp.usage['reads'] == 6
    assert not values[-1].calls and not any(e.kind == 'agent_v2_tool_started' for e in events)
    stopped = next(e for e in events if e.kind == 'agent_v2_read_batch_stopped')
    assert stopped.payload['actual_receipts'] == [] and len(stopped.payload['not_executed']) == 6


def test_failed_read_keeps_real_prefix_and_stops_second_child(tmp_path):
    value = reads(6)
    for i,c in enumerate(value['choices'][0]['message']['tool_calls']):
        c['function']['arguments'] = json.dumps({'path':'calc.py','offset':i*128})
    values = setup(tmp_path, value, gateway=ReadGateway(fail_at=3))
    asyncio.run(values[4].run_once());drain(values[5])
    cp, events, _ = inspect(values)
    assert cp.state == 'blocked' and cp.usage['reads'] == 6 and len(values[-1].calls) == 3
    stopped = next(e for e in events if e.kind == 'agent_v2_read_batch_stopped')
    assert len(stopped.payload['actual_receipts']) == 3 and len(stopped.payload['not_executed']) == 3
    assert len([e for e in events if e.kind == 'agent_v2_read_sub_batch_started']) == 1
    assert asyncio.run(values[4].run_once()) is None


def test_restart_after_first_child_does_not_repeat_completed_calls(tmp_path):
    value = reads(8)
    for i,c in enumerate(value['choices'][0]['message']['tool_calls']):
        c['function']['arguments'] = json.dumps({'path':'calc.py','offset':i*128})
    values = setup(tmp_path, value);db,s,task,sid,model,tool,runner,gateway=values
    asyncio.run(model.run_once())
    for _ in range(5): asyncio.run(tool.run_once())  # Preflight plus first child.
    first = inspect(values)[0]
    assert len(gateway.calls) == 4
    restarted = AgentToolWorker(db,s,gateway,worker_id='read-batch-restarted')
    drain(restarted)
    cp,events,_=inspect(values)
    assert len(gateway.calls)==8 and len({c.call_id for c in gateway.calls})==8
    assert cp.usage['reads']==first.usage['reads']==8
    assert len(runner.prompts)==1 and cp.state=='ready'


def test_duplicate_reads_and_batches_still_trigger_no_progress_without_coding(tmp_path):
    values = setup(tmp_path, *[reads(5) for _ in range(4)])
    for _ in range(4):
        assert asyncio.run(values[4].run_once()).state == 'succeeded'
        drain(values[5])
    cp,events,ledger=inspect(values)
    assert cp.state=='blocked' and ledger.blocker=='agent_no_progress'
    assert len(values[-1].calls)==1 and cp.usage['reads']==20
    assert ledger.steps[3].status!='completed' and ledger.steps[4].status!='completed'
