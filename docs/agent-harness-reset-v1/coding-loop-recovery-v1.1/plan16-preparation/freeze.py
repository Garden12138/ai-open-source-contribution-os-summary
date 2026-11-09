"""Create only the authorized independent Plan/Binding and UNSENT preview.

There is no execution/approval/model endpoint call in this program.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import subprocess

import httpx

from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import native_model_transport
from app.agent_v2.read_capacity import certificate
from app.agent_v2.read_batches import parse_preflight
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.sandbox_worker.specs import SandboxPolicy
from app.workbench_api import ExecutePlanRequest

from verify import (ROOT, REVIEW, DEPLOY, HARNESS, P15, HEAD, CODE, SOURCE, TASK, BASE_URL,
    IMAGES, RUNNER, POLICY, checked, seal, save, local, deployment, readers, prior, view, basis, model_archive, backup)


def contract() -> dict:
    old = checked(P15 / 'acceptance-report-contract.json', 'record_hash')
    value = {k: v for k, v in old.items() if k != 'record_hash'}
    value['version'] = 'plan16-real-coding-recovery-v11-observation-contract-v1'
    value['required_binding'] += ['runtime_source_hash', 'frozen_inputs_hash', 'coding_recovery_policy']
    metrics = value['required_sections']['coding_observation_metrics']
    metrics['missing_or_unavailable'] = 'All actual results refer only to Plan16. Missing observations remain null/not_observed/not_run. No fixture or old session may supply real results.'
    metrics['coding_recovery_6_8_1'] = (
        'Count real requests since last successful OCI Patch. After six unpatched rounds retain the correction evidence. '
        'At eight rounds assess committed response, current-source novelty, exact response/call/receipt hashes, '
        'related path, unknown holds, remaining original budget and exact next prompt. '
        'Only fresh related source actually observed by OCI in round eight AND actually included in round-nine prompt '
        'can grant one bounded opportunity per authorization across workers/repair sessions. '
        'No such evidence or insufficient original budget => stop before dispatch. Unknown operation preserves reservation and blocks. '
        'Round nine without a successful real OCI Patch => stop before request ten. Proposal/read/call count alone is not success.')
    metrics['high_reasoning_truncation'] = (
        'Keep deepseek-v4-pro/high and Coding max_tokens=min(4096, original remaining output budget). '
        'Record finish_reason and actual final_text/tool_calls/usage; length with only reasoning is not Patch success. '
        'Do not infer hidden reasoning, silently raise output capacity, downgrade reasoning or buy another paid run.')
    metrics['human_acceptance'] = 'Require latest-source four Verify results and independent REAL Review; then stop before final human acceptance. No automatic publication.'
    value['required_sections']['budget_and_legacy']['rule'] = (
        'All original Coding/Review caps retained. Plan11–15 histories, fees, approvals, holds and terminal states immutable. '
        'Only separate fresh exact consent can authorize Plan16. No approval reuse, refund, capacity increase or automatic extra run.')
    value['initial_actual_results']['coding_metrics'].update(
        coding_recovery_6_8_1='not_run', high_reasoning_truncation='not_observed', human_acceptance='not_run')
    return seal(value, 'record_hash')


def main() -> None:
    assert not (ROOT / 'plan.json').exists() and not (ROOT / 'execution-preview.json').exists()
    gate = checked(ROOT / 'prerequisite-check.json')
    assert gate['all_frozen_inputs_match'] and gate['backup_available']
    for name, digest in gate['evidence_hashes'].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    local(); deployed = deployment(); container = deployed['roles']['serve']['id']
    before = json.loads((ROOT / 'before-state.json').read_text())
    assert readers.business(container) == before
    assert readers.history(container) == checked(ROOT / 'history-check.json')['Plan11_15']
    assert model_archive(container) == {k: v for k, v in checked(ROOT / 'model-and-archive-check.json').items() if k != 'report_hash'}
    assert backup() == {k: v for k, v in checked(ROOT / 'backup-check.json').items() if k != 'report_hash'}
    old = checked(P15 / 'execution-preview.json', 'proposal_hash')
    # Recompute an existing no-model capacity proof with current trusted code.
    capdir = HARNESS / 'read-batch-scheduler-v1/output-capacity-v1'
    oci = checked(capdir / 'plan14-real-oci-final.json')
    bound = checked(capdir / 'oci-capacity-binding.json')
    proof = parse_preflight(oci['proof']); batch = bound['batch']
    recomputed = certificate(batch, proof, batch_artifact=content_hash(batch), proof_artifact=content_hash(proof),
        remaining=oci['certificate']['remaining_output_bytes'])
    assert recomputed == oci['certificate'] and recomputed['total_bytes_bound'] == 228743 <= recomputed['remaining_output_bytes'] == 459508
    archived_acceptance = checked(DEPLOY / 'acceptance-report.json')
    assert archived_acceptance['source_hash'] == SOURCE and archived_acceptance['limits_unchanged'] == Limits().model_dump()
    assert archived_acceptance['native_transport_hash'] == content_hash(native_model_transport().model_dump())
    report_contract = contract()
    save('acceptance-report-contract.json', report_contract)
    save('acceptance-report-template.json', seal({'version': 'plan16-not-executed-template-v1',
        'classification': 'not_real_execution_evidence', 'contract_hash': report_contract['record_hash'],
        **report_contract['initial_actual_results']}, 'record_hash'))
    with httpx.Client(base_url=BASE_URL, trust_env=False, timeout=30) as client:
        current = view(client); basis(before, current)
        assert current == json.loads((ROOT / 'before-view.json').read_text())
        assert before['latest_plan']['version_number'] == 15 and before['latest_plan']['record_hash'] == old['plan_record_hash']
        request = PlanVersionCreateRequest.model_validate({**old['plan'], 'parent_version_id': old['plan']['id']})
        assert content_hash(plan_content(request).hash_payload()) == old['plan_content_hash']
        response = client.get('/api/v1/meta'); response.raise_for_status()
        headers = {'Origin': BASE_URL, 'X-CSRF-Token': response.json()['csrf_token']}
        # Necessary user_replan transition only; no running Job or terminal is resumed.
        response = client.post(f'/api/v1/tasks/{TASK}/workbench/stop', json={},
            headers={**headers, 'Idempotency-Key': 'harness-coding-v11-plan16-preparation-stop'})
        response.raise_for_status()
        planning = view(client)
        assert planning['state'] == 'planning' and not planning['busy'] and planning['context'] == current['context']
        response = client.post(f'/api/v1/tasks/{TASK}/workbench/plans',
            json={'context_hash': planning['context']['record_hash'], 'plan': request.model_dump()},
            headers={**headers, 'Idempotency-Key': 'harness-coding-v11-plan16-independent-binding'})
        response.raise_for_status(); plan = response.json(); save('plan.json', plan)
        assert plan['version_number'] == 16 and plan['id'] != old['plan']['id']
        assert plan['parent_version_id'] == old['plan']['id'] and plan['parent_record_hash'] == old['plan_record_hash']
        assert plan['content_hash'] == old['plan_content_hash'] and plan['record_hash'] != old['plan_record_hash']
        # Explicitly label mechanical preparation; it is not autonomous completion.
        marker = f'''import json
from app.database import Database
from app.config import Settings
from app.workbench import Workbench
from app.agent_runtime import mark_maintenance
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 wb=Workbench(session,s.artifact_root);binding=wb.binding({TASK!r},{plan['id']!r})
 mark_maintenance(wb,{TASK!r},binding,reason='coding_recovery_v11_plan16_user_authorized_independent_freeze_only')
 session.commit();print(json.dumps({{'binding_hash':binding.record_hash}}))
db.close()'''
        marker_result = subprocess.run(['docker', 'exec', container, 'python', '-B', '-c', marker], capture_output=True, text=True)
        assert marker_result.returncode == 0, 'maintenance marker failed; stop and reconcile, never rerun blindly'
        binding_hash = json.loads(marker_result.stdout)['binding_hash']
        current = view(client); after = readers.business(container); basis(after, current)
        changes = prior.check_preserved(before, after)
        assert [e['kind'] for e in after['history'][len(before['history']):]] == [
            'stop_requested', 'plan_bound', 'assistant_message', 'agent_maintenance_recorded']
        assert after['tables']['plan_versions']['count'] == 16
        assert after['latest_binding']['record_hash'] == binding_hash != old['plan_binding_hash']
        assert after['latest_binding']['payload']['agent_model_transport'] == native_model_transport().model_dump()
        assert current['execution']['model_transport_hash'] == content_hash(native_model_transport().model_dump())
        assert current['state'] == 'planning' and not current['busy'] and current['execution']['preflight']['status'] == 'ready'
        assert readers.history(container) == checked(ROOT / 'history-check.json')['Plan11_15']
        save('after-state.json', after); save('after-view.json', current)
    frozen = seal({'version': 'plan16-immutable-freeze-inputs-v1', 'git_HEAD': HEAD, 'source_commit': CODE,
        'branch': 'refactor/agent-runtime-v2', 'production_source_files': 255, 'production_source_hash': SOURCE,
        'source_files': checked(REVIEW / 'source-manifest.json')['files'], 'images': IMAGES,
        'image_digest_kind': 'local immutable Docker image configuration digest; no registry publication',
        'runner': RUNNER, 'sandbox_policy': asdict(SandboxPolicy()), 'sandbox_policy_hash': SandboxPolicy().policy_hash,
        'target': old['target'], 'upstream_check_hash': checked(ROOT / 'upstream-check.json')['report_hash'],
        'archive': checked(ROOT / 'model-and-archive-check.json'),
        'plan_id': plan['id'], 'plan_hash': plan['record_hash'], 'plan_content_hash': plan['content_hash'],
        'binding': after['latest_binding'], 'binding_hash': binding_hash,
        'parent_reference_only': 'Plan15 is an immutable lineage reference, never a reused approval, Binding or terminal',
        'write_paths': old['write_paths'], 'commands': old['commands'], 'acceptance_criteria': plan['acceptance_criteria'],
        'model_profiles': current['model_profiles'], 'model_identities': after['model_identities'],
        'model_binding_hash': current['model_binding_hash'], 'native_transport': native_model_transport().model_dump(),
        'native_transport_hash': current['execution']['model_transport_hash'], 'prompt': 'native-coding-loop-recovery-v11',
        'coding_recovery_policy': POLICY, 'original_coding_limits': Limits().model_dump(),
        'existing_call_bounds': old['existing_call_bounds'], 'review_bounds': old['review_bounds'],
        'environment': current['execution']['environment'], 'preflight': current['execution']['preflight'],
        'execution_revision': current['execution']['revision'], 'context_hash': current['context']['record_hash'],
        'planning_budget': current['execution']['planning_budget'],
        'acceptance_contract_hash': report_contract['record_hash']}, 'frozen_inputs_hash')
    save('frozen-inputs.json', frozen)
    budget = seal({'version': 'plan16-original-caps-fee-risk-v1', 'budget_allocated': False,
        'coding_limits': Limits().model_dump(), 'single_call': old['existing_call_bounds'], 'review': old['review_bounds'],
        'historical_planning_budget': current['execution']['planning_budget'],
        'conditional_opportunity': {'additional_capacity': 0, 'max_calls_from_original_cap': 1,
            'max_input_bound': 32000, 'max_output_tokens': 4096, 'minimum_remaining_output_to_dispatch': 128,
            'full_4096_headroom_requires_prior_output_at_most': 27904,
            'full_32000_headroom_requires_prior_input_at_most': 168000,
            'guard': 'Exact prompt must fit actual remaining input/calls/time; output is min(4096, remaining). All tool/evidence/read/write/check caps still apply.'},
        'feasibility': 'Original caps admit a bounded attempt, not a guaranteed successful ninth request or full pipeline. Insufficient capacity blocks before dispatch.',
        'worst_case_not_guaranteed': {'nine_full_output_calls': 36864, 'original_output_cap': 32000,
            'nine_max_input_prompts': 288000, 'original_input_cap': 200000},
        'high_reasoning_risk': 'Reasoning consumes the same 4096 output cap; finish_reason=length can yield zero final_text/tool_calls, as Plan15 did. No configuration experiment or capacity increase.',
        'unknown_fee_risk': 'Uncertain calls retain full reservations and block; do not reset/refund/retry the unknown operation.',
        'independent_review_cost': 'Separate real evidence-bound ProviderReview Job, at most 3 attempts, 300 seconds, 1800000 input bytes and profile output 16384 per attempt. It is additional billable usage outside Coding 24-call count. At most two authorized repairs may lead to fresh Review inputs; no unbounded retries.',
        'currency_amount': None, 'currency_reason': 'Frozen model configuration has no verified monetary tariff or currency hard cap. Token/byte/attempt ceilings are precise; currency cost is provider billing, not fabricated here.',
        'fee_formula': 'Coding actual_input_tokens * input_rate + actual_output_tokens * output_rate; add each independent Review actual input/output at its rates, plus any provider-billed failed/unknown work. Rates must be read in the same units. No guarantee of nonzero Patch or acceptance for these charges.',
        'historical_fees_and_holds': 'Plan11–15 and Planning64/64 unchanged; new budgets require separate consent, none allocated now',
        'preparation_paid_calls': 0, 'original_caps_increased': False})
    save('budget-and-risk.json', budget)
    body = ExecutePlanRequest(plan_id=plan['id'], plan_hash=plan['record_hash'], approve_plan=True,
        start_execution=True, max_repairs=2, model_binding_hash=current['model_binding_hash'],
        model_transport_hash=current['execution']['model_transport_hash'], conversation=True).model_dump()
    preview = seal({'version': 'plan16-independent-coding-recovery-v11-exact-preview-v1',
        'recorded_at': datetime.now(timezone.utc).isoformat(), 'status': 'UNAPPROVED_UNSENT_WAIT_FOR_SEPARATE_EXACT_CONSENT',
        'preparation_scope': 'read-only checks then independent Plan16/Binding and preview only',
        'prerequisite_report_hash': gate['report_hash'], 'frozen_inputs_hash': frozen['frozen_inputs_hash'],
        'frozen_inputs': frozen, 'plan': plan, 'plan_record_hash': plan['record_hash'],
        'plan_content_hash': plan['content_hash'], 'binding_hash': binding_hash,
        'execution_revision': current['execution']['revision'], 'preflight_hash': current['execution']['preflight']['report_hash'],
        'budget_risk_report_hash': budget['report_hash'], 'budget_and_risk': budget,
        'real_acceptance_contract': report_contract, 'real_results_initial': report_contract['initial_actual_results'],
        'sequence_after_separate_consent': ['Recheck every frozen input/revision/preflight and stop on change',
            'Fresh credential-free restricted OCI preparation; all four original Baseline commands pass',
            'Real Native Coding within the eight write paths; exact Patch with OCI receipts',
            'Latest modified source four Verify commands; at most two bounded repairs within original limits',
            'Independent real evidence-bound Review, report separate usage; stop for final human acceptance'],
        'go_no_go': {'freeze_preparation': 'GO', 'paid_execution_now': 'NO_GO; no exact execution consent',
            'future_dispatch': 'Requires new exact consent and unchanged frozen inputs plus runtime budget/safety gates',
            'HR4_HR5_P5_G06': 'open', 'Linux_amd64': 'not_run_open'},
        'offline_evidence': {'deployment_acceptance_hash': archived_acceptance['report_hash'],
            'same_image_focus': 50, 'same_image_recovery': 53, 'real_OCI': 28, 'pure_unit_OCI_suite': 2,
            'saved_envelopes': 7, 'recomputed_archived_certificate_hash': content_hash(recomputed),
            'capacity_bound': 228743, 'archived_remaining_bytes': 459508,
            'classification': 'prior offline acceptance and current pure proof recomputation; no fresh Coding/Review/OCI run'},
        'future_execution_request': {'method': 'POST', 'path': f'/api/v1/tasks/{TASK}/workbench/execute',
            'idempotency_key': 'harness-coding-v11-plan16-execute-once', 'body': body, 'sent': False},
        'only_appended_tables': changes, 'counts_before': before['counts'], 'counts_after': after['counts'],
        'old_rows_checkpoints_fees_approvals_unknown_holds': 'unchanged',
        'new_model_calls': 0, 'new_approvals': 0, 'new_jobs': 0, 'new_attempts': 0, 'new_ReviewRuns': 0,
        'execution_approved': False, 'execution_started': False, 'new_run_budget_allocated': False,
        'authorization_hash': None, 'attempt_hash': None, 'diff_hash': None, 'test_hash': None, 'review_hash': None,
        'excluded_actions': ['paid Planner', 'restore Plan15 or any old terminal', 'reuse old consent/approval/Binding',
            'increase budgets', 'configuration experiment', 'Push', 'PR', 'comments', 'publication'],
        'stop_after_delivery': True}, 'proposal_hash')
    save('execution-preview.json', preview)
    print(json.dumps({'Plan16': plan['id'], 'plan_hash': plan['record_hash'], 'binding_hash': binding_hash,
        'frozen_inputs_hash': frozen['frozen_inputs_hash'], 'preview_hash': preview['proposal_hash'],
        'execution_approved': False, 'paid_calls': 0, 'counts': after['counts']}))


if __name__ == '__main__':
    main()
