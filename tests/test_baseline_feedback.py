"""A failed pre-change baseline reaches bounded diagnosis without a new grant."""
from __future__ import annotations
import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.baseline_feedback import KIND, POLICY, advance_feedback
from app.contribution_workflow import advance_workflows, stop_workflow, retry_execution_job
from app.executions import ExecutionAttemptService
from app.models import CodingSession, ExecutionAttempt, Job
from app.planner import PlanningService, PlanningTurnWorker
from app.provenance import content_hash
from app.sandbox_worker.fake import FakeVerifyRuntime
from app.sandbox_worker.verification import VerifyCommandStatus
from app.task_states import ContributionTaskStateService
from tests.test_multipackage_execution import package_files
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_sandbox_dependencies import _FakeDependencyRuntime, _lease


def prepared(tmp_path, monkeypatch, *, environment=False, truncated=False, entrypoints=False):
    from tests import test_workbench as fixtures
    from app.plans import PlanCommand
    files = package_files()
    sources = tuple(p for p in files if p.endswith(('alpha.py', 'beta.py')) and '/src/' in p)
    original = fixtures._plan_content()
    monkeypatch.setattr(fixtures, '_plan_content', lambda: replace(original,
        files_to_inspect=sources, files_likely_to_change=sources,
        commands_to_run=tuple(PlanCommand(name, name, ('python', '-m', 'pytest', 'tests'), 'python/' + name)
                              for name in ('alpha', 'beta'))))
    entries = {p: {'path': p, 'content': text, 'prior_hash': hashlib.sha256(text.encode()).hexdigest(),
                   'executable': False} for p, text in files.items()}
    if entrypoints:
        entries['python/beta/tests/test_beta.py'].update(content=None, prior_hash=None, executable=None)
    def configure(db, settings, task):
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            old = wb.latest(task, 'context_ready')
            evidence = {'inventory': sorted(files), 'entries': list(entries.values()), 'truncated': False}
            wb.append(task, 'context_ready', {**old.payload, 'artifact_id': wb.artifact(evidence)}, key='multi-context')
    db, settings, task, workers, code, review = fixtures.automatic_fixture(tmp_path, configure_models=configure)
    class Inspector:
        async def inspect(self, **kwargs): return {'entries': [entries[p] for p in kwargs['paths']]}
    class Proxy:
        async def start(self, *, image_digest, policy): return _lease(policy)
        async def stop(self, lease, **kwargs): pass
    message = ("ModuleNotFoundError: No module named 'missing_test_dependency'" if environment
               else 'src/shared_namespace/instrumentation/beta.py:1: error: incompatible types [assignment]')
    if entrypoints:
        message = 'tests/test_beta.py:79: error: Call to untyped function "entry_points" in typed context [no-untyped-call]'
    class Failure(FakeVerifyRuntime):
        async def run(self, **kwargs):
            value = await super().run(**kwargs)
            evidence = replace(value.command_evidence[1], status=VerifyCommandStatus.FAILED,
                exit_code=1, stdout_log=message, stdout_bytes=len(message),
                stdout_hash=hashlib.sha256(message.encode()).hexdigest(), logs_truncated=truncated)
            return replace(value, command_evidence=(value.command_evidence[0], evidence))
    workers[-1].dependencies, workers[-1].proxy, workers[-1].inspector = _FakeDependencyRuntime(), Proxy(), Inspector()
    workers[-1].verifier = Failure()
    assert asyncio.run(workers[0].run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    assert asyncio.run(workers[-1].run_once()).state == 'succeeded'
    runner = ScriptedRunner()
    planner = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='diagnose')
    return db, settings, task, workers, code, review, runner, planner, sources


def decision(runner, action, paths):
    runner.contents.append(json.dumps({'summary': '已依据原始检查结果定位问题，保留既有检查。',
                                      'action': action, 'change_paths': list(paths)}))


def test_failed_baseline_automatically_diagnoses_then_reaches_original_coding(tmp_path, monkeypatch):
    db, settings, task, workers, code, _, runner, planner, sources = prepared(tmp_path, monkeypatch)
    advance_workflows(db, settings)
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        original = [(e.id, e.record_hash) for e in wb.history(task)]
        auth = wb.latest(task, 'execution_authorized')
        assert auth.payload['baseline_failure_policy'] == POLICY
        assert len([j for j in wb.jobs(task) if j.kind == KIND]) == 1
        assert session.scalar(select(CodingSession)) is None
    decision(runner, 'repair_within_plan', sources)
    job = asyncio.run(planner.run_once())
    assert job.state == 'succeeded', job.error_message
    prompt = json.loads(runner.invocations[0].prompt)
    assert 'incompatible types' in prompt['baseline']['outcome']['failures'][0]['stdout']
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(original)]] == original
        assert wb.latest(task, 'execution_authorized').record_hash == auth.record_hash
        assert wb.latest(task, 'baseline_repair_ready')
        assert any(j.kind == 'coding_context' and j.state == 'queued' for j in wb.jobs(task))
        assert wb.latest(task, 'baseline_replan_requested') is None
        assert wb.latest(task, 'publication_confirmed') is None
    assert not code.invocations
    assert asyncio.run(workers[1].run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    code.contents.append(json.dumps({'reply': '按原批准范围修复，并运行全部原验证命令。'}))
    turn = asyncio.run(workers[2].run_once())
    assert turn.state == 'succeeded', turn.error_message
    assert 'incompatible types' in code.invocations[0].prompt
    from app.workbench_view import workbench_view
    with db.session() as session:
        view = workbench_view(PlanningService(session, settings.artifact_root), task)
        assert view['baseline_feedback']['status'] == 'repairing'
        assert view['baseline']['outcome']['status'] == 'failed'
        assert view['verification']['status'] == 'not_run'
        assert view['actions']['can_accept'] is False
    db.close()


@pytest.mark.parametrize('case', ['outside', 'environment', 'truncated', 'legacy'])
def test_diagnosis_replans_before_any_outside_or_environment_change(tmp_path, monkeypatch, case):
    if case == 'legacy':
        import app.execution_preflight as preflight
        inspect = preflight.task_preflight
        def legacy_report(*args, **kwargs):
            result = inspect(*args, **kwargs)
            result.pop('baseline_failure_policy', None)
            result.pop('report_hash')
            return {**result, 'report_hash': content_hash(result)}
        monkeypatch.setattr(preflight, 'task_preflight', legacy_report)
    db, settings, task, workers, _, _, runner, planner, sources = prepared(
        tmp_path, monkeypatch, environment=case == 'environment', truncated=case == 'truncated')
    advance_workflows(db, settings)
    # Even an overconfident model cannot authorize paths or environment changes.
    decision(runner, 'repair_within_plan', ['python/outside.py'] if case == 'outside' else sources)
    assert asyncio.run(planner.run_once()).state == 'succeeded'
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        history = [(e.id, e.record_hash) for e in wb.history(task)]
        original_plan = wb.latest_plan(task)
        auth = wb.latest(task, 'execution_authorized').record_hash
    advance_workflows(db, settings)
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert ContributionTaskStateService(session).current(task).to_state == 'planning'
        assert wb.latest(task, 'baseline_replan_requested')
        request = wb.latest(task, 'planning_recovery_requested')
        assert request and request.actor_id == 'orchestrator'
        assert len([j for j in wb.jobs(task) if j.kind == 'planning_turn' and j.state == 'queued']) == 1
        assert not any(j.kind == 'coding_context' for j in wb.jobs(task))
        assert wb.latest_plan(task).id == original_plan.id
        assert wb.latest(task, 'execution_authorized').record_hash == auth
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(history)]] == history
    # Read-only repair planning produces a child; it never starts that child.
    content = original_plan
    from app.plans import PlanVersionService
    request = PlanVersionService._validated_content(content).hash_payload()
    request.pop('schema_version')
    request['goal'] += ' with diagnosed baseline repair'
    runner.contents.append(json.dumps({'reply': '根据真实失败修订方案。', 'questions': [], 'read_paths': [], 'plan': request}))
    planned = asyncio.run(planner.run_once())
    assert planned.state == 'succeeded', planned.error_message
    assert json.loads(runner.invocations[-1].prompt)['baseline_recovery']['diagnosis']
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        child = wb.latest_plan(task)
        assert child.parent_version_id == original_plan.id
        assert wb.latest(task, 'execution_authorized').record_hash == auth
        assert not any(j.kind == 'contribution_start' and j.state == 'queued' for j in wb.jobs(task))
        wb.append(task, 'assistant_message', {'plan_id':child.id,'plan_error':{'constraint':'review_rejected'},
            'plan_receipt':{'status':'rejected'}},key='trusted-plan-review',actor='orchestrator',plan_id=child.id)
        from app.workbench_view import workbench_view
        view=workbench_view(wb,task)
        assert view['baseline_feedback']['status']=='failed'
        assert view['actions']['can_execute'] is False
        wb.append(task, 'assistant_message', {'reply':'继续分析已有证据。','questions':[],
            'plan_error':None},key='later-plain-message',actor='planner')
        view=workbench_view(wb,task)
        assert view['baseline_feedback']['status']=='failed'
        assert view['actions']['can_execute'] is False
    db.close()


def test_upgrade_preserves_approvals_and_rejects_unproven_replan_transition(tmp_path, monkeypatch):
    import app.migrations.versions as versions
    all_migrations = versions.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(versions, 'MIGRATIONS', tuple(m for m in all_migrations if m.revision < '0037'))
        db, settings, task, _, _, _, runner, planner, sources = prepared(tmp_path, patch)
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            before = [(e.id, e.record_hash) for e in wb.history(task)]
            baseline = wb.latest(task, 'execution_baseline_ready').payload['result_hash']
            baseline_bytes = wb.artifacts.read_bytes(baseline)
    assert db.create_schema().applied == ('0037_baseline_feedback', '0038_agent_runtime_v2')
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert [(e.id, e.record_hash) for e in wb.history(task)] == before
        assert wb.artifacts.read_bytes(baseline) == baseline_bytes
        states = ContributionTaskStateService(session)
        state = states.current(task)
        from app.task_states import TaskStateConflictError
        with pytest.raises(TaskStateConflictError) as rejected:
            states.transition(task, expected_sequence=state.sequence, expected_record_hash=state.record_hash,
                              to_state='planning', reason_code='baseline_requires_replan')
        assert isinstance(rejected.value.__cause__, IntegrityError)
        session.rollback()
        assert states.current(task).record_hash == state.record_hash
    advance_workflows(db, settings)
    decision(runner, 'revise_plan', sources)
    assert asyncio.run(planner.run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    with db.session() as session:
        assert ContributionTaskStateService(session).current(task).to_state == 'planning'
        assert PlanningService(session, settings.artifact_root).latest(task, 'baseline_replan_requested')
    db.close()


@pytest.mark.parametrize('change', ['stop', 'stop_during', 'context', 'model', 'failed_provider', 'canary'])
def test_diagnosis_cannot_resume_after_changed_or_untrusted_inputs(tmp_path, monkeypatch, change):
    db, settings, task, _, _, _, runner, planner, sources = prepared(tmp_path, monkeypatch)
    advance_workflows(db, settings)
    decision(runner, 'repair_within_plan', sources)
    if change in {'stop', 'context'}:
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            if change == 'stop': stop_workflow(wb, task, key='stop-diagnosis')
            else:
                context = wb.latest(task, 'context_ready')
                wb.append(task, 'context_ready', context.payload, key='changed-context')
    elif change == 'stop_during':
        complete = runner.complete
        async def interrupted(invocation):
            result = await complete(invocation)
            with db.session() as session:
                stop_workflow(PlanningService(session, settings.artifact_root), task, key='stop-during-diagnosis')
            return result
        runner.complete = interrupted
    elif change == 'model':
        settings = replace(settings, implementation_model='changed-model')
        planner.settings = settings
    elif change == 'failed_provider':
        runner.contents.clear()
    elif change == 'canary':
        runner.contents[:] = [json.dumps({'summary': 'api_key=baselinefeedbackcanary123456',
            'action': 'repair_within_plan', 'change_paths': list(sources)})]
    job = asyncio.run(planner.run_once())
    if change == 'stop':
        assert job is None  # Queued diagnosis was cancelled with the stop scope.
    elif change == 'stop_during':
        assert job.state == 'cancelled'
    else:
        assert job.state == 'failed'
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task, 'baseline_repair_ready') is None
        assert not any(j.kind == 'coding_context' for j in wb.jobs(task))
        assert wb.latest(task, 'baseline_diagnosed') is None
        dump='\n'.join(session.connection().connection.driver_connection.iterdump())
        assert 'baselinefeedbackcanary123456' not in dump
    db.close()


@pytest.mark.parametrize('change', ['command', 'scope', 'manifest', 'unresolved', 'shadow'])
def test_automatic_replan_preserves_original_commands_and_scope(tmp_path, monkeypatch, change):
    from app.plans import PlanVersionService
    db, settings, task, _, _, _, runner, planner, sources = prepared(tmp_path, monkeypatch)
    advance_workflows(db, settings)
    decision(runner, 'revise_plan', sources)
    assert asyncio.run(planner.run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.latest_plan(task)
        content = PlanVersionService._validated_content(plan).hash_payload()
        content.pop('schema_version')
    original = json.loads(json.dumps(content))
    if change == 'command':
        content['commands_to_run'][0]['argv'].append('-x')
    elif change == 'scope':
        content['files_likely_to_change'] = content['files_likely_to_change'][:1]
    elif change == 'manifest':
        content['files_likely_to_change'].append('python/beta/pyproject.toml')
    elif change == 'unresolved':
        content['questions_for_maintainer'] = ['Accept failed checks?']
    else:
        content['files_likely_to_change'].append(sources[0] + 'i')
    runner.contents.append(json.dumps({'reply': '修订。', 'plan': content}))
    job = asyncio.run(planner.run_once())
    assert job.state == 'succeeded' and job.result_data['repair_scheduled']
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task, 'assistant_message').payload['plan_error']['constraint'] == (
            'baseline_commands_changed' if change == 'command' else 'baseline_scope_reduced' if change == 'scope'
            else 'baseline_frozen_manifest_change' if change == 'manifest'
            else 'baseline_acceptance_unresolved' if change == 'unresolved'
            else 'baseline_type_stub_shadows_source')
        assert wb.latest_plan(task).id == plan.id
    original['goal'] += ' with diagnosed repair'
    runner.contents.append(json.dumps({'reply': '保留全部原检查后修订。', 'plan': original}))
    job = asyncio.run(planner.run_once())
    assert job.state == 'succeeded' and job.result_data['plan_status'] == 'saved'
    db.close()


def test_shared_model_budget_stops_new_diagnosis_before_provider(tmp_path, monkeypatch):
    db, settings, task, _, _, _, runner, planner, _ = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr('app.planner.MAX_MODEL_CALLS', 1)
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert not any(j.kind == KIND for j in wb.jobs(task))
        assert wb.active_blocker(task).payload['reason_code'] == 'planning_budget_exceeded'
    assert not runner.invocations
    db.close()


def test_dependency_cleanup_retains_diagnosis_then_releases_replanned_attempt(tmp_path, monkeypatch):
    from app.sandbox_worker.dependency_jobs import DependencyJobWorker
    db, settings, task, workers, _, _, runner, planner, sources = prepared(tmp_path, monkeypatch)
    runtime = workers[-1].dependencies
    cleanup = DependencyJobWorker(db, settings, runtime, workers[-1].proxy, worker_id='cleanup')
    advance_workflows(db, settings)
    asyncio.run(cleanup.cleanup())
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task, 'dependency_workspace_disposed') is None
    decision(runner, 'revise_plan', sources)
    assert asyncio.run(planner.run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    asyncio.run(cleanup.cleanup())
    asyncio.run(cleanup.cleanup())
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert len([e for e in wb.history(task) if e.kind == 'dependency_workspace_disposed']) == 1
        assert wb.latest(task, 'baseline_replan_requested')
        assert any(j.kind == 'planning_turn' and j.state == 'queued' for j in wb.jobs(task))
    db.close()


def test_manifest_edit_is_not_admitted_with_the_old_frozen_environment(tmp_path):
    from tests.test_workbench import seeded
    from app.plans import PlanCommand
    from app.schemas import PlanVersionCreateRequest
    from app.execution_preflight import task_preflight
    from app.contribution_workflow import authorize_execution
    from app.workbench import WorkbenchError
    db, settings, task, content, context = seeded(tmp_path)
    files = package_files()
    sources = tuple(p for p in files if '/src/' in p and p.endswith(('alpha.py', 'beta.py')))
    content = replace(content, files_to_inspect=sources,
        files_likely_to_change=(*sources, 'python/beta/pyproject.toml'),
        commands_to_run=tuple(PlanCommand(name, name, ('python', '-m', 'pytest', 'tests'), 'python/' + name)
                              for name in ('alpha', 'beta')))
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence={'inventory':list(files),'entries':[{'path':p,'content':text,'executable':False,
            'prior_hash':hashlib.sha256(text.encode()).hexdigest()} for p,text in files.items()],'truncated':False}
        context=wb.append(task,'context_ready',{**context.payload,'artifact_id':wb.artifact(evidence)},key='manifest-context')
        values=content.hash_payload();values.pop('schema_version')
        plan=wb.save(task,PlanVersionCreateRequest(**values),context_hash=context.record_hash,key='manifest-plan')
        session.commit()
        report=task_preflight(wb,task)
        assert report['status']=='blocked'
        assert report['issues'][0]['code']=='environment_manifest_change_unsupported'
        with pytest.raises(WorkbenchError) as rejected:
            authorize_execution(wb,task,plan_id=plan.id,plan_hash=plan.record_hash,key='blocked-manifest',settings=settings)
        assert rejected.value.code=='environment_manifest_change_unsupported'
        assert wb.latest(task,'execution_authorized') is None
    db.close()


@pytest.mark.parametrize('prompt', ['请选择授权走向：跳过检查或缩为单包？', 'Do you allow dropping one package?', '缺少失败测试的可读类型信息，具体来源是什么？', '缺少类型信息时选择哪种回退？'])
def test_recovery_rejects_premature_permission_but_preserves_material_questions(tmp_path, monkeypatch, prompt):
    from app.plans import PlanVersionService
    db, settings, task, _, _, _, runner, planner, sources=prepared(tmp_path,monkeypatch)
    advance_workflows(db,settings);decision(runner,'revise_plan',sources)
    assert asyncio.run(planner.run_once()).state=='succeeded'
    advance_workflows(db,settings)
    permission=prompt.startswith(('请选择','Do you','缺少类型信息时'))
    with db.session() as session:
        original=PlanningService(session,settings.artifact_root).latest_plan(task)
        original_id=original.id
        content=PlanVersionService._validated_content(original).hash_payload();content.pop('schema_version')
    runner.contents.append(json.dumps({'reply':'正在核对。','questions':[{'id':'repair_input','prompt':prompt,
        'options':['Add ignore_missing_imports'] if prompt.startswith('缺少类型信息时') else []}],
                                      'plan':content if permission else None}))
    job=asyncio.run(planner.run_once())
    assert job.state=='succeeded'
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        reply=wb.latest(task,'assistant_message').payload
        original=wb.latest_plan(task)
        assert original.id==original_id
        if permission:
            assert reply['questions']==[]
            assert reply['plan_error']['constraint']=='baseline_premature_authorization'
            assert job.result_data['repair_scheduled']
        else:
            assert reply['questions'][0]['prompt']==prompt
            assert not job.result_data.get('repair_scheduled')
    if permission:
        content=PlanVersionService._validated_content(original).hash_payload();content.pop('schema_version')
        content['goal']+=' with a concrete repair'
        runner.contents.append(json.dumps({'reply':'已准备具体修订。','plan':content}))
        repaired=asyncio.run(planner.run_once())
        assert repaired.state=='succeeded' and repaired.result_data['plan_status']=='saved'
    db.close()


def test_provider_failure_can_retry_diagnosis_without_restarting_sandbox(tmp_path, monkeypatch):
    db, settings, task, _, _, _, runner, planner, sources = prepared(tmp_path, monkeypatch)
    advance_workflows(db, settings)
    job = asyncio.run(planner.run_once())
    assert job.state == 'failed'
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.active_blocker(task).payload['retryable'] is True
        retry_execution_job(wb, job.id, settings)
    decision(runner, 'repair_within_plan', sources)
    assert asyncio.run(planner.run_once()).state == 'succeeded'
    advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert len([j for j in wb.jobs(task) if j.kind == 'execution_prepare']) == 1
        assert len([j for j in wb.jobs(task) if j.kind == KIND]) == 1
        assert session.get(Job, job.id).attempt_count == 2
        assert wb.latest(task, 'baseline_repair_ready')
    db.close()
