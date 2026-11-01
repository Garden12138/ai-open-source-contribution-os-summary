"""Fresh requests reuse only source-bound admitted pages, never budget or authority."""
import asyncio
import json

import pytest

from app.planner import PlanningService, PlanningTurnWorker
from app.planning_budget import planning_usage
from app.planning_read_reuse import prior_read_checkpoint, reused_read_pages
from app.providers.contracts import ProviderRunError, ProviderUsage
from app.schemas import PlanVersionCreateRequest
from app.workbench import WorkbenchError
from tests.test_context_compaction import large_fixture
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import result


def admitted_failure(tmp_path):
    db, settings, task, content, _ = large_fixture(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(task, text='Inspect source', parent_id=None,
            expected_hash=None, key='original', runtime_version='agent-runtime-v2')
    first = ScriptedRunner();complete = first.complete
    async def read(invocation):
        body = json.loads(invocation.prompt)
        ref = next(e['context_reference'] for e in body['evidence']['entries'] if e['path'] == 'background.py')
        first.contents.append(json.dumps({'read_context': [{'reference_id': ref, 'query': 'ORIGINAL_BODY_SENTINEL', 'max_bytes': 8000}]}))
        return await complete(invocation)
    first.complete = read
    worker = PlanningTurnWorker(db, settings, first, _identity(settings.implementation_model), worker_id='read-once')
    assert asyncio.run(worker.run_once()).state == 'succeeded'
    class Failed:
        async def complete(self, invocation):
            raise ProviderRunError('deepseek_completion_truncated', 'Fixed output failure', retryable=False,
                usage=ProviderUsage(input_tokens=123, output_tokens=20))
    worker = PlanningTurnWorker(db, settings, Failed(), _identity(settings.implementation_model), worker_id='failed-after-read')
    assert asyncio.run(worker.run_once()).state == 'failed'
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        cp = wb.latest(task, 'context_checkpoint').payload['artifact_id']
        original = wb.read(cp)['prompt']['context_reads']
        assert original and prior_read_checkpoint(wb, task, None) == cp
    return db, settings, task, content, cp, original


def test_fresh_request_reuses_actual_pages_without_repository_io_round_reset_or_usage_duplication(tmp_path):
    db, settings, task, content, cp, original = admitted_failure(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = planning_usage(wb, task)
        historical = [(e.id, e.record_hash) for e in wb.history(task)]
        job = wb.request(task, text='Amend the tests', parent_id=None, expected_hash=None,
            key='fresh', runtime_version='agent-runtime-v2')
        assert job.payload['prior_read_checkpoint_hash'] == cp and job.payload['round'] == 0
        assert planning_usage(wb, task) == before
    runner = ScriptedRunner(result(content))
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='reuse')
    done = asyncio.run(worker.run_once())
    assert done.state == 'succeeded', done.error_message
    body = json.loads(runner.invocations[0].prompt)
    assert body['context_reads'] == original
    assert body['context_read_reuse']['checkpoint_hash'] == cp
    assert body['context_read_reuse']['repository_reads_executed'] == 0
    assert body['planning_state']['read_rounds_remaining'] == 4
    assert body['context_reads'][0]['source_complete'] is False
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert planning_usage(wb, task)['calls'] == before['calls'] + 1
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(historical)]] == historical
        assert wb.latest(task, 'execution_authorized') is None
    db.close()


@pytest.mark.parametrize('change', ['context', 'plan', 'model', 'stop', 'profile', 'legacy'])
def test_new_request_does_not_reuse_stale_or_incompatible_pages(tmp_path, change):
    db, settings, task, content, cp, _ = admitted_failure(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        if change == 'context':
            wb.append(task, 'context_ready', wb.latest(task, 'context_ready').payload, key='new-context')
        elif change == 'plan':
            payload = content.hash_payload();payload.pop('schema_version')
            wb.save(task, PlanVersionCreateRequest(**payload), context_hash=wb.latest(task, 'context_ready').record_hash, key='changed-plan')
        elif change == 'model':
            wb.append(task, 'model_changed', {'planning_profile_id': 'different'}, key='changed-model')
        elif change == 'stop':
            wb.append(task, 'stop_requested', {}, key='stop')
        parent = wb.latest_plan(task)
        if change == 'profile':
            assert prior_read_checkpoint(wb, task, 'different') is None
        else:
            job = wb.request(task, text='Fresh request', parent_id=parent.id if parent else None,
                expected_hash=parent.record_hash if parent else None, key='fresh',
                **({} if change == 'legacy' else {'runtime_version': 'agent-runtime-v2'}))
            assert 'prior_read_checkpoint_hash' not in job.payload
        assert wb.latest(task, 'execution_authorized') is None
    db.close()


def test_reuse_rechecks_binding_at_consumption_and_rejects_unissued_or_corrupt_receipts(tmp_path):
    db, settings, task, _, cp, _ = admitted_failure(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        with pytest.raises(WorkbenchError):
            reused_read_pages(wb, task, 'a' * 64, None)
        wb.append(task, 'context_ready', wb.latest(task, 'context_ready').payload, key='new-binding')
        with pytest.raises(WorkbenchError) as caught:
            reused_read_pages(wb, task, cp, None)
        assert caught.value.code == 'planning_read_reuse_stale'
    db.close()


def test_corrupt_receipt_fails_closed_before_new_job_or_model_call(tmp_path):
    db, settings, task, _, cp, _ = admitted_failure(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        from app.models import Artifact
        artifact = session.get(Artifact, cp)
        from pathlib import Path
        (Path(settings.artifact_root) / artifact.storage_key).write_bytes(b'corrupt fixture')
        count = len(wb.jobs(task));calls = planning_usage(wb, task)['calls']
        with pytest.raises(WorkbenchError) as caught:
            wb.request(task, text='Fresh', parent_id=None, expected_hash=None, key='bad', runtime_version='agent-runtime-v2')
        assert caught.value.code == 'planning_read_reuse_invalid'
        assert len(wb.jobs(task)) == count and planning_usage(wb, task)['calls'] == calls
    db.close()
