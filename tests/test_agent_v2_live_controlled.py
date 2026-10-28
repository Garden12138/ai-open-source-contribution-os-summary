"""Opt-in real-model/real-sandbox loop. Fixture planning is NOT native M5 proof."""
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agent_v2.adapter import StructuredModelAdapter
from app.agent_v2.loop import AgentModelWorker, AgentToolWorker
from app.contribution_workflow import advance_workflows
from app.executions import ExecutionAttemptService
from app.models import ExecutionAttempt, ReviewRun
from app.plans import PlanCommand
from app.provenance import content_hash
from app.providers.gateway import GatewayTaskCredentialBroker, GatewayTaskTokenCodec
from app.providers.nvidia_nim import NvidiaNimGatewayRunner, NvidiaNimParameters
from app.sandbox_worker.agent_tools import DockerAgentToolGateway
from app.workbench import Workbench
from tests.test_node_workflow import node_workflow, cleanup_node_workflow
from tests.test_nvidia_workflows import _identity

pytestmark=pytest.mark.skipif(os.getenv('CONTRIBOS_RUN_V2_LIVE_CONTROLLED')!='1',reason='paid real gateway and real OCI controlled v2 loop')


@pytest.mark.parametrize('language',['python','typescript'])
def test_live_controlled_incremental_loop_and_independent_review(tmp_path,monkeypatch,language):
    model=os.environ['CONTRIBOS_V2_LIVE_MODEL']
    identity=_identity(model)
    from app import model_settings
    monkeypatch.setattr(model_settings,'task_identity',lambda *args:identity)
    if language=='python':
        source,test='tags.py','tests/test_tags.py'
        files={source:'def normalize_tag(value):\n    return value.lower()\n',
            test:'import unittest\nfrom tags import normalize_tag\nclass TagTests(unittest.TestCase):\n    def test_lower(self): self.assertEqual(normalize_tag("Hello"),"hello")\n'}
        argv=('python3','-m','unittest','discover','-s','tests')
    else:
        source,test='tags.ts','test.mjs'
        files={source:'export const normalizeTag = (value: string): string => value.toLowerCase();\n',
            test:'import {strict as assert} from "node:assert";\nimport {normalizeTag} from "./tags.ts";\nassert.equal(normalizeTag("Hello"),"hello");\n'}
        argv=('node','--experimental-strip-types','test.mjs')
    goal='Extend the tag normalizer to trim surrounding whitespace before lowercasing and return "anonymous" for empty or whitespace-only input. Preserve existing behavior for nonempty tags. Add meaningful regression tests for the new behavior in the approved test file. Use only the two approved paths; read source and tests, edit incrementally, run all checks and fix any regression.'
    variant=os.environ.get('CONTRIBOS_V2_CONTROLLED_VARIANT','tags-v1')
    if variant=='labels-v2':
        files={p.replace('tags','labels'):text.replace('normalize_tag','normalize_label').replace('normalizeTag','normalizeLabel').replace('tags','labels') for p,text in files.items()}
        source,test=source.replace('tags','labels'),test.replace('tags','labels')
        goal=goal.replace('tag normalizer','label normalizer').replace('tags','labels').replace('"anonymous"','"guest"')
    if variant=='names-v3':
        files={p.replace('tags','names'):text.replace('normalize_tag','normalize_name').replace('normalizeTag','normalizeName').replace('tags','names') for p,text in files.items()}
        source,test=source.replace('tags','names'),test.replace('tags','names')
        goal=goal.replace('tag normalizer','name normalizer').replace('tags','names').replace('"anonymous"','"visitor"')
    if variant in {'count-v4','limit-v5','quota-v6'}:
        if language=='python':
            source,test='counts.py','tests/test_counts.py'
            files={source:'def parse_count(value):\n    return int(value)\n',
                test:'import unittest\nfrom counts import parse_count\nclass CountTests(unittest.TestCase):\n    def test_positive(self): self.assertEqual(parse_count("12"),12)\n'}
        else:
            source,test='counts.ts','test.mjs'
            files={source:'export const parseCount = (value: string): number => Number(value);\n',
                test:'import {strict as assert} from "node:assert";\nimport {parseCount} from "./counts.ts";\nassert.equal(parseCount("12"),12);\n'}
        goal='Extend the count parser to accept decimal nonnegative integer strings with optional surrounding whitespace. Blank or whitespace-only strings must return zero. Negative numbers, fractional numbers and other noninteger strings must raise an error instead of returning a number. Preserve existing positive integer behavior. Read actual source and tests, implement within the two approved paths, add meaningful regression tests, run all approved checks and repair failures. Do not weaken tests.'
        if variant in {'limit-v5','quota-v6'}:
            files={p.replace('counts','limits'):text.replace('counts','limits').replace('parse_count','parse_limit').replace('parseCount','parseLimit') for p,text in files.items()}
            source,test=source.replace('counts','limits'),test.replace('counts','limits')
            goal='Implement a decimal limit parser accepting integer strings from 0 through 100 inclusive, with optional surrounding whitespace; blank strings mean zero. Reject negative, fractional, nonnumeric and greater-than-100 inputs with an error. Preserve valid existing inputs. First read source/tests, then add meaningful regression tests ONLY and run all approved checks to observe the genuine new failure before changing implementation. Read that actual failure, repair the implementation within scope and rerun every check. Finish only with real passing evidence. Do not weaken or remove tests.'
            if variant=='quota-v6':
                files={p.replace('limits','quotas'):text.replace('limits','quotas').replace('parse_limit','parse_quota').replace('parseLimit','parseQuota') for p,text in files.items()}
                source,test=source.replace('limits','quotas'),test.replace('limits','quotas')
                goal=goal.replace('limit parser','quota parser').replace('100','250')
    manifest={'classification':'controlled_archive_real_model_real_docker_fixture_plan',
        'sample':language,'variant':variant,'files_hash':content_hash(files),'goal':goal,'model':model,
        'write_paths':[source,test],'human_acceptance':False,'counts_as_M5':False}
    evidence=Path(os.environ.get('CONTRIBOS_V2_EVIDENCE_ROOT',str(tmp_path)))
    evidence.mkdir(parents=True,exist_ok=True)
    (evidence/(language+'-manifest.json')).write_text(json.dumps(manifest,indent=2)+'\n')
    db,s,task,workers,_,_=node_workflow(tmp_path,monkeypatch,'npm','10.9.4',real=True,
        files_override=files,source_override=source,write_paths_override=[source,test],
        commands_override=(PlanCommand('tests','Run complete local regression tests',argv),),goal_override=goal,runtime_v2=True)
    s=replace(s,agent_runtime_v2_enabled=True)
    key=bytes.fromhex(Path(os.environ['CONTRIBOS_V2_GATEWAY_KEY_FILE']).read_text())
    broker=GatewayTaskCredentialBroker(codec=GatewayTaskTokenCodec(key),
        base_url=os.environ['CONTRIBOS_V2_GATEWAY_URL'],network='contribos-model-gateway-arv2',
        service_name='contribos-model-gateway-arv2',provider_name='nvidia_nim')
    def runner():return NvidiaNimGatewayRunner(broker=broker,timeout_seconds=120,
        parameters=NvidiaNimParameters(max_tokens=4096,temperature=0.2,reasoning_effort=None))
    def adapter(session,spec):return StructuredModelAdapter(runner(),identity,spec.binding.attempt_id)
    model_worker=AgentModelWorker(db,s,adapter,worker_id='live-v2-model')
    environment={k:os.environ[k] for k in ('HOME','PATH','TMPDIR','DOCKER_HOST','DOCKER_CONTEXT') if k in os.environ}
    def repository(attempt_id):
        with db.session() as session:return ExecutionAttemptService(session).get_verified(attempt_id).repository_full_name
    tool_worker=AgentToolWorker(db,s,DockerAgentToolGateway(artifact_root=s.artifact_root,
        docker_environment=environment,repository_for_attempt=repository),worker_id='live-v2-sandbox')
    workers[3].runner,workers[3].identity=runner(),identity
    report={**manifest,'status':'failed','operator_business_patches':0,'operator_interventions':0,'external_writes':0}
    try:
        for step in range(90):
            for worker in (workers[0],workers[-1],workers[1],model_worker,tool_worker,workers[3]):
                job=asyncio.run(worker.run_once())
                if job:assert job.state=='succeeded',(job.kind,job.error_code,job.error_message)
            advance_workflows(db,s)
            with db.session() as session:
                wb=Workbench(session,s.artifact_root)
                blocker=wb.active_blocker(task)
                assert blocker is None,blocker.payload if blocker else None
                if wb.latest(task,'awaiting_acceptance'):
                    review=session.scalar(select(ReviewRun).order_by(ReviewRun.created_at.desc()))
                    assert review.verdict=='pass'
                    events=wb.history(task)
                    results=[wb.read(e.payload['artifact_id']) for e in events if e.kind=='agent_v2_tool_result']
                    assert any(r['data'].get('tool')=='apply_patch' for r in results)
                    assert any(r['data'].get('outcome')=='passed' for r in results)
                    report.update(status='passed',task_id=task,review_hash=review.record_hash,
                        diff_hash=review.diff_hash,tests_hash=review.test_results_hash,
                        model_call_count=sum(e.kind=='agent_v2_model_started' for e in events),
                        tools=[{'name':r['data'].get('tool'),'status':r['status'],'source_hash':r['output_source_hash']} for r in results])
                    break
        assert report['status']=='passed','bounded controlled journey did not reach review'
    finally:
        with db.session() as session:
            wb=Workbench(session,s.artifact_root)
            events=wb.history(task)
            report['event_hashes']=[e.record_hash for e in events]
            report['model_call_count']=sum(e.kind=='agent_v2_model_started' for e in events)
            report['failed_jobs']=[{'kind':j.kind,'reason_code':j.error_code} for j in wb.jobs(task) if j.state in {'failed','timed_out'}]
        (evidence/(language+'-result.json')).write_text(json.dumps(report,indent=2)+'\n')
        asyncio.run(cleanup_node_workflow(db,s,workers))
        db.close()
