"""Unavailable source stays unavailable while fixed-enum facts inform repair."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tarfile

import pytest

from app.baseline_metadata import KIND
from app.contribution_workflow import advance_workflows, stop_workflow
from app.planner import PlanningContextWorker, PlanningService
from app.sandbox_worker.import_metadata import VERSION, entry_points_origin, validate_metadata
from tests.test_baseline_feedback import prepared, decision


@pytest.mark.parametrize('source,origin', [
    ('from importlib.metadata import entry_points', 'importlib.metadata'),
    ('from importlib_metadata import entry_points as ep', 'importlib_metadata'),
    ('from pkg_resources import entry_points', 'pkg_resources'),
    ('from opentelemetry.util._importlib_metadata import entry_points', 'opentelemetry.util._importlib_metadata'),
    ('from .importlib.metadata import entry_points', 'unknown'),
    ('from arbitrary import entry_points', 'unknown'),
    ('from importlib.metadata import entry_points\nentry_points = 3', 'unknown'),
    ('from importlib.metadata import entry_points\ndef f(entry_points): pass', 'unknown'),
    ('from importlib.metadata import entry_points\nfrom pkg_resources import entry_points', 'unknown'),
    ('broken : : syntax', 'unknown'),
])
def test_import_probe_returns_only_a_fixed_enum(source, origin):
    assert entry_points_origin(source) == origin


@pytest.mark.parametrize('change', ['ok', 'stop_during', 'context_during', 'malformed'])
def test_metadata_job_continues_readonly_planning_without_opening_source(tmp_path, monkeypatch, change):
    from app.archives import RepositoryArchiveStore
    from app.sandbox_worker.coding_context import CodingContextError
    db, settings, task, _, _, _, runner, planner, sources = prepared(tmp_path, monkeypatch, entrypoints=True)
    advance_workflows(db, settings)
    decision(runner, 'revise_plan', sources)
    assert asyncio.run(planner.run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        auth = wb.latest(task, 'execution_authorized').record_hash
        context = wb.latest(task, 'context_ready')
        original_evidence = wb.read(context.payload['artifact_id'])
        original_events = [(e.id, e.record_hash) for e in wb.history(task)]
    queued = asyncio.run(planner.run_once())
    assert queued.state == 'succeeded' and queued.result_data['metadata_requested']
    assert len(runner.invocations) == 1  # Only the preceding baseline diagnosis.
    calls = []
    class Runtime:
        async def inspect_import_metadata(self, **kwargs):
            calls.append(kwargs)
            if change != 'ok':
                with db.session() as session:
                    wb = PlanningService(session, settings.artifact_root)
                    if change == 'stop_during':
                        stop_workflow(wb, task, key='stop-metadata')
                    elif change == 'context_during':
                        wb.append(task, 'context_ready', context.payload, key='new-context')
            return {'version': VERSION, 'entries':[{'path':kwargs['paths'][0], 'source_hash':'a'*64,
                'origin':'unbounded source text' if change == 'malformed' else 'importlib.metadata'}],
                'archive_hash':context.payload['archive_hash'], 'base_sha':context.payload['base_sha'],
                'image':kwargs['image_digest'], 'policy_hash':kwargs['policy'].policy_hash,
                'full_source_available':False}
    monkeypatch.setattr(RepositoryArchiveStore, 'lookup', lambda *a, **kw: object())
    worker = PlanningContextWorker(db, settings, Runtime(), worker_id='metadata')
    job = asyncio.run(worker.run_once())
    assert len(calls) == 1 and calls[0]['paths'] == ('python/beta/tests/test_beta.py',)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task, 'execution_authorized').record_hash == auth
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(original_events)]] == original_events
        assert wb.read(context.payload['artifact_id']) == original_evidence
        assert next(e for e in original_evidence['entries'] if e['path'].endswith('tests/test_beta.py'))['content'] is None
        assert len([j for j in wb.jobs(task) if j.kind == KIND]) == 1
        if change == 'ok':
            assert job.state == 'succeeded', job.error_message
            assert wb.latest(task, 'baseline_import_metadata_ready')
            assert not wb.latest(task, 'baseline_repair_ready')
            from app.workbench_view import workbench_view
            assert workbench_view(wb, task)['baseline_feedback']['status'] == 'replanning'
        else:
            assert job.state == ('cancelled' if change == 'stop_during' else 'failed')
            assert wb.latest(task, 'baseline_import_metadata_ready') is None
    if change == 'ok':
        runner.contents.append(json.dumps({'reply':'导入来源已确认，但完整源码仍不可用。',
            'questions':[{'id':'material','prompt':'需要另一个安全类型接口的定义。','options':[]}]}))
        assert asyncio.run(planner.run_once()).state == 'succeeded'
        prompt = json.loads(runner.invocations[-1].prompt)
        assert prompt['baseline_recovery']['import_metadata']['entries'][0]['origin'] == 'importlib.metadata'
        assert asyncio.run(worker.run_once()) is None  # No repeated probe.
    db.close()


@pytest.mark.parametrize('stale', [False, True])
@pytest.mark.parametrize('failure', ['provider', 'invalid_output'])
def test_native_continue_preserves_recovery_after_provider_failure(tmp_path, monkeypatch, stale, failure):
    from app.conversation import receive_message, advance_conversations
    from app.workbench_view import workbench_view
    db, settings, task, _, _, _, runner, planner, sources = prepared(tmp_path, monkeypatch)
    advance_workflows(db, settings); decision(runner, 'revise_plan', sources)
    assert asyncio.run(planner.run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    if failure == 'invalid_output':
        for _ in range(3):
            runner.contents.append(json.dumps({'reply':'没有形成可执行修订。'}))
            failed = asyncio.run(planner.run_once())
            assert failed.state == 'succeeded' and failed.result_data['plan_status'] == 'rejected'
    else:
        failed = asyncio.run(planner.run_once())
        assert failed.state == 'failed'  # Fake provider deliberately has no response.
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        auth = wb.latest(task, 'execution_authorized').record_hash
        if stale:
            context = wb.latest(task, 'context_ready')
            wb.append(task, 'context_ready', context.payload, key='changed-before-resume')
            from app.baseline_feedback import resume_failed_replanning
            assert resume_failed_replanning(wb, task, text='继续', key='stale-resume') is None
        else:
            first = receive_message(wb, task, text='继续', key='native-resume', settings=settings)
            assert receive_message(wb, task, text='继续', key='native-resume', settings=settings) == first
            session.commit()
            advance_conversations(db, settings)
            session.expire_all()
            retry = wb.jobs(task)[-1]
            assert retry.id != failed.id and retry.kind == 'planning_turn'
            assert retry.payload['baseline_recovery_hash'] == failed.payload['baseline_recovery_hash']
            assert retry.payload['context_id'] == failed.payload['context_id']
            assert workbench_view(wb, task)['baseline_feedback']['status'] == 'replanning'
            assert not workbench_view(wb, task)['actions']['can_execute']
        assert wb.latest(task, 'execution_authorized').record_hash == auth
        assert not any(j.kind == 'coding_context' for j in wb.jobs(task))
    db.close()


def test_metadata_validator_rejects_unbounded_or_extra_fields():
    from app.sandbox_worker.coding_context import CodingContextError
    with pytest.raises(CodingContextError):
        validate_metadata({'version':VERSION,'entries':[{'path':'x.py','origin':'unknown',
            'source_hash':None, 'content':'must not escape'}]}, ('x.py',))


@pytest.mark.skipif(os.getenv('CONTRIBOS_RUN_DOCKER_ACCEPTANCE') != '1', reason='real enum-only metadata sandbox acceptance')
def test_real_probe_never_exports_or_executes_protected_source(tmp_path):
    from app.sandbox_worker.explore import RepositoryArchive
    from app.sandbox_worker.planning_context import DockerPlanningContextRuntime
    from app.sandbox_worker.specs import SandboxPolicy
    canary = 'ghp_metadataonlycanary123456789012345678'
    source = 'from importlib.metadata import entry_points\nTOKEN = "' + canary + '"\nraise RuntimeError("must not execute")\n'
    path = tmp_path / 'repository.tar.gz'
    with tarfile.open(path, 'w:gz') as bundle:
        member = tarfile.TarInfo('fixture-' + 'a' * 40 + '/tests/test_imports.py')
        member.size = len(source.encode()); member.mode = 0o644
        bundle.addfile(member, io.BytesIO(source.encode()))
    archive = RepositoryArchive(repository_full_name='fixture/fixture', base_commit_sha='a'*40,
        archive_hash=hashlib.sha256(path.read_bytes()).hexdigest(), path=path, size_bytes=path.stat().st_size)
    environment = {k:v for k,v in os.environ.items() if k in {'HOME','PATH','TMPDIR','DOCKER_HOST',
        'DOCKER_CONFIG','DOCKER_CONTEXT','DOCKER_CERT_PATH','DOCKER_TLS_VERIFY'}}
    runtime = DockerPlanningContextRuntime(docker_environment=environment)
    kwargs = {'archive':archive,'image_digest':os.environ['CONTRIBOS_SANDBOX_RUNNER_IMAGE'],
              'policy':SandboxPolicy(),'paths':('tests/test_imports.py',)}
    async def run():
        before = await runtime.inspect(**kwargs, query='')
        facts = await runtime.inspect_import_metadata(**kwargs)
        after = await runtime.inspect(**kwargs, query='')
        assert before == after and before['entries'][0]['content'] is None
        assert facts['entries'] == [{'path':'tests/test_imports.py',
            'source_hash':hashlib.sha256(source.encode()).hexdigest(), 'origin':'importlib.metadata'}]
        assert facts['full_source_available'] is False
        assert canary not in json.dumps(facts) and 'RuntimeError' not in json.dumps(facts)
    asyncio.run(run())
    archive.verify()
