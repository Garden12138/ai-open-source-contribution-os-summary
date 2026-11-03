from pathlib import Path
from datetime import datetime,timezone
from collections import Counter,defaultdict
import hashlib,json,sqlite3
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
r=Path('docs/acceptance/universal-environment-v1');root=Path('/tmp/upeve-plan11-terminal-20261103');execution=json.loads((r/'ci-native-plan11-execution.json').read_text());reads=[];prompts=[]
with sqlite3.connect('file:'+str(root/'state.db')+'?mode=ro',uri=True) as db:
 def read(aid):
  key=db.execute('select storage_key from artifacts where id=?',(aid,)).fetchone()[0];raw=(root/'artifacts'/key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid;return json.loads(raw)
 for t in execution['actual_tool_results']:
  if t['name']!='read_file':continue
  d=read(t['artifact_id'])['data'];reads.append(dict(sequence=t['sequence'],artifact_id=t['artifact_id'],path=d.get('path'),offset=d.get('offset'),returned_bytes=len(d.get('text','').encode()),next_offset=d.get('next_offset'),total_bytes=d.get('total_bytes'),content_hash=d.get('content_hash'),status=t['status'],source_hash=t['source_hash']))
 for x in execution['actual_prompt_delivery']:
  prompt=read(x['prompt_artifact']);feedback=[]
  for receipt in prompt['feedback']:
   d=receipt['data']
   if d.get('tool')=='read_file':feedback.append(dict(receipt_artifact=receipt['receipt_artifact'],path=d.get('path'),offset=d.get('offset'),delivered_bytes=len(d.get('text','').encode()),next_offset=d.get('next_offset'),total_bytes=d.get('total_bytes'),inline_partial=receipt.get('inline_projection',{}).get('partial',False)))
  prompts.append(dict(sequence=x['sequence'],input_bound=x['input_bound'],complete_task_inline=x['complete_task_inline'],history_inline_entries=len(prompt['history_index']),history_full_entries=prompt.get('history_evidence',{}).get('total_entries',len(prompt['history_index'])),history_artifact=prompt.get('history_evidence',{}).get('artifact_id'),read_feedback=feedback,native_protocol_correction=x['native_protocol_correction'],stale_protocol_feedback_present=x['protocol_feedback'] is not None))
paths=defaultdict(list)
for x in reads:paths[x['path']].append(x)
counts=Counter((x['source_hash'],x['path'],x['offset']) for x in reads);delivered={x['receipt_artifact'] for p in prompts for x in p['read_feedback']}
summary=[dict(path=n,read_requests=len(xs),unique_start_offsets=len({x['offset'] for x in xs}),start_offsets=[x['offset'] for x in xs],total_bytes=xs[0]['total_bytes'],returned_bytes=sum(x['returned_bytes'] for x in xs),tool_receipts_ever_in_later_prompt=sum(x['artifact_id'] in delivered for x in xs)) for n,xs in paths.items()]
corrected=[]
for i,p in enumerate(prompts):
 if p['native_protocol_correction'] is None:continue
 later_valid=next((t for t in execution['model_decisions'] if t['sequence']>p['sequence']),None)
 following=next((x for x in prompts if later_valid and x['sequence']>later_valid['sequence']),None)
 corrected.append(dict(prompt_sequence=p['sequence'],remaining_repairs=p['native_protocol_correction']['remaining_protocol_repairs'],receipt=p['native_protocol_correction']['rejection'],following_valid_turn=later_valid['sequence'] if later_valid else None,following_prompt_sequence=following['sequence'] if following else None,following_prompt_cleared_stale_feedback=following is not None and not following['stale_protocol_feedback_present'] and following['native_protocol_correction'] is None))
v=dict(version='upeve-Plan11-read-and-protocol-observation-v1',recorded_at=datetime.now(timezone.utc).isoformat(),classification='D_offline_actual_evidence_analysis_no_model_intention_inferred',source='ci-native-plan11-execution.json',source_model_requests=14,accepted_Decisions=12,rejected_batches=[5,5],source_read_results=39,source_paths=len(paths),unique_source_path_start_offset_tuples=len(counts),repeated_start_offset_requests=sum(c-1 for c in counts.values()),source_path_statistics=summary,source_reads=reads,prompt_feedback=prompts,actual_protocol_corrections=corrected,read_tools_not_used=['search_code','read_artifact','inspect_failure'],raw_tool_receipts_not_delivered_due_no_later_model=[x['artifact_id'] for x in reads if x['artifact_id'] not in delivered],full_task_inline_count=sum(p['complete_task_inline'] for p in prompts),all_task_and_feedback_bounds_below32000=all(p['input_bound']<=32000 for p in prompts),writes=0,checks=0,missing_required_native_journey='no_patch_no_Issue_regression_no_Verify_no_independent_Review',next_attempt_budget_gate='next complete native request exceeds28744 remaining input; failed admission did not start or charge a15th model request',inference_limit='Repeated requests and limited inline history are measurable; they do not prove hidden model intent or a unique cause. No quota or credential failure observed. Existing full history artifact remains issued and scoped.',new_model_requests=0,business_data_modified=False,third_party_writes=0,formal_data_modified=False)
ensure_no_sensitive_data(v,context='Plan11 read evidence analysis');v['record_hash']=content_hash(v)
with (r/'ci-native-plan11-read-observation.json').open('x') as f:f.write(json.dumps(v,indent=2)+'\n')
print(json.dumps({'paths':len(paths),'read_results':len(reads),'unique_path_offsets':len(counts),'repeated_starts':v['repeated_start_offset_requests'],'per_path':summary,'protocol_corrections':corrected,'full_task_inline_count':v['full_task_inline_count'],'undelivered_read_receipts':len(v['raw_tool_receipts_not_delivered_due_no_later_model'])}))
