"""Read-only delivery checks. Credential values stay in memory and are never exported."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from app.provenance import content_hash
from app.security import ensure_no_sensitive_bytes, _CREDENTIAL_PATTERNS
ROOT=Path(__file__).resolve().parent
REPO=Path.cwd()
source=json.loads((ROOT/'source-manifest.json').read_text())
assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in source['files'].items())
assert content_hash(source['files'])==source['source_hash']
state=json.loads(Path('/tmp/upeve-isolated-20261029-v2/container-roles.json').read_text())
containers=[v['id'] for v in state['roles'].values()]
# The unchanged local gateway can contain real provider access values. Inspect
# only in memory; never persist Docker inspect output or environments.
for port in ('18091','18092'):
 containers.extend(subprocess.check_output(['docker','ps','-q','--filter','publish='+port],text=True).split())
containers=list(dict.fromkeys(containers))
values=dict(os.environ)
if Path('.env').exists():
 for line in Path('.env').read_text().splitlines():
  if '=' in line and not line.lstrip().startswith('#'):
   k,v=line.split('=',1);values[k.strip()]=v.strip().strip('\"\'')
for container in containers:
 for item in json.loads(subprocess.check_output(['docker','inspect',container]))[0]['Config']['Env']:
  if '=' in item:
   k,v=item.split('=',1);values[k]=v
credentials=[]
for k,v in values.items():
 if any(x in k.upper() for x in ('TOKEN','SECRET','PASSWORD','API_KEY','SIGNING_KEY')) and len(v)>=16:
  if not k.endswith(('_FILE','_PATH','_ENV_NAME')):credentials.append(v)
credentials=list(set(credentials))
private=[str(Path.home()).encode()]
email=subprocess.run(['git','config','--get','user.email'],capture_output=True,text=True).stdout.strip()
if email:private.append(email.encode())
tracked=subprocess.check_output(['git','diff','--name-only'],text=True).splitlines()
new=['app/agent_v2/read_capacity.py','app/migrations/versions/v0046_read_capacity.py',
 'tests/capacity_fixture.py','tests/capacity_projection.py','tests/test_read_capacity.py','tests/test_read_capacity_oci.py']
paths={Path(p) for p in tracked+new}
paths.update(p for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name!='delivery-check.json')
# Reviewed existing English prose about forwarding, extraction, billing,
# compensation, authentication, material and leakage; these are ordinary words.
# Exact configured secrets are checked on the original bytes BEFORE this view.
prose_hashes={
 'bd0499596b419041b1eabbc85f5ddc408162f503e9da3c4cdb6a2bd5e3cdfcfe',
 'bb1b3117ff3814e62e2bf4da554aa55e1024c823c1b07997e7d346e2c5eedc04',
 '15b66e7e73ba999d9d22d2c209a17b1be26463ea9a80857cae14f65ef15ca5b8',
 '20ee560baf34b6470003c4f24a65b652d6b528c025653b59972f30617f86ee06',
 'b12056328fb4b14d081075f7c385c9751ae00fa4bae7432bfb81af4312f62144',
 'f4a9189503737e664f8825dad9698a66cbdad9cb98abcae59563520016d7746c'}
reviewed=[]
entries=[]
for p in sorted(paths):
 assert p.name!='.env' and not any(str(p).endswith(x) for x in ('.db','.sqlite','.sqlite3','.pem','.key','.zip'))
 raw=p.read_bytes()
 assert not any(v.encode() in raw for v in credentials), 'Exact configured credential found: '+str(p)
 scan=raw
 if str(p)=='docs/tasks.md':
  baseline=subprocess.check_output(['git','show','HEAD:docs/tasks.md'])
  text=raw.decode()
  for match in _CREDENTIAL_PATTERNS[2].finditer(text):
   fragment=match.group().encode();digest=hashlib.sha256(fragment).hexdigest()
   assert digest in prose_hashes and baseline.count(fragment)==raw.count(fragment)
   reviewed.append({'file':'docs/tasks.md','match_sha256':digest,'classification':'existing_English_technical_prose','baseline_unchanged':True})
   scan=scan.replace(fragment,b'[technical-term]')
 if str(p) in {'tests/test_baseline_feedback.py','tests/test_execution_scheduling_recovery.py','tests/test_planning.py','tests/test_workbench.py'}:
  baseline=subprocess.check_output(['git','show','HEAD:'+str(p)])
  for pattern in _CREDENTIAL_PATTERNS:
   for match in pattern.finditer(raw.decode()):
    fragment=match.group().encode()
    assert baseline.count(fragment)==raw.count(fragment)
    reviewed.append({'file':str(p),'match_sha256':hashlib.sha256(fragment).hexdigest(),
     'classification':'existing_deterministic_credential_canary_or_error_fixture','baseline_unchanged':True})
    scan=scan.replace(fragment,b'[test-fixture]')
 ensure_no_sensitive_bytes(scan,context=str(p))
 assert not any(v in raw for v in private),str(p)
 entries.append({'path':str(p.relative_to(REPO) if p.is_absolute() else p),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
new_file_checks=0
for p in paths:
 relative=str(p.relative_to(REPO) if p.is_absolute() else p)
 if relative not in tracked:
  result=subprocess.run(['git','diff','--no-index','--check','--','/dev/null',str(p)],capture_output=True)
  assert not result.stdout and not result.stderr,relative
  new_file_checks+=1
for p in (ROOT/'artifacts').iterdir():assert hashlib.sha256(p.read_bytes()).hexdigest()==p.stem
for container in containers:
 result=subprocess.run(['docker','logs','--tail','2000',container],capture_output=True)
 assert result.returncode==0
 ensure_no_sensitive_bytes(result.stdout+result.stderr,secrets=credentials,context='owned service logs')
subprocess.run(['git','diff','--check'],check=True)
subprocess.run(['git','apply','--reverse','--check','--unidiff-zero',str(ROOT/'code.diff')],check=True)
subprocess.run([str(REPO/'.venv/bin/python'),'-m','compileall','-q','app'],check=True)
status=subprocess.check_output(['git','status','--short'],text=True)
assert not any(line[:2] not in (' M','??') for line in status.splitlines())
report={'version':'capacity-delivery-check-v1','source_hash':source['source_hash'],'proposed_files':entries,
 'credential_pattern_scan':'passed_with_reviewed_existing_prose_and_test_canaries','reviewed_pattern_matches':reviewed,'exact_configured_credential_scan':'passed','credential_values_checked_count':len(credentials),
 'local_home_and_email_privacy_scan':'passed','owned_container_logs_scanned':len(containers),
 'raw_databases_ENV_archives_private_keys_in_delivery':False,'content_addressed_artifact_bytes':'verified',
 'git_diff_check':'passed','new_file_whitespace_checks':new_file_checks,'reverse_apply_diff_check':'passed_with_unidiff_zero','compileall_app':'passed',
 'git_HEAD':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'git_branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),
 'index_unmodified':True,'commit_created':False,'push_or_PR':False,
 'unrelated_untracked_files_preserved_excluded':['agent-harness-reset-v1.zip','old four unredacted JUnit reports','plan13-execution/preflight-diagnostic.jsonl']}
report['report_hash']=content_hash(report)
(ROOT/'delivery-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'files':len(entries),'logs_scanned':len(containers),'credential_scan':'passed','privacy_scan':'passed','diff_check':'passed','HEAD_unchanged':True}))
