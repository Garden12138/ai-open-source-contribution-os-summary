"""Execute only a separately human-approved exact Plan11 preview, once."""
from pathlib import Path
import asyncio
import datetime
import hashlib
import json
import subprocess

import httpx
from app.config import Settings
from app.github import GitHubClient
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.workbench_api import ExecutePlanRequest



def runtime_fingerprint(value: dict) -> str:
    host_keys = ('Binds', 'Mounts', 'NetworkMode', 'PortBindings', 'ReadonlyRootfs',
        'Privileged', 'CapAdd', 'CapDrop', 'SecurityOpt', 'Tmpfs', 'Memory',
        'NanoCpus', 'PidsLimit', 'GroupAdd', 'ExtraHosts', 'Devices', 'DeviceRequests',
        'PidMode', 'IpcMode', 'VolumesFrom', 'Ulimits')
    public_names = {'DATABASE_URL', 'ARTIFACT_ROOT', 'TMPDIR', 'DOCKER_HOST',
        'MODEL_GATEWAY_BASE_URL', 'MODEL_GATEWAY_NETWORK', 'MODEL_GATEWAY_SERVICE_NAME',
        'PUBLISHER_MANAGEMENT_URL', 'GITHUB_READ_PROXY_URL', 'UPEVE_TARGET_PLATFORM',
        'SANDBOX_STAGE_RUNTIME', 'WORKBENCH_RUNNER_IMAGE', 'DEPENDENCY_BUILDER_IMAGE'}
    public_env = {k: v for row in value['Config']['Env'] for k, v in [row.split('=', 1)] if k in public_names}
    return content_hash({'image': value['Image'], 'user': value['Config']['User'],
        'entrypoint': value['Config']['Entrypoint'], 'cmd': value['Config']['Cmd'],
        'host': {k: value['HostConfig'].get(k) for k in host_keys}, 'public_env': public_env,
        'labels': value['Config']['Labels'], 'mounts': [{k: m.get(k) for k in
            ('Type', 'Name', 'Source', 'Destination', 'RW')} for m in value['Mounts']]})


def observe_native_state(api_id: str, task_id: str) -> dict:
    code = """import sqlite3,json,sys
with sqlite3.connect('file:/data/state.db?mode=ro',uri=True) as db:
 print(json.dumps({'active':db.execute("select count(*) from jobs where state in ('queued','leased','running')").fetchone()[0],
  'history':[dict(id=i,record_hash=h) for i,h in db.execute('select id,record_hash from workbench_events where task_id=? order by sequence',(sys.argv[1],))],
  'defaults':db.execute("select record_hash from model_config_versions where scope='defaults' order by sequence desc limit 1").fetchone()[0]}))
"""
    return json.loads(subprocess.check_output(['docker', 'exec', api_id, 'python', '-c', code, task_id], text=True))


def main() -> None:
    records=Path(__file__).resolve().parent
    root=Path('/tmp/upeve-isolated-20261029-v2')
    preview=json.loads((records/'ci-native-plan11-execution-proposal.json').read_text())
    approval=json.loads((records/'ci-native-plan11-execution-approval.json').read_text())
    for value,key in ((preview,'proposal_hash'),(approval,'approval_hash')):
        assert content_hash({k:v for k,v in value.items() if k!=key})==value[key]
    assert approval['approved_proposal_hash']==preview['proposal_hash']
    assert approval['actor']=='human' and approval['decision']=='approved' and approval['execution_approved']
    assert not approval['publication_approved'] and not approval['third_party_writes_approved']
    assert approval['execution_request_body']==preview['execution_request_body']
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==preview['execution_script_sha256']
    ExecutePlanRequest.model_validate(preview['execution_request_body'])
    for path,digest in preview['tested_production_files'].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
    out=records/'ci-native-plan11-execution.json'
    assert not out.exists(), 'Existing or ambiguous action must be reconciled, never repeated automatically'
    runtime = preview['runtime_deployment']
    state = json.loads((root / 'container-roles.json').read_text())
    assert state['roles'] == runtime['roles'] and state['business_volume'] == runtime['business_volume']
    assert state['source_manifest_hash'] == preview['tested_source_manifest_hash']
    for role, item in runtime['roles'].items():
        value = json.loads(subprocess.check_output(['docker', 'inspect', item['id']], text=True))[0]
        assert value['Id'] == item['id'] and value['State']['Running']
        assert value['Config']['Labels']['contribos.owner'] == 'upeve-acceptance'
        assert runtime_fingerprint(value) == runtime['configuration_fingerprints'][role]
    observed = observe_native_state(runtime['roles']['serve']['id'], preview['task_id'])
    assert observed['active'] == 0 and observed['history'] == preview['native_journal']
    assert observed['defaults'] == preview['defaults_record_hash']
    history = observed['history']
    async def head():
        settings=Settings(github_read_proxy_url='http://127.0.0.1:18092',github_read_proxy_key=bytes.fromhex((root/'github-read-proxy-key').read_text().strip()),github_max_retries=0,request_timeout_seconds=20)
        async with GitHubClient(settings) as github:
            return await github.resolve_repository_head(preview['repository'],preview['context']['branch'])
    branch,sha=asyncio.run(head())
    assert sha==preview['context']['base_sha']
    route=f'/api/v1/tasks/{preview["task_id"]}/workbench'
    with httpx.Client(base_url='http://127.0.0.1:18090',timeout=30,trust_env=False) as client:
        response=client.get(route);response.raise_for_status();view=response.json()
        assert not view['busy'] and view['state']=='planning' and view['plans'][-1]==preview['plan']
        assert view['context']==preview['context'] and view['model_binding_hash']==preview['model_binding_hash']
        assert view['model_profiles']==preview['task_profiles']
        assert view['execution']['actions']['can_execute'] and not view['execution']['plan_stale']
        assert view['execution']['preflight']['report_hash']==preview['preflight_hash']
        assert view['execution']['environment']==preview['environment']
        assert view['execution']['model_transport_hash']==preview['model_transport_hash']
        assert view['execution']['planning_budget']==preview['planning_budget']
        assert view['execution']['agent_runtime_v2']['checkpoint']['revision']==preview['old_checkpoint_revision']
        assert view['execution']['agent_runtime_v2']['checkpoint']['usage']==preview['old_native_reservation_retained']
        result=dict(version='upeve-native-plan11-execution-v1',recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            classification='D_native_execution_after_operator_protocol_feedback_transport_rebind_not_new_unseen',
            proposal_hash=preview['proposal_hash'],approval_hash=approval['approval_hash'],task_id=preview['task_id'],
            plan_id=preview['plan_id'],plan_record_hash=preview['plan_record_hash'],plan_content_hash=preview['plan_content_hash'],
            engine_revision=preview['engine_revision'],tested_source_manifest_hash=preview['tested_source_manifest_hash'],
            runtime_backend='single_Linux_kernel_named_volume',runtime_deployment=runtime,
            context=preview['context'],model_binding_hash=preview['model_binding_hash'],task_profiles=preview['task_profiles'],
            model_transport_hash=preview['model_transport_hash'],verification_contract_hash=preview['verification_contract_hash'],
            preflight_hash=preview['preflight_hash'],history_before=history,start_sequence=len(history),
            planning_budget_before=view['execution']['planning_budget'],old_checkpoint_revision=preview['old_checkpoint_revision'],
            old_native_reservation_retained=preview['old_native_reservation_retained'],agent_limits=preview['agent_limits'],
            fresh_upstream_head=dict(branch=branch,sha=sha,matches_frozen_base=True),
            operator_mechanical_child_draft=True,operator_business_patch=False,native_delivery_claim=False,
            publication_approved=False,third_party_writes_approved=False,formal_data_modified=False,
            status='exact_consent_checked_before_single_native_execute_action')
        ensure_no_sensitive_data(result,context='native Plan11 start receipt')
        with out.open('x') as file:file.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        response=client.get('/api/v1/meta');response.raise_for_status()
        headers={'Origin':'http://127.0.0.1:18090','X-CSRF-Token':response.json()['csrf_token'],
            'Idempotency-Key':preview['execution_idempotency_key']}
        response=client.post(preview['execution_endpoint'],json=preview['execution_request_body'],headers=headers)
        if response.status_code not in (200,202):
            raise RuntimeError('Native execute HTTP '+str(response.status_code)+'; reconcile before any repeat')
        job=response.json()
        result.update(native_job_receipt={k:job[k] for k in ('id','kind','state')},status='native_execution_queued')
        ensure_no_sensitive_data(result,context='native Plan11 enqueue receipt')
        out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'status':result['status'],'job':result['native_job_receipt'],
            'approval_hash':approval['approval_hash'],'plan_record_hash':preview['plan_record_hash'],'publication_approved':False}))


if __name__=='__main__':
    main()
