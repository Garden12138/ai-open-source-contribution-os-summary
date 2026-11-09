"""Final read-only audit of actual Plan16 and its UNSENT exact preview."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

import httpx

from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import native_model_transport
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_bytes, ensure_no_sensitive_data
from app.workbench_api import ExecutePlanRequest
from verify import (ROOT, REVIEW, DEPLOY, P15, HEAD, SOURCE, TASK, BASE_URL,
    checked, seal, save, local, deployment, readers, prior, view, basis, backup, run_json)


def main() -> None:
    assert not (ROOT / 'final-checks.json').exists()
    p = checked(ROOT / 'execution-preview.json', 'proposal_hash')
    f = checked(ROOT / 'frozen-inputs.json', 'frozen_inputs_hash')
    budget = checked(ROOT / 'budget-and-risk.json')
    contract = checked(ROOT / 'acceptance-report-contract.json', 'record_hash')
    template = checked(ROOT / 'acceptance-report-template.json', 'record_hash')
    assert p['frozen_inputs'] == f and p['frozen_inputs_hash'] == f['frozen_inputs_hash']
    assert p['budget_and_risk'] == budget and p['real_acceptance_contract'] == contract
    initial = {k:v for k,v in template.items() if k not in {'version','classification','contract_hash','record_hash'}}
    assert p['real_results_initial'] == initial
    assert template['contract_hash'] == contract['record_hash'] == f['acceptance_contract_hash']
    assert f['original_coding_limits'] == budget['coding_limits'] == Limits().model_dump()
    old = checked(P15 / 'execution-preview.json', 'proposal_hash')
    assert f['write_paths'] == old['write_paths'] and f['commands'] == old['commands']
    assert f['review_bounds'] == budget['review'] == old['review_bounds']
    assert f['existing_call_bounds'] == budget['single_call'] == old['existing_call_bounds']
    assert plan_content(PlanVersionCreateRequest.model_validate(p['plan'])).hash_payload() == plan_content(
        PlanVersionCreateRequest.model_validate(old['plan'])).hash_payload()
    assert p['plan']['version_number'] == 16 and p['plan']['id'] != old['plan']['id']
    assert p['plan_record_hash'] != old['plan_record_hash'] and p['binding_hash'] != old['plan_binding_hash']
    local_report = local(); deployed = deployment(); container = deployed['roles']['serve']['id']
    assert local_report == {k:v for k,v in checked(ROOT / 'source-check.json').items() if k != 'report_hash'}
    before = json.loads((ROOT / 'before-state.json').read_text())
    after = readers.business(container)
    assert after == json.loads((ROOT / 'after-state.json').read_text())
    assert prior.check_preserved(before, after) == p['only_appended_tables']
    assert readers.history(container) == checked(ROOT / 'history-check.json')['Plan11_15']
    assert len(after['checkpoints']) == 11 and after['latest_plan']['id'] == p['plan']['id']
    assert after['latest_plan']['record_hash'] == p['plan_record_hash']
    assert after['latest_binding']['record_hash'] == p['binding_hash']
    assert after['latest_binding'] == f['binding']
    assert f['native_transport'] == native_model_transport().model_dump()
    assert f['native_transport_hash'] == content_hash(native_model_transport().model_dump())
    assert after['counts'] == p['counts_after'] == {'active_jobs':0,'all_active_jobs':0,'model_starts':106,
        'execution_authorizations':13,'plan_approvals':13,'task_events':2555,'artifacts':1802}
    assert backup() == {k:v for k,v in checked(ROOT / 'backup-check.json').items() if k != 'report_hash'}
    with httpx.Client(base_url=BASE_URL, timeout=30, trust_env=False) as client:
        current = view(client); basis(after, current)
        assert current == json.loads((ROOT / 'after-view.json').read_text())
        assert current['state'] == 'planning' and not current['busy']
        assert current['execution']['revision'] == p['execution_revision'] == f['execution_revision']
        assert current['execution']['preflight'] == f['preflight']
        assert current['execution']['preflight']['status'] == 'ready'
        assert current['execution']['preflight']['report_hash'] == p['preflight_hash']
        assert current['execution']['environment'] == f['environment']
        assert f['environment']['readiness'] == {'static_admitted': True,
            'toolchain_prepared': 'unverified', 'baseline_verified': 'unverified'}
        health = {}
        for port in (18090,18091,18092,18080):
            response = client.get(f'http://127.0.0.1:{port}/health'); response.raise_for_status()
            health[str(port)] = response.json(); assert response.json()['status'] == 'ok'
    request = ExecutePlanRequest.model_validate(p['future_execution_request']['body'])
    assert request.plan_id == p['plan']['id'] and request.plan_hash == p['plan_record_hash']
    assert request.model_binding_hash == f['model_binding_hash']
    assert request.model_transport_hash == f['native_transport_hash']
    assert p['future_execution_request']['idempotency_key'] == 'harness-coding-v11-plan16-execute-once'
    assert not p['future_execution_request']['sent'] and not p['execution_approved'] and not p['execution_started']
    assert not p['new_run_budget_allocated'] and p['new_model_calls'] == p['new_jobs'] == p['new_approvals'] == 0
    assert all(p[k] is None for k in ('authorization_hash','attempt_hash','diff_hash','test_hash','review_hash'))
    reports = []
    for row in checked(DEPLOY / 'test-report-index.json')['reports']:
        raw = (DEPLOY / row['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row['public_sha256']
        cases = ET.fromstring(raw).findall('.//testcase'); assert len(cases) == row['tests']
        for tag, key in (('failure','failed'),('error','errors'),('skipped','skipped')):
            assert sum(c.find(tag) is not None for c in cases) == row[key]
        reports.append({'file':row['file'],'sha256':row['public_sha256'],'passed':row['passed'],
            'failed':row['failed'],'classification':row['classification']})
    containers = [r['id'] for r in deployed['roles'].values()]
    for port in ('18091','18092'):
        containers += subprocess.check_output(['docker','ps','-q','--filter','publish='+port],text=True).split()
    containers = list(dict.fromkeys(containers))
    values = dict(os.environ)
    for line in Path('.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=',1); values[key.strip()] = value.strip().strip('\"\'')
    for cid in containers:
        info = readers.docker('inspect',cid)[0]
        for item in info['Config']['Env']:
            if '=' in item:
                key, value = item.split('=',1); values[key] = value
    credentials = list({v for k,v in values.items() if len(v) >= 16
        and re.search(r'TOKEN|SECRET|PASSWORD|API_KEY|SIGNING_KEY|MANAGEMENT_KEY|PROXY_KEY',k.upper())
        and not k.endswith(('_FILE','_PATH','_ENV_NAME'))})
    canaries = [prefix + 'CANARY_DO_NOT_LEAK_123456789' for prefix in ('github_' + 'pat_', 'gh' + 'p_')]
    credentials += canaries
    scans = run_json(container, f'''import json,os,sqlite3
from pathlib import Path
from sqlalchemy import text
from app.config import Settings
from app.database import Database
from app.security import ensure_no_sensitive_data,ensure_no_sensitive_bytes
s=Settings.from_env(); db=Database(s.database_url)
secrets=[v for k,v in os.environ.items() if len(v)>=16 and any(x in k.upper() for x in ('TOKEN','SECRET','PASSWORD','API_KEY','SIGNING_KEY','MANAGEMENT_KEY','PROXY_KEY')) and not k.endswith(('_FILE','_PATH','_ENV_NAME'))]
secrets += {canaries!r}
with db.session() as session:
 session.execute(text('BEGIN'));session.execute(text('PRAGMA query_only=ON'));count=0
 for (name,) in session.execute(text("select name from sqlite_master where type='table' and name not like 'sqlite_%'")):
  assert all(c.isalnum() or c=='_' for c in name)
  for row in session.execute(text('select * from "'+name+'"')):
   ensure_no_sensitive_data(list(row),secrets=secrets,context='Plan16 database exact credential audit');count+=1
 artifacts=session.execute(text('select storage_key from artifacts')).all()
 for (key,) in artifacts: ensure_no_sensitive_bytes((Path(s.artifact_root)/key).read_bytes(),secrets=secrets,context='Plan16 Artifact audit')
 print(json.dumps({{'database_rows_scanned':count,'artifacts_scanned':len(artifacts),'credential_matches':0,'query_only':True}}))
db.close()''')
    files = []
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts: continue
        raw = path.read_bytes(); ensure_no_sensitive_bytes(raw,secrets=credentials,context=path.name)
        files.append({'file':str(path.relative_to(ROOT)),'sha256':hashlib.sha256(raw).hexdigest()})
        diff = subprocess.run(['git','diff','--no-index','--check','--','/dev/null',str(path)],capture_output=True)
        assert not diff.stdout and not diff.stderr, path.name
    logs = {}
    for cid in containers:
        result = subprocess.run(['docker','logs','--tail','2000',cid],capture_output=True,check=True)
        raw = result.stdout+result.stderr; ensure_no_sensitive_bytes(raw,secrets=credentials,context='owned runtime logs')
        assert not re.search(rb'Traceback \(most recent call last\)|\b(?:ERROR|CRITICAL)\b',raw)
        logs[cid] = {'sha256':hashlib.sha256(raw).hexdigest(),'credential_matches':0}
    confirmation = (ROOT/'confirmation-text.txt').read_text()
    for digest in (p['proposal_hash'],f['frozen_inputs_hash'],p['binding_hash'],p['plan_record_hash'],
        p['execution_revision'],p['preflight_hash'],f['production_source_hash']):
        assert digest in confirmation and digest in (ROOT/'execution-preview.md').read_text()
    assert confirmation != (P15/'confirmation-text.txt').read_text()
    assert old['proposal_hash'] not in confirmation and old['plan_binding_hash'] not in confirmation
    for path,digest in local_report['unrelated_untracked_hashes'].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    assert not subprocess.check_output(['git','diff','HEAD','--','app','tests'])
    assert not subprocess.check_output(['git','diff','--cached','--name-only'])
    subprocess.run(['.venv/bin/python','-m','compileall','-q','app',str(ROOT)],check=True)
    subprocess.run(['git','diff','--check'],check=True)
    # Last transaction: preparation must remain idle and all audited records unchanged.
    assert readers.business(container) == after
    result = seal({'version':'plan16-final-freeze-audit-v1','recorded_at':datetime.now(timezone.utc).isoformat(),
        'audit':'passed','classification':'read_only_after_authorized_Plan_Binding_preparation; no real execution',
        'plan_id':p['plan']['id'],'plan_hash':p['plan_record_hash'],'binding_hash':p['binding_hash'],
        'frozen_inputs_hash':f['frozen_inputs_hash'],'preview_hash':p['proposal_hash'],
        'execution_revision':p['execution_revision'],'preflight_hash':p['preflight_hash'],
        'source_hash':SOURCE,'source_files':255,'five_roles_images_source_security_limits_networks':'verified',
        'all_45_table_preexisting_rows_11_checkpoints_and_historical_states':'unchanged',
        'only_appended_tables':p['only_appended_tables'],'counts':after['counts'],
        'Plan11_15_fees_approvals_budgets_unknown_holds':'unchanged','archive_JUnit_reverified':reports,
        'new_Provider_calls':0,'new_execution_authorizations':0,'new_plan_approvals':0,'new_jobs':0,
        'new_attempts':0,'new_ReviewRuns':0,'new_budget_allocated':False,'future_execution_request_sent':False,
        'confirmation_sha256':hashlib.sha256(confirmation.encode()).hexdigest(),'confirmation_consumed':False,
        'database_and_artifact_sensitive_scan':scans,'logs_scanned_in_memory_only':logs,
        'configured_credential_values_and_canaries_checked':len(credentials),'new_files':files,
        'compileall':'passed','git_diff_check':'passed','untracked_text_diff_check':'passed',
        'unrelated_files_preserved':True,'HEAD_and_index_unchanged':True,
        'services_redeployed':False,'redeploy_reason':'No production code/config change; current explicit scope permits freeze only',
        'health':health,'backup':'same_hash_readable_45_tables_integrity_foreign_keys; full_rollback_not_performed',
        'old_containers_images':'retained','HR4_HR5_P5_G06':'open','Linux_amd64':'not_run_open',
        'Go':'freeze_preparation_for_independent_review','No_Go':'any_paid_execution_without_new_exact_consent',
        'next_action':'STOP; await independent review and exact one-time Plan16 execution authorization'})
    save('final-checks.json',result)
    print(json.dumps({'audit':'passed','report_hash':result['report_hash'],'preview_hash':p['proposal_hash'],
        'confirmation_sha256':result['confirmation_sha256'],'counts':after['counts'],'new_paid_calls':0}))


if __name__ == '__main__':
    main()
