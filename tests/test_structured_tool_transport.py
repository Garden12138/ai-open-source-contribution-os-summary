"""Transport contracts use fakes; no live model or native journey is claimed."""
import asyncio
import copy
import json

import httpx
import pytest

from app.providers.codex_cli import CodexExecInvocation
from app.providers.contracts import ProviderRunError, ProviderStage
from app.providers.gateway import GatewayTaskCredentialBroker, GatewayTaskTokenCodec
from app.providers.nvidia_nim import STRUCTURED_OUTPUT_TOOL_NAME, _completion
from app.providers.response_diagnostics import CompletionResponseFacts, completion_response_facts
from app.providers.studio import StudioRunner, STRUCTURED_TOOL_TRANSPORT_VERSION, structured_tool_instruction
from tests.test_model_settings import KEY, CANARY


def function(arguments='{"ok":true}', name=STRUCTURED_OUTPUT_TOOL_NAME):
    return {'type':'function','function':{'name':name,'arguments':arguments}}


@pytest.mark.parametrize('calls,state,count', [
    (None,'absent',None),([], 'absent',0),({},'not_list',None),
    ([function(),function()],'multiple_calls',2),([None],'call_invalid',1),
    ([{'function':None}],'function_invalid',1),([function(name=CANARY)],'name_mismatch',1),
    ([{'function':{'name':STRUCTURED_OUTPUT_TOOL_NAME}}],'arguments_missing',1),
    ([function(' ')],'arguments_blank',1),([function([])],'arguments_invalid',1),
    ([function()],'arguments_text',1),([function({'ok':True})],'arguments_object',1),
])
def test_envelope_facts_are_bounded_and_never_accept_or_execute_a_call(calls,state,count):
    data={'choices':[{'finish_reason':'tool_calls','message':{'content':None,'tool_calls':calls,'reasoning_content':CANARY}}],
        'usage':{'prompt_tokens':20,'completion_tokens':10}}
    facts=completion_response_facts(data,'final_content_missing',expected_tool_name=STRUCTURED_OUTPUT_TOOL_NAME)
    assert facts.tool_envelope==state and facts.tool_call_count==count
    assert facts.to_wire()['version']=='completion-response-facts-v2' and CANARY not in json.dumps(facts.to_wire())
    if state.startswith('arguments_') and state in {'arguments_text','arguments_object'}:
        content,_=_completion(data,error_prefix='deepseek');assert json.loads(content)=={'ok':True}
    else:
        with pytest.raises(ProviderRunError):_completion(data,error_prefix='deepseek')


def test_multiple_registered_calls_or_application_function_never_become_a_decision():
    for calls in ([function(),function()], [function(name='read_file')]):
        data={'choices':[{'finish_reason':'tool_calls','message':{'content':None,'tool_calls':calls}}],
            'usage':{'prompt_tokens':100,'completion_tokens':277}}
        with pytest.raises(ProviderRunError) as error:_completion(data,error_prefix='deepseek')
        assert error.value.code=='deepseek_completion_invalid'
        assert error.value.diagnostic.tool_envelope in {'multiple_calls','name_mismatch'}
        assert not error.value.retryable


def test_legacy_fact_wire_format_remains_exact_and_new_values_are_strict():
    facts=CompletionResponseFacts('choices_invalid',0,'missing',False,False,False)
    assert facts.to_wire()=={'version':'completion-response-facts-v1','reason':'choices_invalid','choice_count':0,
        'finish':'missing','final_text_present':False,'tool_calls_present':False,'reasoning_present':False}
    for changes in ({'tool_envelope':CANARY},{'tool_call_count':True},{'tool_envelope':'name_mismatch','tool_call_count':1025},
                    {'tool_call_count':1}):
        with pytest.raises(ValueError):
            CompletionResponseFacts('choices_invalid',0,'missing',False,False,False,**changes)


@pytest.mark.parametrize('mode,provider', [('tools','deepseek'),('tools','minimax'),('tools','openai_compatible'),('json_schema','openai_compatible')])
def test_wire_instruction_matches_the_only_registered_function_and_preserves_input_and_bounds(mode,provider):
    observed=[]
    async def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'finish_reason':'tool_calls','message':{'content':None,'tool_calls':[function()]}}],
            'usage':{'prompt_tokens':20,'completion_tokens':10}})
    client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker=GatewayTaskCredentialBroker(codec=GatewayTaskTokenCodec(KEY),base_url='http://contribos-model-gateway/v1',
        network='contribos-model-gateway-test',service_name='contribos-model-gateway',provider_name=provider)
    model={'deepseek':'deepseek-v4-pro','minimax':'MiniMax-M3','openai_compatible':'model-one'}[provider]
    profile={'id':'profile-test','model':model,'structured_output':mode,'max_tokens':16384,'timeout_seconds':300,'connection':{'provider':provider}}
    frozen=copy.deepcopy(profile)
    invocation=CodexExecInvocation(stage=ProviderStage.CHANGE_SET,request_id='r1',correlation_id='a1',snapshot_id='s1',
        input_hash='a'*64,model=model,prompt='{"tools":{"read_file":{}},"goal":"inspect"}',
        output_schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False},output_token_limit=4096)
    result=asyncio.run(StudioRunner(profile,broker=broker,client=client).complete(invocation))
    assert result.content=='{"ok":true}' and len(observed)==1 and profile==frozen
    payload=observed[0]
    assert payload['max_tokens']==4096 and payload['messages'][1]['content'].startswith(invocation.prompt+'\nREQUIRED_SCHEMA=')
    if mode=='tools':
        assert [tool['function']['name'] for tool in payload['tools']]==[STRUCTURED_OUTPUT_TOOL_NAME]
        assert payload['messages'][0]['content'].endswith(structured_tool_instruction())
        assert STRUCTURED_TOOL_TRANSPORT_VERSION in payload['messages'][0]['content']
        assert payload['tools'][0]['function']['parameters']==invocation.output_schema
        assert payload['tool_choice']['function']['name']==STRUCTURED_OUTPUT_TOOL_NAME
    else:
        assert 'tools' not in payload and STRUCTURED_TOOL_TRANSPORT_VERSION not in payload['messages'][0]['content']
    assert CANARY not in json.dumps(payload)
