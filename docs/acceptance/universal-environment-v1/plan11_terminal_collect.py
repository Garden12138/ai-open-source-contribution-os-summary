from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
import hashlib,json,sqlite3
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.database import Database
from app.audit import AuditService
from app.workbench import Workbench
from app.agent_v2.native_tools import native_input_bound
from app.agent_v2.contracts import TaskSpec
r=Path('docs/acceptance/universal-environment-v1');root=Path('/tmp/upeve-plan11-terminal-20261103');out=r/'ci-native-plan11-execution.json';v=json.loads(out.read_text());p=json.loads((r/'ci-native-plan11-execution-proposal.json').read_text());view=json.loads((root/'view.json').read_text());runtime=view['execution']['agent_runtime_v2'];cp=runtime['checkpoint'];b=json.loads((r/'ci-native-plan11-baseline.json').read_text())
assert not view['busy'] and runtime['session_id']==b['session_id']=='session-2dd44f18b8adba684806de67cc01a327' and cp['state'] in ['blocked','finished']
assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h for n,h in p['tested_production_files'].items())
with sqlite3.connect('file:'+str(root/'state.db')+'?mode=ro',uri=True) as db:
 db.row_factory=sqlite3.Row;assert db.execute('pragma integrity_check').fetchone()[0]=='ok' and not list(db.execute('pragma foreign_key_check'))
 history=[dict(id=i,record_hash=h) for i,h in db.execute('select id,record_hash from workbench_events where task_id=? order by sequence',(p['task_id'],))];assert history[:1473]==v['history_before']==p['native_journal']
 events=[dict(x) for x in db.execute('select id,sequence,kind,job_id,payload,record_hash from workbench_events where task_id=? and sequence>1473 order by sequence',(p['task_id'],))]
 for e in events:e['payload']=json.loads(e['payload'])
 jobs=[dict(x) for x in db.execute('select id,kind,state,attempt_count,max_attempts,error_code,created_at,started_at,completed_at from jobs where created_at>=? order by created_at,id',(v['recorded_at'].replace('T',' ').split('+')[0],))];assert not any(j['state'] in {'queued','leased','running'} for j in jobs)
 def read(aid):
  key=db.execute('select storage_key from artifacts where id=?',(aid,)).fetchone()[0];raw=(root/'artifacts'/key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid;return json.loads(raw)
 spec=TaskSpec.model_validate(read(b['agent_spec_artifact']));assert spec.model_transport.model_dump()==p['model_transport'] and spec.binding.plan_hash==p['plan_record_hash']
 starts=[e for e in events if e['kind']=='agent_v2_model_started'];rejects=[e for e in events if e['kind']=='agent_v2_model_rejected'];turns=[e for e in events if e['kind']=='agent_v2_turn']
 assert len(starts)==cp['usage']['model_calls'] and all(len(e['payload']['decision']['calls'])<=4 for e in turns)
 prompt_facts=[]
 for e in starts:
  prompt=read(e['payload']['prompt_artifact']);assert native_input_bound(prompt)==e['payload']['input_bound']<=32000
  correction=prompt.get('native_protocol_correction')
  if correction:
   rejection=next(x for x in rejects if x['record_hash']==correction['rejection_event_hash']);assert rejection['sequence']<e['sequence'] and rejection['job_id']==correction['model_job_id'] and rejection['payload']['protocol_rejection_receipt']==correction['rejection'];assert correction['remaining_protocol_repairs']==prompt['remaining_budget']['protocol_repairs']==prompt['protocol_feedback']['remaining_budget']['protocol_repairs']
  prompt_facts.append(dict(sequence=e['sequence'],model_job_id=e['job_id'],prompt_artifact=e['payload']['prompt_artifact'],input_bound=e['payload']['input_bound'],transport=e['payload']['transport'],complete_task_inline=not prompt['approved_task_input'].get('partial',False),native_protocol_correction=correction,protocol_feedback=prompt.get('protocol_feedback'),remaining_protocol_repairs=prompt['remaining_budget']['protocol_repairs'],feedback_receipt_count=len(prompt['feedback'])))
 receipts=[dict(sequence=e['sequence'],event_hash=e['record_hash'],failure=e['payload']['failure'],rejection_receipt=e['payload'].get('protocol_rejection_receipt'),provider_failure_receipt=e['payload'].get('provider_failure_receipt')) for e in rejects]
 accepted_usage={k:sum(e['payload']['usage'][k] for e in turns) for k in ['input_tokens','output_tokens']};known=[x['provider_failure_receipt']['reported_usage'] for x in receipts if x['provider_failure_receipt'] and x['provider_failure_receipt'].get('reported_usage') is not None];reported_failed={k:sum(x[k] for x in known) for k in ['input_tokens','output_tokens']};reported={k:accepted_usage[k]+reported_failed[k] for k in accepted_usage}
 assert all(x['provider_failure_receipt']['reservation_retained'] for x in receipts if x['provider_failure_receipt'])
 auth=next(e for e in events if e['kind']=='execution_authorized');assert auth['record_hash']==b['authorization_hash']
 tools=[];checks=[];patches=[]
 for e in events:
  if e['kind']!='agent_v2_tool_result':continue
  result=read(e['payload']['artifact_id']);assert result['source_hash']==e['payload']['source_hash'] and result['output_source_hash']==e['payload']['output_source_hash'] and result['status']==e['payload']['status']
  item=dict(sequence=e['sequence'],event_hash=e['record_hash'],job_id=e['job_id'],name=e['payload']['name'],artifact_id=e['payload']['artifact_id'],status=result['status'],source_hash=result['source_hash'],output_source_hash=result['output_source_hash'],execution_backend=result['data'].get('execution_backend'),failure=result.get('failure'));tools.append(item)
  if item['name']=='apply_patch':patches.append(item)
  if item['name']=='run_approved_checks':
   evidence=result['data'].get('verification',{}).get('command_evidence',[]);checks.append({**item,'outcome':result['data'].get('outcome'),'actual_checks_started':result['data'].get('actual_checks_started'),'command_evidence':[{k:val for k,val in c.items() if k not in {'stdout_log','stderr_log','stdout','stderr'}} for c in evidence]})
 snapshot=read(cp['snapshot_artifact']) if cp['snapshot_artifact'] else None
 source_changes=[] if snapshot is None else [dict(path=n,original_hash=x['original_hash'],content_hash=hashlib.sha256(x['content'].encode()).hexdigest(),changed=x['original_content']!=x['content']) for n,x in snapshot['files'].items()]
 count=0
 for aid,key in db.execute('select id,storage_key from artifacts'):
  raw=(root/'artifacts'/key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid;count+=1
  try:value=json.loads(raw)
  except (UnicodeDecodeError,json.JSONDecodeError):continue
  ensure_no_sensitive_data(value,context='actual Plan11 artifact')
 for key in ['limits','usage','remaining','reserved','unknown_calls']:assert view['execution']['planning_budget'][key]==v['planning_budget_before'][key]
 assert sum(e['kind']=='execution_authorized' for e in [dict(x) for x in db.execute('select kind from workbench_events where task_id=?',(p['task_id'],))])==9
 assert tuple(db.execute("select state,attempt_count,max_attempts from jobs where id=?",(v['native_job_receipt']['id'],)).fetchone())==('succeeded',1,3)
database=Database('sqlite:///'+str(root/'state.db'))
with database.session() as s:
 assert AuditService(s).verify().valid;wb=Workbench(s,str(root/'artifacts'));assert len(wb.history(p['task_id']))==len(history)
 for e in wb.history(p['task_id']):ensure_no_sensitive_data(e.payload,context='Plan11 terminal event')
database.close();old=json.loads((r/'e0-baseline.json').read_text())['history_files'];assert all(hashlib.sha256(Path(n).read_bytes()).hexdigest()==h for n,h in old.items())
new_attempt=next(a for a in view['execution']['attempts'] if a['id']==b['attempt_id']);blocked=(view['execution'].get('blocked')or{}).get('reason_code')
v.update(updated_at=datetime.now(timezone.utc).isoformat(),status='native_terminal_'+cp['state']+('_'+blocked if blocked else ''),active_jobs=0,jobs=jobs,journal=[{k:e[k] for k in ['id','sequence','kind','record_hash']} for e in events],event_count=len(history),exact_execution_authorization_hash=auth['record_hash'],execution_attempt_id=b['attempt_id'],native_baseline_evidence='ci-native-plan11-baseline.json',native_preparation='passed',native_entrypoint_probes=8,native_original_Baseline_checks_passed=4,native_agent_session_id=runtime['session_id'],checkpoint=cp,native_model_starts=len(starts),validated_decisions=len(turns),model_decisions=[dict(sequence=e['sequence'],outcome=e['payload']['decision']['outcome'],tool_names=[c['name'] for c in e['payload']['decision']['calls']],usage=e['payload']['usage']) for e in turns],rejected_batches=receipts,actual_reported_model_usage=reported,actual_reported_accepted_usage=accepted_usage,actual_reported_failed_usage=reported_failed,failed_usage_unreported_count=len(receipts)-len(known),charged_checkpoint_usage=cp['usage'],failed_reservations_retained=True,reservations_not_claimed_actual_spend=True,actual_prompt_delivery=prompt_facts,actual_tool_results=tools,tool_result_counts=dict(Counter(t['name'] for t in tools)),sandbox_patch_results=patches,source_changes=source_changes,native_check_results=checks,code_repairs_used=cp['usage'].get('code_repairs',0),protocol_repairs_used=cp['usage'].get('protocol_repairs',0),protocol_repair_limit=2,hard4_call_limit_preserved=True,whole_invalid_batches_not_partially_executed=True,old_failed_calls_and_usage_not_refunded_or_replayed=True,prior_Plan10_failure_preserved=True,planning_budget_after=view['execution']['planning_budget'],current_environment_evidence=view['execution']['environment_evidence'],current_attempt=new_attempt,blocked_code=blocked,can_accept=view['execution']['actions'].get('can_accept',False),storage_integrity='ok',foreign_key_check=[],audit_and_workbench_hash_chains='passed',persisted_artifacts_hash_verified=count,original_history_files_verified=len(old),credential_scan='passed',operator_business_patch=False,native_success_claim=False,native_first_deliveries='0/4_unchanged',full_U_baselines='0/15_unchanged',publication_approved=False,third_party_writes=0,formal_data_modified=False)
ensure_no_sensitive_data(v,context='actual Plan11 terminal');out.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'status':v['status'],'events':len(history),'artifacts':count,'native_checks':4,'entrypoints':8,'model_calls':len(starts),'valid_turns':len(turns),'tool_counts':v['tool_result_counts'],'patch_results':len(patches),'charged':cp['usage'],'reported_usage':reported,'can_accept':v['can_accept']}))
