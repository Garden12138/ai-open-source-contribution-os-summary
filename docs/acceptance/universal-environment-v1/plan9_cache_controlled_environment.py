from pathlib import Path
from datetime import datetime,timezone
import asyncio,hashlib,json,os,sqlite3,subprocess
from app.archives import RepositoryArchiveStore
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
import app.sandbox_worker.dependencies as dep
from app.sandbox_worker.implementation import DockerImplementRuntime
from app.sandbox_worker.verification import DockerVerifyRuntime
from app.sandbox_worker.specs import SandboxPolicy,SandboxCommand
from app.environment_engine.probe import probe_prepared
from app.environment_engine.baseline import classify_baseline
r=Path('docs/acceptance/universal-environment-v1');root=Path('/tmp/upeve-plan9-terminal-20261103');p=json.loads((r/'ci-native-plan9-execution-proposal.json').read_text());out=r/'ci-native-plan9-cached-proxy-controlled-environment.json';assert not out.exists()
with sqlite3.connect('file:'+str(root/'state.db')+'?mode=ro',uri=True) as db:
 def read(aid):
  key=db.execute('select storage_key from artifacts where id=?',(aid,)).fetchone()[0];raw=(root/'artifacts'/key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid;return json.loads(raw)
 failure=json.loads(db.execute("select payload from workbench_events where task_id=? and kind='execution_preparation_failed' order by sequence desc limit 1",(p['task_id'],)).fetchone()[0]);receipt=read(failure['result_hash']);wire=read(receipt['environment_spec_hash'])
values=dict(wire);values['implement_result_hash']=values.pop('source_result_hash');values['ecosystem']=dep.DependencyEcosystem(values['ecosystem']);values['manifests']=tuple(dep.DependencyManifest(**x) for x in values['manifests']);values['local_projects']=tuple(values.get('local_projects',()));values['project_directories']=tuple(values.get('project_directories',()));values.pop('environment_layout',None);plan=dep.DependencyPreparationPlan(**values);assert plan.to_wire()==wire and plan.environment_contract==p['environment']['verification_contract'] and plan.environment_contract['binding']['plan_version_hash']==p['plan_record_hash']
environment={k:v for k,v in os.environ.items() if k in {'HOME','PATH','TMPDIR','DOCKER_HOST','DOCKER_CONTEXT'}};policy=SandboxPolicy();materializer=DockerImplementRuntime(docker_environment=environment).for_workspace_scope(tuple(p['workspace_paths']));dependencies=dep.DockerDependencyRuntime(docker_environment=environment);proxy=dep.DockerDependencyProxyRuntime(docker_environment=environment);signer=dep.DependencyPlanSigner(key_id='controlled-readonly-plan9-diagnosis',signing_key=b'd'*32);proxy_policy=dep.DependencyProxyPolicy.for_ecosystem('node');archive=RepositoryArchiveStore(root/'artifacts').lookup(p['context']['archive_hash'],repository_full_name=p['repository'],base_commit_sha=p['context']['base_sha'])
def state():
 code="import sqlite3,json;from app.provenance import content_hash;db=sqlite3.connect('file:/data/state.db?mode=ro',uri=True);tables=[x[0] for x in db.execute(\"select name from sqlite_master where type='table'\")];print(json.dumps({'active':db.execute(\"select count(*) from jobs where state in ('queued','leased','running')\").fetchone()[0],'tables':{t:content_hash(sorted(list(db.execute('select * from '+t)),key=str)) for t in tables},'events':db.execute(\"select count(*) from workbench_events where task_id='f0e017fa-d2a6-4eb8-8a94-32bb1d29586c'\").fetchone()[0]}))"
 return json.loads(subprocess.check_output(['docker','exec',p['runtime_deployment']['roles']['serve']['id'],'python','-c',code],text=True))
async def main():
 before=state();assert before['active']==0 and before['events']==1430
 value=dict(version='upeve-plan9-controlled-cached-proxy-v1',recorded_at=datetime.now(timezone.utc).isoformat(),classification='D_independent_real_OCI_frozen_recipe_not_native_retry_or_success',proposal_hash=p['proposal_hash'],actual_native_failure_job='5eee31d5-043b-4b13-b117-95132af7c6ff',native_failure_artifact=failure['result_hash'],exact_original_dependency_spec_hash=receipt['environment_spec_hash'],unchanged_dependency_plan_hash=plan.plan_hash,plan_record_hash=p['plan_record_hash'],runner_image_digest=plan.runner_image_digest,policy_hash=policy.policy_hash,proxy_policy_hash=proxy_policy.policy_hash,all_resource_network_integrity_limits_unchanged=True,unmodified_dependency_runtime=True,proxy_only_change='Validated public endpoints cached30s in proxy-local memory; concurrent DNS serialized; expired and failed entries never used; original host allowlist/TLS/ports/resource limits unchanged',candidate_source_hashes={name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in ['app/sandbox_worker/dependencies.py','app/sandbox_worker/package_dns.py']},candidate_proxy_script_hash=hashlib.sha256(dep._PROXY_SCRIPT.encode()).hexdigest(),component_signing_scope='independent_diagnostic_not_business_authority',native_job_retried=False,native_baseline_or_failure_rewritten=False,model_calls=0,third_party_writes=0,formal_data_modified=False)
 workspace=await materializer.create_workspace(image_digest=plan.runner_image_digest,policy=policy);lease=execution=None
 try:
  await materializer.materialize(workspace=workspace,archive=archive,policy=policy)
  lease=await proxy.start(image_digest=plan.runner_image_digest,policy=proxy_policy)
  try:
   execution=await dependencies.prepare(signed_plan=signer.sign(plan),signer=signer,repository_volume=workspace.volume_name,proxy_policy=proxy_policy,proxy_lease=lease,sandbox_policy=policy)
   value.update(status='controlled_preparation_passed_not_native',dependency_result_hash=execution.result.result_hash,environment_receipt=execution.result.environment_receipt)
   verifier=DockerVerifyRuntime(docker_environment=environment)
   from dataclasses import replace
   current=replace(workspace,inventory_hash=plan.workspace_inventory_hash)
   prepared,entrypoint=await probe_prepared(verifier,contract=plan.environment_contract,dependency=execution,workspace=current,policy=policy);value.update(toolchain_receipt=prepared.model_dump(mode='json'),entrypoint_evidence=entrypoint)
   if prepared.status=='prepared':
    commands=tuple(SandboxCommand(command_id=c['command_id'],argv=tuple(c['argv']),working_directory=c['working_directory']) for c in p['commands'])
    inspection=await verifier.run(workspace=current,commands=commands,policy=policy,dependency_workspace=execution.workspace)
    evidence=[{k:v for k,v in item.to_wire().items() if k not in {'stdout_log','stderr_log','stdout','stderr'}} for item in inspection.command_evidence]
    verdict=classify_baseline(plan.environment_contract,content_hash(prepared.model_dump(mode='json')),[item.to_wire() for item in inspection.command_evidence],content_hash([item.to_wire() for item in inspection.command_evidence]))
    value.update(status='controlled_complete_environment_measured_not_native',baseline_verdict=verdict.model_dump(mode='json'),command_evidence=evidence,source_before=inspection.before_inventory_hash,source_after=inspection.after_inventory_hash)
    assert inspection.before_inventory_hash==inspection.after_inventory_hash==plan.workspace_inventory_hash
  except dep.DependencyPreparationError as exc:value.update(status='controlled_failure_reproduced',safe_error_code=exc.code,diagnostic=exc.diagnostic)
 finally:
  if lease:await proxy.stop(lease,ignore_missing=True)
  if execution:await dependencies.destroy(execution.workspace,ignore_missing=True)
  await materializer.destroy(workspace)
 after=state();assert after==before;value.update(native_all_table_hashes_unchanged=before['tables'],native_events_unchanged=1430,native_model_effect='not_run',native_success_claim=False)
 ensure_no_sensitive_data(value,context='original recipe controlled OCI');out.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'status':value['status'],'diagnostic':value.get('diagnostic'),'entrypoints':len(value.get('toolchain_receipt',{}).get('executables',[])),'baseline':value.get('baseline_verdict',{}).get('outcome'),'native_events_unchanged':1430,'native_retry':False,'model_calls':0}),flush=True)
asyncio.run(main())
