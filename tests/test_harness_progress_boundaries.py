"""Stale evidence, page boundaries, CAS and illegal completion regressions."""
import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.agent_v2.contracts import ToolCall, ToolResult
from app.agent_v2.journal import AgentJournal
from app.agent_v2.progress import record_tool
from app.api import create_app
from app.workbench import Workbench, WorkbenchError
from tests.test_agent_native_tools import response
from tests.test_harness_progress import progress
from tests.test_native_read_progress import setup, run_round, results
from tests.test_native_protocol_repairs import fixture


@pytest.mark.parametrize('last', [{'max_bytes': 8192}, {'expected_hash': '0' * 64}])
def test_changed_request_or_expected_hash_cannot_reuse_a_different_receipt(tmp_path, last):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, last)
    run_round(model, tool)
    run_round(model, tool)
    assert len(gateway.calls) == 2


def test_utf8_byte_ranges_remain_distinct_and_exact(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path,
        {'max_bytes': 384}, {'offset': 384, 'max_bytes': 384}, {'offset': 384, 'max_bytes': 384})
    gateway.text = '中' * 3000
    for _ in range(3):
        run_round(model, tool)
    cp, receipts = results(db, settings, task, sid)
    assert len(gateway.calls) == 2 and cp.state == 'ready'
    assert receipts[-1]['data']['offset'] == 384 and receipts[-1]['data']['next_offset'] == 768
    assert len(receipts[-1]['data']['text'].encode()) == 384


def test_different_limits_returning_identical_bytes_do_not_reset_no_progress(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path,
        {'max_bytes': 128}, {'max_bytes': 256}, {'max_bytes': 512}, {'max_bytes': 1024})
    gateway.text = 'short complete source'
    for _ in range(3):
        run_round(model, tool)
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['no_progress_streak'] == 2 and ledger['correction_issued']
    run_round(model, tool)
    assert len(gateway.calls) == 4  # Different legal requests still execute.
    assert results(db, settings, task, sid)[0].state == 'blocked'


def test_forged_artifact_without_worker_receipt_cannot_complete_a_step(tmp_path):
    db, settings, task, sid, *_ = fixture(tmp_path)
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        cp = j.checkpoint()
        artifact = j.wb.artifact(ToolResult(call_id='invented', input_hash='a' * 64,
            source_hash=cp.source_hash, output_source_hash=cp.source_hash,
            status='succeeded', data={'tool': 'run_approved_checks', 'outcome': 'passed'}).model_dump())
        with pytest.raises(WorkbenchError, match='进度对应'):
            record_tool(j, cp, ToolCall(call_id='invented', name='run_approved_checks', arguments={}),
                artifact, batch_complete=True, job_id='invented-job')
        session.rollback()


def test_progress_revision_is_cas_guarded_and_old_ledger_is_immutable(tmp_path):
    db, settings, task, sid, *_ = fixture(tmp_path)
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        prior = next(e for e in reversed(j.events()) if e.kind == 'agent_v2_progress')
        old = prior.record_hash
        with pytest.raises(IntegrityError):
            j.append('progress', prior.payload, key='stale-progress-cas')
        session.rollback()
        assert next(e for e in reversed(j.events()) if e.kind == 'agent_v2_progress').record_hash == old


def test_check_without_patch_does_not_skip_implementation_or_review_gate(tmp_path):
    db, settings, task, sid, model, tool, *_ = fixture(tmp_path, response('run_approved_checks', {}))
    run_round(model, tool)
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['phase'] == 'investigating'
    assert next(s for s in ledger['steps'] if s['step_id'] == 'verify')['status'] == 'pending'
    assert next(s for s in ledger['steps'] if s['step_id'] == 'review')['status'] == 'pending'


def test_source_edit_invalidates_completed_verify_evidence(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path)
    with db.session() as session:
        path = AgentJournal(Workbench(session, settings.artifact_root), task, sid).spec().write_paths[0]
    def patch(old, new):
        return response('apply_patch', {'path': path, 'expected_hash': hashlib.sha256(old.encode()).hexdigest(),
            'old_text': old, 'new_text': new})
    runner.responses.extend([patch('content', 'first'), response('run_approved_checks', {}), patch('first', 'second')])
    run_round(model, tool)
    run_round(model, tool)
    checked, _ = progress(db, settings, task, sid)
    assert next(s for s in checked['steps'] if s['step_id'] == 'verify')['status'] == 'completed'
    run_round(model, tool)
    changed, _ = progress(db, settings, task, sid)
    verify = next(s for s in changed['steps'] if s['step_id'] == 'verify')
    assert verify['status'] == 'pending' and verify['evidence_refs'] == []
    assert changed['source_hash'] != checked['source_hash']


def test_new_source_hash_invalidates_cached_read_even_in_the_same_phase(tmp_path):
    from tests.test_agent_v2_loop import FixtureGateway
    from app.agent_v2.loop import AgentToolWorker
    class VersionedGateway(FixtureGateway):
        async def execute(self, spec, call, arguments, **kw):
            if call.name == 'apply_patch':
                text = (kw['snapshot'] or {}).get('files', {}).get(arguments.path, {}).get('content', 'content')
                assert hashlib.sha256(text.encode()).hexdigest() == arguments.expected_hash
            value = await super().execute(spec, call, arguments, **kw)
            if call.name == 'read_file':
                text = (kw['snapshot'] or {}).get('files', {}).get(arguments.path, {}).get('content', 'content')
                value['result']['data'].update(path=arguments.path, offset=0, text=text,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(), total_bytes=len(text.encode()),
                    next_offset=None, execution_backend='docker')
            return value
    db, settings, task, sid, model, _, runner, _ = fixture(tmp_path)
    gateway = VersionedGateway()
    tool = AgentToolWorker(db, settings, gateway, worker_id='versioned-source')
    with db.session() as session:
        path = AgentJournal(Workbench(session, settings.artifact_root), task, sid).spec().write_paths[0]
    def patch(old, new):
        return response('apply_patch', {'path': path, 'expected_hash': hashlib.sha256(old.encode()).hexdigest(),
            'old_text': old, 'new_text': new})
    runner.responses.extend([patch('content', 'first'), response('read_file', {'path': path}),
        patch('first', 'second'), response('read_file', {'path': path})])
    for _ in range(4):
        run_round(model, tool)
    _, receipts = results(db, settings, task, sid)
    assert [r['data']['text'] for r in receipts if r['data']['tool'] == 'read_file'] == ['first', 'second']
    assert len([c for c in gateway.calls if c.name == 'read_file']) == 2
    assert receipts[-1]['data'].get('reused_receipt') is None


def test_runtime_exposes_task_scoped_progress_and_response_artifacts(tmp_path):
    db, settings, task, sid, model, tool, *_ = fixture(tmp_path, response('list_paths', {}))
    run_round(model, tool)
    with TestClient(create_app(settings)) as client:
        route = f'/api/v1/tasks/{task}/workbench/runtime'
        view = client.get(route).json()
        assert view['implementation_progress']['revision'] >= 2
        assert view['implementation_progress']['coding']['rounds_without_applied_patch'] == 1
        assert view['implementation_progress']['coding']['delivered_source_bytes'] == 0
        for event in view['timeline']:
            if event['kind'] not in {'agent_v2_model_response', 'agent_v2_progress'}:
                continue
            page = client.get(route + '/artifacts/' + event['payload']['artifact_id'])
            assert page.status_code == 200
            assert 'PRIVATE_REASONING_SENTINEL' not in page.text
        assert client.get(route + '/artifacts/' + 'a' * 64).status_code == 409


def test_native_ledger_and_read_receipt_survive_a_real_worker_process_restart(tmp_path):
    import subprocess
    import sys
    from dataclasses import asdict
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {})
    assert asyncio.run(model.run_once()).state == 'succeeded'
    payload = asdict(settings)
    payload['sandbox_job_spec_signing_key'] = settings.sandbox_job_spec_signing_key.hex()
    code = '''
import asyncio,json,sys
from app.config import Settings
from app.database import Database
from app.agent_v2.loop import AgentToolWorker
from tests.test_native_read_progress import SourceGateway
v=json.load(sys.stdin)
v['sandbox_job_spec_signing_key']=bytes.fromhex(v['sandbox_job_spec_signing_key'])
settings=Settings(**v)
db=Database(settings.database_url)
gateway=SourceGateway()
job=asyncio.run(AgentToolWorker(db,settings,gateway,worker_id='child-native-tool').run_once())
assert job.state=='succeeded'
print(json.dumps({'source_calls':len(gateway.calls)}))
db.close()
'''
    completed = subprocess.run([sys.executable, '-c', code], input=json.dumps(payload),
        text=True, capture_output=True, timeout=30, check=True)
    assert json.loads(completed.stdout)['source_calls'] == 1
    run_round(model, tool)
    assert gateway.calls == []  # Parent reused the child's persisted receipt.
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['no_progress_streak'] == 1
