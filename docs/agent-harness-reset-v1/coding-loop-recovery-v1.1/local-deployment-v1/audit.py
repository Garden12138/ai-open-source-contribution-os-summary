"""Final query-only postdeployment history, image, security and log audit."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

from preflight import ROOT, STATE, SOURCE, CODE, RUNNER, docker, business, history, source, local, save
from switch import runtime, health


def main() -> None:
    assert not (ROOT / 'postdeploy-audit.json').exists()
    gate = json.loads((ROOT / 'preflight.json').read_text())
    local()
    state = json.loads(STATE.read_text()); deployment = json.loads((ROOT / 'deployment-report.json').read_text())
    before = json.loads((ROOT / 'history-before.json').read_text())
    assert state['source_manifest_hash'] == SOURCE and state['deployment_status'] == 'healthy'
    observed = business(state['roles']['serve']['id']); assert observed == before['business_snapshot']
    plans = history(state['roles']['serve']['id']); assert plans == before['Plan11_15']
    checks = {}
    for role, item in state['roles'].items():
        value = docker('inspect',item['id'])[0]
        old = docker('inspect',deployment['old_roles_preserved_stopped'][role])[0]
        assert value['State']['Running'] and not value['State']['Restarting'] and not value['State'].get('OOMKilled')
        assert not old['State']['Running']
        expected_image = deployment['new_images']['worker' if role=='sandbox-worker' else 'app']
        assert value['Image'] == expected_image
        assert value['HostConfig'] == old['HostConfig'] and value['Mounts'] == old['Mounts']
        for key in ('Env','User','Cmd','Entrypoint','WorkingDir'): assert value['Config'][key] == old['Config'][key]
        code = source(item['id']); assert len(code['files']) == 255 and code['source_hash'] == SOURCE
        probe = runtime(item['id']); assert probe['transport'] == gate['local']['native_transport']
        assert probe['policy'] == gate['local']['policy'] and probe['prompt_version'] == gate['local']['prompt_version']
        confidential = [e.split('=',1)[1] for e in value['Config']['Env'] if '=' in e and
            re.search(r'(?:SIGNING_KEY|MANAGEMENT_KEY|PROXY_KEY|ACCESS_TOKEN|API_KEY|PASSWORD)',e.split('=',1)[0])]
        logs = subprocess.run(['docker','logs','--tail','250',item['id']],capture_output=True,check=True)
        raw = logs.stdout + logs.stderr
        ensure_no_sensitive_data(raw,secrets=confidential,context='local runtime logs')
        assert not re.search(rb'Traceback \(most recent call last\)|\b(?:ERROR|CRITICAL)\b',raw), role
        checks[role] = {'image':value['Image'],'source_hash':SOURCE,'source_files':255,
            'process_and_runtime_imports':'passed','configured_HTTP_health':health(18090 if role=='serve' else 18091) if role in {'serve','publisher'} else None,
            'docker_healthcheck_configured':bool(value['Config'].get('Healthcheck')),
            'container_running':True,'restarting':False,'security_limits_networks_environment_mounts_unchanged':True,
            'logs_scanned_in_memory_only':True,'credential_matches':0,'runtime_errors':0,'log_digest':hashlib.sha256(raw).hexdigest(),
            'old_container_preserved_stopped':True,'old_image_available':docker('image','inspect',old['Image'])[0]['Id']}
    assert docker('image','inspect',RUNNER)[0]['Id'] == RUNNER
    # These independent services were not included in the five-role switch.
    outside = {'host_model_gateway':health(18080),'host_read_proxy':health(18092)}
    save('history-after-tests.json',{'business_snapshot':observed,'Plan11_15':plans,
        'all_45_table_rows_11_checkpoints_and_1802_artifacts_unchanged':True})
    report = save('postdeploy-audit.json',{'source_commit':CODE,'source_hash':SOURCE,'roles':checks,
        'native_transport':gate['local']['native_transport'],'native_transport_hash':gate['local']['native_transport_hash'],
        'prompt_version':gate['local']['prompt_version'],'policy':gate['local']['policy'],
        'limits_unchanged':gate['local']['limits_unchanged'],'runner_unchanged':RUNNER,
        'counts':observed['counts'],'all_business_rows_approvals_fees_budgets_unknown_holds_unchanged':True,
        'audit_chain_and_all_Artifact_hashes_valid':True,'migrations':0,'other_host_services_not_restarted':outside,
        'live_model_identities':observed['model_identities'],'paid_provider_calls':0,'Plan16':False,'old_Plans_resumed':False,
        'new_approvals_or_business_jobs':0,'cross_platform_Linux_amd64':'NOT_RUN'})
    print(json.dumps({'audit':'passed','roles':5,'tables':45,'artifacts':1802,'source_hash':SOURCE,
        'counts':observed['counts'],'report_hash':report['report_hash']}))


if __name__ == '__main__': main()
