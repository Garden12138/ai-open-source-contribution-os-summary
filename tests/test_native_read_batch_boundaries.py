"""Batch capacity, failure, stale-input, crash and persistence boundaries."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import schedule
from app.jobs import JobService
from app.models import Job
from app.provenance import content_hash
from app.workbench import Workbench
from tests.test_native_read_batches import ReadGateway, reads, setup, inspect, drain


def seed_reserved_usage(values, charges):
    db,s,task,sid=values[:4]
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        for job in j.wb.jobs(task):
            if job.state=='queued': JobService(session).request_cancel(job.id,commit=False)
        cp=j.transition(j.checkpoint(),state='model',key='synthetic-prior-reservation-start')
        j.transition(cp,state='ready',usage=j.charge(cp,charges),key='synthetic-prior-reservation')
        schedule(j);session.commit()


@pytest.mark.parametrize('charges',[{'reads':60},{'output_bytes':490000}])
def test_whole_batch_capacity_rejects_before_preflight_and_preserves_old_usage(tmp_path,charges):
    values=setup(tmp_path,reads(5));seed_reserved_usage(values,charges)
    assert asyncio.run(values[4].run_once()).state=='succeeded'
    cp,events,_=inspect(values)
    assert cp.state=='blocked' and asyncio.run(values[5].run_once()) is None
    for key,value in charges.items():assert cp.usage[key]>=value
    assert cp.usage['output_tokens']==4096  # Failed admission retains the entire new model reservation too.
    assert not any(e.kind=='agent_v2_read_batch' for e in events)
    assert not values[-1].calls and values[-1].preflights==0


def test_ambiguous_started_read_is_not_called_unexecuted_or_replayed(tmp_path):
    class LostResult(ReadGateway):
        async def execute(self,*args,**kwargs):
            await super().execute(*args,**kwargs)
            raise RuntimeError('simulated result lost after the read')
    values=setup(tmp_path,reads(5),gateway=LostResult())
    asyncio.run(values[4].run_once());asyncio.run(values[5].run_once())
    assert asyncio.run(values[5].run_once()).state=='failed'
    db,s,task,sid=values[:4]
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert schedule(j) is None;session.commit()
    cp,events,_=inspect(values)
    assert cp.state=='blocked' and cp.usage['reads']==5
    stopped=next(e for e in events if e.kind=='agent_v2_read_batch_stopped')
    assert len(stopped.payload['unknown_calls'])==1 and len(stopped.payload['not_executed'])==4
    assert stopped.payload['actual_receipts']==[]
    drain(values[5]);assert len(values[-1].calls)==1


def test_source_hash_mismatch_in_read_preserves_observation_and_stops_batch(tmp_path):
    class ChangedSource(ReadGateway):
        async def execute(self,*args,**kwargs):
            value=await super().execute(*args,**kwargs);value['result']['output_source_hash']='b'*64;return value
    values=setup(tmp_path,reads(5),gateway=ChangedSource())
    asyncio.run(values[4].run_once());before=inspect(values)[0].source_hash;drain(values[5])
    cp,events,_=inspect(values)
    assert cp.state=='blocked' and cp.source_hash==before
    db,s,task,sid=values[:4]
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        result=j.wb.read(next(e for e in events if e.kind=='agent_v2_tool_result').payload['artifact_id'])
        assert result['status']=='unknown' and result['failure']['safe_reason_code']=='agent_read_batch_source_changed'
        assert j.wb.read(result['data']['observed_result_artifact'])['output_source_hash']=='b'*64
    assert len(values[-1].calls)==1


def test_failure_field_cannot_be_hidden_behind_a_success_status(tmp_path):
    class Inconsistent(ReadGateway):
        async def execute(self,*args,**kwargs):
            value=await super().execute(*args,**kwargs);value['result']['status']='succeeded';return value
    values=setup(tmp_path,reads(5),gateway=Inconsistent(fail_at=1))
    asyncio.run(values[4].run_once());drain(values[5])
    cp,events,_=inspect(values)
    assert cp.state=='blocked' and len(values[-1].calls)==1
    assert any(e.kind=='agent_v2_read_batch_stopped' for e in events)


def test_preflight_source_or_manifest_forgery_never_dispatches_reads(tmp_path):
    class Forged(ReadGateway):
        async def preflight_read_batch(self,*args,**kwargs):
            value=await super().preflight_read_batch(*args,**kwargs);value['requests_hash']='a'*64;return value
    values=setup(tmp_path,reads(8),gateway=Forged());asyncio.run(values[4].run_once());drain(values[5])
    assert inspect(values)[0].state=='blocked' and not values[-1].calls


def test_real_worker_process_restart_keeps_completed_child_and_all_call_ids(tmp_path):
    value=reads(8)
    for i,c in enumerate(value['choices'][0]['message']['tool_calls']):c['function']['arguments']=json.dumps({'path':'calc.py','offset':i*128})
    counter=tmp_path/'actual-read-ids.jsonl'
    class Meter(ReadGateway):
        async def execute(self,spec,call,*args,**kw):
            with counter.open('a') as out:out.write(call.call_id+'\n');out.flush();os.fsync(out.fileno())
            return await super().execute(spec,call,*args,**kw)
    values=setup(tmp_path,value,gateway=Meter());db,s,task,sid,model,tool,*_=values
    asyncio.run(model.run_once())
    for _ in range(3):asyncio.run(tool.run_once())  # Preflight and two real fixture reads.
    data={'database_url':s.database_url,'artifact_root':s.artifact_root,'sandbox_job_spec_signing_key':s.sandbox_job_spec_signing_key.hex(),
        'agent_runtime_v2_enabled':True,'counter':str(counter)}
    program="""import asyncio,json,os,sys
from pathlib import Path
from app.config import Settings
from app.database import Database
from app.agent_v2.loop import AgentToolWorker
from tests.test_native_read_batches import ReadGateway
c=json.load(sys.stdin);counter=Path(c.pop('counter'));c['sandbox_job_spec_signing_key']=bytes.fromhex(c['sandbox_job_spec_signing_key']);s=Settings(**c);db=Database(s.database_url)
class Meter(ReadGateway):
 async def execute(self,spec,call,*args,**kw):
  with counter.open('a') as out:out.write(call.call_id+'\\n');out.flush();os.fsync(out.fileno())
  return await super().execute(spec,call,*args,**kw)
tool=AgentToolWorker(db,s,Meter(),worker_id='real-child-process')
for _ in range(2):assert asyncio.run(tool.run_once()).state=='succeeded'
os._exit(23)
"""
    child=subprocess.run([sys.executable,'-c',program],input=json.dumps(data),text=True,capture_output=True,
        env={k:os.environ[k] for k in ('PATH','LANG') if k in os.environ},timeout=40)
    assert child.returncode==23,child.stderr
    drain(tool)
    ids=counter.read_text().splitlines();assert len(ids)==len(set(ids))==8
    cp,events,_=inspect(values);assert cp.state=='ready' and cp.usage['reads']==8
    assert len([e for e in events if e.kind=='agent_v2_read_sub_batch_completed'])==2
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        charges=[e for e in j.events() if e.kind=='agent_v2_output_charge']
        model_bytes=sum(len(j.wb.artifacts.read_bytes(e.payload['artifact_id'])) for e in j.events() if e.kind=='agent_v2_model_response')
        assert cp.usage['output_bytes']==model_bytes+sum(e.payload['bytes'] for e in charges)
        pages=[e.payload['artifact_id'] for e in charges if e.payload['role']=='source_page']
        assert len(pages)==len(set(pages))


def test_migration_upgrade_and_lifecycle_guard_preserve_existing_rows(tmp_path,monkeypatch):
    from app.migrations import versions
    with monkeypatch.context() as patch:
        patch.setattr(versions,'MIGRATIONS',tuple(m for m in versions.MIGRATIONS if m.revision<'0044_native_read_batches'))
        values=setup(tmp_path,reads(5))
    db,s,task,sid=values[:4]
    with db.session() as session:
        before=[e.record_hash for e in AgentJournal(Workbench(session,s.artifact_root),task,sid).events()]
    assert db.create_schema().applied==('0044_native_read_batches', '0045_source_file_observations', '0046_read_capacity')
    with db.session() as session:
        j=AgentJournal(Workbench(session,s.artifact_root),task,sid)
        assert [e.record_hash for e in j.events()]==before
        aid=j.wb.artifact({'forged':True})
        with pytest.raises(IntegrityError):
            j.append('read_batch_completed',{'batch_artifact':'b'*64,'artifact_id':aid},key='forged-batch-completion')
        session.rollback()
    asyncio.run(values[4].run_once());drain(values[5]);assert inspect(values)[0].state=='ready'
