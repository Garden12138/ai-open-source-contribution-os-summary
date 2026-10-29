"""The actual mixed-read failure shape, with both results surviving restart."""
import asyncio
import hashlib
import json

from app.archives import RepositoryArchiveStore
from app.database import Database
from app.planner import PlanningContextWorker, PlanningService, PlanningTurnWorker
from tests.test_context_compaction import large_fixture
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import result, run_turn


def test_composite_archived_and_repository_read_results_survive_context_change(tmp_path, monkeypatch):
    db,s,task,content,_=large_fixture(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        context=wb.latest(task,'context_ready');value=wb.read(context.payload['artifact_id'])
        value['inventory'].append('safe.py')
        wb.append(task,'context_ready',{**context.payload,'artifact_id':wb.artifact(value)},key='mixed-source')
        wb.request(task,text='读完两类证据后制定方案',parent_id=None,expected_hash=None,key='mixed-reads')
    runner=ScriptedRunner();complete=runner.complete
    async def response(invocation):
        prompt=json.loads(invocation.prompt)
        ref=next(r['reference_id'] for r in prompt['context_management']['references'] if r['label']=='repository file: background.py')
        runner.contents.append(json.dumps({'read_context':[{'reference_id':ref,'max_bytes':1000}],
            'read_paths':['safe.py'],'reply':'依次读取两类证据'}))
        return await complete(invocation)
    runner.complete=response
    job=asyncio.run(PlanningTurnWorker(db,s,runner,_identity(s.implementation_model),worker_id='planner').run_once())
    assert job.state=='succeeded',job.error_message
    class Reader:
        async def inspect(self,**kw):
            assert kw['paths']==('safe.py',)
            return {'inventory':[*content.files_to_inspect,*content.files_likely_to_change,'background.py','safe.py'],
                'entries':[{'path':'safe.py','content':'actual captured helper','prior_hash':hashlib.sha256(b'actual captured helper').hexdigest(),'executable':False}],
                'truncated':False}
    monkeypatch.setattr(RepositoryArchiveStore,'lookup',lambda *a,**kw:object())
    read=asyncio.run(PlanningContextWorker(db,s,Reader(),worker_id='reader').run_once())
    assert read.state=='succeeded',read.error_message
    db.close();db=Database(s.database_url)
    final, model=run_turn(db,s,result(content))
    assert final.state=='succeeded',final.error_message
    prompt=json.loads(model.invocations[0].prompt)
    assert 'ORIGINAL_BODY_SENTINEL' in prompt['context_reads'][0]['text']
    assert any(e['path']=='safe.py' for e in prompt['evidence']['entries'])
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        assert wb.latest_plan(task)
        assert not wb.latest(task,'execution_authorized')
        assert not any(e.payload.get('constraint')=='context_read_tool_conflict' for e in wb.history(task))


def test_v2_requested_large_source_is_indexed_and_searchable_by_hash_without_execution(tmp_path):
    import pytest
    from app.context_compaction import ContextRead, compact_prompt, read_context
    from app.workbench import WorkbenchError
    from tests.test_context_compaction import packet
    db,s,task,_,context=large_fixture(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        source_before=wb.artifacts.read_bytes(context.payload['artifact_id'])
        prompt,cp=compact_prompt(wb,task,packet(wb,task),scope='planning',focus_paths={'background.py'},
            page_source_files=True,target_bytes=48000,trigger_bytes=48000)
        data=json.loads(prompt);entry=next(e for e in data['evidence']['entries'] if e['path']=='background.py')
        assert entry['partial'] and entry['original_bytes']>200000 and len(prompt.encode())<48000
        ref=entry['context_reference']
        search=read_context(wb,task,cp['artifact_id'],[ContextRead(reference_id=ref,query='ORIGINAL_BODY_SENTINEL')])[0]
        locations=json.loads(search['text']);assert locations['matches'][0]['line']==3
        page=read_context(wb,task,cp['artifact_id'],[ContextRead(reference_id=ref,start_line=2,max_lines=3)])[0]
        exact=json.loads(page['text']);assert exact['start_line']==2 and exact['end_line']==4
        assert 'ORIGINAL_BODY_SENTINEL' in exact['content'] and page['partial']
        assert page['source_hash']==entry['prior_hash']
        with pytest.raises(WorkbenchError):
            read_context(wb,task,cp['artifact_id'],[ContextRead(reference_id=ref,start_line=999999)])
        assert wb.artifacts.read_bytes(context.payload['artifact_id'])==source_before
        assert not wb.latest(task,'execution_authorized')
    db.close()


def test_v2_planning_protocol_is_pinned_only_for_new_requests(tmp_path):
    from app.jobs import JobService
    from app.models import Job
    db,s,task,_,_=large_fixture(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        old=wb.request(task,text='Legacy request',parent_id=None,expected_hash=None,key='legacy-pin')
        assert 'agent_runtime_version' not in old.payload
        jobs=JobService(session)
        leased=jobs.lease_next(worker_id='legacy-pin',kinds=(old.kind,))
        jobs.start(leased.id,worker_id='legacy-pin')
        jobs.fail(leased.id,worker_id='legacy-pin',error_code='fixture_end',error_message='fixture ended')
        fresh=wb.request(task,text='Read and plan',parent_id=None,expected_hash=None,key='new-pin',runtime_version='agent-runtime-v2')
        assert fresh.payload['agent_runtime_version']=='agent-runtime-v2'
        assert 'agent_runtime_version' not in session.get(Job,old.id).payload
    db.close()


def test_v2_many_captured_files_do_not_duplicate_pinned_source_pages(tmp_path):
    from app.context_compaction import ContextRead, compact_prompt, read_context
    from tests.test_context_compaction import packet
    db,s,task,content,_=large_fixture(tmp_path)
    entries=[]
    for i in range(32):
        text=f'SOURCE_SENTINEL_{i}\n'+'# Actual captured source\n'*110
        entries.append({'path':f'package/file_{i}.py','content':text,
            'prior_hash':hashlib.sha256(text.encode()).hexdigest(),'executable':False})
    pinned=[{'reference_id':str(i)*64,'text':'exact source page '+str(i)+'x'*8000,'partial':True} for i in range(3)]
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root);payload=packet(wb,task)
        payload.update(evidence={'entries':entries,'inventory':[e['path'] for e in entries]},
            context_reads=pinned,current_plan=content.hash_payload(),
            planning_state={'source_capture_index':[{'path':e['path'],'content_hash':e['prior_hash'],
                'available':True,'bytes':len(e['content'].encode())} for e in entries]})
        prompt,cp=compact_prompt(wb,task,payload,scope='planning',focus_paths={e['path'] for e in entries},
            page_source_files=True,target_bytes=48000,trigger_bytes=48000)
        assert cp['original_bytes']>96000 and len(prompt.encode())<96000
        actual=json.loads(prompt)
        assert actual['context_reads']==pinned and actual['current_plan']==content.hash_payload()
        assert actual['planning_state']['source_capture_count']==32
        projected=next(e for e in actual['evidence']['entries'] if e.get('partial'))
        page=read_context(wb,task,cp['artifact_id'],[ContextRead(reference_id=projected['context_reference'],max_bytes=8000)])[0]
        source=next(e for e in entries if e['path']==projected['path'])
        assert json.loads(page['text'])['content']==source['content']
        assert page['source_hash']==source['prior_hash']
        assert not wb.latest(task,'execution_authorized')
    db.close()


def test_source_completeness_uses_original_hash_and_returned_bytes(tmp_path):
    from app.context_compaction import ContextRead, compact_prompt, read_context
    from tests.test_context_compaction import packet
    db,s,task,_,_=large_fixture(tmp_path)
    text='FIRST_LINE\n'+'# original source line\n'*220
    entry={'path':'full_source.py','content':text,'prior_hash':hashlib.sha256(text.encode()).hexdigest(),'executable':False}
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root);payload=packet(wb,task)
        payload['evidence']={'entries':[entry],'inventory':[entry['path']]}
        _,cp=compact_prompt(wb,task,payload,scope='planning',page_source_files=True)
        ref=next(r['reference_id'] for r in wb.read(cp['artifact_id'])['references'] if r['label']=='repository file: full_source.py')
        full=read_context(wb,task,cp['artifact_id'],[ContextRead(reference_id=ref,max_bytes=16000)])[0]
        assert full['source_complete'] is True and full['source_path']=='full_source.py'
        assert full['source_lines']==221
        for request in [ContextRead(reference_id=ref,max_bytes=256),ContextRead(reference_id=ref,start_line=2),ContextRead(reference_id=ref,query='FIRST_LINE')]:
            assert read_context(wb,task,cp['artifact_id'],[request])[0]['source_complete'] is False
        # A complete line selection still has a partial wrapper; distinguish
        # that from missing file bytes without weakening the existing flag.
        short='FIRST_LINE\n'+'# original source line\n'*100
        short_entry={**entry,'path':'short_source.py','content':short,'prior_hash':hashlib.sha256(short.encode()).hexdigest()}
        payload['evidence']={'entries':[short_entry],'inventory':['short_source.py']}
        # Force an issued reference through the collective-size fallback.
        payload['background']='large background '*400
        _,cp2=compact_prompt(wb,task,payload,scope='planning',page_source_files=True,target_bytes=1000,trigger_bytes=1000)
        ref2=next(r['reference_id'] for r in wb.read(cp2['artifact_id'])['references'] if r['label']=='repository file: short_source.py')
        selected=read_context(wb,task,cp2['artifact_id'],[ContextRead(reference_id=ref2,start_line=1,max_lines=160,max_bytes=16000)])[0]
        assert selected['partial'] is True and selected['source_complete'] is True
        # A legacy skeleton carrying the original file hash is not full source.
        payload['evidence']['entries']=[{**short_entry,'content':'COMPRESSED_BODY\n'*20}]
        _,cp3=compact_prompt(wb,task,payload,scope='planning',page_source_files=True,target_bytes=1000,trigger_bytes=1000)
        ref3=next(r['reference_id'] for r in wb.read(cp3['artifact_id'])['references'] if r['label']=='repository file: short_source.py')
        assert read_context(wb,task,cp3['artifact_id'],[ContextRead(reference_id=ref3,max_bytes=16000)])[0]['source_complete'] is False
    db.close()


def test_v2_prior_source_pages_survive_multiple_rounds_capture_and_restart(tmp_path, monkeypatch):
    from app.database import Database
    db,s,task,content,_=large_fixture(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        context=wb.latest(task,'context_ready');source=wb.read(context.payload['artifact_id'])
        source['inventory'].append('safe.py')
        wb.append(task,'context_ready',{**context.payload,'artifact_id':wb.artifact(source)},key='history-source')
        wb.request(task,text='Read both pages and the helper before planning',parent_id=None,expected_hash=None,
            key='history-rounds',runtime_version='agent-runtime-v2')
    pages=[]
    for line in (2,4):
        runner=ScriptedRunner();complete=runner.complete
        async def response(invocation):
            prompt=json.loads(invocation.prompt)
            if pages:
                assert prompt['context_reads']==[pages[-1]]
            ref=next(r['reference_id'] for r in prompt['context_management']['references']
                if r['label']=='repository file: background.py')
            runner.contents.append(json.dumps({'read_context':[{'reference_id':ref,'start_line':line,'max_lines':2}]}))
            return await complete(invocation)
        runner.complete=response
        job=asyncio.run(PlanningTurnWorker(db,s,runner,_identity(s.implementation_model),worker_id='pages').run_once())
        assert job.state=='succeeded',job.error_message
        if line==4:
            pages.append(json.loads(runner.invocations[0].prompt)['context_reads'][0])
        db.close();db=Database(s.database_url)
    job,model=run_turn(db,s,json.dumps({'read_paths':['safe.py']}))
    assert job.state=='succeeded',job.error_message
    prompt=json.loads(model.invocations[0].prompt)
    assert prompt['context_read_history']==pages
    latest=prompt['context_reads']
    class Reader:
        async def inspect(self,**kw):
            return {**source,'entries':[*source['entries'],{'path':'safe.py','content':'captured helper',
                'prior_hash':hashlib.sha256(b'captured helper').hexdigest(),'executable':False}]}
    monkeypatch.setattr(RepositoryArchiveStore,'lookup',lambda *a,**kw:object())
    read=asyncio.run(PlanningContextWorker(db,s,Reader(),worker_id='helper').run_once())
    assert read.state=='succeeded',read.error_message
    db.close();db=Database(s.database_url)
    final,model=run_turn(db,s,result(content))
    assert final.state=='succeeded',final.error_message
    actual=json.loads(model.invocations[0].prompt)
    assert actual['context_read_history']==pages and actual['context_reads']==latest
    assert 'ORIGINAL_BODY_SENTINEL' in pages[0]['text']
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        assert wb.latest_plan(task) and not wb.latest(task,'execution_authorized')
    db.close()


def test_v2_older_tool_results_are_retrievable_within_the_existing_prompt_limit(tmp_path):
    from app.context_compaction import ContextRead, compact_prompt, read_context
    from tests.test_context_compaction import packet
    db,s,task,_,_=large_fixture(tmp_path)
    older=[{'reference_id':hashlib.sha256(str(i).encode()).hexdigest(),
        'source_path':f'file_{i}.py','source_complete':False,'text':f'ACTUAL_OLD_PAGE_{i} '+'x'*8000}
        for i in range(12)]
    current=[{'text':'EXACT_CURRENT_PAGE'+'y'*8000} for _ in range(2)]
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root);payload=packet(wb,task)
        payload.update(context_read_history=older,context_reads=current)
        prompt,cp=compact_prompt(wb,task,payload,scope='planning',page_source_files=True,
            target_bytes=48000,trigger_bytes=48000)
        actual=json.loads(prompt)
        assert len(prompt.encode())<96000 and actual['context_reads']==current
        assert len(actual['context_read_history'])==12
        indexed=next(r for r in actual['context_read_history'] if r.get('text_indexed'))
        found=read_context(wb,task,cp['artifact_id'],[ContextRead(reference_id=indexed['history_reference'],max_bytes=16000)])[0]
        restored=json.loads(found['text'])
        assert restored==next(r for r in older if r['reference_id']==indexed['reference_id'])
        assert 'text_indexed' not in older[0] and not wb.latest(task,'execution_authorized')
    db.close()


def test_planning_schema_does_not_offer_empty_object_as_a_valid_outcome():
    import pytest
    from app.planner import PlanningReply
    schema=PlanningReply.model_json_schema()
    assert {tuple(branch['required']) for branch in schema['anyOf']}=={('reply',),('questions',),('read_paths',),('read_context',),('plan',)}
    with pytest.raises(ValueError):PlanningReply.model_validate({})


def test_environment_prerequisites_drain_in_bounded_batches_across_restart(tmp_path, monkeypatch):
    from app.workbench import Workbench
    db,s,task,content,_=large_fixture(tmp_path)
    prerequisites=[f'configuration/{i:02}.txt' for i in range(40)]
    with db.session() as session:
        wb=PlanningService(session,s.artifact_root)
        source=wb.latest(task,'context_ready');value=wb.read(source.payload['artifact_id'])
        value['inventory']+=prerequisites
        wb.append(task,'context_ready',{**source.payload,'artifact_id':wb.artifact(value)},key='prerequisite-source')
        wb.request(task,text='Plan using all required configuration',parent_id=None,expected_hash=None,
            key='batched-config',runtime_version='agent-runtime-v2')
    def inspect(selected,evidence,**kwargs):
        seen={e['path'] for e in evidence['entries'] if e.get('content') is not None}
        missing=[p for p in prerequisites if p not in seen]
        return {**selected,'required_paths':missing} if missing else selected
    monkeypatch.setattr('app.execution_preflight.inspect_environment',inspect)
    batches=[]
    class Reader:
        async def inspect(self,**kw):
            paths=kw['paths'];batches.append(list(paths))
            assert 1<=len(paths)<=32
            return {'inventory':[*content.files_to_inspect,*content.files_likely_to_change,'background.py',*prerequisites],
                'entries':[{'path':p,'content':'configuration','prior_hash':hashlib.sha256(b'configuration').hexdigest(),
                    'executable':False} for p in paths],'truncated':False}
    monkeypatch.setattr(RepositoryArchiveStore,'lookup',lambda *a,**kw:object())
    for index in range(2):
        job,_=run_turn(db,s,result(content));assert job.state=='succeeded',job.error_message
        with db.session() as session:
            wb=Workbench(session,s.artifact_root)
            assert wb.latest_plan(task) is None
            assert not wb.latest(task,'model_output_rejected')
            assert not wb.latest(task,'execution_authorized')
        read=asyncio.run(PlanningContextWorker(db,s,Reader(),worker_id=f'config-{index}').run_once())
        assert read.state=='succeeded',read.error_message
        db.close();db=Database(s.database_url)
    final,_=run_turn(db,s,result(content));assert final.state=='succeeded',final.error_message
    assert list(map(len,batches))==[32,8]
    assert [p for batch in batches for p in batch]==prerequisites
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        assert wb.latest_plan(task) and not wb.latest(task,'execution_authorized')
        assert wb.latest(task,'planning_read_batch').payload['remaining_count']==8
    db.close()


def test_background_v2_plan_job_cannot_finish_with_only_a_reading_promise(tmp_path):
    from tests.test_workbench import run_turn, result
    db,s,task,content,_=large_fixture(tmp_path)
    with db.session() as session:
        w=PlanningService(session,s.artifact_root)
        w.request(task,text='Read source and save a plan',parent_id=None,expected_hash=None,
            key='required-draft',runtime_version='agent-runtime-v2')
    rejected,runner=run_turn(db,s,json.dumps({'reply':'我会先读文件，是否同意？'}))
    assert rejected.state=='succeeded'
    assert all(branch['required']!=['reply'] for branch in runner.invocations[0].output_schema['anyOf'])
    with db.session() as session:
        w=PlanningService(session,s.artifact_root)
        assert w.latest_plan(task) is None
        assert w.latest(task,'model_output_rejected').payload['constraint']=='plan_required_after_repair_or_read'
        assert w.jobs(task)[-1].state=='queued'
    completed,_=run_turn(db,s,result(content))
    assert completed.state=='succeeded',completed.error_message
    with db.session() as session:
        w=PlanningService(session,s.artifact_root)
        assert w.latest_plan(task) and not w.latest(task,'execution_authorized')
    db.close()
