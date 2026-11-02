"""Bounded protocol correction; fixtures never count as native delivery."""
import asyncio
import copy
import json

import pytest
from sqlalchemy import select
from app.agent_v2.adapter import model_adapter
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentModelWorker, AgentToolWorker, schedule
from app.agent_v2.orchestrator import TaskOrchestrator
from app.contribution_workflow import advance_workflows
from app.model_settings import ModelSettingsService, DEEPSEEK_MODELS
from app.models import ExecutionAttempt
from app.providers.studio import profile_identity
from app.providers.contracts import ProviderRunError
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_agent_v2_loop import FixtureGateway
from tests.test_model_settings import configure
from tests.test_workbench import automatic_fixture


def overflow(count=5):
    value=response('list_paths', {})
    call=value['choices'][0]['message']['tool_calls'][0]
    value['choices'][0]['message']['tool_calls']=[{**copy.deepcopy(call),'id':str(i)} for i in range(count)]
    return value


def fixture(tmp_path, *responses):
    def models(db, settings, task):
        with db.session() as session:configure(session,provider='deepseek',model=DEEPSEEK_MODELS[0])
    db,settings,task,workers,_,_=automatic_fixture(tmp_path,configure_models=models,runtime_v2=True,native_transport=True)
    for worker in (workers[0],workers[4]):
        assert asyncio.run(worker.run_once()).state=='succeeded'
        advance_workflows(db,settings)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        attempt=session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id==task))
        journal=TaskOrchestrator(wb,settings).start(attempt.id)
        models=ModelSettingsService(session);identity=profile_identity(models.profile(models.task_profiles(task)['implementation']))
        sid=journal.session_id;schedule(journal);session.commit()
    class Runner:
        def __init__(self):self.responses=list(responses);self.prompts=[]
        async def complete_native_tools(self,invocation,tools,instruction):
            self.prompts.append(json.loads(invocation.prompt));value=self.responses.pop(0)
            if isinstance(value,Exception):raise value
            return value,1
    runner,gateway=Runner(),FixtureGateway()
    model=AgentModelWorker(db,settings,lambda _,spec:model_adapter(runner,identity,spec),worker_id='native-protocol-model')
    tool=AgentToolWorker(db,settings,gateway,worker_id='native-protocol-tool')
    return db,settings,task,sid,model,tool,runner,gateway


def test_overflow_rejects_whole_batch_then_corrects_with_original_protocol_budget(tmp_path):
    db,s,task,sid,model,tool,runner,gateway=fixture(tmp_path,overflow(),response('list_paths',{}))
    assert asyncio.run(model.run_once()).state=='succeeded'
    assert asyncio.run(tool.run_once()) is None and gateway.calls==[]
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid);cp=j.checkpoint()
        assert cp.state=='ready' and cp.usage['protocol_repairs']==1
        assert cp.usage.get('provider_retries',0)==0 and cp.usage['output_tokens']==4096
        rejection=j.events()[-2].payload
        assert rejection['failure']['category']=='protocol'
        assert rejection['protocol_rejection_receipt']['received_call_count']==5
        assert rejection['provider_failure_receipt']['reservation_retained']
        assert 'PRIVATE_REASONING_SENTINEL' not in json.dumps(rejection)
    assert asyncio.run(model.run_once()).state=='succeeded'
    assert asyncio.run(tool.run_once()).state=='succeeded'
    assert len(gateway.calls)==1 and gateway.calls[0].name=='list_paths'
    assert runner.prompts[-1]['protocol_feedback']['safe_reason_code']=='agent_native_call_count_invalid'


def test_repeated_overflow_exhausts_original_two_repairs_without_any_tool(tmp_path):
    db,s,task,sid,model,tool,runner,gateway=fixture(tmp_path,overflow(),overflow(),overflow())
    for _ in range(3):assert asyncio.run(model.run_once()).state=='succeeded'
    assert asyncio.run(model.run_once()) is None and asyncio.run(tool.run_once()) is None
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid);cp=j.checkpoint()
        assert cp.state=='blocked' and cp.usage['protocol_repairs']==2
        assert cp.usage['model_calls']==3 and cp.usage['output_tokens']==3*4096
        assert cp.usage.get('provider_retries',0)==0 and not gateway.calls
        assert j.spec().limits.model_calls==24 and j.spec().limits.protocol_repairs==2


@pytest.mark.parametrize('kind',['missing_usage','over_usage','unknown_function','empty_calls'])
def test_untrusted_usage_or_other_failures_do_not_acquire_protocol_retry(tmp_path,kind):
    value=overflow()
    if kind=='missing_usage':value.pop('usage')
    elif kind=='over_usage':value['usage']['prompt_tokens']=100000
    elif kind=='unknown_function':value=response('shell',{})
    elif kind=='empty_calls':value['choices'][0]['message']['tool_calls']=[]
    db,s,task,sid,model,tool,runner,gateway=fixture(tmp_path,value)
    assert asyncio.run(model.run_once()).state=='succeeded'
    with db.session() as session:
        cp=AgentJournal(Workbench(session,s.artifact_root),task,sid).checkpoint()
        assert cp.state=='blocked' and cp.usage.get('protocol_repairs',0)==0
        assert cp.usage['model_calls']==1 and not gateway.calls


def test_upstream_error_code_cannot_spoof_trusted_protocol_classification(tmp_path):
    error=ProviderRunError('agent_native_call_count_invalid','Rejected upstream',retryable=False)
    db,s,task,sid,model,tool,runner,gateway=fixture(tmp_path,error)
    assert asyncio.run(model.run_once()).state=='succeeded'
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert j.checkpoint().state=='blocked' and j.checkpoint().usage.get('protocol_repairs',0)==0
        assert j.events()[-2].payload['failure']['category']=='model'


@pytest.mark.parametrize('count,expected',[(5,5),(1025,None)])
def test_rejection_metadata_is_bounded_and_has_no_raw_provider_data(count,expected):
    from app.agent_v2.native_tools import NativeCallCountProtocolError,parse_native_decision
    from tests.test_agent_v2_tools import tool_spec
    with pytest.raises(NativeCallCountProtocolError) as caught:
        parse_native_decision(overflow(count),spec=tool_spec(),request_id='fixture')
    receipt=caught.value.rejection_receipt()
    assert receipt['received_call_count']==expected and receipt['max_allowed_calls']==4
    assert receipt['executed_calls']==0 and receipt['whole_batch_rejected']
    assert 'PRIVATE_REASONING_SENTINEL' not in json.dumps(receipt)
