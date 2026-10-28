import asyncio, json, os, tempfile
from importlib.metadata import version
from typing import TypedDict
os.environ['OPENAI_AGENTS_DISABLE_TRACING']='1'

report={'classification':'deterministic_framework_spike_no_model_no_credentials','versions':{p:version(p) for p in ['langgraph','langgraph-checkpoint-sqlite','openai-agents']}}

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command
class State(TypedDict):
    value:int

def read(state): return {'value':state['value']+1}
def pause(state):
    decision=interrupt({'tool':'exact_patch','value':state['value']})
    return {'value':state['value']+1 if decision else state['value']}
graph=StateGraph(State);graph.add_node('read',read);graph.add_node('pause',pause)
graph.add_edge(START,'read');graph.add_edge('read','pause');graph.add_edge('pause',END)
with tempfile.TemporaryDirectory() as tmp:
    path=tmp+'/state.db'; config={'configurable':{'thread_id':'spike'}}
    with SqliteSaver.from_conn_string(path) as saver:
        app=graph.compile(checkpointer=saver);out=app.invoke({'value':0},config)
        assert out['value']==1 and out['__interrupt__']
    with SqliteSaver.from_conn_string(path) as saver:
        app=graph.compile(checkpointer=saver);out=app.invoke(Command(resume=True),config)
        assert out['value']==2
report['langgraph']={'sqlite_reopen_resume':True,'separate_checkpoint_authority_required':True}

from agents import Agent, Runner, function_tool, RunConfig, SQLiteSession
from agents.models.interface import Model, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
class FixtureModel(Model):
    def __init__(self):self.calls=0
    async def get_response(self,*args,**kwargs):
        self.calls+=1
        if self.calls<=2:
            output=[ResponseFunctionToolCall(type='function_call',id='item'+str(self.calls),call_id='call'+str(self.calls),name='read',arguments='{}')]
        else:
            output=[ResponseOutputMessage(type='message',id='done',role='assistant',status='completed',content=[ResponseOutputText(type='output_text',text='done',annotations=[])])]
        return ModelResponse(output=output,usage=Usage(requests=1,input_tokens=1,output_tokens=1,total_tokens=2),response_id='response'+str(self.calls))
    async def stream_response(self,*args,**kwargs):
        raise NotImplementedError
        yield
calls=[]
@function_tool
def read()->str:
    calls.append('read'); return 'source'
async def sdk():
    with tempfile.TemporaryDirectory() as tmp:
        model=FixtureModel();session=SQLiteSession('spike',tmp+'/sdk.db')
        result=await Runner.run(Agent(name='fixture',model=model,tools=[read]),'read twice',session=session,run_config=RunConfig(tracing_disabled=True))
        assert result.final_output=='done' and len(calls)==2
        reopened=SQLiteSession('spike',tmp+'/sdk.db')
        assert len(await reopened.get_items())>=5
        return {'model_calls':model.calls,'tool_calls':len(calls),'sqlite_history_reopen':True,'platform_approval_cas_requires_adapter':True}
report['agents_sdk']=asyncio.run(sdk())
print(json.dumps(report,indent=2))
