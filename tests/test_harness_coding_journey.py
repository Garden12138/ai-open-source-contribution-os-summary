"""Native-tool domain journey with deterministic runtimes; never real acceptance."""
import asyncio
import hashlib
import json

import pytest

from app.agent_v2.journal import AgentJournal
from app.agent_v2.native_tools import FINISH
from app.agent_v2.loop import AgentToolWorker
from app.contribution_workflow import advance_workflows
from app.model_settings import task_identity
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_agent_v2_loop import FixtureGateway
from tests.test_harness_progress import progress
from tests.test_native_protocol_repairs import fixture
from tests.test_native_read_progress import run_round


@pytest.mark.parametrize('repair', [False, True])
@pytest.mark.parametrize('batch', [False, True])
def test_native_coding_verify_repair_and_independent_review_stop_for_human(tmp_path, repair, batch):
    workers = []
    db, settings, task, sid, model, _, runner, _ = fixture(tmp_path, capture_workers=workers)
    # The protocol fixture starts its session directly; drain the already queued
    # context job before exercising the complete workflow coordinator.
    assert asyncio.run(workers[1].run_once()).state == 'succeeded'
    from tests.test_native_read_batches import ReadGateway, reads, drain
    read_gateway = ReadGateway(); read_gateway.text = "content"
    class Gateway(FixtureGateway):
        async def preflight_read_batch(self, *args, **kwargs):
            return await read_gateway.preflight_read_batch(*args, **kwargs)
        failed = False
        async def execute(self, spec, call, args, **kw):
            value = (await read_gateway.execute(spec, call, args, **kw) if batch and call.name == "read_file"
                else await super().execute(spec, call, args, **kw))
            if repair and call.name == 'run_approved_checks' and not self.failed:
                self.failed = True
                value['result']['data']['outcome'] = 'failed'
            return value
    gateway = Gateway()
    tool = AgentToolWorker(db, settings, gateway, worker_id='native-journey-tool')
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        path, attempt = j.spec().write_paths[0], j.spec().binding.attempt_id
        workers[3].identity = task_identity(session, settings, task, 'review')
    def patch(old, new):
        return response('apply_patch', {'path': path, 'expected_hash': hashlib.sha256(old.encode()).hexdigest(),
            'old_text': old, 'new_text': new})
    runner.responses.extend([reads(8, path=path, max_bytes=16000) if batch else response('read_file', {'path': path}), patch('content', 'changed'), response('run_approved_checks', {})])
    for _ in range(3):
        run_round(model, tool)
        if batch: drain(tool)
    if repair:
        ledger, _ = progress(db, settings, task, sid)
        assert ledger['phase'] == 'repairing'
        runner.responses.extend([patch('changed', 'repaired'), response('run_approved_checks', {})])
        run_round(model, tool)
        run_round(model, tool)
    runner.responses.append(response(FINISH, {'reply': 'CODER_SELF_SUMMARY_SENTINEL'}))
    assert asyncio.run(model.run_once()).state == 'succeeded'
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['phase'] == 'reviewing'
    assert ledger['steps'][5]['status'] == 'pending'
    from app.agent_v2.integration import advance_coding
    with db.session() as session:
        advance_coding(Workbench(session, settings.artifact_root), attempt, settings)
    for _ in range(2):
        job = asyncio.run(workers[0].run_once())
        assert job.state == 'succeeded', job.error_message
    advance_workflows(db, settings)
    workers[3].runner.contents.append(json.dumps({'verdict': 'pass', 'reason_code': 'evidence_checked', 'findings': [
        {'severity': 'info', 'location': path, 'evidence': 'Frozen diff and Verify checked',
            'recommendation': 'Human acceptance', 'verdict': 'pass'}]}))
    reviewed = asyncio.run(workers[3].run_once())
    assert reviewed.state == 'succeeded', reviewed.error_message
    reviewer_prompt = workers[3].runner.invocations[-1].prompt
    assert 'CODER_SELF_SUMMARY_SENTINEL' not in reviewer_prompt
    envelope = json.loads(reviewer_prompt.split('=', 1)[1])
    assert envelope['unified_diff'] and envelope['normalized_test_results']
    advance_workflows(db, settings)
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['phase'] == 'ready_for_human' and ledger['steps'][5]['status'] == 'completed'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        j = AgentJournal(wb, task, sid)
        from app.agent_v2.coding_context import observe
        coding, _ = observe(j)
        assert coding['first_patch_round'] == 2
        assert coding['next_action'] == 'human_acceptance'
        assert j.checkpoint().usage.get('code_repairs', 0) == int(repair)
        assert wb.latest(task, 'awaiting_acceptance')
        assert not wb.latest(task, 'execution_accepted') and not wb.latest(task, 'publication_confirmed')
        assert len([e for e in wb.history(task) if e.kind == 'execution_authorized']) == 1
