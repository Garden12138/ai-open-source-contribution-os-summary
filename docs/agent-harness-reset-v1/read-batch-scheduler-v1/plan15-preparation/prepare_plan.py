"""Append only the user-authorized Plan15/Binding and an UNSENT execution preview."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import subprocess

import httpx

from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import NATIVE_INPUT_LIMIT, NATIVE_PROMPT_VERSION, native_model_transport
from app.agent_v2.read_batches import POLICY
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.sandbox_worker.specs import SandboxPolicy
from app.workbench_api import ExecutePlanRequest
from check_prerequisites import (ROOT, CAP, TASK, HEAD, SOURCE, basis_checks, checked_json,
    check_preserved, deployment_checks, docker_read, get_view, local_checks, save)


def report_contract() -> dict:
    old = checked_json(ROOT.parent / "plan14-preparation/acceptance-report-contract.json", "record_hash")
    contract = {k: v for k, v in old.items() if k != "record_hash"}
    contract["version"] = "output-capacity-plan15-real-observation-contract-v1"
    contract["required_binding"] += ["prompt_projection_version", "capacity_contract", "native_transport_hash"]
    contract["required_sections"]["coding_observation_metrics"] = {
        "purpose": "Observe the new real run without adding calls, tokens, reads or acceptance retries.",
        "model_calls": "Count hash-bound model_started/request/response events for this Attempt by phase; distinguish started, completed, rejected, failed and unknown. Report independent Review invocation/attempt counts separately. Preserve actual usage and charged/reserved usage separately.",
        "effective_reads": "Per source_hash/file_content_hash, compute unique newly delivered UTF8 byte intervals from committed successful read receipts and the subsequent REAL request prompt. Report tool-observed and model-input-delivered bytes separately; not_found, reused bytes and fixture results do not count as new source bytes.",
        "repeated_reads": "Report requested/executed/reused duplicate ranges and overlapping bytes by source/content/range, including changed max_bytes returning identical bytes. Report same-offset requests separately. Never treat a changed max_bytes alone as progress.",
        "first_patch_round": "One-based real Coding request ordinal of first patch proposal and first successful OCI application, separately; include original response/call/receipt hashes and source before/after. No patch => null/not_observed, never a fixture ordinal.",
        "prompt_inline_source_coverage": "For EACH actual request, record full prompt artifact, native bound, per-file available/inline UTF8 byte intervals, truncation/next_offset and raw source references. Separate tool-returned bytes from visible inline bytes and unique cumulative delivered bytes. Check frozen Issue/Plan references remain recoverable; no assumed memory between requests.",
        "artifact_pagination_recovery": "Record issued source-page/Plan Artifact hash, requested offset/max_bytes, actual bytes and next_offset, receipt, next REAL model prompt inclusion, continuity and reconstructed SHA256. Distinguish raw page completion from full-file completion; file tails need authorized read_file. No recovery requested => not_observed.",
        "output_capacity_and_billing": "Retain original batch/proof/certificate hashes, v3 OCI binding, all call IDs/order, certificate upper bound and remaining bytes, exact final result/raw-page/metadata charges, dedup and unknown/not-dispatched prefix. Recompute charges from immutable bytes under unchanged 500000 ceiling.",
        "Verify": "All four exact approved commands on the latest modified source, with source/diff/test/contract hashes and real outcomes. Baseline, fixtures or an older source do not satisfy Verify.",
        "independent_Review": "Independent ReviewRun and invocation bound to this Attempt's latest diff/Verify/tests, actual reviewer identity, verdict/findings and usage; coder finish cannot substitute. Stop at Human Acceptance.",
        "missing_or_unavailable": "Use not_run/not_observed/unavailable with null values and reason; never infer success, hidden reasoning or buy extra coverage. All counters initially refer only to the unexecuted Plan15."}
    contract["initial_actual_results"]["coding_metrics"] = {
        "status": "not_run", "model_calls": 0, "effective_reads": None, "repeated_reads": None,
        "first_patch_proposal_round": None, "first_patch_application_round": None,
        "prompt_inline_source_coverage": [], "artifact_pagination_recovery": [],
        "Verify": "not_run", "independent_Review": "not_run"}
    contract["required_sections"]["budget_and_legacy"]["rule"] = (
        "All original per-run/Review caps retained. Plan11/12/13/14 histories, spending, approvals and holds immutable. "
        "Only fresh one-time consent can create authority for Plan15. No budget reset/refund/extra paid round. Push/PR/comments/publication forbidden.")
    contract["record_hash"] = content_hash(contract)
    return contract


def frozen_model_and_archive(container: str, old: dict) -> dict:
    code = '''import hashlib,json
from pathlib import Path
from sqlalchemy import text
from app.config import Settings
from app.database import Database
from app.model_settings import ModelSettingsService
from app.workbench import Workbench
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 session.execute(text('BEGIN'));session.execute(text('PRAGMA query_only=ON'))
 models=ModelSettingsService(session);profiles=models.task_profiles(%r,bind=False)
 values={}
 for stage in ('implementation','review'):
  p=models.profile(profiles[stage])
  values[stage]={k:p[k] for k in ('id','record_hash','model','max_tokens','timeout_seconds','temperature','reasoning_effort','structured_output')}
  values[stage]['connection_hash']=p['connection']['record_hash']
  values[stage]['identity']=models.identity(profiles[stage]).hash_payload()
 wb=Workbench(session,s.artifact_root);context=wb.latest(%r,'context_ready')
 aid=context.payload['archive_hash'];path=Path(s.artifact_root)/'repository-archives'/aid[:2]/aid
 assert path.is_file() and not path.is_symlink()
 raw=path.read_bytes();assert hashlib.sha256(raw).hexdigest()==aid
 print(json.dumps({'model_configuration':values,'archive_hash':aid,'archive_bytes':len(raw),'context_hash':context.record_hash}))
db.close()
''' % (TASK, TASK)
    value = json.loads(subprocess.check_output(["docker", "exec", container, "python", "-c", code]))
    assert value["archive_hash"] == old["baseline_source_archive_hash"] and value["context_hash"] == old["context_hash"]
    assert {k: v["identity"] for k, v in value["model_configuration"].items()} == old["model_identities"]
    return value


def main() -> None:
    assert not (ROOT / "plan.json").exists() and not (ROOT / "execution-preview.json").exists()
    check = checked_json(ROOT / "prerequisite-check.json", "report_hash")
    assert check["all_frozen_inputs_match"] and check["fresh_offline_regression"]["failed"] == 0
    assert local_checks()["native_transport_hash"] == check["native_transport_hash"]
    deployment = deployment_checks()
    container = deployment["new_roles"]["serve"]["id"]
    old = checked_json(ROOT.parent / "plan14-preparation/execution-preview.json", "proposal_hash")
    before = json.loads((ROOT / "before-state.json").read_text())
    assert docker_read(container) == before
    frozen = frozen_model_and_archive(container, old)
    save("model-and-archive-check.json", frozen)
    contract = report_contract(); save("acceptance-report-contract.json", contract)
    template = {"version": "output-capacity-plan15-acceptance-template-v1", "classification": "not_execution_evidence",
        "contract_hash": contract["record_hash"], **contract["initial_actual_results"]}
    template["record_hash"] = content_hash(template); save("acceptance-report-template.json", template)
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        view = get_view(client); basis_checks(before, view)
        assert view == json.loads((ROOT / "before-view.json").read_text())
        assert before["latest_plan"]["version_number"] == 14 and before["latest_plan"]["record_hash"] == old["plan_record_hash"]
        request = PlanVersionCreateRequest.model_validate({**old["plan"], "parent_version_id": old["plan"]["id"]})
        assert content_hash(plan_content(request).hash_payload()) == old["plan_content_hash"]
        r = client.get("/api/v1/meta"); r.raise_for_status()
        headers = {"Origin": "http://127.0.0.1:18090", "X-CSRF-Token": r.json()["csrf_token"]}
        # No active Jobs. This only appends user_replan; historical terminals are untouched.
        r = client.post(f"/api/v1/tasks/{TASK}/workbench/stop", json={},
            headers={**headers, "Idempotency-Key": "harness-capacity-plan15-preparation-stop"}); r.raise_for_status()
        planning = get_view(client)
        assert planning["state"] == "planning" and not planning["busy"] and planning["context"] == view["context"]
        # Only a mechanical Plan save: no /plan model endpoint, approval or /execute call.
        r = client.post(f"/api/v1/tasks/{TASK}/workbench/plans",
            json={"context_hash": planning["context"]["record_hash"], "plan": request.model_dump()},
            headers={**headers, "Idempotency-Key": "harness-capacity-plan15-same-content-binding"}); r.raise_for_status()
        plan = r.json(); save("plan.json", plan)
        assert plan["version_number"] == 15 and plan["parent_version_id"] == old["plan"]["id"]
        assert plan["parent_record_hash"] == old["plan_record_hash"] and plan["content_hash"] == old["plan_content_hash"]
        marker = '''import json
from app.database import Database
from app.config import Settings
from app.workbench import Workbench
from app.agent_runtime import mark_maintenance
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 wb=Workbench(session,s.artifact_root);binding=wb.binding(%r,%r)
 mark_maintenance(wb,%r,binding,reason='output_capacity_plan15_user_authorized_binding_only')
 session.commit();print(json.dumps({'binding_hash':binding.record_hash}))
db.close()
''' % (TASK, plan["id"], TASK)
        bound = json.loads(subprocess.check_output(["docker", "exec", container, "python", "-c", marker]))
        current, after = get_view(client), docker_read(container)
        basis_checks(after, current); changes = check_preserved(before, after)
        assert current["state"] == "planning" and not current["busy"]
        assert current["execution"]["model_transport"] == native_model_transport().model_dump()
        assert current["execution"]["model_transport_hash"] == check["native_transport_hash"]
        assert current["execution"]["preflight"]["status"] == "ready"
        assert after["latest_binding"]["record_hash"] == bound["binding_hash"] != old["plan_binding_hash"]
        assert [e["kind"] for e in after["history"][len(before["history"]):]] == [
            "stop_requested", "plan_bound", "assistant_message", "agent_maintenance_recorded"]
        assert after["tables"]["plan_versions"]["count"] == 15
        save("after-state.json", after); save("after-view.json", current)
        body = ExecutePlanRequest(plan_id=plan["id"], plan_hash=plan["record_hash"], approve_plan=True,
            start_execution=True, max_repairs=2, model_binding_hash=current["model_binding_hash"],
            model_transport_hash=current["execution"]["model_transport_hash"], conversation=True).model_dump()
        preview = {"version": "output-capacity-plan15-bound-preview-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": "awaiting_separate_new_exact_one_time_execution_consent", "preparation_authorization": "Plan15_binding_preview_only",
            "parent_preview_hash": old["proposal_hash"], "prerequisite_check_hash": check["report_hash"],
            "target": old["target"], "upstream_check": checked_json(ROOT / "upstream-check.json", "record_hash"),
            "baseline_source_archive_hash": old["baseline_source_archive_hash"], "baseline_archive_verified_bytes": frozen["archive_bytes"],
            "git_HEAD": HEAD, "branch": "refactor/agent-runtime-v2", "runtime_source_hash": SOURCE, "runtime_source_files": 254,
            "source_manifest_file": "../output-capacity-v1/source-manifest.json", "production_worktree_matches_HEAD": True,
            "deployed_images": deployment["images"], "runner_image_digest": after["runner_image"],
            "sandbox_policy": asdict(SandboxPolicy()), "sandbox_policy_hash": SandboxPolicy().policy_hash,
            "plan": plan, "plan_record_hash": plan["record_hash"], "plan_content_hash": plan["content_hash"],
            "plan_binding": after["latest_binding"], "plan_binding_hash": bound["binding_hash"],
            "write_paths": old["write_paths"], "commands": old["commands"],
            "model_profiles": current["model_profiles"], "model_identities": after["model_identities"],
            "model_configuration": frozen["model_configuration"], "model_binding_hash": current["model_binding_hash"],
            "model_transport": current["execution"]["model_transport"], "model_transport_hash": check["native_transport_hash"],
            "scheduler_policy": POLICY, "preflight_version": "native-read-batch-preflight-v3", "capacity_contract": "native-read-capacity-v1",
            "prompt_projection_version": NATIVE_PROMPT_VERSION, "native_input_limit": NATIVE_INPUT_LIMIT,
            "source_observation_version": "source-file-observation-v1", "environment": current["execution"]["environment"],
            "preflight": current["execution"]["preflight"], "preflight_hash": current["execution"]["preflight"]["report_hash"],
            "execution_revision": current["execution"]["revision"], "context_hash": current["context"]["record_hash"],
            "planning_budget": current["execution"]["planning_budget"],
            "additional_run_limits_pending_consent": Limits().model_dump(), "review_bounds": old["review_bounds"],
            "existing_call_bounds": {"native_model_output_tokens_per_call": 4096, "native_step_max_job_attempts": 1,
                "native_step_timeout_seconds": 300, "native_input_bound": 32000, "page_max_bytes": 16000,
                "original_read_batch_max_calls": 8, "sub_batch_and_Decision_max_calls": 4,
                "bounded_failure_envelope_bytes": 8192, "note": "Existing enforced ceilings, no new allowance; subject to remaining per-run budgets."},
            "allowed_actions_only_after_new_execution_consent": old["allowed_actions_only_after_new_execution_consent"],
            "execution_conditions": ["Fresh one-time consent bound to this preview/Plan/Binding, model, images, source, Issue/base, paths, commands and all caps",
                "Immediately recheck every frozen input, execution revision and preflight; changes stop before dispatch",
                "Fresh credential-free bounded OCI preparation and all four Baseline commands before Coding",
                "Latest modified source all-four Verify and independent evidence-bound Review, then stop at Human Acceptance",
                "No paid Planner, old terminal restart/approval reuse, refunds, cap increases or automatic extra paid run",
                "Fail closed on stale hashes, security/unknown results or exhausted budgets; preserve real spending and receipts"],
            "success_conditions": old["success_conditions"], "coverage_condition": old["coverage_condition"],
            "offline_capacity_and_projection_prerequisites": {k: check[k] for k in (
                "OCI_report_hash", "OCI_binding_hash", "capacity_certificate_hash", "capacity_bytes_bound", "capacity_remaining_bytes",
                "source_page_bytes", "source_coverage_report_hash", "native_input_bound", "fresh_offline_regression")},
            "offline_metrics_are_not_real_Coding_results": True,
            "acceptance_report_contract_hash": contract["record_hash"], "acceptance_report_contract_file": "acceptance-report-contract.json",
            "real_Coding_observation_metrics": contract["required_sections"]["coding_observation_metrics"],
            "real_results_initial": contract["initial_actual_results"],
            "future_execution_request": {"method": "POST", "path": f"/api/v1/tasks/{TASK}/workbench/execute",
                "idempotency_key": "harness-capacity-plan15-execute-once", "body": body, "sent": False},
            "only_appended_tables": changes, "counts_before": before["counts"], "counts_after": after["counts"],
            "all_old_rows_checkpoints_approvals_usage_and_holds_unchanged": True,
            "new_model_calls": 0, "execution_approved": False, "execution_started": False, "new_run_budget_allocated": False,
            "new_execution_authorization_hash": "not_created", "new_attempt_id": "not_created",
            "new_diff_test_review_hashes": "not_created", "third_party_writes_approved": False,
            "Push_PR_comments": False, "Linux_amd64_gate": "not_run_still_open", "HR4_HR5": "not_passed"}
        preview["proposal_hash"] = content_hash(preview); save("execution-preview.json", preview)
        print(json.dumps({"plan_id": plan["id"], "plan_hash": plan["record_hash"], "binding_hash": bound["binding_hash"],
            "preview_hash": preview["proposal_hash"], "counts": after["counts"], "execution_approved": False, "new_model_calls": 0}))


if __name__ == "__main__":
    main()
