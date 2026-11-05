"""Contract fixtures for source-bound read progress; never native delivery evidence."""
import asyncio
import copy
import hashlib

import pytest

from app.agent_v2.contracts import ToolResult
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentToolWorker
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_protocol_repairs import fixture


class SourceGateway:
    def __init__(self):
        self.calls = []
        self.text = 'a' * 10000

    async def execute(self, spec, call, arguments, *, source_hash, snapshot, input_hash):
        self.calls.append(call)
        raw = self.text.encode()
        text = raw[arguments.offset:arguments.offset + arguments.max_bytes].decode()
        end = arguments.offset + len(text.encode())
        result = ToolResult(call_id=call.call_id, input_hash=input_hash, source_hash=source_hash,
            output_source_hash=source_hash, status='succeeded', data={'tool': 'read_file',
                'execution_backend': 'docker', 'path': arguments.path, 'offset': arguments.offset,
                'text': text, 'total_bytes': len(raw), 'next_offset': end if end < len(raw) else None,
                'content_hash': hashlib.sha256(raw).hexdigest()})
        return {'result': result.model_dump()}


def setup(tmp_path, *args):
    db, settings, task, sid, model, _, runner, _ = fixture(tmp_path,
        *[response('read_file', {'path': 'calc.py', **a}) for a in args])
    gateway = SourceGateway()
    tool = AgentToolWorker(db, settings, gateway, worker_id='progress-tool')
    return db, settings, task, sid, model, tool, runner, gateway


def run_round(model, tool):
    assert asyncio.run(model.run_once()).state == 'succeeded'
    assert asyncio.run(tool.run_once()).state == 'succeeded'


def results(db, settings, task, sid):
    with db.session() as s:
        journal = AgentJournal(Workbench(s, settings.artifact_root), task, sid)
        return journal.checkpoint(), [journal.wb.read(e.payload['artifact_id'])
            for e in journal.events() if e.kind == 'agent_v2_tool_result']


def test_two_visible_duplicate_receipts_bound_repeated_reads_across_worker_restart(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {}, {}, {})
    for _ in range(3):
        run_round(model, tool)
    cp, raw = results(db, settings, task, sid)
    assert [r['data']['source_read_progress']['prior_same_range_reads'] for r in raw] == [0, 1, 2]
    assert raw[1]['data']['source_read_progress']['prior_receipt']
    tool = AgentToolWorker(db, settings, gateway, worker_id='restarted-progress-tool')
    run_round(model, tool)
    cp, raw = results(db, settings, task, sid)
    assert cp.state == 'blocked' and cp.usage['model_calls'] == cp.usage['reads'] == 4
    assert cp.usage.get('protocol_repairs', 0) == cp.usage.get('code_repairs', 0) == 0
    assert len(gateway.calls) == 1
    assert raw[-1]['data']['execution_backend'] == 'platform_receipt_reuse'
    assert len(raw[-1]['data']['source_read_progress']['warning_receipts_seen']) == 2
    assert asyncio.run(model.run_once()) is None


def test_duplicate_batch_without_any_delivered_warning_is_not_prematurely_blocked(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {})
    first = runner.responses[0]
    item = first['choices'][0]['message']['tool_calls'][0]
    first['choices'][0]['message']['tool_calls'] = [{**copy.deepcopy(item), 'id': str(i)} for i in range(3)]
    assert asyncio.run(model.run_once()).state == 'succeeded'
    for _ in range(3):
        assert asyncio.run(tool.run_once()).state == 'succeeded'
    cp, raw = results(db, settings, task, sid)
    assert cp.state == 'ready' and len(gateway.calls) == 1
    assert all(not r['data']['source_read_progress']['warning_receipts_seen'] for r in raw)
    run_round(model, tool)
    assert results(db, settings, task, sid)[0].state == 'ready'
    # Three identical calls in one turn are one no-progress observation, not three turns.
    runner.responses.extend([response('read_file', {'path': 'calc.py'}) for _ in range(2)])
    run_round(model, tool)
    run_round(model, tool)
    assert results(db, settings, task, sid)[0].state == 'blocked'


@pytest.mark.parametrize('last', [{'offset': 4096}, {'max_bytes': 8192}, {'path': 'other.py'}])
def test_changed_read_range_or_path_can_make_progress(tmp_path, last):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {}, {}, last)
    for _ in range(4):
        run_round(model, tool)
    cp, raw = results(db, settings, task, sid)
    assert cp.state == 'ready' and len(gateway.calls) == 2
    assert raw[-1]['data']['source_read_progress']['prior_same_range_reads'] == 0
