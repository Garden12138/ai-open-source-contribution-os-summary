"""Controlled local acceptance readers. Never creates a Plan or invokes a model."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

import httpx

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent
REVIEW = ROOT.parent
HARNESS = REVIEW.parent
STATE = Path('/tmp/upeve-isolated-20261029-v2/container-roles.json')
SOURCE = '627a3689ca6647f8747796469f7e93fddff23a8445557066a7cae061e3346d47'
OLD_SOURCE = '31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95'
HEAD = '0e6d43f8c6d32034d5af5fc2407c08c3d5d430bb'
CODE = 'bd3f8927bfed1a280b8d8ae15cdd446d30357917'
RUNNER = 'sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6'
ROLES = ('serve', 'worker', 'sandbox-worker', 'provider-worker', 'publisher')
OLD_IMAGES = {'app': 'sha256:ba41b3d68e9281a7e5ce965a2d2780fd30b4f997f1ac6b2bc1ffed52b541313a',
    'worker': 'sha256:2b0e9e989c01562706169f2f5fe6e856f558c8b251ddc3c8926ca9371919f2d2'}


def save(name: str, value: dict) -> dict:
    ensure_no_sensitive_data(value, context='local deployment acceptance')
    value = {**value, 'report_hash': content_hash(value)}
    with (ROOT / name).open('x') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return value


def docker(*args: str) -> object:
    return json.loads(subprocess.check_output(['docker', *args]))


def business(container: str) -> dict:
    spec = importlib.util.spec_from_file_location('original_query_only_reader',
        HARNESS / 'read-batch-scheduler-v1/plan13-preparation/prepare_plan.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.docker_read(container)


def history(container: str) -> dict:
    program = '''
import json
from sqlalchemy import text
from app.config import Settings
from app.database import Database
from app.agent_v2.journal import AgentJournal
from app.workbench import Workbench
from app.provenance import content_hash
s=Settings.from_env(); db=Database(s.database_url)
with db.session() as session:
 session.execute(text('BEGIN'));session.execute(text('PRAGMA query_only=ON'))
 wb=Workbench(session,s.artifact_root);task='f0e017fa-d2a6-4eb8-8a94-32bb1d29586c';rows={}
 for e in wb.history(task):
  if e.kind!='agent_v2_session':continue
  j=AgentJournal(wb,task,e.payload['session_id']); spec=j.spec()
  rows[spec.binding.plan_id]={'session_id':j.session_id,'checkpoint':j.checkpoint().model_dump(),
   'plan_hash':spec.binding.plan_hash,'authorization_hash':spec.binding.authorization_hash,
   'event_count':len(j.events()),'event_hash_set_hash':content_hash([x.record_hash for x in j.events()])}
 print(json.dumps(rows))
db.close()
'''
    raw = json.loads(subprocess.check_output(['docker','exec',container,'python','-B','-c',program]))
    frozen = json.loads((REVIEW / 'history-plan11-15.json').read_text())['plans']
    selected = {}
    for label, row in frozen.items():
        observed = {**raw[row['plan_id']], 'plan_id': row['plan_id']}
        assert observed == row, label
        selected[label] = observed
    return selected


def source(container: str) -> dict:
    program = """
import hashlib,json
from pathlib import Path
from app.provenance import content_hash
f={str(p.relative_to('/app')):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/app/app').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
print(json.dumps({'files':f,'source_hash':content_hash(f)}))
"""
    return json.loads(subprocess.check_output(['docker','exec',container,'python','-B','-c',program]))


def local() -> dict:
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip() == HEAD
    assert subprocess.check_output(['git','branch','--show-current'],text=True).strip() == 'refactor/agent-runtime-v2'
    assert not subprocess.check_output(['git','diff','HEAD','--name-only','--','app','tests'],text=True).strip()
    manifest = json.loads((REVIEW / 'source-manifest.json').read_text())
    actual = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('app').rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    assert actual == manifest['files'] and len(actual) == 255 and content_hash(actual) == SOURCE
    subprocess.run(['git','apply','--reverse','--check',str(REVIEW / 'code.diff')],check=True)
    from app.agent_v2.coding_context import POLICY
    from app.agent_v2.native_tools import native_model_transport, NATIVE_PROMPT_VERSION
    assert POLICY == manifest['policy'] and native_model_transport().model_dump() == manifest['native_transport']
    assert NATIVE_PROMPT_VERSION == 'native-coding-loop-recovery-v11'
    delivery = json.loads((REVIEW / 'git-delivery-check.json').read_text())
    assert hashlib.sha256((REVIEW / 'code.diff').read_bytes()).hexdigest() == delivery['complete_code_diff_sha256']
    assert hashlib.sha256((REVIEW / 'delta-from-review-closeout.diff').read_bytes()).hexdigest() == delivery['delta_from_closeout_sha256']
    for path, digest in delivery['source_test_files'].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    for row in json.loads((REVIEW / 'test-report-index.json').read_text())['reports']:
        assert hashlib.sha256((REVIEW / row['file']).read_bytes()).hexdigest() == row['public_sha256']
        assert row['failed'] == row['errors'] == 0
    return {'git_HEAD':HEAD,'source_commit':CODE,'production_files':255,'source_hash':SOURCE,
        'native_transport':native_model_transport().model_dump(),'native_transport_hash':content_hash(native_model_transport().model_dump()),
        'prompt_version':NATIVE_PROMPT_VERSION,'policy':POLICY,'limits_unchanged':manifest['limits_unchanged'],
        'source_and_test_blobs_equal_reviewed_commit':True,'complete_diff_matches':True,'source_uncommitted_changes':False}


def main() -> None:
    assert not (ROOT / 'preflight.json').exists()
    local_report = local()
    state = json.loads(STATE.read_text()); assert set(state['roles']) == set(ROLES)
    assert state['source_manifest_hash'] == OLD_SOURCE and state['deployment_status'] == 'healthy'
    expected = json.loads((HARNESS / 'read-batch-scheduler-v1/plan15-execution/after-terminal-state.json').read_text())
    live = business(state['roles']['serve']['id']); assert live == expected
    assert live['counts']['all_active_jobs'] == 0 and live['runner_image'] == RUNNER
    plans = history(state['roles']['serve']['id'])
    roles = {}
    manifest = json.loads((REVIEW / 'source-manifest.json').read_text())
    expected_env_forbidden = {'GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','DEEPSEEK_API_KEY','NVIDIA_API_KEY','AWS_SECRET_ACCESS_KEY'}
    for role in ROLES:
        info = docker('inspect',state['roles'][role]['id'])[0]; host = info['HostConfig']
        image = OLD_IMAGES['worker' if role == 'sandbox-worker' else 'app']
        assert info['State']['Running'] and info['Image'] == image
        assert docker('image','inspect',image)[0]['Id'] == image
        assert info['Config']['Labels']['contribos.owner'] == 'upeve-acceptance'
        assert info['Config']['User'] == '501:20' and host['ReadonlyRootfs'] and not host['Privileged']
        assert host['CapDrop'] == ['ALL'] and host['SecurityOpt'] == ['no-new-privileges:true']
        assert host['Memory'] > 0 and host['NanoCpus'] > 0 and 0 < host['PidsLimit'] <= 128
        assert not host.get('ContainerIDFile')
        assert host['NetworkMode'] == ('none' if role == 'sandbox-worker' else state['network'])
        assert any(m.get('Name') == state['business_volume'] for m in info['Mounts'])
        assert all(m['Type']=='volume' or role=='sandbox-worker' and m['Destination']=='/var/run/docker.sock' for m in info['Mounts'])
        assert all(m.get('Name') != state['publisher_private_volume'] or role=='publisher' for m in info['Mounts'])
        env = {x.split('=',1)[0] for x in info['Config']['Env']}; assert not (env & expected_env_forbidden)
        observed = source(info['Id'])
        assert len(observed['files']) == 254 and observed['source_hash'] == OLD_SOURCE
        for path, digest in manifest['files'].items():
            if path.startswith('app/migrations/'):
                assert observed['files'][path] == digest
        roles[role] = {'id':info['Id'],'name':info['Name'].lstrip('/'),'image':image,'running':True,
            'source_files':254,'source_hash':OLD_SOURCE,'user':info['Config']['User'],
            'network_mode':host['NetworkMode'],'readonly_rootfs':True,'cap_drop':host['CapDrop'],
            'security_opt':host['SecurityOpt'],'memory':host['Memory'],'nano_cpus':host['NanoCpus'],'pids':host['PidsLimit'],
            'environment_keys':sorted(env),'mounts':[{'type':m['Type'],'name':m.get('Name'),'destination':m['Destination'],'rw':m['RW']} for m in info['Mounts']],
            'docker_healthcheck_configured':bool(info['Config'].get('Healthcheck'))}
    assert docker('image','inspect',RUNNER)[0]['Id'] == RUNNER
    net = docker('network','inspect',state['network'])[0]
    assert net['Driver'] == 'bridge'
    private = Path(tempfile.mkdtemp(prefix='coding-recovery-v11-private-',dir='/tmp')); os.chmod(private,0o700)
    backup = private / 'state-before.db'
    program = 'import sqlite3,sys;s=sqlite3.connect("file:/data/state.db?mode=ro",uri=True);t=sqlite3.connect(":memory:");s.backup(t);sys.stdout.buffer.write(t.serialize())'
    with os.fdopen(os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
        subprocess.run(['docker','exec',state['roles']['serve']['id'],'python','-B','-c',program],stdout=stream,check=True)
    smoke = '''import sys,sqlite3,json
from pathlib import Path
from app.provenance import content_hash
p=Path('/tmp/rollback.db');p.write_bytes(sys.stdin.buffer.read())
d=sqlite3.connect('file:/tmp/rollback.db?mode=ro',uri=True);d.execute('pragma query_only=ON');assert d.execute('pragma integrity_check').fetchone()[0]=='ok';assert not d.execute('pragma foreign_key_check').fetchall()
t={}
for (n,) in d.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"):
 rows=d.execute('select * from "'+n+'"').fetchall();t[n]={'count':len(rows),'hash':content_hash(sorted(content_hash(list(r)) for r in rows))}
print(json.dumps({'tables':len(t),'tables_hash':content_hash(t),'integrity':'ok'}))'''
    assert backup.stat().st_size < 128 * 1024 * 1024
    with backup.open('rb') as stream:
        rollback = json.loads(subprocess.check_output(['docker','run','--rm','-i','--network','none','--read-only',
            '--user','501:20','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','512m','--cpus','1','--pids-limit','128',
            '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=128m,mode=0700,uid=501,gid=20',
            '--entrypoint','python',OLD_IMAGES['app'],'-B','-c',smoke],stdin=stream))
    compact = {k:{x:v[x] for x in ('count','hash')} for k,v in live['tables'].items()}
    assert rollback['tables'] == 45 and rollback['tables_hash'] == content_hash(compact)
    assert business(state['roles']['serve']['id']) == expected
    save('history-before.json',{'business_snapshot':live,'Plan11_15':plans,'query_only':True,'all_frozen_rows_and_artifacts_verified':True})
    save('roles-before.json',{'roles':roles,'state_before':state,'network':{'id':net['Id'],'name':net['Name'],'driver':net['Driver'],'internal':net['Internal']}})
    save('rollback-readiness.json',{'private_backup_path':str(backup),'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest(),
        'private_directory_mode':'0700','backup_mode':'0600','raw_backup_in_repository':False,
        'original_images_available':OLD_IMAGES,'old_image_network_none_backup_read_test':rollback,
        'initial_probe':'WAL-format serialized image rejected by memory-only deserialize; backup independently passed readonly 45-table audit; filesystem restore in bounded tmpfs used instead',
        'old_roles_must_be_retained_not_deleted':True,'automatic_DB_restore_forbidden':True})
    save('preflight.json',{'local':local_report,'old_source_hash':OLD_SOURCE,'old_images':OLD_IMAGES,
        'plans_and_all_45_tables_11_checkpoints_unchanged':True,'artifacts_verified':1802,'active_jobs':0,
        'migration_files_unchanged':True,'expected_migrations':0,'role_security_networks_volumes_verified':True,
        'rollback_smoke_passed':True,'runner_unchanged':RUNNER,'new_paid_calls':0,'ready_for_controlled_local_deployment':True})
    print(json.dumps({'preflight':'passed','old_source':OLD_SOURCE,'new_source':SOURCE,'roles':5,'tables':45,
        'artifacts':1802,'active_jobs':0,'rollback_smoke':'passed','new_migrations':0}))


if __name__ == '__main__': main()
