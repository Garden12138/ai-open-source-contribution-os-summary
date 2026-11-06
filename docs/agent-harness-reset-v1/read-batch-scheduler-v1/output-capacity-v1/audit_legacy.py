"""Read-only business integrity and old Plan11/12/13/14 audit, inside Linux DB owner."""
import importlib.util
import json
import subprocess
from pathlib import Path
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('old_read_only_preparation',ROOT.parent/'plan14-preparation/prepare_plan.py')
prior=importlib.util.module_from_spec(spec);spec.loader.exec_module(prior)
state=json.loads(Path('/tmp/upeve-isolated-20261029-v2/container-roles.json').read_text())
after=prior.docker_read(state['roles']['serve']['id'])
before=json.loads((ROOT/'legacy-before.json').read_text())
assert after['integrity']=='ok' and after['audit_chain_valid'] and after['foreign_key_violations']==0
assert after['checkpoints']==before['checkpoints']
assert after['history']==before['history']
assert after['model_profiles']==before['model_profiles'] and after['model_identities']==before['model_identities']
assert after['latest_plan']==before['latest_plan'] and after['latest_binding']==before['latest_binding']
assert after['counts']==before['counts'] and after['counts']['all_active_jobs']==0
for table,value in before['tables'].items():
 if table=='_schema_migrations':
  assert set(value['row_hashes'])<=set(after['tables'][table]['row_hashes'])
  assert after['tables'][table]['count']==value['count']+1
 else:assert after['tables'][table]==value,table
assert after['runner_image']==before['runner_image']
ensure_no_sensitive_data(after,context='read-only legacy compatibility audit')
(ROOT/'legacy-after.json').write_text(json.dumps(after,indent=2)+'\n')
report={'version':'read-capacity-legacy-audit-v1','classification':'real_business_database_query_only',
 'before_hash':content_hash(before),'after_hash':content_hash(after),'all_old_rows_preserved':True,
 'old_task_events_preserved':len(before['history']),'old_checkpoints_preserved':len(before['checkpoints']),
 'all_old_budgets_and_terminal_states_unchanged':True,'model_profiles_and_identities_unchanged':True,
 'counts':after['counts'],'plans':after['tables']['plan_versions']['count'],
 'only_schema_migration_appended':'0046_read_capacity','Plan15_created':False,'Plan14_resumed':False,
 'model_calls':0,'execution_approvals_added':0,'third_party_writes':0,
 'runner_image_unchanged':after['runner_image']}
code = """import json
from sqlalchemy import text
from app.config import Settings
from app.database import Database
from app.workbench import Workbench
from app.agent_v2.journal import AgentJournal
from app.provenance import content_hash
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 session.execute(text('BEGIN'));session.execute(text('PRAGMA query_only=ON'))
 plans=dict(session.execute(text('select id,version_number from plan_versions')).all())
 wb=Workbench(session,s.artifact_root);rows=[]
 for e in wb.history('f0e017fa-d2a6-4eb8-8a94-32bb1d29586c'):
  if e.kind!='agent_v2_session':continue
  spec=wb.read(e.payload['spec_artifact']);version=plans[spec['binding']['plan_id']]
  if version not in (11,12,13,14):continue
  cp=AgentJournal(wb,e.task_id,e.payload['session_id']).checkpoint().model_dump()
  rows.append({'Plan':version,'session_id':cp['session_id'],'checkpoint_hash':content_hash(cp),'state':cp['state'],'usage':cp['usage']})
 print(json.dumps(rows))
"""
report['historical_Plans']=json.loads(subprocess.check_output(['docker','exec',state['roles']['serve']['id'],'python','-c',code]))
assert {r['Plan'] for r in report['historical_Plans']}=={11,12,13,14}
assert all(r['state']=='blocked' for r in report['historical_Plans'])
for row in report['historical_Plans']:
 assert row['usage']==before['checkpoints'][row['session_id']]['usage']
report['report_hash']=content_hash(report)
(ROOT/'legacy-audit.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
