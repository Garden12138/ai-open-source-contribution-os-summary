"""Read-only Plan16 freeze gate. Never invokes Provider or mutates business state."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess

import httpx

from app.agent_v2.coding_context import POLICY
from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import NATIVE_PROMPT_VERSION, native_model_transport
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_data
from app.sandbox_worker.specs import SandboxPolicy

ROOT = Path(__file__).resolve().parent
REVIEW = ROOT.parent
DEPLOY = REVIEW / 'local-deployment-v1'
HARNESS = REVIEW.parent
P15 = HARNESS / 'read-batch-scheduler-v1/plan15-preparation'
HEAD = 'b33349d684b1705cc9e1d30ee4aa7d71fd32e740'
CODE = 'bd3f8927bfed1a280b8d8ae15cdd446d30357917'
SOURCE = '627a3689ca6647f8747796469f7e93fddff23a8445557066a7cae061e3346d47'
TASK = 'f0e017fa-d2a6-4eb8-8a94-32bb1d29586c'
STATE = Path('/tmp/upeve-isolated-20261029-v2/container-roles.json')
BASE_URL = 'http://127.0.0.1:18090'
IMAGES = {'app': 'sha256:c9c07093cc6d8cd001c3fbd3d39a819580da5ab4e16b7c0990bfcd0f13d7917f',
    'worker': 'sha256:1c82d9a6e0a0143d13a1ba3ab2728cec19472130f28a346172c87daacbbe9476'}
RUNNER = 'sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6'


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


readers = module('plan16_deployment_readers', DEPLOY / 'preflight.py')
prior = module('plan16_preservation_readers', HARNESS / 'read-batch-scheduler-v1/plan14-preparation/prepare_plan.py')


def save(name: str, value: dict) -> dict:
    ensure_no_sensitive_data(value, context=name)
    with (ROOT / name).open('x') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    return value


def seal(value: dict, field: str = 'report_hash') -> dict:
    return {**value, field: content_hash(value)}


def checked(path: Path, field: str = 'report_hash') -> dict:
    value = json.loads(path.read_text())
    assert value[field] == content_hash({k: v for k, v in value.items() if k != field}), str(path)
    return value


def run_json(container: str, program: str) -> dict:
    result = subprocess.run(['docker', 'exec', container, 'python', '-B', '-c', program],
        capture_output=True, text=True)
    if result.returncode:
        # Exceptions from external services can include private values; never print them.
        raise RuntimeError('read_only_container_probe_failed; raw error withheld')
    return json.loads(result.stdout)


def view(client: httpx.Client) -> dict:
    response = client.get(f'/api/v1/tasks/{TASK}/workbench')
    response.raise_for_status()
    return response.json()


def local() -> dict:
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == HEAD
    assert subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip() == 'refactor/agent-runtime-v2'
    assert not subprocess.check_output(['git', 'diff', 'HEAD', '--', 'app', 'tests'])
    expected = checked(REVIEW / 'source-manifest.json')
    actual = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('app').rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    assert actual == expected['files'] and len(actual) == 255 and content_hash(actual) == SOURCE
    committed = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', CODE, '--', 'app'], text=True).splitlines()
    assert set(committed) == set(actual)
    for path, digest in actual.items():
        assert hashlib.sha256(subprocess.check_output(['git', 'show', CODE + ':' + path])).hexdigest() == digest, path
    assert not subprocess.check_output(['git', 'diff', CODE, HEAD, '--', 'app'])
    old = checked(P15 / 'execution-preview.json', 'proposal_hash')
    assert SandboxPolicy().policy_hash == old['sandbox_policy_hash']
    assert Limits().model_dump() == old['additional_run_limits_pending_consent'] == expected['limits_unchanged']
    assert POLICY == expected['policy']
    assert native_model_transport().model_dump() == expected['native_transport']
    assert content_hash(native_model_transport().model_dump()) == expected['native_transport_hash']
    assert NATIVE_PROMPT_VERSION == 'native-coding-loop-recovery-v11'
    request = PlanVersionCreateRequest.model_validate(old['plan'])
    assert content_hash(plan_content(request).hash_payload()) == old['plan_content_hash']
    assert request.files_likely_to_change == old['write_paths'] and len(old['write_paths']) == 8
    assert [{k: c[k] for k in ('command_id', 'argv', 'working_directory')}
        for c in plan_content(request).hash_payload()['commands_to_run']] == old['commands']
    assert len(old['commands']) == 4
    untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z']).split(b'\0')
    unrelated = {p.decode(): hashlib.sha256(Path(p.decode()).read_bytes()).hexdigest()
        for p in untracked if p and not p.decode().startswith(str(ROOT.relative_to(Path.cwd())) + '/')}
    return {'HEAD': HEAD, 'source_commit': CODE, 'source_files': 255, 'source_hash': SOURCE,
        'source_matches_committed_and_worktree': True, 'native_transport': native_model_transport().model_dump(),
        'native_transport_hash': expected['native_transport_hash'], 'prompt': NATIVE_PROMPT_VERSION,
        'coding_recovery_policy': POLICY, 'all_original_limits': Limits().model_dump(),
        'write_paths': old['write_paths'], 'commands': old['commands'],
        'acceptance_criteria': request.acceptance_criteria, 'unrelated_untracked_hashes': unrelated}


def deployment() -> dict:
    state = json.loads(STATE.read_text())
    archived = checked(DEPLOY / 'deployment-report.json')
    assert state['deployment_status'] == 'healthy' and state['source_manifest_hash'] == SOURCE
    assert set(state['roles']) == set(readers.ROLES) and archived['new_images'] == IMAGES
    roles = {}
    for role, item in state['roles'].items():
        info = readers.docker('inspect', item['id'])[0]
        old = readers.docker('inspect', archived['old_roles_preserved_stopped'][role])[0]
        assert info['Id'] == archived['roles'][role]['container']['id']
        assert info['State']['Running'] and not info['State']['Restarting'] and not info['State']['OOMKilled']
        assert not old['State']['Running']
        assert info['Image'] == IMAGES['worker' if role == 'sandbox-worker' else 'app']
        assert readers.docker('image', 'inspect', info['Image'])[0]['Id'] == info['Image']
        assert readers.docker('image', 'inspect', old['Image'])[0]['Id'] == old['Image']
        assert info['HostConfig'] == old['HostConfig'] and info['Mounts'] == old['Mounts']
        assert all(info['Config'][k] == old['Config'][k] for k in ('Env', 'User', 'Cmd', 'Entrypoint', 'WorkingDir'))
        host = info['HostConfig']
        assert info['Config']['User'] == '501:20' and host['ReadonlyRootfs'] and not host['Privileged']
        assert host['CapDrop'] == ['ALL'] and host['SecurityOpt'] == ['no-new-privileges:true']
        assert host['NanoCpus'] == 1000000000 and host['PidsLimit'] == 128
        assert host['Memory'] == (2147483648 if role == 'sandbox-worker' else 536870912)
        assert host['NetworkMode'] == ('none' if role == 'sandbox-worker' else state['network'])
        assert all(m['Type'] == 'volume' or role == 'sandbox-worker' and m['Destination'] == '/var/run/docker.sock'
            for m in info['Mounts'])
        observed = readers.source(item['id'])
        assert observed['files'] == checked(REVIEW / 'source-manifest.json')['files'] and observed['source_hash'] == SOURCE
        runtime = run_json(item['id'], '''import os,json
from pathlib import Path
from app.agent_v2 import coding_context,model_receipts,read_progress
from app.agent_v2.native_tools import native_model_transport,NATIVE_PROMPT_VERSION
from app.runtime_integrity import missing_runtime_modules
os.kill(1,0);assert not missing_runtime_modules(Path('/app/app'))
assert all(m.__file__.startswith('/app/app/') for m in (coding_context,model_receipts,read_progress))
print(json.dumps({'policy':coding_context.POLICY,'transport':native_model_transport().model_dump(),'prompt':NATIVE_PROMPT_VERSION}))''')
        assert runtime == {'policy': POLICY, 'transport': native_model_transport().model_dump(), 'prompt': NATIVE_PROMPT_VERSION}
        roles[role] = {'id': item['id'], 'image': info['Image'], 'source_files': 255, 'source_hash': SOURCE,
            'old_container_stopped': True, 'old_image_available': old['Image'],
            'full_configuration_equal_retained_old_container': True,
            'runtime': runtime, 'user': info['Config']['User'], 'network': host['NetworkMode'],
            'memory': host['Memory'], 'nano_cpus': host['NanoCpus'], 'pids': host['PidsLimit'],
            'readonly_rootfs': True, 'cap_drop': host['CapDrop'], 'security_opt': host['SecurityOpt']}
    assert readers.docker('image', 'inspect', RUNNER)[0]['Id'] == RUNNER
    return {'roles': roles, 'images': IMAGES, 'runner': RUNNER}


def upstream(container: str) -> dict:
    value = run_json(container, '''import asyncio,json,hashlib
from app.config import Settings
from app.github import GitHubClient
from app.provenance import content_hash
async def main():
 s=Settings.from_env()
 async with GitHubClient(s) as g:
  items=await g.search_issues('repo:patoles/agent-flow is:issue "tool" "duplicate"',100)
  issue,=[x for x in items if x.get('number')==107 and x.get('html_url')=='https://github.com/patoles/agent-flow/issues/107']
  branch,head=await g.resolve_repository_head('patoles/agent-flow','main')
  repo=(await g.get_repository_bundle('patoles/agent-flow'))['repository']
 v={k:issue.get(k) for k in ('state','title','updated_at','assignees','locked','pull_request')}
 v.update(issue_url=issue['html_url'],issue_body_hash=content_hash(issue['body']),branch=branch,HEAD=head,
  issue_body_raw_utf8_sha256=hashlib.sha256(issue['body'].encode()).hexdigest(),
  repository={k:repo.get(k) for k in ('full_name','private','archived','disabled','default_branch')})
 print(json.dumps(v))
asyncio.run(main())''')
    old = checked(P15 / 'upstream-check.json', 'record_hash')
    differences = [k for k in ('issue_url', 'state', 'title', 'updated_at', 'issue_body_hash', 'branch', 'HEAD') if value[k] != old[k]]
    value.update(changed_fields=differences, recorded_at=datetime.now(timezone.utc).isoformat(),
        transport='existing_read_only_proxy_from_existing_API_configuration; credentials_stay_in_container',
        missing_host_key_file='No recreation; existing container configuration used', paid_calls=0, third_party_writes=0)
    save('upstream-check.json', seal(value))
    assert not differences, 'STOP: upstream frozen inputs changed'
    assert value['state'] == 'open' and not value['assignees'] and not value['locked'] and not value['pull_request']
    assert value['repository'] == {'full_name': 'patoles/agent-flow', 'private': False, 'archived': False,
        'disabled': False, 'default_branch': 'main'}
    return value


def model_archive(container: str) -> dict:
    value = run_json(container, f'''import hashlib,json,tarfile
from pathlib import Path
from sqlalchemy import text
from app.config import Settings
from app.database import Database
from app.model_settings import ModelSettingsService
from app.workbench import Workbench
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 session.execute(text('BEGIN'));session.execute(text('PRAGMA query_only=ON'))
 models=ModelSettingsService(session);profiles=models.task_profiles({TASK!r},bind=False);values={{}}
 for stage in ('implementation','review'):
  p=models.profile(profiles[stage]);values[stage]={{k:p[k] for k in ('id','record_hash','model','max_tokens','timeout_seconds','temperature','reasoning_effort','structured_output')}}
  values[stage]['connection_hash']=p['connection']['record_hash'];values[stage]['identity']=models.identity(profiles[stage]).hash_payload()
 wb=Workbench(session,s.artifact_root);ctx=wb.latest({TASK!r},'context_ready')
 aid=ctx.payload['archive_hash'];path=Path(s.artifact_root)/'repository-archives'/aid[:2]/aid
 assert path.is_file() and not path.is_symlink();raw=path.read_bytes();assert hashlib.sha256(raw).hexdigest()==aid
 with tarfile.open(path) as tar: roots=sorted({{m.name.split('/')[0] for m in tar.getmembers()}})
 print(json.dumps({{'model_configuration':values,'archive_hash':aid,'archive_bytes':len(raw),'archive_roots':roots,'context_hash':ctx.record_hash}}))
db.close()''')
    old = checked(P15 / 'execution-preview.json', 'proposal_hash')
    frozen = json.loads((P15 / 'model-and-archive-check.json').read_text())
    assert value['model_configuration'] == frozen['model_configuration']
    assert value['archive_hash'] == frozen['archive_hash'] == old['baseline_source_archive_hash']
    assert value['archive_bytes'] == frozen['archive_bytes'] == 877322
    assert value['context_hash'] == frozen['context_hash'] == old['context_hash']
    assert value['archive_roots'] == ['agent-flow-' + old['target']['base_sha']]
    for config in value['model_configuration'].values():
        assert config['model'] == 'deepseek-v4-pro' and config['reasoning_effort'] == 'high'
        assert config['max_tokens'] == 16384 and config['timeout_seconds'] == 300
    return value


def backup() -> dict:
    old = checked(DEPLOY / 'rollback-readiness.json')
    path = Path(old['private_backup_path'])
    assert path.is_file() and not path.is_symlink()
    assert path.parent.stat().st_mode & 0o777 == 0o700 and path.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(path.read_bytes()).hexdigest() == old['backup_sha256']
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
        db.execute('pragma query_only=ON')
        assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('pragma foreign_key_check').fetchall()
        tables = {}
        for (name,) in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"):
            assert all(c.isalnum() or c == '_' for c in name)
            rows = db.execute('select * from "' + name + '"').fetchall()
            tables[name] = {'count': len(rows), 'hash': content_hash(sorted(content_hash(list(r)) for r in rows))}
    assert len(tables) == 45 and content_hash(tables) == old['old_image_network_none_backup_read_test']['tables_hash']
    return {'private_backup_path': str(path), 'sha256': old['backup_sha256'], 'directory_mode': '0700',
        'file_mode': '0600', 'integrity': 'ok', 'foreign_keys': 'ok', 'tables': 45,
        'tables_hash': content_hash(tables), 'matches_prior_old_image_read_test': True,
        'current_read_only_sqlite_check': True, 'full_rollback_restore': 'not_performed', 'database_restore': False}


def basis(state: dict, current: dict) -> None:
    old = checked(P15 / 'execution-preview.json', 'proposal_hash')
    assert state['counts']['all_active_jobs'] == 0 and not current['busy']
    assert state['model_profiles'] == old['model_profiles'] and state['model_identities'] == old['model_identities']
    assert current['model_binding_hash'] == old['model_binding_hash']
    assert current['context']['record_hash'] == old['context_hash']
    assert current['context']['base_sha'] == old['target']['base_sha']
    assert state['runner_image'] == old['runner_image_digest'] == RUNNER
    for key in ('record_hash', 'limits', 'usage', 'remaining', 'reserved', 'unknown_calls'):
        assert current['execution']['planning_budget'][key] == old['planning_budget'][key], key
    contract = current['execution']['environment']['verification_contract']
    old_contract = old['environment']['verification_contract']
    assert contract['commands'] == old_contract['commands']
    for key in ('snapshot_hash', 'source_archive_hash', 'base_commit_sha', 'approved_commands_hash',
        'target_platform', 'runner_image_digest', 'sandbox_policy_hash'):
        assert contract['binding'][key] == old_contract['binding'][key], key


def main() -> None:
    assert not (ROOT / 'prerequisite-check.json').exists()
    code = local()
    deployed = deployment()
    container = deployed['roles']['serve']['id']
    up = upstream(container)
    state = readers.business(container)
    frozen = checked(DEPLOY / 'history-after-tests.json')
    assert state == frozen['business_snapshot'], 'STOP: business state changed since deployment acceptance'
    plans = readers.history(container)
    assert plans == frozen['Plan11_15']
    assert len(state['checkpoints']) == 11 and state['artifacts_hash_and_credentials_verified'] == 1802
    archive = model_archive(container)
    rollback = backup()
    with httpx.Client(base_url=BASE_URL, trust_env=False, timeout=30) as client:
        current = view(client); basis(state, current)
        assert state['latest_plan']['version_number'] == 15
        assert current['state'] == 'executing' and current['execution']['agent_runtime_v2']['checkpoint']['state'] == 'blocked'
        health = {}
        for port in (18090, 18091, 18092, 18080):
            response = client.get(f'http://127.0.0.1:{port}/health'); response.raise_for_status()
            health[str(port)] = response.json(); assert response.json()['status'] == 'ok'
    assert readers.business(container) == state
    save('before-state.json', state)
    save('before-view.json', current)
    save('source-check.json', seal(code))
    save('deployment-check.json', seal(deployed))
    save('history-check.json', seal({'Plan11_15': plans, 'all_45_tables_unchanged': True,
        'all_11_checkpoints_unchanged': True, 'all_1802_artifacts_verified': True, 'counts': state['counts'],
        'historical_fees_approvals_budgets_unknown_holds': 'unchanged', 'query_only': True}))
    save('model-and-archive-check.json', seal(archive))
    save('backup-check.json', seal(rollback))
    report = seal({'version': 'plan16-freeze-prerequisites-v1', 'recorded_at': datetime.now(timezone.utc).isoformat(),
        'git_HEAD': HEAD, 'source_commit': CODE, 'source_hash': SOURCE, 'source_files': 255,
        'all_frozen_inputs_match': True, 'five_roles_verified': True, 'upstream_unchanged': True,
        'all_original_limits_unchanged': True, 'planning_budget_exhausted_no_planner_authorized': True,
        'new_run_budget_allocated': False, 'paid_calls': 0, 'new_plan': False, 'new_binding': False,
        'new_approval_or_job': False, 'health': health, 'counts': state['counts'],
        'old_roles_images_retained': True, 'backup_available': True,
        'Linux_amd64': 'not_run_still_open', 'HR4_HR5_P5_G06': 'not_passed',
        'upstream_hash': content_hash(up),
        'evidence_hashes': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            'before-state.json', 'before-view.json', 'source-check.json', 'deployment-check.json',
            'history-check.json', 'model-and-archive-check.json', 'backup-check.json', 'upstream-check.json')}})
    save('prerequisite-check.json', report)
    print(json.dumps({'gate': 'passed', 'report_hash': report['report_hash'], 'counts': state['counts'], 'paid_calls': 0}))


if __name__ == '__main__':
    main()
