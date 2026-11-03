from pathlib import Path
from datetime import datetime,timezone
import ast,hashlib,importlib.util,json,sqlite3,subprocess
import httpx
from app.provenance import content_hash
from app.database import Database
from app.audit import AuditService
from app.workbench import Workbench
from app.agent_v2.contracts import TaskSpec
from app.agent_v2.native_tools import require_native_transport
from app.security import ensure_no_sensitive_data
r=Path('docs/acceptance/universal-environment-v1');root=Path('/tmp/upeve-plan11-terminal-20261103');before=Path('/tmp/upeve-plan11-preview-20261103');p=json.loads((r/'ci-native-plan11-execution-proposal.json').read_text());v=json.loads((r/'ci-native-plan11-execution.json').read_text());b=json.loads((r/'ci-native-plan11-baseline.json').read_text());o=json.loads((r/'ci-native-plan11-read-observation.json').read_text());approval=json.loads((r/'ci-native-plan11-execution-approval.json').read_text())
assert p['proposal_hash']==content_hash({k:z for k,z in p.items() if k!='proposal_hash'})==approval['approved_proposal_hash'];assert approval['approval_hash']==v['approval_hash']==content_hash({k:z for k,z in approval.items() if k!='approval_hash'})
assert approval['execution_approved'] and not approval['publication_approved'] and not approval['third_party_writes_approved']
assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h for n,h in p['tested_production_files'].items());assert len(p['tested_production_files'])==242
helper=Path(p['execution_script']);assert hashlib.sha256(helper.read_bytes()).hexdigest()==p['execution_script_sha256'];ast.parse(helper.read_text())
module_spec=importlib.util.spec_from_file_location('Plan11_frozen_helper',helper);m=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(m)
observed=m.observe_native_state(p['runtime_deployment']['roles']['serve']['id'],p['task_id']);assert observed['active']==0 and len(observed['history'])==1782 and observed['history'][:1473]==p['native_journal']
previous=json.loads((r/'e0-baseline.json').read_text())['history_files'];assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h for n,h in previous.items())
state=json.loads(Path('/tmp/upeve-isolated-20261029-v2/container-roles.json').read_text());assert state['roles']==p['runtime_deployment']['roles'] and state['source_manifest_hash']==p['tested_source_manifest_hash']
kernels=[]
for role,item in state['roles'].items():
 value=json.loads(subprocess.check_output(['docker','inspect',item['id']],text=True))[0];assert value['State']['Running'] and m.runtime_fingerprint(value)==p['runtime_deployment']['configuration_fingerprints'][role]
 kernels.append(subprocess.check_output(['docker','exec',item['id'],'python','-c','from pathlib import Path;import hashlib;print(hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest())'],text=True).strip())
 result=subprocess.run(['docker','logs','--tail','180',item['id']],capture_output=True,text=True);ensure_no_sensitive_data(result.stdout+result.stderr,context='Plan11 role logs')
assert len(set(kernels))==1
with sqlite3.connect('file:'+str(before/'state.db')+'?mode=ro',uri=True) as old, sqlite3.connect('file:'+str(root/'state.db')+'?mode=ro',uri=True) as new:
 assert new.execute('pragma integrity_check').fetchone()[0]=='ok' and not list(new.execute('pragma foreign_key_check'))
 unchanged=[];changed=[];old_rows_replaced={}
 for name, in old.execute("select name from sqlite_master where type='table' order by name"):
  a=set(old.execute('select * from "'+name+'"'));z=set(new.execute('select * from "'+name+'"'))
  (unchanged if a==z else changed).append(name)
  if a-z:old_rows_replaced[name]=len(a-z)
 for name in ['workbench_events','plan_versions','execution_attempts','artifacts','jobs','model_config_versions']:
  assert name not in old_rows_replaced,(name,old_rows_replaced.get(name))
 for aid,key in old.execute('select id,storage_key from artifacts'):
  assert new.execute('select storage_key from artifacts where id=?',(aid,)).fetchone()==(key,)
  assert hashlib.sha256((root/'artifacts'/key).read_bytes()).hexdigest()==aid
 spec=TaskSpec.model_validate(json.loads((root/'artifacts'/new.execute('select storage_key from artifacts where id=?',(b['agent_spec_artifact'],)).fetchone()[0]).read_text()));require_native_transport(spec.model_transport)
 for name in ['ci-native-plan10-execution.json','ci-native-plan8-execution.json']:
  prior=json.loads((r/name).read_text());session_id=prior['native_agent_session_id'] if 'native_agent_session_id' in prior else prior.get('session_id')
  if name=='ci-native-plan10-execution.json':
   checkpoints=[json.loads(x[0])['checkpoint'] for x in new.execute("select payload from workbench_events where task_id=? and kind='agent_v2_checkpoint' order by sequence",(p['task_id'],)) if json.loads(x[0]).get('session_id')==prior['checkpoint']['session_id']]
   assert checkpoints[-1]['usage']==prior['checkpoint']['usage'] and checkpoints[-1]['state']=='blocked'
 assert new.execute("select state,attempt_count,max_attempts,error_code from jobs where id='5eee31d5-043b-4b13-b117-95132af7c6ff'").fetchone()==('failed',1,1,'environment_node_preparation_failed')
 authorizations=new.execute("select count(*) from workbench_events where task_id=? and kind='execution_authorized'",(p['task_id'],)).fetchone()[0];assert authorizations==9
 assert new.execute("select count(*) from workbench_events where task_id=? and kind='agent_v2_model_started'",(p['task_id'],)).fetchone()[0]==78
 failed_jobs=[dict(id=i,kind=k,error_code=e,attempt_count=a,max_attempts=x) for i,k,e,a,x in new.execute('select id,kind,error_code,attempt_count,max_attempts from jobs where created_at>=? and state=\'failed\'',(v['recorded_at'].replace('T',' ').split('+')[0],))]
 assert len(failed_jobs)==1 and failed_jobs[0]['kind']=='agent_v2_model' and failed_jobs[0]['error_code']=='agent_budget_exceeded' and failed_jobs[0]['attempt_count']==failed_jobs[0]['max_attempts']==1
 assert not list(new.execute("select id from jobs where state in ('queued','leased','running')"))
registered=0
db=Database('sqlite:///'+str(root/'state.db'))
with db.session() as s:
 assert AuditService(s).verify().valid;wb=Workbench(s,str(root/'artifacts'));events=wb.history(p['task_id']);assert len(events)==1782 and [dict(id=e.id,record_hash=e.record_hash) for e in events[:1473]]==p['native_journal']
 bad=next(e for e in events if e.sequence==1013);assert bad.record_hash=='0b86f1acaae3be01a601d0ad02f29c168d8868c07c6ac5f06c4cf7e40985f8e2' and 'source_id' not in bad.payload
 for e in events:ensure_no_sensitive_data(e.payload,context='Plan11 event chain')
db.close();health={}
with httpx.Client(timeout=30,trust_env=False) as c:
 for port in [18090,18091,18092,18080]:response=c.get('http://127.0.0.1:'+str(port)+'/health');response.raise_for_status();health[str(port)]=response.json()
 response=c.get('http://127.0.0.1:18090/api/v1/tasks/'+p['task_id']+'/workbench');response.raise_for_status();view=response.json()
 assert not view['busy'] and view['execution']['agent_runtime_v2']['checkpoint']==v['checkpoint'] and not view['execution']['actions']['can_accept']
 assert view['execution']['blocked']['reason_code']=='agent_budget_exceeded'
 assert view['model_profiles']==p['task_profiles'] and view['model_binding_hash']==p['model_binding_hash']
 assert {k:z for k,z in view['execution']['planning_budget'].items() if k!='revision'}=={k:z for k,z in p['planning_budget'].items() if k!='revision'}
 metrics=view['execution']['agent_runtime_v2']['metrics'];assert metrics['model_call_count']==78 and metrics['native_journey_completed']==0 and metrics['operator_interventions']==8 and 'legacy_unverified' in metrics['measurement_note']
ledger=list(r.rglob('*.json'))
for path in ledger:ensure_no_sensitive_data(json.loads(path.read_text()),context='terminal UPEVE ledger')
for cmd in [['git','diff','--check'],['.venv/bin/python','-m','compileall','-q','app'],['.venv/bin/python','-m','app.runtime_integrity']]:subprocess.run(cmd,check=True,capture_output=True)
value=dict(version='upeve-native-Plan11-terminal-audit-v1',recorded_at=datetime.now(timezone.utc).isoformat(),engine_revision=p['engine_revision'],source_manifest_hash=p['tested_source_manifest_hash'],source_files_verified=242,current_runtime_images_and_roles_unchanged=True,source_frozen_during_native_execution=True,original_history_files_verified=74,old_event_prefix_preserved=1473,current_events=1782,registered_artifacts_hash_verified=v['persisted_artifacts_hash_verified'],old_registered_artifacts_retained=1446,storage_integrity='ok',foreign_keys=[],audit_and_event_chain='passed',tables_unchanged=unchanged,tables_changed_by_authorized_execution=changed,old_rows_changed=old_rows_replaced,old_immutable_events_Plans_Attempts_artifacts_jobs_models_preserved=True,execution_authorizations=authorizations,Plans=11,all_old_failures_and_charges_preserved=True,planning64_three_unknown_holds_preserved=True,planning_revision_observation={'before':p['planning_budget']['revision'],'after':view['execution']['planning_budget']['revision'],'cause':'revision includes task state hash; planning to executing transition changes revision, budget record_hash/limits/usage/remaining/reserves/unknown counts remain identical'},audit_initial_assumption_corrected='Read-only audit initially compared complete planning view including state-bound revision; corrected to verify unchanged budget record and all accounting fields. No business mutation or model request occurred.',native_model_starts_total=78,Plan11_actual_native_models=14,Plan11_valid_Decisions=12,Plan11_tool_results=43,Plan11_reads=39,Plan11_list_paths=4,Plan11_actual_sandbox_patches=0,Plan11_native_tests_after_change=0,Plan11_code_repairs=0,Plan11_protocol_repairs_consumed=2,Plan11_actual_protocol_corrections_delivered_and_cleared_after_valid_turn=True,Plan11_Baseline='fresh_native8_entrypoints4_commands_passed',Plan11_charged_usage=v['checkpoint']['usage'],Plan11_reported_usage=v['actual_reported_model_usage'],failed_admission_job=failed_jobs[0],fifteenth_model_request_not_started_or_charged=True,terminal_reason='agent_budget_exceeded',remaining_input=28744,quota_or_credential_failure='not_observed_no_hidden_model_intent_inferred',source_read_repeat_facts='39 reads6 paths6 start offsets33 repeats_all_offset0',new_paid_retry_or_new_Plan='not_created',independent_Review='not_run',Issue_regression='not_run',final_human_acceptance='not_available_no_patch',health=health,task_page_HTTP200=True,single_Linux_kernel_for_all5_business_roles=True,kernel_domain_hash=kernels[0],model_Gateway_not_restarted=True,model_Gateway_cold_restart='not_run',canary_and_credential_scan='passed',JSON_records_scanned=len(ledger),compileall='passed',runtime_integrity='passed',diff_check='passed',active_jobs=0,Linux_amd64='blocked_not_run',native_first_deliveries='0/4_unchanged',full_U_baselines='0/15_unchanged',historical_Node='failed_unsupported_dynamic_installer',current_task='UPEVE-E04',phase_gate_promoted=False,operator_business_patch=False,third_party_writes=0,formal_data_modified=False)
ensure_no_sensitive_data(value,context='Plan11 terminal audit')
with (r/'ci-native-plan11-terminal-audit.json').open('x') as f:f.write(json.dumps(value,indent=2)+'\n')
print(json.dumps({'events':1782,'artifacts':v['persisted_artifacts_hash_verified'],'old_rows_changed':old_rows_replaced,'table_changes':changed,'health':health,'model_starts':14,'patches':0,'reason':'agent_budget_exceeded','old_charges_and_holds':'preserved'}))
