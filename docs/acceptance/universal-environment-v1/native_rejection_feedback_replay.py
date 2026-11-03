from pathlib import Path
from datetime import datetime,timezone
import json,sqlite3,hashlib
from app.agent_v2.contracts import TaskSpec
from app.agent_v2.native_tools import NativeProtocolCorrection,native_model_transport,native_input_bound,project_native_prompt
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
r=Path('docs/acceptance/universal-environment-v1');root=Path('/tmp/upeve-plan10-terminal-20261103');db=sqlite3.connect('file:'+str(root/'state.db')+'?mode=ro',uri=True)
def read(aid):
 key=db.execute('select storage_key from artifacts where id=?',(aid,)).fetchone()[0];raw=(root/'artifacts'/key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid;return json.loads(raw)
events=[dict(sequence=s,kind=k,record_hash=h,job_id=j,payload=json.loads(p)) for s,k,h,j,p in db.execute('select sequence,kind,record_hash,job_id,payload from workbench_events where task_id=? and sequence>1434 order by sequence',('f0e017fa-d2a6-4eb8-8a94-32bb1d29586c',))]
se=next(e for e in events if e['kind']=='agent_v2_session');old_spec=TaskSpec.model_validate(read(se['payload']['spec_artifact']));candidate_spec=old_spec.model_copy(update={'model_transport':native_model_transport()});facts=[]
for event in [e for e in events if e['kind']=='agent_v2_model_started']:
 prompt=read(event['payload']['prompt_artifact']);assert native_input_bound(prompt)==event['payload']['input_bound'];candidate=dict(prompt);prior=next((e for e in reversed(events) if e['kind']=='agent_v2_model_rejected' and e['sequence']<event['sequence']),None)
 if prior:
  candidate['protocol_feedback']={**prompt['protocol_feedback'],'remaining_budget':prompt['remaining_budget']}
  candidate['native_protocol_correction']=NativeProtocolCorrection(rejection=prior['payload']['protocol_rejection_receipt'],rejection_event_hash=prior['record_hash'],model_job_id=prior['job_id'],remaining_protocol_repairs=prompt['remaining_budget']['protocol_repairs']).model_dump()
 projected=project_native_prompt(candidate,candidate_spec,task_input=read(prompt['task_context_artifact']));assert native_input_bound(projected)<=32000 and projected['approved_task_input']==prompt['approved_task_input'] and projected['approved_checks']==prompt['approved_checks'] and projected['write_paths']==prompt['write_paths']
 facts.append(dict(sequence=event['sequence'],original_prompt_artifact=event['payload']['prompt_artifact'],original_input_bound=event['payload']['input_bound'],candidate_input_bound=native_input_bound(projected),complete_task_preserved=True,original_nested_repairs=(prompt.get('protocol_feedback')or{}).get('remaining_budget',{}).get('protocol_repairs'),actual_top_level_repairs=prompt['remaining_budget']['protocol_repairs'],candidate_nested_repairs=(projected.get('protocol_feedback')or{}).get('remaining_budget',{}).get('protocol_repairs'),candidate_correction=projected.get('native_protocol_correction'),candidate_prompt_hash=content_hash(projected)))
v=dict(version='upeve-native-rejection-fact-delivery-offline-replay-v1',recorded_at=datetime.now(timezone.utc).isoformat(),classification='D_unissued_projection_of_three_real_Plan10_prompts_not_native_retry_or_success',old_execution='ci-native-plan10-execution.json',old_model_transport=old_spec.model_transport.model_dump(),candidate_model_transport=candidate_spec.model_transport.model_dump(),candidate_model_transport_hash=content_hash(candidate_spec.model_transport.model_dump()),actual_old_model_calls=3,actual_old_batch_sizes=[5,5,5],actual_old_tools_executed=0,candidate_prompts=facts,candidate_max_input_bound=max(x['candidate_input_bound'] for x in facts),per_call_cap_unchanged=32000,whole_batch_call_limit_unchanged=4,protocol_repair_limit_unchanged=2,old_reservations_preserved=True,new_model_requests=0,old_failed_steps_replayed=False,candidate_native_effect='not_run',third_party_writes=0,formal_data_modified=False)
ensure_no_sensitive_data(v,context='unissued rejection feedback replay')
with (r/'native-rejection-feedback-replay.json').open('x') as f:f.write(json.dumps(v,indent=2)+'\n')
print(json.dumps({'replayed':len(facts),'correction_turns':sum(bool(x['candidate_correction']) for x in facts),'max_input_bound':v['candidate_max_input_bound'],'actual_repairs':[x['actual_top_level_repairs'] for x in facts],'new_model_requests':0,'native_effect':'not_run'}))
