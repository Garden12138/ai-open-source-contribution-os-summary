from dataclasses import replace

from fastapi.testclient import TestClient

from app.agent_v2.orchestrator import TaskOrchestrator
from app.agent_v2.contracts import ToolResult
from app.api import create_app
from app.workbench import Workbench
from tests.test_agent_v2_orchestrator import prepared


def test_runtime_api_progress_control_csrf_artifact_scope_and_no_implicit_approval(tmp_path):
    db,s,task,attempt,_=prepared(tmp_path)
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        j=TaskOrchestrator(wb,s).start(attempt)
        artifact=wb.artifact(ToolResult(call_id='read',input_hash='a'*64,source_hash='a'*64,
            output_source_hash='a'*64,status='succeeded',data={'observed':'source receipt','long':'a'*5000}).model_dump())
        j.append('tool_result',{'call_id':'read','artifact_id':artifact,'source_hash':'a'*64,'output_source_hash':'a'*64},key='api-read')
        session.commit()
    s=replace(s,local_access_token='runtime-test-local-token')
    with TestClient(create_app(s)) as client:
        url=f'/api/v1/tasks/{task}/workbench/runtime'
        response=client.get(url); assert response.status_code==200,response.text
        cp=response.json()['checkpoint']
        payload={'action':'pause','expected_revision':cp['revision']}
        assert client.post(url+'/control',json=payload).status_code==401
        headers={'X-ContribOS-Token':s.local_access_token}
        assert client.post(url+'/control',json=payload,headers={**headers,'Origin':'https://evil.example'}).status_code==403
        assert client.post(url+'/control',json=payload,headers=headers).status_code==200
        value=client.get(url+'/artifacts/'+artifact).json()
        assert value['artifact_id']==artifact and value['next_offset'] is not None
        assert client.get(url+'/artifacts/'+'e'*64).status_code==409
        assert client.get(url+'/artifacts/'+artifact+'?max_bytes=999999').status_code==422
        assert client.get(url+'/artifacts/'+artifact+'?offset=-1').status_code==422
        # A free-form chat confirmation cannot acquire a new execution/publication capability.
        assert client.post(url+'/control',json={'action':'approve','expected_revision':cp['revision']},headers=headers).status_code==422
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        assert len([e for e in wb.history(task) if e.kind=='execution_authorized'])==1
        assert not wb.latest(task,'publication_confirmed')


def test_v2_facade_preserves_safe_provider_rate_limit_code(tmp_path):
    import asyncio
    from app.workbench_worker import WorkbenchJobWorker
    from app.providers.contracts import ProviderRunError
    db,s,task,_,_=prepared(tmp_path)
    class Worker(WorkbenchJobWorker):
        kinds=('conversation_intent',)
        settings=s
        async def execute(self,job):
            raise ProviderRunError('model_gateway_rate_limited','模型请求被限流',retryable=True)
    with db.session() as session:
        Workbench(session,s.artifact_root).enqueue(task,'conversation_intent',{},key='rate-limit-test')
    job=asyncio.run(Worker(db,worker_id='rate-test').run_once())
    assert job.state=='failed' and job.error_code=='model_gateway_rate_limited'


def test_read_presentation_omission_cannot_normalize_authority_or_final_answer():
    import json
    import pytest
    from app.conversation_protocol import parse_step, step_schema, RUNTIME_REASONING_VERSION
    step, normalized = parse_step(json.dumps({'action':'respond','reads':[{'name':'task'}]}),
        version=RUNTIME_REASONING_VERSION, actions=['respond'], known=set(), allow_reads=True)
    assert normalized and step.reply == '' and step.references == []
    for raw in [
        {'action':'respond','reads':[],'references':[]},
        {'action':'resume_work','reads':[{'name':'task'}],'references':[]},
        {'action':'respond','reads':[{'name':'context_reference'}]},
        {'action':'respond','reads':[{'name':'history','source_hash':'a'*64}]},
    ]:
        with pytest.raises(ValueError):
            parse_step(json.dumps(raw), version=RUNTIME_REASONING_VERSION,
                actions=['respond','resume_work'], known=set(), allow_reads=True)
    schema=step_schema(['respond'],set(),allow_reads=True,version=RUNTIME_REASONING_VERSION)
    choices=schema['$defs']['EvidenceRead']['oneOf']
    assert choices[1]['required']==['name','reference_id']
    assert choices[2]['required']==['name','path']
    assert 'context_reference' not in choices[0]['properties']['name']['enum']
    assert '"if"' not in json.dumps(schema) and '"allOf"' not in json.dumps(schema)
