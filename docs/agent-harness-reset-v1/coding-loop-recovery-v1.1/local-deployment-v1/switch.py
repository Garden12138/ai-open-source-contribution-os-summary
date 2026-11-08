"""One controlled five-role switch, with no business state repair or retries."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import httpx

from preflight import ROOT, STATE, SOURCE, CODE, ROLES, OLD_IMAGES, RUNNER, docker, local, business, history, source, save


def write_state(value: dict) -> None:
    temporary = STATE.with_name('container-roles.v11-switch.tmp')
    with temporary.open('x') as stream: stream.write(json.dumps(value,indent=2) + '\n')
    os.replace(temporary,STATE)


def health(port: int) -> dict:
    with httpx.Client(trust_env=False,timeout=5) as client:
        for _ in range(30):
            try:
                response = client.get('http://127.0.0.1:' + str(port) + '/health')
                response.raise_for_status(); return response.json()
            except httpx.HTTPError: time.sleep(0.5)
    raise RuntimeError('local_health_not_ready')


def runtime(container: str) -> dict:
    program = '''import os,json
from pathlib import Path
from app.agent_v2.native_tools import native_model_transport,NATIVE_PROMPT_VERSION
from app.agent_v2 import coding_context,model_receipts,read_progress
from app.runtime_integrity import missing_runtime_modules
os.kill(1,0);assert not missing_runtime_modules(Path('/app/app'))
assert all(m.__file__.startswith('/app/app/') for m in (coding_context,model_receipts,read_progress))
print(json.dumps({'pid1_alive':True,'runtime_imports':'passed','policy':coding_context.POLICY,
 'transport':native_model_transport().model_dump(),'prompt_version':NATIVE_PROMPT_VERSION}))'''
    return json.loads(subprocess.check_output(['docker','exec',container,'python','-B','-c',program]))


def main() -> None:
    assert not (ROOT / 'switch-start.json').exists()
    gate = json.loads((ROOT / 'preflight.json').read_text()); assert gate['ready_for_controlled_local_deployment']
    build = json.loads((ROOT / 'build-report.json').read_text()); assert build['source_hash'] == SOURCE
    before = json.loads((ROOT / 'history-before.json').read_text())
    roles_before = json.loads((ROOT / 'roles-before.json').read_text())
    local()
    state = json.loads(STATE.read_text()); assert state == roles_before['state_before']
    assert business(state['roles']['serve']['id']) == before['business_snapshot']
    assert history(state['roles']['serve']['id']) == before['Plan11_15']
    old = {role:docker('inspect',state['roles'][role]['id'])[0] for role in ROLES}
    for role, value in old.items():
        assert value['State']['Running'] and value['Image'] == OLD_IMAGES['worker' if role=='sandbox-worker' else 'app']
    for role, image in build['images'].items(): assert docker('image','inspect',image)[0]['Id'] == image
    assert docker('image','inspect',RUNNER)[0]['Id'] == RUNNER
    backup_state = STATE.with_name('container-roles.before-coding-v11-' + SOURCE[:12] + '.json')
    assert not backup_state.exists(); shutil.copy2(STATE,backup_state)
    save('switch-start.json',{'recorded_at':datetime.now(timezone.utc).isoformat(),'source_hash':SOURCE,
        'old_roles':state['roles'],'images':build['images'],'all_active_jobs':0,'same_45_tables_required':True,
        'expected_new_migrations':0,'persistent_volumes_retained':[state['business_volume'],state['publisher_private_volume']]})
    new = {}
    try:
        for role in ('provider-worker','worker','sandbox-worker','serve','publisher'):
            subprocess.run(['docker','stop','--time','10',old[role]['Id']],capture_output=True,check=True)
            print(json.dumps({'old_role_stopped':role}),flush=True)
        endpoint = docker('context','inspect')[0]['Endpoints']['docker']['Host']; assert endpoint.startswith('unix://')
        with httpx.Client(transport=httpx.HTTPTransport(uds=endpoint[7:]),base_url='http://docker',timeout=30) as engine:
            for role, value in old.items():
                config = {k:value['Config'][k] for k in ('User','Env','Entrypoint','Cmd','WorkingDir','ExposedPorts','Healthcheck') if k in value['Config']}
                config['Labels'] = {**value['Config']['Labels'],'contribos.source':'git-' + CODE,'contribos.source_hash':SOURCE}
                config['Image'] = build['images']['worker' if role=='sandbox-worker' else 'app']
                config['HostConfig'] = value['HostConfig']
                if role != 'sandbox-worker':
                    config['NetworkingConfig'] = {'EndpointsConfig':{state['network']:{'Aliases':['publisher'] if role=='publisher' else []}}}
                name = 'coding-v11-' + ('api' if role=='serve' else role) + '-' + SOURCE[:12]
                response = engine.post('/containers/create',params={'name':name},json=config)
                if response.status_code != 201: raise RuntimeError('owned_role_create_failed')
                new[role] = {'id':response.json()['Id'],'name':name}
                save('created-' + role + '.json',{'role':role,**new[role],'image':config['Image']})
            state.update(roles=new,source_revision='git-' + CODE,source_manifest_hash=SOURCE,
                deployment_status='created_not_started',recorded_at=datetime.now(timezone.utc).isoformat())
            write_state(state)
            # Provider remains stopped while general services start and the
            # unchanged history/no-job gate is rechecked. No approval is issued.
            for role in ('serve','publisher','sandbox-worker','worker'):
                response = engine.post('/containers/' + new[role]['id'] + '/start')
                if response.status_code != 204: raise RuntimeError('owned_role_start_failed')
                print(json.dumps({'new_role_started':role}),flush=True)
            service_health = {'serve':health(18090),'publisher':health(18091)}
            assert business(new['serve']['id']) == before['business_snapshot']
            assert history(new['serve']['id']) == before['Plan11_15']
            for role in ('serve','publisher','sandbox-worker','worker'): runtime(new[role]['id'])
            response = engine.post('/containers/' + new['provider-worker']['id'] + '/start')
            if response.status_code != 204: raise RuntimeError('provider_role_start_failed')
            print(json.dumps({'new_role_started':'provider-worker'}),flush=True)
        time.sleep(2)
        after = business(new['serve']['id']); assert after == before['business_snapshot']
        plans = history(new['serve']['id']); assert plans == before['Plan11_15']
        checks = {}
        for role, item in new.items():
            info = docker('inspect',item['id'])[0]
            assert info['State']['Running'] and not docker('inspect',old[role]['Id'])[0]['State']['Running']
            assert info['HostConfig'] == old[role]['HostConfig'] and info['Mounts'] == old[role]['Mounts']
            for field in ('User','Env','Entrypoint','Cmd','WorkingDir'): assert info['Config'][field] == old[role]['Config'][field]
            observed = source(item['id']); assert len(observed['files']) == 255 and observed['source_hash'] == SOURCE
            probe = runtime(item['id'])
            assert probe['policy'] == gate['local']['policy'] and probe['transport'] == gate['local']['native_transport']
            assert probe['prompt_version'] == gate['local']['prompt_version']
            checks[role] = {'container':item,'image':info['Image'],'source_files':255,'source_hash':SOURCE,
                'runtime':probe,'HTTP_health':service_health.get(role),'docker_healthcheck_configured':False,
                'health_basis':'HTTP and runtime/process' if role in service_health else 'PID1 and runtime/import checks; no Docker HTTP healthcheck configured',
                'environment_security_limits_network_mounts_unchanged':True}
        save('history-after-switch.json',{'business_snapshot':after,'Plan11_15':plans,'all_rows_and_artifacts_unchanged':True})
        state['deployment_status'] = 'healthy'; write_state(state)
        result = save('deployment-report.json',{'recorded_at':datetime.now(timezone.utc).isoformat(),
            'source_commit':CODE,'source_hash':SOURCE,'new_images':build['images'],'old_images':OLD_IMAGES,
            'roles':checks,'old_roles_preserved_stopped':{k:v['Id'] for k,v in old.items()},
            'old_state_record_retained':str(backup_state),'business_volume':state['business_volume'],
            'publisher_private_volume':state['publisher_private_volume'],'persistent_volumes_unchanged':True,
            'migration_count':0,'all_45_tables_11_checkpoints_and_1802_artifacts_unchanged':True,
            'new_active_jobs':0,'new_model_calls':0,'new_plans_approvals_or_budget_changes':0,
            'host_Gateway_and_read_proxy_not_restarted':True,'automatic_DB_restore':False})
        print(json.dumps({'deployment':'passed','roles':5,'source_hash':SOURCE,'images':build['images'],
            'migrations':0,'paid_calls':0,'report_hash':result['report_hash']}),flush=True)
    except BaseException as error:
        # Fail closed: no blind retry, container deletion or business DB rewind.
        for item in new.values(): subprocess.run(['docker','stop','--time','5',item['id']],capture_output=True)
        save('switch-failed.json',{'error_type':type(error).__name__,'new_roles':new,'old_roles_preserved':state['roles'] if not new else {r:{'id':v['Id']} for r,v in old.items()},
            'new_roles_stopped':True,'automatic_retry':False,'database_restored_or_rewritten':False})
        raise RuntimeError('controlled_switch_stopped_inspect_sanitized_evidence') from None


if __name__ == '__main__': main()
