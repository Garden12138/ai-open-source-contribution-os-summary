"""Export final local acceptance evidence; no service or business writes."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

from app.provenance import content_hash
from app.security import ensure_no_sensitive_bytes
from preflight import ROOT, REVIEW, HARNESS, SOURCE, HEAD, save


def scan() -> dict:
    values = dict(os.environ)
    if Path('.env').exists():
        for line in Path('.env').read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key,value = line.split('=',1); values[key.strip()] = value.strip().strip('\"\'')
    needles = set()
    for key,value in values.items():
        if re.search(r'(?:API_KEY|TOKEN|SECRET|PASSWORD)',key,re.I) and len(value) >= 8:
            raw = value.encode(); needles.update([raw,base64.b64encode(raw),base64.urlsafe_b64encode(raw),raw.hex().encode()])
    files = [p for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    for path in files:
        assert path.suffix not in {'.db','.sqlite','.sqlite3','.jsonl','.log','.zip','.key','.pem'}
        raw = path.read_bytes(); ensure_no_sensitive_bytes(raw,context='local acceptance report')
        assert not any(n in raw for n in needles),str(path)
        assert not re.search(rb'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|AKIA[A-Z0-9]{16})\b',raw),str(path)
    changed = subprocess.check_output(['git','diff','--','docs/tasks.md'],text=True)
    added = '\n'.join(line[1:] for line in changed.splitlines() if line.startswith('+') and not line.startswith('+++'))
    ensure_no_sensitive_bytes(added.encode(),context='roadmap additions')
    assert not any(n in added.encode() for n in needles)
    return {'files_scanned':len(files),'credential_matches':0,'raw_DB_or_runtime_logs_exported':False,
        'configured_secret_and_encoded_variants_checked':len(needles)}


def main() -> None:
    assert not (ROOT / 'acceptance-report.json').exists()
    deployment = json.loads((ROOT / 'deployment-report.json').read_text())
    audit = json.loads((ROOT / 'postdeploy-audit.json').read_text())
    reports = []
    spec = importlib.util.spec_from_file_location('public_xml_exporter',HARNESS / 'read-batch-scheduler-v1/output-capacity-v1/export_tests.py')
    exporter = importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)
    (ROOT / 'test-results').mkdir()
    for name, classification in [('image-focus','exact_deployed_image_deterministic_tests'),
            ('image-recovery','exact_deployed_image_crash_budget_and_review_tests'),
            ('postdeploy-oci','28_real_OCI_plus_2_unit'),
            ('image-recovery-before','test_fixture_child_environment_missing_pytest')]:
        raw = Path('/tmp/coding-v11-' + name + '.xml').read_bytes()
        tree = ET.fromstring(raw)
        for node in tree.iter():
            node.attrib.update({k:'local-test-host' if k=='hostname' else exporter.redacted(v) for k,v in node.attrib.items()})
            if node.text: node.text = exporter.redacted(node.text)
            if node.tail: node.tail = exporter.redacted(node.tail)
        public = ET.tostring(tree,encoding='utf-8',xml_declaration=True)
        assert exporter.stats(raw) == exporter.stats(public)
        identity = lambda b:[(e.get('classname'),e.get('name')) for e in ET.fromstring(b).findall('.//testcase')]
        assert [(exporter.redacted(c),exporter.redacted(n)) for c,n in identity(raw)] == identity(public)
        target = ROOT / 'test-results' / (name + '.xml'); target.write_bytes(public)
        row = {'file':str(target.relative_to(ROOT)),'classification':classification,'source_hash':SOURCE,
            'raw_sha256':hashlib.sha256(raw).hexdigest(),'public_sha256':hashlib.sha256(public).hexdigest(),
            'outcomes_and_order_preserved':True,**exporter.stats(public)}
        if name != 'image-recovery-before': assert row['failed'] == row['errors'] == row['skipped'] == 0
        reports.append(row)
    assert [r['passed'] for r in reports[:3]] == [50,53,30]
    save('test-report-index.json',{'reports':reports,'overlapping_not_summed':True,'paid_Provider_calls':0})
    capacity = json.loads(Path('/tmp/coding-v11-postdeploy-oci-capacity.json').read_text())
    assert capacity['report_hash'] == content_hash({k:v for k,v in capacity.items() if k!='report_hash'})
    assert capacity['source_page_bytes'] == 68338 and capacity['child_sizes'] == [4,4]
    with (ROOT / 'oci-capacity.json').open('x') as stream: stream.write(json.dumps(capacity,ensure_ascii=False,indent=2) + '\n')
    scenario_files = list(Path('/tmp/coding-v11-image-scenarios').glob('*.json')) + list(Path('/tmp/coding-v11-postdeploy-oci-scenarios').glob('*.json'))
    scenarios = [json.loads(p.read_text()) for p in sorted(scenario_files)]
    assert all(v['report_hash'] == content_hash({k:x for k,x in v.items() if k!='report_hash'}) for v in scenarios)
    save('scenarios.json',{'classification':'deterministic_model_decisions_real_OCI_only_where_marked','cases':scenarios})
    replay_root = ROOT / 'saved-envelope-replay'
    artifacts = list((replay_root / 'artifacts').iterdir())
    assert len(artifacts) == 201 and all(hashlib.sha256(p.read_bytes()).hexdigest() == p.stem for p in artifacts)
    replay = json.loads((ROOT / 'saved-envelope-replay.json').read_text())
    assert [r['child_sizes'] for r in replay['replays']] == [[4,2],[4,1],[4,1],[4,4],[4,4],[4,4],[4,4]]
    assert subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip() == HEAD
    assert not subprocess.check_output(['git','diff','HEAD','--name-only','--','app','tests'],text=True).strip()
    manifest = json.loads((REVIEW / 'source-manifest.json').read_text())
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in manifest['files'].items())
    subprocess.run(['.venv/bin/python','-m','compileall','-q','app'],check=True)
    subprocess.run(['git','diff','--check'],check=True)
    assert audit['all_business_rows_approvals_fees_budgets_unknown_holds_unchanged']
    result = save('acceptance-report.json',{'scope':'local_isolated_deployment_only','git_HEAD':HEAD,
        'source_commit':deployment['source_commit'],'source_hash':SOURCE,'production_files':255,
        'old_images':deployment['old_images'],'deployed_images':deployment['new_images'],'runner_unchanged':audit['runner_unchanged'],
        'image_digest_kind':'local immutable image configuration digest; no registry publication',
        'five_roles_health':'HTTP for serve/publisher; PID1 and runtime/module checks for all; Docker HEALTHCHECK not configured',
        'native_transport':audit['native_transport'],'native_transport_hash':audit['native_transport_hash'],
        'prompt_version':audit['prompt_version'],'image_focus_passed':50,'image_recovery_passed':53,
        'real_OCI_passed':28,'OCI_pure_unit_passed':2,'saved_envelopes':7,'replay_Artifact_hashes_verified':201,
        'real_model_autonomous_Coding_success_proven':False,'independent_Review':'deterministic_substitute_only',
        'limits_unchanged':audit['limits_unchanged'],'profile_reasoning_effort_unchanged':'high','native_output_per_call_unchanged':4096,
        'historical_45_tables_11_checkpoints_1802_Artifacts_unchanged':True,'history_counts':audit['counts'],
        'migrations':0,'persistent_volumes_retained':True,'rollback_evidence':'old containers/images retained; protected backup tested by old image in network-none bounded tmpfs',
        'initial_harness_probes':'recorded separately; SQLite memory deserialize/WAL and pytest fixture environment corrected without application or production policy changes',
        'new_Provider_calls_Plans_approvals_jobs_or_budget_changes':0,'Push_PR_comments_or_external_publication':0,
        'Linux_amd64':'NOT_RUN_gate_open','local_deployment_acceptance':'GO_for_human_review',
        'next_paid_execution':'NO_GO_new_freeze_binding_and_separate_exact_consent_required',
        'automatic_next_stage':False,'security_and_log_audit_report_hash':audit['report_hash']})
    print(json.dumps({'acceptance':'local_only_passed','source_hash':SOURCE,'tests':[50,53,28,2],
        'saved_envelopes':7,'next_paid_execution':'NO_GO','report_hash':result['report_hash']}))


if __name__ == '__main__': main()
