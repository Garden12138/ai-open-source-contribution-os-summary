from pathlib import Path
from datetime import datetime,timezone
import ast,hashlib,importlib.util,json,sqlite3,subprocess
import httpx
from app.provenance import content_hash
from app.database import Database
from app.audit import AuditService
from app.workbench import Workbench
from app.plans import PlanVersionService
from app.security import ensure_no_sensitive_data
from app.agent_v2.contracts import TaskSpec
from app.agent_v2.native_tools import require_native_transport,native_model_transport
r=Path('docs/acceptance/universal-environment-v1');root=Path('/tmp/upeve-plan11-preview-20261103');p=json.loads((r/'ci-native-plan11-execution-proposal.json').read_text());old=json.loads((r/'ci-native-plan10-execution.json').read_text());checks=json.loads((r/'native-rejection-feedback-checks.json').read_text());deployment=json.loads((r/'native-rejection-feedback-deployment.json').read_text());b=json.loads((r/'ci-native-plan10-baseline.json').read_text())
assert content_hash({k:v for k,v in p.items() if k!='proposal_hash'})==p['proposal_hash'];assert not p['execution_approved'] and not (r/'ci-native-plan11-execution-approval.json').exists() and not (r/'ci-native-plan11-execution.json').exists()
assert p['tested_production_files']==checks['tested_production_files'] and p['tested_source_manifest_hash']==checks['source_manifest_hash']==deployment['source_manifest_hash'];assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h for n,h in p['tested_production_files'].items())
assert subprocess.run(['git','diff','--quiet','HEAD','--','app','tests'],capture_output=True).returncode==0
helper=Path(p['execution_script']);assert hashlib.sha256(helper.read_bytes()).hexdigest()==p['execution_script_sha256'];ast.parse(helper.read_text());require_native_transport(p['model_transport'])
previous=json.loads((r/'e0-baseline.json').read_text())['history_files'];assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h for n,h in previous.items())
spec=importlib.util.spec_from_file_location('p11_helper',helper);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);observed=module.observe_native_state(p['runtime_deployment']['roles']['serve']['id'],p['task_id']);assert observed['active']==0 and observed['history']==p['native_journal'] and observed['defaults']==p['defaults_record_hash'] and len(observed['history'])==1473
kernels=[]
for role,item in p['runtime_deployment']['roles'].items():
 v=json.loads(subprocess.check_output(['docker','inspect',item['id']],text=True))[0];assert v['State']['Running'] and module.runtime_fingerprint(v)==p['runtime_deployment']['configuration_fingerprints'][role]
 kernels.append(subprocess.check_output(['docker','exec',item['id'],'python','-c','from pathlib import Path;import hashlib;print(hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest())'],text=True).strip())
assert len(set(kernels))==1
code="import sqlite3,json;from app.provenance import content_hash;db=sqlite3.connect('file:/data/state.db?mode=ro',uri=True);print(json.dumps({'changed':[t for t,h in "+repr(deployment['all_table_hashes_unchanged'])+".items() if content_hash(sorted(list(db.execute('select * from '+t)),key=str))!=h],'integrity':db.execute('pragma integrity_check').fetchone()[0],'FK':list(db.execute('pragma foreign_key_check'))}))"
current=json.loads(subprocess.check_output(['docker','exec',p['runtime_deployment']['roles']['serve']['id'],'python','-c',code],text=True));assert set(current['changed'])=={'contribution_task_state_versions','plan_versions','workbench_events'} and current['integrity']=='ok' and not current['FK']
live_script_hash=subprocess.check_output(['docker','exec',p['runtime_deployment']['roles']['sandbox-worker']['id'],'python','-c','from app.sandbox_worker.dependencies import _PROXY_SCRIPT;import hashlib;print(hashlib.sha256(_PROXY_SCRIPT.encode()).hexdigest())'],text=True).strip();assert live_script_hash==json.loads((r/'package-dns-cache-checks.json').read_text())['fixed_proxy_script_hash']
registered=0
with sqlite3.connect('file:'+str(root/'state.db')+'?mode=ro',uri=True) as db:
 def read(aid):
  key=db.execute('select storage_key from artifacts where id=?',(aid,)).fetchone()[0];raw=(root/'artifacts'/key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid;return json.loads(raw)
 for aid,key in db.execute('select id,storage_key from artifacts'):
  path=(root/'artifacts'/key).resolve();assert path.is_relative_to((root/'artifacts').resolve());raw=path.read_bytes();assert hashlib.sha256(raw).hexdigest()==aid;registered+=1
  try:v=json.loads(raw)
  except (UnicodeDecodeError,json.JSONDecodeError):continue
  ensure_no_sensitive_data(v,context='verified actual artifact')
 for job in old['jobs']:
  assert db.execute('select state,attempt_count,max_attempts,error_code from jobs where id=?',(job['id'],)).fetchone()==tuple(job[k] for k in ['state','attempt_count','max_attempts','error_code'])
 assert not list(db.execute("select id from jobs where state in ('queued','leased','running')"))
 raw=read(b['agent_spec_artifact']);old_spec=TaskSpec.model_validate(raw);assert old_spec.model_dump()==raw
 try:require_native_transport(old_spec.model_transport)
 except ValueError:pass
 else:raise AssertionError('Old native model policy cannot adopt v4')
 assert TaskSpec.model_validate(read('327ef693b1dfb04169e2032ca96bb01ff816cdc331e69b01bb0e20c42fec63d0')).model_transport is None
 assert read(b['baseline_artifact_id'])['baseline_verdict']['outcome']=='passed'
 assert old_spec.model_transport!=native_model_transport()
 assert db.execute("select state,attempt_count,max_attempts,error_code from jobs where id='5eee31d5-043b-4b13-b117-95132af7c6ff'").fetchone()==('failed',1,1,'environment_node_preparation_failed')
 assert db.execute("select state,attempt_count,max_attempts from jobs where id='f619363b-ad79-4586-9ba9-c3661b81cb9d'").fetchone()==('succeeded',2,3)
 assert len([x for x in old['journal'] if x['kind']=='agent_v2_model_rejected'])==3
 assert old['checkpoint']['usage']==dict(model_calls=3,input_tokens=60399,output_tokens=12288,protocol_repairs=2)
 assert old['actual_agent_tools_executed']==0
 assert registered==1446
database=Database('sqlite:///'+str(root/'state.db'))
with database.session() as s:
 assert AuditService(s).verify().valid;wb=Workbench(s,str(root/'artifacts'));events=wb.history(p['task_id']);assert [dict(id=e.id,record_hash=e.record_hash) for e in events]==p['native_journal']
 assert [dict(id=e.id,record_hash=e.record_hash) for e in events[:1469]]==old['history_before']+[{k:e[k] for k in ['id','record_hash']} for e in old['journal']]
 assert sum(e.kind=='execution_authorized' for e in events)==8
 child=PlanVersionService(s).get_verified(p['plan_id']);parent=PlanVersionService(s).get_verified(p['parent_plan_id']);assert child.content_hash==parent.content_hash==p['plan_content_hash']
 new=[e for e in events if e.sequence>1469];assert len(new)==4 and not any(e.kind in {'job_queued','execution_authorized','agent_v2_model_started','execution_baseline_ready'} for e in new)
 bad=next(e for e in events if e.sequence==1013);assert bad.record_hash=='0b86f1acaae3be01a601d0ad02f29c168d8868c07c6ac5f06c4cf7e40985f8e2' and 'source_id' not in bad.payload
 for e in events:ensure_no_sensitive_data(e.payload,context='actual event chain')
database.close();health={}
with httpx.Client(timeout=30,trust_env=False) as c:
 for port in [18090,18091,18092,18080]:response=c.get('http://127.0.0.1:'+str(port)+'/health');response.raise_for_status();health[str(port)]=response.json()
 response=c.get('http://127.0.0.1:18090/api/v1/tasks/'+p['task_id']+'/workbench');response.raise_for_status();view=response.json()
 response=c.get('http://127.0.0.1:18090/api/v1/publisher-settings');response.raise_for_status();account=response.json()
assert not view['busy'] and view['state']=='planning' and len(view['plans'])==11 and view['plans'][-1]==p['plan'];assert view['context']==p['context'] and view['model_profiles']==p['task_profiles'] and view['model_binding_hash']==p['model_binding_hash']
assert view['execution']['preflight']['report_hash']==p['preflight_hash'] and view['execution']['actions']['can_execute'] and view['execution']['model_transport_hash']==p['model_transport_hash']
assert view['execution']['agent_runtime_v2']['checkpoint']==old['checkpoint'] and view['execution']['planning_budget']==p['planning_budget']
assert view['execution']['environment_evidence']['status']=='not_prepared'
assert view['execution']['baseline']['attempt_id']==old['execution_attempt_id'] and view['execution']['baseline']['result_hash']==b['baseline_artifact_id']
metrics=view['execution']['agent_runtime_v2']['metrics'];assert metrics['model_call_count']==64 and metrics['operator_interventions']==8 and metrics['native_journey_completed']==0 and 'legacy_unverified' in metrics['measurement_note']
assert account['connected'] and account['login']=='Garden12138'
ledger=list(r.rglob('*.json'))
for path in ledger:ensure_no_sensitive_data(json.loads(path.read_text()),context='current UPEVE ledger')
for role,item in p['runtime_deployment']['roles'].items():
 result=subprocess.run(['docker','logs','--tail','160',item['id']],capture_output=True,text=True);ensure_no_sensitive_data(result.stdout+result.stderr,context='new isolated role logs')
subprocess.run(['git','diff','--check'],check=True)
v=dict(version='upeve-native-Plan11-final-audit-v1',recorded_at=datetime.now(timezone.utc).isoformat(),source_revision=p['engine_revision'],source_manifest_hash=p['tested_source_manifest_hash'],source_files_verified=242,original_history_files_verified=len(previous),registered_artifacts_hash_verified=registered,credential_scan='passed',JSON_ledger_records_scanned=len(ledger),current_events=1473,all1469_old_native_events_preserved=True,Plans=11,execution_authorizations=8,new_Plan11_approved=False,exact_preview=p['proposal_hash'],new_model_calls_after_protocol_fix=0,prior_native_model_starts_total=64,old_Plan10_models=3,old_Plan10_Baseline='passed8_entrypoints4_checks',old_Plan10_rejections=[5,5,5],old_Plan10_tools_executed=0,old_Plan10_checkpoint_usage=old['checkpoint']['usage'],old_Plan10_actual_reported_usage=old['actual_reported_model_usage'],old_Plan10_repair_2of2_and_all_jobs_preserved=True,old_Plan9_network_failure_1of1_and_root2of3_preserved=True,planning64_three_unknown_holds_preserved=True,current_model_transport_changed_requires_new_exact_approval=True,current_worker_proxy_script_hash=live_script_hash,default_regression=checks['default_regression'],focused=checks['focused'],new_protocol_cases=9,unissued_replay='3_original_prompts_max_bound20999_not_model_effect',new_native_Prepare_entrypoints_Baseline_Coding_Verify_Review='not_run',actual_environment_ready=False,historical_Baseline_still_visible={'attempt_id':old['execution_attempt_id'],'result_hash':b['baseline_artifact_id'],'belongs_to':'Plan10_not_new_Plan11'},audit_first_assumption_corrected='Initial read-only audit expected no historical Baseline; actual API retains Plan10 attempt/result. Corrected assertion verifies exact prior provenance without treating it as new Plan11 readiness. No model or business mutation occurred.',operator_mechanical_child=True,native_metrics=metrics,single_Linux_kernel_for5_business_roles=True,kernel_domain_hash=kernels[0],all45_tables_unchanged_during_deploy=True,tables_unchanged_after_child=42,expected_changed_tables=current['changed'],health=health,task_page_HTTP200=True,GitHub_connection='Garden12138_connected',model_Gateway_not_restarted=True,model_Gateway_cold_restart='not_run',Linux_amd64='blocked_not_run',active_jobs=0,integrity='ok',foreign_keys=[],native_first_deliveries='0/4_unchanged',full_U_baselines='0/15_unchanged',historical_Node='failed',current_task='UPEVE-E04',phase_gate_promoted=False,operator_business_patch=False,third_party_writes=0,formal_data_modified=False)
ensure_no_sensitive_data(v,context='Plan11 final audit')
with (r/'ci-native-plan11-final-audit.json').open('x') as f:f.write(json.dumps(v,indent=2)+'\n')
print(json.dumps({'source_files':242,'events':1473,'artifacts':registered,'old_Plan10_native_models':3,'new_native_models_after_fix':0,'credential_scan':'passed','pending_preview':p['proposal_hash']}))
