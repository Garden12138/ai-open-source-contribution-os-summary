"""Task-local budget configuration preserves actual usage and authorization."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import create_app
from app.database import Database
from app.planner import PlanningService, PlanningTurnWorker
from app.planning_budget import (
    EVENT, VERSION, PlanningBudgetLimits, PlanningBudgetRequest,
    configure_planning_budget, planning_budget_view, planning_usage, require_planning_budget,
)
from app.workbench import WorkbenchError
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import seeded, result


def update(wb, task, *, key='budget', limits=None, expected_hash=None):
    current = planning_budget_view(wb, task)
    request = PlanningBudgetRequest(expected_hash=expected_hash or current['revision'],
        limits=PlanningBudgetLimits(**(limits or {**current['limits'], 'input_tokens': 3_000_000})))
    return configure_planning_budget(wb, task, request, key=key)


def spent(wb, task):
    wb.append(task, 'assistant_message', {'reply': '原有讨论保留。',
        'usage': {'input_tokens': 2_079_795, 'output_tokens': 52_195}}, key='old-usage', actor='planner')


def test_configuration_keeps_usage_and_history_across_upgrade_and_restart(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        spent(wb, task)
        original = [(event.id, event.record_hash) for event in wb.history(task)]
        context = wb.latest(task, 'context_ready')
        source = wb.artifacts.read_bytes(context.payload['artifact_id'])
        before = planning_budget_view(wb, task)
        assert before['remaining']['input_tokens'] == 0
        with pytest.raises(WorkbenchError): require_planning_budget(wb, task)
        configured = update(wb, task)
        assert configured['limits'] == {'calls': 32, 'input_tokens': 3_000_000, 'output_tokens': 100_000}
        assert configured['usage'] == before['usage']
        assert configured['remaining']['input_tokens'] == 920_205
        require_planning_budget(wb, task)
        assert [(e.id, e.record_hash) for e in wb.history(task)[:-1]] == original
        assert wb.artifacts.read_bytes(context.payload['artifact_id']) == source
        assert not wb.jobs(task) and wb.latest(task, 'execution_authorized') is None
    db.close()
    db = Database(settings.database_url)
    assert db.create_schema().applied == ()
    with db.session() as session:
        assert planning_budget_view(PlanningService(session, settings.artifact_root), task) == configured
    db.close()


def test_other_tasks_keep_their_own_limits_and_cannot_reuse_another_revision(tmp_path):
    from app.planning import ContributionTaskService
    from app.models import AnalysisVersion
    from app.providers.fake import FakeProvider, FakeProviderScript
    from app.providers.budgets import AnalysisBudget
    from app.providers.workflow import ManualAnalysisService, ManualAnalysisRequest
    from app.providers.versions import AnalysisVersionService
    from app.providers.jobs import ProviderAnalysisJobWorker
    from datetime import datetime, timedelta, timezone
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        parent = ContributionTaskService(session).get_verified(task)
        analysis = session.get(AnalysisVersion, parent.analysis_version_id)
        provider = FakeProvider(FakeProviderScript(analyze_output={**analysis.structured_output,
            'problem_summary':'Another independently assessed task.'}))
        job, _ = ManualAnalysisService(session).request(ManualAnalysisRequest(snapshot_id=parent.snapshot_id,
            correlation_id='another-task',idempotency_key='another-task-analysis',
            budget=AnalysisBudget(max_retries=0),expected_provider=provider.identity))
        job_id = job.id
    completed = asyncio.run(ProviderAnalysisJobWorker(db,provider,worker_id='budget-other').run_once(
        now=datetime.now(timezone.utc)+timedelta(seconds=2)))
    assert completed.state == 'succeeded'
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        analysis = AnalysisVersionService(session).create_from_succeeded_job(job_id=job_id)
        other = ContributionTaskService(session).create(analysis_version_id=analysis.id,idempotency_key='other-task')
        assert other.id != task
        first = update(wb, task)
        assert planning_usage(wb, other.id)['limits']['input_tokens'] == 2_000_000
        with pytest.raises(WorkbenchError, match='已变化'):
            update(wb, other.id, expected_hash=first['revision'])
    db.close()


def test_retry_is_idempotent_and_conflicting_retry_is_rejected(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = planning_budget_view(wb, task)
        first = update(wb, task, expected_hash=before['revision'])
        assert update(wb, task, expected_hash=before['revision']) == first
        with pytest.raises(WorkbenchError) as error:
            update(wb, task, limits={**first['limits'], 'input_tokens': 5_000_000}, expected_hash=before['revision'])
        assert error.value.code == 'planning_budget_conflict'
        assert len([event for event in wb.history(task) if event.kind == EVENT]) == 1
    db.close()


@pytest.mark.parametrize('change', ['configuration', 'usage', 'busy', 'below_usage'])
def test_stale_running_and_underfunded_updates_fail_without_appending(tmp_path, change):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = planning_budget_view(wb, task)
        expected = before['revision']
        limits = {**before['limits'], 'input_tokens': 3_000_000}
        if change == 'configuration': update(wb, task, key='other')
        elif change == 'usage': spent(wb, task)
        elif change == 'busy': wb.request(task, text='准备方案', parent_id=None, expected_hash=None, key='running')
        else:
            spent(wb, task)
            expected = planning_budget_view(wb, task)['revision']
            limits['input_tokens'] = 2_000_000
        count = len(wb.history(task))
        with pytest.raises(WorkbenchError) as error:
            update(wb, task, key='stale', limits=limits, expected_hash=expected)
        assert error.value.code == ('planning_budget_busy' if change == 'busy' else
                                    'planning_budget_below_usage' if change == 'below_usage' else 'planning_budget_stale')
        assert len(wb.history(task)) == count
    db.close()


def test_concurrent_edit_has_one_winner_and_never_loses_history(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        basis = planning_budget_view(PlanningService(session, settings.artifact_root), task)['revision']
    def save(number):
        with db.session() as session:
            try:
                update(PlanningService(session, settings.artifact_root), task, key=str(number),
                       limits={'calls':32, 'input_tokens':number * 1_000_000, 'output_tokens':100_000}, expected_hash=basis)
                return 'saved'
            except WorkbenchError as error:
                return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, [3, 5])) == ['planning_budget_stale', 'saved']
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert len([event for event in wb.history(task) if event.kind == EVENT]) == 1
    db.close()


@pytest.mark.parametrize('value', [True, 3_000_000.0, '3000000', 0, 20_000_001])
def test_limits_reject_non_integer_or_unbounded_values(value):
    with pytest.raises(ValidationError):
        PlanningBudgetLimits(calls=32, input_tokens=value, output_tokens=100_000)


@pytest.mark.parametrize('actor,job_bound,version', [('planner',False,VERSION), ('local-user',False,'unknown'),
                                                  ('local-user',True,VERSION)])
def test_model_or_unknown_record_cannot_grant_a_budget(tmp_path, actor, job_bound, version):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = planning_budget_view(wb, task)
        job = wb.enqueue(task, 'planning_turn', {}, key='forged-job') if job_bound else None
        limits = {**before['limits'],'input_tokens':3_000_000}
        wb.append(task, EVENT, {'version':version, 'limits':limits,
            'request':{'expected_hash':before['revision'],'limits':limits}, 'usage_at_change':before['usage'],
            'previous_limits':before['limits'], 'previous_budget_hash':None}, key='forged', actor=actor,
            job_id=job.id if job else None)
        with pytest.raises(WorkbenchError) as error: require_planning_budget(wb, task)
        assert error.value.code == 'planning_budget_invalid'
    db.close()


def test_budget_update_preserves_plan_authorization_baseline_and_source_binding(tmp_path, monkeypatch):
    from app.context_compaction import source_binding
    from app.execution_preflight import task_preflight
    from tests.test_baseline_feedback import prepared
    db, settings, task, *_ = prepared(tmp_path, monkeypatch)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        binding = source_binding(wb, task)
        preflight = task_preflight(wb, task)['report_hash']
        count = len(wb.jobs(task))
        update(wb, task)
        assert source_binding(wb, task) == binding
        assert task_preflight(wb, task)['report_hash'] == preflight
        assert len(wb.jobs(task)) == count
    db.close()


def test_increased_budget_unblocks_planning_with_compression_without_auto_execution(tmp_path):
    from tests.test_context_compaction import large_fixture
    db, settings, task, content, _ = large_fixture(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        spent(wb, task)
        wb.request(task, text='继续方案', parent_id=None, expected_hash=None, key='blocked')
    runner = ScriptedRunner(result(content))
    worker = PlanningTurnWorker(db, settings, runner, _identity(settings.implementation_model), worker_id='budget-resume')
    failed = asyncio.run(worker.run_once())
    assert failed.state == 'failed' and not runner.invocations
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        update(wb, task)
        assert not runner.invocations and wb.latest_plan(task) is None
        assert wb.session.get(type(failed), failed.id).state == 'failed'
        wb.request(task, text='按原范围恢复规划', parent_id=None, expected_hash=None, key='resume')
    completed = asyncio.run(worker.run_once())
    assert completed.state == 'succeeded', completed.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task,'context_checkpoint').payload['stats']['saved_bytes'] > 0
        assert wb.latest_plan(task) and wb.latest(task,'execution_authorized') is None
        assert planning_usage(wb,task)['input_tokens'] >= 2_079_795
    db.close()


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('stale', [False, True])
def test_continue_resumes_budget_interruption_without_reasoning_or_rewriting_failed_job(tmp_path,legacy,stale):
    from app.conversation import receive_message
    from app.planning_budget import failed_budget_planning_job
    from app.schemas import PlanVersionCreateRequest
    db,settings,task,content,context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        values = content.hash_payload(); values.pop('schema_version')
        plan = wb.save(task,PlanVersionCreateRequest(**values),context_hash=context.record_hash,key='saved')
        spent(wb,task)
        wb.request(task,text='保留两个包和所有原检查，修订完整方案。',parent_id=plan.id,
                   expected_hash=plan.record_hash,key='budget-failed')
    runner = ScriptedRunner(result(content))
    worker = PlanningTurnWorker(db,settings,runner,_identity(settings.implementation_model),worker_id='continue-budget')
    failed = asyncio.run(worker.run_once())
    assert failed.state == 'failed'
    with db.session() as session:
        wb = PlanningService(session,settings.artifact_root)
        if legacy:
            row = session.get(type(failed),failed.id)
            row.error_code = 'planning_turn_failed'
            row.error_message = '规划累计用量已达上限，请缩小范围；现有代码和方案已保留'
            session.commit()
        assert failed_budget_planning_job(wb,task).id == failed.id
        update(wb,task)
        if stale:
            from app.planning_budget import resume_budget_planning
            wb.append(task,'context_ready',context.payload,key='changed-source')
            assert failed_budget_planning_job(wb,task) is None
            assert resume_budget_planning(wb,task,text='继续',key='stale-resume') is None
            assert len(wb.jobs(task)) == 1 and not runner.invocations
            db.close()
            return
        old_job = next(job for job in wb.jobs(task) if job.id == failed.id)
        old_signature = (old_job.state,old_job.error_code,old_job.payload_hash,old_job.attempt_count)
        first = receive_message(wb,task,text='继续',key='resume-budget',settings=settings)
        assert receive_message(wb,task,text='继续',key='resume-budget',settings=settings) == first
        queued = [job for job in wb.jobs(task) if job.state == 'queued']
        assert len(queued) == 1 and queued[0].kind == 'planning_turn'
        assert queued[0].payload['text'].startswith('保留两个包和所有原检查')
        assert not any(job.kind=='conversation_intent' for job in wb.jobs(task))
        assert (old_job.state,old_job.error_code,old_job.payload_hash,old_job.attempt_count) == old_signature
        assert wb.latest(task,'execution_authorized') is None
    resumed = asyncio.run(worker.run_once())
    assert resumed.state == 'succeeded',resumed.error_message
    assert len(runner.invocations) == 1
    db.close()


def test_all_three_limits_can_be_configured_and_calls_use_current_task_limit(tmp_path, monkeypatch):
    import app.planner as planner
    monkeypatch.setattr(planner, 'MAX_MODEL_CALLS', 1)
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.append(task,'model_call',{},key='spent-call',actor='planner')
        with pytest.raises(WorkbenchError): wb.request(task,text='旧额度',parent_id=None,expected_hash=None,key='old')
        current = planning_budget_view(wb,task)
        update(wb,task,limits={**current['limits'],'calls':2,'output_tokens':200_000})
        require_planning_budget(wb,task)
        assert wb.request(task,text='新额度',parent_id=None,expected_hash=None,key='new').state == 'queued'
    db.close()


def test_api_contract_csrf_auth_stale_schema_and_secret_redaction(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    app = create_app(replace(settings,local_access_token='test-local-token'))
    route = f'/api/v1/tasks/{task}/workbench'
    with TestClient(app) as client:
        before = client.get(route).json()['execution']['planning_budget']
        body = {'expected_hash':before['revision'], 'limits':{**before['limits'],'input_tokens':3_000_000}}
        headers = {'Idempotency-Key':'api-budget'}
        assert client.post(route+'/budget',json=body,headers=headers).status_code == 401
        token = {**headers,'X-ContribOS-Token':'test-local-token'}
        assert client.post(route+'/budget',json=body,headers={**token,'Origin':'http://testserver'}).status_code == 403
        csrf = client.get('/api/v1/meta').json()['csrf_token']
        browser = {**token,'Origin':'http://testserver','X-CSRF-Token':csrf}
        assert client.post(route+'/budget',json=body,headers={**browser,'Origin':'https://evil.example'}).status_code == 403
        response = client.post(route+'/budget',json=body,headers=browser)
        assert response.status_code == 200, response.text
        assert response.json()['limits']['input_tokens'] == 3_000_000
        assert client.post(route+'/budget',json=body,headers=browser).status_code == 200
        assert client.post(route+'/budget',json=body,headers={**token,'Idempotency-Key':'stale'}).status_code == 409
        canary = 'ghp_budgetconfigurationcanary123456789012345678'
        bad = {**body,'limits':{**body['limits'],'input_tokens':canary}}
        response = client.post(route+'/budget',json=bad,headers=token)
        assert response.status_code == 422 and canary not in response.text
        schema = client.get('/openapi.json').json()['components']['schemas']
        assert 'PlanningBudgetRequest' in schema
        assert 'planning_budget' in schema['WorkbenchResponse']['properties']['execution']['properties']
    db.close()
