"""Deterministic protocol fixtures; never reported as live model acceptance."""
import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

from app.agent_v2.adapter import StructuredModelAdapter
from app.agent_v2.contracts import ToolResult
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentModelWorker, AgentToolWorker, schedule
from app.agent_v2.orchestrator import TaskOrchestrator, runtime_view
from app.jobs import JobService
from app.provenance import content_hash
from app.workbench import Workbench, WorkbenchError
from tests.test_agent_v2_orchestrator import prepared
from tests.test_nvidia_workflows import ScriptedRunner, _identity


class FixtureGateway:
    """Pure fixture data only. Real source execution is tested separately."""
    def __init__(self): self.calls=[]

    async def execute(self, spec, call, arguments, *, source_hash, snapshot, input_hash):
        self.calls.append(call)
        data={"tool":call.name}
        value=None
        output=source_hash
        if call.name=='apply_patch':
            previous=(snapshot or {}).get('files',{}).get(arguments.path)
            original=previous['original_content'] if previous else 'content'
            current=previous['content'] if previous else original
            updated=current.replace(arguments.old_text,arguments.new_text,1)
            output=content_hash({'path':arguments.path,'content':updated})
            value={"version":"agent-workspace-snapshot-v1","archive_hash":spec.binding.archive_hash,
                "source_hash":output,"files":{spec.write_paths[0]:{
                    "original_hash":hashlib.sha256(original.encode()).hexdigest(),"original_content":original,
                    "content":updated, "executable":False}}}
        elif call.name=='read_file': data.update(text='content',content_hash=hashlib.sha256(b'content').hexdigest())
        elif call.name=='run_approved_checks': data.update(outcome='passed')
        return {"result":ToolResult(call_id=call.call_id,input_hash=input_hash,source_hash=source_hash,
            output_source_hash=output,status='succeeded',data=data).model_dump(),"snapshot":value}


def fixture(tmp_path, *responses):
    db,s,task,attempt,workers=prepared(tmp_path)
    runner=ScriptedRunner(*[json.dumps(r) if isinstance(r,dict) else r for r in responses])
    factory=lambda session,spec:StructuredModelAdapter(runner,_identity(s.implementation_model),attempt)
    model=AgentModelWorker(db,s,factory,worker_id='model-v2')
    gateway=FixtureGateway()
    tool=AgentToolWorker(db,s,gateway,worker_id='tool-v2')
    with db.session() as session:
        journal=TaskOrchestrator(Workbench(session,s.artifact_root),s).start(attempt)
        schedule(journal); session.commit()
    return db,s,task,attempt,workers,runner,model,tool,gateway,journal.session_id


def test_persistent_multi_tool_round_and_restart_exactly_once(tmp_path):
    values=fixture(tmp_path,{"calls":[{"call_id":"one","name":"read_file","arguments":{"path":"app/plans.py"}},
        {"call_id":"two","name":"list_paths","arguments":{}}]}, {"outcome":"blocked","reply":"Need additional evidence"})
    db,s,task,_,_,runner,model,tool,gateway,sid=values
    assert asyncio.run(model.run_once()).state=='succeeded'
    assert asyncio.run(tool.run_once()).state=='succeeded'
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        old=j.checkpoint()
        assert len(old.pending_calls)==1
        assert schedule(j).id==schedule(j).id
        session.commit()
    # New Worker object, same DB: the completed first tool is not repeated.
    tool=AgentToolWorker(db,s,gateway,worker_id='restarted-tool')
    assert asyncio.run(tool.run_once()).state=='succeeded'
    assert asyncio.run(tool.run_once()) is None
    assert asyncio.run(model.run_once()).state=='succeeded'
    assert [c.name for c in gateway.calls]==['read_file','list_paths']
    assert len({c.call_id for c in gateway.calls})==2
    assert len(runner.invocations)==2
    with db.session() as session:
        view=runtime_view(Workbench(session,s.artifact_root),task)
        assert view['checkpoint']['state']=='blocked'
        assert view['checkpoint']['usage']['reads']==2
        assert not view['native_journey_completed']


def test_malformed_reply_consumes_bounded_budget_and_preserves_feedback(tmp_path):
    db,s,task,_,_,runner,model,_,_,sid=fixture(tmp_path,'invalid JSON','{"outcome":"blocked"}')
    assert asyncio.run(model.run_once()).state=='succeeded'
    assert asyncio.run(model.run_once()).state=='succeeded'
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert j.checkpoint().usage['model_calls']==2
        assert j.checkpoint().usage['protocol_repairs']==1
        assert j.checkpoint().usage['output_tokens']>=4096 # interrupted/invalid response reservation kept
    assert json.loads(runner.invocations[-1].prompt)['protocol_feedback']['category']=='protocol'


def test_unknown_tool_outcome_after_worker_loss_never_repeats_check(tmp_path):
    db,s,task,_,_,_,model,_,_,sid=fixture(tmp_path,{"calls":[{"call_id":"check","name":"run_approved_checks","arguments":{}}]})
    assert asyncio.run(model.run_once()).state=='succeeded'
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        jobs=JobService(session)
        job=jobs.lease_next(worker_id='lost',kinds=('agent_v2_tool',)); jobs.start(job.id,worker_id='lost')
        cp=j.checkpoint()
        j.append('tool_started',{'call_id':'check','name':'run_approved_checks','input_hash':'a'*64,'source_hash':cp.source_hash},key='lost-start',job_id=job.id)
        j.transition(cp,state='tools',usage=j.charge(cp,{'checks':1}),key='lost-charge',job_id=job.id)
        jobs.fail(job.id,worker_id='lost',error_code='worker_lost',error_message='lost',commit=False)
        session.commit()
        assert schedule(j) is None
        assert j.checkpoint().state=='blocked'
        assert j.checkpoint().usage['checks']==1
        session.commit()


def test_process_exit_after_committed_tool_result_resumes_successor(tmp_path):
    import subprocess, sys, os
    from dataclasses import asdict
    values=fixture(tmp_path,{'calls':[{'call_id':'read-before-exit','name':'read_file','arguments':{'path':'app/plans.py'}},
        {'call_id':'read-after-restart','name':'list_paths','arguments':{}}]})
    db,s,task,_,_,_,model,tool,gateway,sid=values
    assert asyncio.run(model.run_once()).state=='succeeded'
    settings=asdict(s)
    settings['sandbox_job_spec_signing_key']=s.sandbox_job_spec_signing_key.hex()
    process=subprocess.run([sys.executable,'-c',
        'import asyncio,json,sys,os; from app.config import Settings; from app.database import Database; '
        'from app.agent_v2.loop import AgentToolWorker; from tests.test_agent_v2_loop import FixtureGateway; '
        'v=json.loads(sys.stdin.read()); v["sandbox_job_spec_signing_key"]=bytes.fromhex(v["sandbox_job_spec_signing_key"]); '
        's=Settings(**v); db=Database(s.database_url); '
        'job=asyncio.run(AgentToolWorker(db,s,FixtureGateway(),worker_id="crash-process").run_once()); '
        'assert job.state=="succeeded"; os._exit(23)'],input=json.dumps(settings),text=True,capture_output=True,
        env={k:os.environ[k] for k in ('PATH','LANG') if k in os.environ},timeout=30)
    assert process.returncode==23,process.stderr
    assert asyncio.run(tool.run_once()).state=='succeeded'
    assert [c.name for c in gateway.calls]==['list_paths']
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert len([e for e in j.events() if e.kind=='agent_v2_tool_result'])==2
        assert j.checkpoint().usage['reads']==2


def test_finish_without_real_check_is_not_delivery(tmp_path):
    db,s,task,_,_,_,model,_,_,sid=fixture(tmp_path,{"outcome":"finish","reply":"Everything passed"})
    assert asyncio.run(model.run_once()).state=='succeeded'
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert j.checkpoint().state=='ready'
        assert j.events()[-2].kind=='agent_v2_model_rejected'
        assert not j.wb.latest(task,'automatic_change_accepted')


def test_vendor_call_ids_can_repeat_in_different_durable_model_turns(tmp_path):
    db,s,task,_,_,_,model,tool,gateway,sid=fixture(tmp_path,
        {'calls':[{'call_id':'1','name':'list_paths','arguments':{}}]},
        {'calls':[{'call_id':'1','name':'list_paths','arguments':{}}]},
        {'outcome':'blocked'})
    for worker in (model,tool,model,tool,model):
        job=asyncio.run(worker.run_once());assert job.state=='succeeded',job.error_message
    assert len(gateway.calls)==2 and gateway.calls[0].call_id!=gateway.calls[1].call_id
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        turns=[e for e in j.events() if e.kind=='agent_v2_turn']
        assert turns[0].payload['decision']['calls'][0]['call_id']=='1'
        assert turns[1].payload['decision']['calls'][0]['call_id']=='1'
        assert j.checkpoint().usage['reads']==2


def test_context_replay_exposes_actual_workspace_and_named_tool_history(tmp_path):
    from app.agent_v2.loop import prompt_for
    db,s,task,_,_,_,model,tool,_,sid=fixture(tmp_path,{'calls':[{'call_id':'r','name':'read_file','arguments':{'path':'app/plans.py'}}]})
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert prompt_for(j)['workspace_state']['required_next_step']=='inspect_source'
    assert asyncio.run(model.run_once()).state=='succeeded'
    assert asyncio.run(tool.run_once()).state=='succeeded'
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        prompt=prompt_for(j)
        assert prompt['history_index'][0]['tool']=='read_file'
        assert prompt['history_index'][0]['artifact_id']
        assert prompt['recent_decisions'][0]['tools'][0]['name']=='read_file'
        assert prompt['workspace_state']['required_next_step']=='continue_implementation'


def test_model_secret_canary_is_blocked_without_persistence_or_retry(tmp_path,caplog):
    canary='ghp_agentv2canary123456789012345678'
    db,s,task,_,_,runner,model,_,_,sid=fixture(tmp_path,{'outcome':'blocked','reply':canary})
    job=asyncio.run(model.run_once());assert job.state=='succeeded'
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert j.checkpoint().state=='blocked'
        assert j.events()[-2].payload['failure']['category']=='security'
        assert not j.remaining()['model_calls']==j.spec().limits.model_calls
    with db.engine.connect() as connection:
        dump='\n'.join(connection.connection.driver_connection.iterdump())
    assert canary not in dump and canary not in caplog.text
    for artifact in __import__('pathlib').Path(s.artifact_root).rglob('*'):
        if artifact.is_file():assert canary.encode() not in artifact.read_bytes()
    assert asyncio.run(model.run_once()) is None


@pytest.mark.parametrize('verdict',['insufficient_evidence','environment_blocked'])
def test_review_missing_evidence_is_never_pass(verdict):
    from app.review_provider import _parse_review
    outcome,reason,findings=_parse_review(json.dumps({'verdict':verdict,'reason_code':'cannot_verify','findings':[]}))
    assert outcome=='block' and reason==verdict and any(f.verdict=='block' for f in findings)


@pytest.mark.parametrize('review_repair',[False,True])
def test_v2_snapshot_exports_through_existing_implement_verify_independent_review(tmp_path,review_repair):
    # Uses the pre-existing full domain fixture and exact human approval.
    db,s,task,attempt,workers,runner,model,tool,gateway,sid=fixture(tmp_path)
    s=replace(s,agent_runtime_v2_enabled=True)
    with db.session() as session:
        path=AgentJournal(Workbench(session,s.artifact_root),task,sid).spec().write_paths[0]
    runner.contents.extend([json.dumps({"calls":[{"call_id":"edit","name":"apply_patch","arguments":{
        "path":path,"expected_hash":hashlib.sha256(b'content').hexdigest(),"old_text":"content","new_text":"changed"}},
        {"call_id":"checks","name":"run_approved_checks","arguments":{}}]}),json.dumps({"outcome":"finish"})])
    for worker in (model,tool,tool,model):
        job=asyncio.run(worker.run_once()); assert job.state=='succeeded',job.error_message
    from app.agent_v2.integration import advance_coding
    with db.session() as session:
        advance_coding(Workbench(session,s.artifact_root),attempt,s)
    assert asyncio.run(workers[0].run_once()).state=='succeeded'
    assert asyncio.run(workers[0].run_once()).state=='succeeded'
    from app.contribution_workflow import advance_workflows
    advance_workflows(db,s)
    def review_response(verdict):
        return json.dumps({'verdict':verdict,'reason_code':'verified' if verdict=='pass' else 'repair_required','findings':[
            {'severity':'info' if verdict=='pass' else 'blocking','location':'app/plans.py','evidence':'Exact diff and test artifacts checked',
             'recommendation':'Accept verified change' if verdict=='pass' else 'Repair the approved boundary','verdict':verdict}]})
    workers[3].runner.contents.append(review_response('block' if review_repair else 'pass'))
    job=asyncio.run(workers[3].run_once()); assert job.state=='succeeded',job.error_message
    advance_workflows(db,s)
    if review_repair:
        # Re-enter the real orchestrator/attempt/prepare/context path, retaining
        # the original approval and cumulative tool-session budget.
        for worker in (workers[0],workers[4],workers[1]):
            job=asyncio.run(worker.run_once()); assert job.state=='succeeded',job.error_message
            advance_workflows(db,s)
        with db.session() as session:
            wb=Workbench(session,s.artifact_root);root=wb.latest(task,'agent_v2_session')
            repair=AgentJournal(wb,task,root.payload['session_id'])
            assert repair.checkpoint().snapshot_artifact
            assert repair.checkpoint().usage['model_calls']==2
            assert repair.checkpoint().usage['code_repairs']==1
        runner.contents.extend([json.dumps({'calls':[{'call_id':'repair-read','name':'read_file','arguments':{'path':path}},
            {'call_id':'repair-patch','name':'apply_patch','arguments':{'path':path,'expected_hash':hashlib.sha256(b'changed').hexdigest(),
                'old_text':'changed','new_text':'repaired'}},
            {'call_id':'repair-check','name':'run_approved_checks','arguments':{}}]}),json.dumps({'outcome':'finish'})])
        for worker in (model,tool,tool,tool,model):
            job=asyncio.run(worker.run_once());assert job.state=='succeeded',job.error_message
        assert json.loads(runner.invocations[-2].prompt)['independent_review_findings'][0]['verdict']=='block'
        advance_workflows(db,s)
        for _ in range(2):
            job=asyncio.run(workers[0].run_once());assert job.state=='succeeded',job.error_message
        advance_workflows(db,s)
        workers[3].runner.contents.append(review_response('pass'))
        job=asyncio.run(workers[3].run_once());assert job.state=='succeeded',job.error_message
        advance_workflows(db,s)
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        assert wb.latest(task,'awaiting_acceptance')
        assert len([e for e in wb.history(task) if e.kind=='execution_authorized'])==1
        assert not wb.latest(task,'publication_confirmed')


@pytest.mark.parametrize('foreign',[False,True])
def test_only_platform_issued_task_context_can_be_read_as_artifact(tmp_path,foreign):
    db,s,task,_,_,runner,model,tool,_,sid=fixture(tmp_path,{'outcome':'blocked'})
    complete=runner.complete
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        unrelated=wb.artifact({'unrelated':'other-task-private-content'})
    async def response(invocation):
        prompt=json.loads(invocation.prompt)
        chosen=unrelated if foreign else prompt['task_context_artifact']
        runner.contents.insert(0,json.dumps({'calls':[{'call_id':'facts','name':'read_artifact',
            'arguments':{'artifact_id':chosen,'max_bytes':4096}}]}))
        return await complete(invocation)
    runner.complete=response
    assert asyncio.run(model.run_once()).state=='succeeded'
    assert asyncio.run(tool.run_once()).state=='succeeded'
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        e=next(e for e in j.events() if e.kind=='agent_v2_tool_result')
        receipt=j.wb.read(e.payload['artifact_id'])
        if foreign:
            assert receipt['failure']['category']=='security'
            assert receipt['failure']['safe_reason_code']=='agent_artifact_scope_denied'
            assert j.checkpoint().state=='blocked'
            assert 'other-task-private-content' not in json.dumps(receipt)
        else:
            assert receipt['status']=='succeeded'
            assert 'approved_plan' in receipt['data']['text'] and 'issue' in receipt['data']['text']
            assert j.checkpoint().usage['reads']==1
            assert not j.wb.latest(task,'execution_accepted')
    db.close()
