"""Prompt compaction preserves authority and every issued evidence reference."""
import hashlib
import json
import asyncio
import pytest

from app.agent_v2.contracts import ToolResult
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import prompt_for
from app.agent_v2.native_tools import native_input_bound, native_tool_definitions
from app.provenance import canonical_json,content_hash
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_protocol_repairs import fixture


def test_native_prompt_uses_registered_schema_without_dropping_issued_receipts(tmp_path):
    db,s,task,sid,*_=fixture(tmp_path,response('list_paths',{}))
    with db.session() as session:
        wb=Workbench(session,s.artifact_root);j=AgentJournal(wb,task,sid);spec=j.spec()
        refs=[]
        for i in range(32):
            result=ToolResult(call_id='tool-'+hashlib.sha256(str(i).encode()).hexdigest()[:40],input_hash='a'*64,
                source_hash=spec.binding.archive_hash,output_source_hash=spec.binding.archive_hash,status='succeeded',
                data={'tool':'read_file','path':'app/plans.py','content_hash':'b'*64,'text':'source page '*300})
            aid=wb.artifact(result.model_dump());refs.append(aid)
            j.append('tool_result',{'call_id':result.call_id,'artifact_id':aid,'name':'read_file'},key='projection-'+str(i))
        prompt=prompt_for(j)
        assert 'tools' not in prompt
        assert prompt['model_transport']['tools_schema_hash']==spec.model_transport.tools_schema_hash
        index=wb.read(prompt['history_evidence']['artifact_id'])
        assert [row['artifact_id'] for row in index['rows']]==refs
        assert [row['artifact_id'] for row in prompt['history_index']]==refs[-8:]
        assert len(prompt['history_index'])==8 and prompt['history_evidence']['total_entries']==32
        assert index['source_hash']==spec.binding.archive_hash and index['binding_hash']==content_hash(spec.binding.model_dump())
        assert all('call_id' not in row and None not in row.values() for row in prompt['history_index'])
        assert all(wb.read(row['artifact_id'])['data']['content_hash']==row['content_hash'] for row in prompt['history_index'])
        assert prompt['approved_checks']==spec.commands and prompt['write_paths']==spec.write_paths
        assert prompt['source_hash']==spec.binding.archive_hash and prompt['feedback'][-1]['data']['text']=='source page '*300
        assert set(t['function']['name'] for t in native_tool_definitions()) >= {'read_artifact','read_file','apply_patch','run_approved_checks'}
        assert native_input_bound(prompt)<32000


def test_legacy_prompt_wire_and_embedded_schemas_are_unchanged(tmp_path):
    from tests.test_agent_v2_loop import fixture as legacy_fixture
    from app.agent_v2.native_tools import project_native_prompt
    from app.agent_v2.tools import RestrictedToolRegistry
    db,s,task,*rest=legacy_fixture(tmp_path,{'outcome':'blocked'})
    sid=rest[-1]
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        prompt=prompt_for(j);before=canonical_json(prompt)
        assert j.spec().model_transport is None
        assert prompt['tools']==RestrictedToolRegistry().descriptions()
        assert 'model_transport' not in prompt
        assert project_native_prompt(prompt,j.spec()) is prompt
        assert canonical_json(prompt)==before


def test_old_native_contract_remains_readable_but_cannot_silently_change_projection():
    from app.agent_v2.contracts import ModelTransport,TaskSpec
    from app.agent_v2.native_tools import INSTRUCTION,require_native_transport
    from tests.test_agent_v2_tools import tool_spec
    old=ModelTransport(tools_schema_hash=content_hash({'tools':native_tool_definitions(),'instruction':INSTRUCTION,'max_calls':4}))
    spec=tool_spec().model_copy(update={'model_transport':old})
    assert TaskSpec.model_validate_json(spec.model_dump_json())==spec
    with pytest.raises(ValueError,match='stale'):require_native_transport(old)


def test_oversized_native_wire_still_fails_before_model_request_or_budget_charge(tmp_path,monkeypatch):
    db,s,task,sid,model,tool,runner,gateway=fixture(tmp_path,response('list_paths',{}))
    monkeypatch.setattr('app.agent_v2.loop.prompt_for',lambda _: {'oversized_data':'x'*40000})
    job=asyncio.run(model.run_once())
    assert job.state=='failed' and job.error_code=='agent_context_budget'
    assert runner.prompts==[] and gateway.calls==[]
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert j.checkpoint().usage=={} and not any(e.kind=='agent_v2_model_started' for e in j.events())


def test_generated_history_index_is_issued_for_paged_reads_but_arbitrary_refs_still_deny(tmp_path):
    from app.agent_v2.loop import issued_artifacts
    db,s,task,sid,model,tool,runner,gateway=fixture(tmp_path,response('list_paths',{}))
    with db.session() as session:
        wb=Workbench(session,s.artifact_root);j=AgentJournal(wb,task,sid);spec=j.spec()
        for i in range(12):
            result=ToolResult(call_id='history-'+str(i),input_hash='a'*64,source_hash=spec.binding.archive_hash,
                output_source_hash=spec.binding.archive_hash,status='succeeded',data={'tool':'list_paths','text':'app/plans.py'})
            aid=wb.artifact(result.model_dump());j.append('tool_result',{'call_id':result.call_id,'artifact_id':aid,'name':'list_paths'},key='history-'+str(i))
        prompt=prompt_for(j);index_id=prompt['history_evidence']['artifact_id'];session.commit()
    runner.responses=[response('read_artifact',{'artifact_id':index_id,'max_bytes':1024}),response('read_artifact',{'artifact_id':'f'*64})]
    assert asyncio.run(model.run_once()).state=='succeeded'
    with db.session() as session:
        assert index_id in issued_artifacts(AgentJournal(Workbench(session,s.artifact_root),task,sid))
    assert asyncio.run(tool.run_once()).state=='succeeded'
    with db.session() as session:
        wb=Workbench(session,s.artifact_root);j=AgentJournal(wb,task,sid)
        result=wb.read(next(e for e in reversed(j.events()) if e.kind=='agent_v2_tool_result').payload['artifact_id'])
        assert result['status']=='succeeded' and result['data']['next_offset'] is not None
        assert '"rows"' in result['data']['text'] and 'history-0' in result['data']['text']
        assert wb.read(index_id)['version']=='agent-tool-history-index-v1'
    assert asyncio.run(model.run_once()).state=='succeeded'
    assert asyncio.run(tool.run_once()).state=='succeeded'
    with db.session() as session:
        wb=Workbench(session,s.artifact_root);j=AgentJournal(wb,task,sid)
        result=wb.read(next(e for e in reversed(j.events()) if e.kind=='agent_v2_tool_result').payload['artifact_id'])
        assert result['status']=='rejected' and result['failure']['safe_reason_code']=='agent_artifact_scope_denied'
    assert not gateway.calls


def test_missing_full_history_reference_cannot_silently_drop_older_rows():
    from app.agent_v2.native_tools import project_native_prompt,native_model_transport
    from tests.test_agent_v2_tools import tool_spec
    spec=tool_spec().model_copy(update={'model_transport':native_model_transport()})
    prompt={'tools':{},'history_index':[{'artifact_id':content_hash(i),'call_id':str(i)} for i in range(12)]}
    before=canonical_json(prompt)
    with pytest.raises(ValueError,match='history_reference_missing'):project_native_prompt(prompt,spec)
    assert canonical_json(prompt)==before
