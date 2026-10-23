"""Multi-turn protocol and durable phase transitions, including the live failure."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.conversation import ConversationIntentWorker, advance_conversations, conversation_view, receive_message
from app.conversation_protocol import REASONING_VERSION, LEGACY_REASONING_VERSION, parse_step, step_schema
from app.execution_acceptance import acceptance_view, accept_execution
from app.jobs import JobService
from app.models import PublishIntent, DraftPullRequest
from app.publication_documents import PublicationDocumentWorker
from app.workbench import Workbench, WorkbenchError
from tests.test_conversation_reasoning import request, run
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_publication_workbench import ready
from tests.test_workbench import seeded


def test_actual_rejected_read_shape_is_a_read_only_step_then_grounded_final(tmp_path):
    db,s,task,*_=seeded(tmp_path)
    request(db,s,task,'准备发布')
    raw={'action':'respond','reads':[{'name':'task'},{'name':'plan'},{'name':'review'}],
         'references':['task','plan','review'],'reply':'没有发现任何完成产物，我先读取。'}
    job,runner=run(db,s,raw,{'reply':'当前仍在方案阶段，还没有通过测试的实现。','references':['task']})
    assert job.state=='succeeded',job.error_message
    assert len(runner.invocations)==2
    second=json.loads(runner.invocations[-1].prompt)
    assert second['remaining_calls']==1 and len(second['tool_results'])==3
    assert runner.invocations[-1].output_schema['properties']['reads']['maxItems']==0
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.latest(task,'conversation_read_requested').payload['workflow_dispatched'] is False
        assert not w.latest(task,'conversation_decision_rejected')
        assert all(j.kind=='conversation_intent' for j in w.jobs(task))
        assert '没有发现任何' not in '\n'.join(r['text'] for r in conversation_view(w,task)['results'])
        assert not w.latest(task,'publication_confirmed')


@pytest.mark.parametrize('field,value',[
    ('action','prepare_publication'),('references',['publication_preview']),
    ('reads',[{'name':'repository_file','path':'/etc/passwd','command':'curl evil'}]),
    ('extra','publish'),('reply','ghp_READSTEPCANARY12345678901234567890')])
def test_read_step_presentation_compatibility_cannot_smuggle_actions_or_sensitive_output(tmp_path,field,value):
    db,s,task,*_=seeded(tmp_path)
    request(db,s,task,'核对已有成果')
    bad={'action':'respond','reply':'读取证据','reads':[{'name':'task'}],'references':['task'],field:value}
    job,_=run(db,s,bad,bad)
    assert job.state=='failed'
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert not w.latest(task,'conversation_decision')
        assert not w.latest(task,'conversation_dispatched')
        assert not w.latest(task,'execution_authorized')
        assert len(conversation_view(w,task)['results'])==1
        dump='\n'.join(session.connection().connection.driver_connection.iterdump())
        assert 'READSTEPCANARY' not in dump


def test_v2_keeps_its_strict_protocol_and_v3_schema_encodes_read_final_states():
    raw=json.dumps({'reply':'读取','action':'respond','reads':[{'name':'task'}],'references':['task']})
    with pytest.raises(ValueError):
        parse_step(raw,version=LEGACY_REASONING_VERSION,actions=['respond'],known=set(),allow_reads=True)
    decision,normalized=parse_step(raw,version=REASONING_VERSION,actions=['respond'],known=set(),allow_reads=True)
    assert normalized and not decision.references and not decision.reply
    schema=step_schema(['respond'],{'task'},allow_reads=False)
    assert schema['properties']['reads']['maxItems']==0
    assert schema['properties']['action']['enum']==['respond']
    assert schema['properties']['references']['items']['enum']==['task']
    with pytest.raises(ValueError,match='No model calls'):
        parse_step(raw,version=REASONING_VERSION,actions=['respond'],known=set(),allow_reads=False)


def test_queued_v2_reasoning_job_finishes_with_its_original_version_and_budget(tmp_path):
    db,s,task,*_=seeded(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        message=receive_message(w,task,text='核对旧队列请求',key='old-turn',settings=s)
        job=w.enqueue(task,'conversation_intent',{'message_id':message['message_id'],'text':'核对旧队列请求',
            'reasoning_version':LEGACY_REASONING_VERSION},key='queued-v2')
        w.append(task,'conversation_classifying',{'version':'conversation-v1','message_id':message['message_id']},
            key='conversation-classifying:'+message['message_id'],job_id=job.id)
    completed,runner=run(db,s,'not json',{'reply':'当前请求已保存，尚未派发操作。'})
    assert completed.id==job.id and completed.state=='succeeded'
    assert len(runner.invocations)==2 and json.loads(runner.invocations[0].prompt)['version']==LEGACY_REASONING_VERSION
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.latest(task,'conversation_decision').payload['reasoning_version']==LEGACY_REASONING_VERSION
        assert conversation_view(w,task)['pending_count']==0


def test_restart_restores_validation_feedback_without_an_extra_call(tmp_path,monkeypatch):
    from app import conversation_reasoning as reasoning
    db,s,task,*_=seeded(tmp_path)
    request(db,s,task,'解释当前已保存的内容')
    original=reasoning.reasoning_prompt
    def crash(wb,task_id,message_id,reads,correction,remaining_calls,**kwargs):
        if correction:raise OSError('injected restart after rejection')
        return original(wb,task_id,message_id,reads,correction,remaining_calls,**kwargs)
    monkeypatch.setattr(reasoning,'reasoning_prompt',crash)
    failed,_=run(db,s,'not json');assert failed.state=='failed'
    with db.session() as session:JobService(session).retry(failed.id)
    monkeypatch.setattr(reasoning,'reasoning_prompt',original)
    resumed,runner=run(db,s,{'reply':'当前内容已保存，尚未执行。'})
    assert resumed.id==failed.id and resumed.state=='succeeded'
    prompt=json.loads(runner.invocations[0].prompt)
    assert prompt['remaining_calls']==1 and prompt['correction']['rejected_output']=='not json'
    assert prompt['correction']['diagnostic']['instruction']
    with db.session() as session:
        assert len([e for e in Workbench(session,s.artifact_root).history(task) if e.kind=='model_call'])==2


def accepted_fixture(tmp_path):
    from app.publisher_accounts import append_account
    db,s,task,transport,publisher=ready(tmp_path)
    coordinator=replace(s,publisher_management_key=None,publisher_mode='none',
                        implementation_provider='nvidia_nim',local_access_token='workflow-contract-local')
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        for j in w.jobs(task):
            if j.kind=='publication_prepare':JobService(session).request_cancel(j.id)
        append_account(session,{'state':'connected','login':'contributor','user_id':123,'scopes':['public_repo']},correlation_id='account')
        session.commit()
        binding=acceptance_view(w,task)['binding_hash']
    return db,coordinator,task,transport,publisher,binding


def finish_preview(db,s,task,publisher):
    # Fresh coordinator ticks model process/browser restarts between each child.
    for _ in range(2):advance_conversations(db,s)
    first=asyncio.run(publisher.run_once());assert first.state=='succeeded',first.error_message
    advance_conversations(db,s)
    runner=ScriptedRunner(json.dumps({'title':'fix: preserve streamed content',
        'body':'## Summary\nPreserve streamed content.\n\n## Testing\nLocal sandbox verification completed.\n','notes':[]}))
    writer=PublicationDocumentWorker(db,s,runner,_identity(s.implementation_model),worker_id='document')
    document=asyncio.run(writer.run_once());assert document.state=='succeeded',document.error_message
    advance_conversations(db,s)
    final=asyncio.run(publisher.run_once());assert final.state=='succeeded',final.error_message
    for _ in range(2):advance_conversations(db,s)
    return runner


def test_acceptance_api_continues_through_document_preview_without_a_classifier(tmp_path):
    db,s,task,transport,publisher,binding=accepted_fixture(tmp_path)
    url=f'/api/v1/tasks/{task}/workbench/acceptance'
    headers={'X-ContribOS-Token':s.local_access_token,'Idempotency-Key':'human-accept'}
    body={'binding_hash':binding,'text':'确认代码测试和审查结果','prepare_pr':True}
    with TestClient(create_app(s)) as client:
        assert client.post(url,headers={**headers,'Origin':'https://evil.example'},json=body).status_code==403
        assert client.post(url,headers=headers,json={**body,'binding_hash':'f'*64}).status_code==409
        response=client.post(url,headers=headers,json=body);assert response.status_code==200,response.text
        advance_conversations(db,s)
        assert client.post(url,headers=headers,json=body).json()==response.json()
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert not any(j.kind=='conversation_intent' for j in w.jobs(task))
        assert len([j for j in w.jobs(task) if j.kind=='publication_requirements'])==1
        hashes=[e.record_hash for e in w.history(task)]
    runner=finish_preview(db,s,task,publisher)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert [e.record_hash for e in w.history(task)[:len(hashes)]]==hashes
        assert w.latest(task,'publication_document_ready') and w.latest(task,'publication_prepared')
        assert conversation_view(w,task)['pending_count']==0
        assert conversation_view(w,task)['pending_action']['kind']=='publication'
        assert not w.latest(task,'publication_confirmed')
        assert session.scalar(select(PublishIntent)) is None and session.scalar(select(DraftPullRequest)) is None
        assert not any(e.kind=='model_call' and e.payload.get('purpose')=='conversation_intent' for e in w.history(task))
    assert len(runner.invocations)==1 and all(method=='GET' for method,_,_ in transport.calls)


def test_acceptance_and_continuation_request_roll_back_together(tmp_path,monkeypatch):
    db,s,task,transport,publisher,binding=accepted_fixture(tmp_path)
    original=Workbench.append
    def crash(self,task_id,kind,*args,**kwargs):
        if kind=='conversation_continuation_requested':raise WorkbenchError('injected persistence failure')
        return original(self,task_id,kind,*args,**kwargs)
    monkeypatch.setattr(Workbench,'append',crash)
    with TestClient(create_app(s)) as client:
        response=client.post(f'/api/v1/tasks/{task}/workbench/acceptance',
            headers={'X-ContribOS-Token':s.local_access_token,'Idempotency-Key':'accept'},
            json={'binding_hash':binding,'text':'确认结果','prepare_pr':True})
        assert response.status_code==409
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert not w.latest(task,'execution_accepted')
        assert not w.latest(task,'conversation_received')
        assert not w.latest(task,'conversation_continuation_requested')
    assert not transport.calls


def test_accepted_work_continues_even_when_conversation_model_budget_is_exhausted(tmp_path):
    from app.conversation_continuations import request_after_acceptance
    from app.planner import MAX_MODEL_CALLS
    db,s,task,transport,publisher,binding=accepted_fixture(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        existing=sum(e.kind=='model_call' for e in w.history(task))
        for i in range(MAX_MODEL_CALLS-existing):
            w.append(task,'model_call',{'purpose':'conversation_intent','attempt':i},key=f'used-call-{i}')
        receipt=accept_execution(w,task,binding_hash=binding,text='确认结果',key='accepted')
        request_after_acceptance(w,task,receipt_hash=receipt.record_hash,settings=s)
    finish_preview(db,s,task,publisher)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert sum(e.kind=='model_call' for e in w.history(task))==MAX_MODEL_CALLS
        assert not any(j.kind=='conversation_intent' for j in w.jobs(task))
        assert w.latest(task,'publication_prepared') and not w.latest(task,'publication_confirmed')


@pytest.mark.parametrize('interrupted',[False,True])
def test_old_failed_button_request_recovers_once_without_rewriting_history_or_job(tmp_path,interrupted):
    db,s,task,transport,publisher,binding=accepted_fixture(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        accepted=accept_execution(w,task,binding_hash=binding,text='确认结果',key='old-human')
        receive_message(w,task,text='准备发布',key='accepted-'+accepted.id,settings=s)
    advance_conversations(db,s)
    bad,runner=run(db,s,'not json','not json');assert bad.state=='failed'
    advance_conversations(db,s)  # Finish the old turn before recovery.
    with db.session() as session:
        w=Workbench(session,s.artifact_root);before=[e.record_hash for e in w.history(task)]
        if interrupted:
            receive_message(w,task,text='停止',key='stop-after-failure',settings=s)
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert [e.record_hash for e in w.history(task)[:len(before)]]==before
        assert bool(w.latest(task,'conversation_continuation_recovered')) is not interrupted
        old=JobService(session).get(bad.id)
        assert old.state=='failed' and old.attempt_count==1
        assert len([e for e in w.history(task) if e.kind=='model_call' and e.job_id==bad.id])==2
    if not interrupted:
        finish_preview(db,s,task,publisher)
        advance_conversations(db,s)
        with db.session() as session:
            w=Workbench(session,s.artifact_root)
            assert len([e for e in w.history(task) if e.kind=='conversation_continuation_recovered'])==1
            assert w.latest(task,'publication_prepared') and not w.latest(task,'publication_confirmed')
            assert len([j for j in w.jobs(task) if j.kind=='conversation_intent'])==1


def test_missing_account_has_a_bound_continue_action_after_connection(tmp_path):
    from app.publisher_accounts import append_account
    from app.conversation_continuations import request_after_acceptance
    db,s,task,transport,publisher,binding=accepted_fixture(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        append_account(session,{'state':'disconnected'},correlation_id='disconnect');session.commit()
        receipt=accept_execution(w,task,binding_hash=binding,text='确认结果',key='accept')
        request_after_acceptance(w,task,receipt_hash=receipt.record_hash,settings=s)
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert conversation_view(w,task)['pending_action']['kind']=='publisher_setup'
        append_account(session,{'state':'connected','login':'contributor','user_id':123,'scopes':['public_repo']},correlation_id='reconnect');session.commit()
        assert conversation_view(w,task)['pending_action']['kind']=='prepare_publication'
    headers={'X-ContribOS-Token':s.local_access_token,'Idempotency-Key':'explicit-continue'}
    url=f'/api/v1/tasks/{task}/workbench/publication/continue'
    with TestClient(create_app(s)) as client:
        assert client.post(url,headers={**headers,'Origin':'https://evil.example'},json={'binding_hash':binding}).status_code==403
        assert client.post(url,headers=headers,json={'binding_hash':'f'*64}).status_code==409
        first=client.post(url,headers=headers,json={'binding_hash':binding});assert first.status_code==200,first.text
        advance_conversations(db,s)
        assert client.post(url,headers=headers,json={'binding_hash':binding}).json()['message_id']==first.json()['message_id']
    finish_preview(db,s,task,publisher)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert not any(j.kind=='conversation_intent' for j in w.jobs(task))
        assert w.latest(task,'publication_prepared') and not w.latest(task,'publication_confirmed')
    assert all(method=='GET' for method,_,_ in transport.calls)


def test_one_long_conversation_plans_executes_accepts_prepares_and_publishes_only_after_exact_confirmation(tmp_path):
    from app.archives import RepositoryArchiveStore
    from app.changesets import ChangeSetStore
    from app.coding import CodingContextJobWorker, NvidiaCodingJobWorker
    from app.contribution_workflow import ContributionStartWorker
    from app.execution_worker import ExecutionStageWorker
    from app.providers.runtime import resolve_job_spec_signer
    from app.review_provider import NvidiaReviewJobWorker
    from app.publication import PublicationWorker
    from app.publisher_accounts import append_account
    from app.sandbox_worker.fake import FakeExploreRuntime, FakeImplementRuntime, FakeVerifyRuntime
    from app.sandbox_worker.workspace_jobs import WorkspacePreparationWorker
    from tests.test_execution_pipeline import ARCHIVE_BYTES
    from tests.test_nvidia_workflows import FakeContextRuntime
    from tests.test_workbench import ReadOnlyGitHub, result, run_turn, populate_round, drive_round
    from tests.test_publication_workbench import FakeTransport, FakeCheckout
    db,s,task,content,_=seeded(tmp_path)
    s=replace(s,local_access_token='journey-local-access')
    archive=tmp_path/'frozen.tar.gz';archive.write_bytes(ARCHIVE_BYTES)
    archive_hash=RepositoryArchiveStore(s.artifact_root).put_file(archive)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        w.append(task,'context_ready',{**w.latest(task,'context_ready').payload,'archive_hash':archive_hash},key='archive-ready')
    # Repeated scoped plan revisions all stay in this conversation; no approval is inferred.
    for index,text in enumerate(['先给出当前范围的方案','补充考虑空值边界','再核对测试安排']):
        request(db,s,task,text,key=f'plan-{index}')
        job,_=run(db,s,{'reply':'我会根据这条要求完善方案。','action':'discuss_plan'})
        assert job.state=='succeeded'
        advance_conversations(db,s)
        assert run_turn(db,s,result(content))[0].state=='succeeded'
        advance_conversations(db,s)
    for index in range(12):
        request(db,s,task,f'请解释当前已保存内容的第 {index+1} 个核对点',key=f'dialogue-{index}')
        job,_=run(db,s,{'reply':'当前已保存方案，代码实施仍等待精确确认。'})
        assert job.state=='succeeded'
        advance_conversations(db,s)
    headers={'X-ContribOS-Token':s.local_access_token,'Idempotency-Key':'human-start'}
    url=f'/api/v1/tasks/{task}/workbench'
    with TestClient(create_app(s)) as client:
        before=client.get(url).json();plan=before['plans'][-1]
        assert not any(e['kind']=='execution_authorized' for e in before['events'])
        response=client.post(url+'/execute',headers=headers,json={'plan_id':plan['id'],'plan_hash':plan['record_hash'],
            'approve_plan':True,'start_execution':True,'max_repairs':2,'conversation':True,
            'model_binding_hash':before['model_binding_hash']})
        assert response.status_code==202,response.text
    assert asyncio.run(ContributionStartWorker(db,s,ReadOnlyGitHub,worker_id='start').run_once()).state=='succeeded'
    code,review=ScriptedRunner(),ScriptedRunner()
    workers=[ExecutionStageWorker(db,resolve_job_spec_signer(s),worker_id='sandbox',
        archive_store=RepositoryArchiveStore(s.artifact_root),change_set_store=ChangeSetStore(s.artifact_root),artifact_root=s.artifact_root,
        explore_runtime=FakeExploreRuntime(),implement_runtime=FakeImplementRuntime(),verify_runtime=FakeVerifyRuntime()),
        CodingContextJobWorker(db,artifact_root=s.artifact_root,runtime=FakeContextRuntime(),worker_id='context'),
        NvidiaCodingJobWorker(db,artifact_root=s.artifact_root,runner=code,identity=_identity(s.implementation_model),worker_id='code'),
        NvidiaReviewJobWorker(db,artifact_root=s.artifact_root,runner=review,identity=_identity(s.review_model),worker_id='review'),
        WorkspacePreparationWorker(db,s,FakeImplementRuntime(),FakeVerifyRuntime(),None,None,worker_id='prepare')]
    populate_round(code,review)
    drive_round(db,s,task,workers)
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert conversation_view(w,task)['pending_action']['kind']=='accept'
        append_account(session,{'state':'connected','login':'contributor','user_id':123,'scopes':['public_repo']},correlation_id='account');session.commit()
        binding=acceptance_view(w,task)['binding_hash']
        from app.models import ExecutionAttempt
        repository=session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id==task)).repository_full_name
    with TestClient(create_app(s)) as client:
        accepted=client.post(url+'/acceptance',headers={**headers,'Idempotency-Key':'human-accept'},
            json={'binding_hash':binding,'text':'已核对结果','prepare_pr':True})
        assert accepted.status_code==200,accepted.text
    transport=FakeTransport(repository)
    publisher=PublicationWorker(db,replace(s,publisher_mode='gh'),worker_id='publisher',transport=transport,checkout=FakeCheckout())
    finish_preview(db,s,task,publisher)
    with db.session() as session:
        w=Workbench(session,s.artifact_root);preview=w.latest(task,'publication_prepared')
        assert session.scalar(select(PublishIntent)) is None
        assert all(method=='GET' for method,_,_ in transport.calls)
        assert len(conversation_view(w,task)['messages'])==15
        assert len([e for e in w.history(task) if e.kind=='conversation_received'])==16
        command=w.latest(task,'conversation_continuation_requested')
        assert next(e for e in w.history(task) if e.id==command.payload['message_id']).actor_id=='orchestrator'
        assert len([j for j in w.jobs(task) if j.kind=='conversation_intent'])==15
        assert not any(j.state in {'failed','timed_out'} for j in w.jobs(task))
    # The fake external transport is reached for writes only after an exact human API confirmation.
    with TestClient(create_app(replace(s,publisher_mode='gh'))) as client:
        confirmed=client.post(url+'/publication/confirm',headers={**headers,'Idempotency-Key':'human-publish'},
            json={'intent_hash':preview.record_hash,'nonce':preview.payload['nonce']})
        assert confirmed.status_code==202,confirmed.text
    published=asyncio.run(publisher.run_once());assert published.state=='succeeded',published.error_message
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.latest(task,'publication_completed')
        assert conversation_view(w,task)['pending_count']==0
        assert session.scalar(select(DraftPullRequest)).number==transport.prs[0]['number']
        assert transport.prs[0]['draft'] is True
