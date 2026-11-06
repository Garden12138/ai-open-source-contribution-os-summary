"""Prepare a separately authorized Plan binding; never approve, execute or invoke a model."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import httpx

from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import native_model_transport
from app.agent_v2.read_batches import POLICY
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_data
from app.sandbox_worker.specs import SandboxPolicy
from app.workbench_api import ExecutePlanRequest

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "missing-file-semantics-v1"
TASK = "f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"
INTENT = "5c20cae76a88bdc5f06cf2cb819cf63aa1355993512f6f0f74b30cda47999376"
STOP_KEY = "harness-missing-plan14-preparation-stop"
PLAN_KEY = "harness-missing-plan14-same-content-binding"

# Reuse the previously audited query_only business reader, never open the live
# database on macOS. This module has no import-time network or mutations.
module_spec = importlib.util.spec_from_file_location("prior_plan_preparation", ROOT.parent / "plan13-preparation/prepare_plan.py")
prior = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(prior)
docker_read, get_view, checked_json = prior.docker_read, prior.get_view, prior.checked_json


def save(name: str, value: dict) -> None:
    ensure_no_sensitive_data(value, context=name)
    with (ROOT / name).open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def local_checks() -> tuple[dict, dict, dict]:
    intent = checked_json(PRIOR / "real-model-acceptance-preview.json", "proposal_hash")
    assert intent["proposal_hash"] == INTENT
    assert subprocess.check_output(["git", "branch", "--show-current"], text=True).strip() == "refactor/agent-runtime-v2"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == intent["git_HEAD"]
    manifest = json.loads((PRIOR / "source-manifest.json").read_text())
    assert content_hash(manifest["files"]) == intent["runtime_source_hash"]
    assert set(manifest["files"]) == {str(p) for p in Path("app").rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    assert plan_content(PlanVersionCreateRequest.model_validate(intent["candidate_materialization_request_not_sent"])).hash_payload() == intent["candidate_plan_content"]
    assert content_hash(intent["candidate_plan_content"]) == intent["candidate_plan_content_hash"]
    assert intent["write_paths"] == intent["candidate_plan_content"]["files_likely_to_change"] and len(intent["write_paths"]) == 8
    assert intent["commands"] == [{k: c[k] for k in ("command_id", "argv", "working_directory")} for c in intent["candidate_plan_content"]["commands_to_run"]]
    assert len(intent["commands"]) == 4
    assert intent["additional_run_limits_pending_new_consent"] == Limits().model_dump()
    assert intent["native_transport"] == native_model_transport().model_dump()
    assert intent["native_transport_hash"] == content_hash(intent["native_transport"])
    assert intent["scheduler_policy"] == POLICY and intent["sandbox_policy_hash"] == SandboxPolicy().policy_hash
    validation = checked_json(PRIOR / "validation-report.json", "report_hash")
    assert validation["source_hash"] == intent["runtime_source_hash"]
    assert validation["full_regression"]["passed"] == 2025 and validation["full_regression"]["skipped"] == 77
    assert validation["actual_OCI"]["passed"] == 24 and validation["actual_OCI"]["skipped"] == 0
    plan12 = checked_json(ROOT.parent / "offline-replay.json", "report_hash")
    assert [r["child_sizes"] for r in plan12["replays"]] == [[4, 2], [4, 1], [4, 1]]
    assert plan12["model_provider_requests"] == 0
    reports = json.loads((PRIOR / "test-report-index.json").read_text())
    for report in reports["reports"]:
        path = PRIOR / report["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == report["sha256"]
        cases = ET.parse(path).getroot().findall(".//testcase")
        assert len(cases) == report["tests"]
        assert sum(c.find("failure") is not None for c in cases) == report["failures"]
        assert sum(c.find("error") is not None for c in cases) == report["errors"]
        assert sum(c.find("skipped") is not None for c in cases) == report["skipped"]
    final_cases = [c for r in validation["full_regression"]["reports"] for c in ET.parse(PRIOR / r["file"]).getroot().findall(".//testcase")]
    plan12_cases = [c for c in final_cases if c.attrib["name"].startswith("test_real_saved_envelopes_split_and_aggregate_original_order[")]
    assert len(plan12_cases) == 3 and all(len(c) == 0 for c in plan12_cases)
    gates = checked_json(ROOT / "new-file-gate-check.json", "report_hash")
    assert gates["stale_Verify_after_edit_rejected"] and gates["independent_Review_required_before_human_ready"]
    assert gates["paid_model_calls"] == 0
    replay = checked_json(PRIOR / "scheduler-replay.json", "report_hash")
    assert replay["next_model_visible_statuses"] == ["succeeded"] * 7 + ["not_found"]
    oci = checked_json(PRIOR / "frozen-oci-replay-final.json", "report_hash")
    assert [r["status"] for r in oci["ordered_results"]] == ["succeeded"] * 7 + ["not_found"]
    assert oci["provider_requests"] == 0 and oci["child_sizes"] == [4, 4]
    upstream = checked_json(ROOT / "upstream-check.json", "record_hash")
    assert upstream["HEAD"] == intent["target"]["base_sha"] and upstream["issue_body_matches"] and upstream["baseline_matches"]
    deployment = json.loads((PRIOR / "deployment-report.json").read_text())
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    assert roles["roles"] == deployment["new_roles"]
    for role, value in roles["roles"].items():
        info = json.loads(subprocess.check_output(["docker", "inspect", value["id"]]))[0]
        assert info["State"]["Running"]
        assert info["Image"] == intent["deployed_images"]["worker" if role == "sandbox-worker" else "app"]
        old = json.loads(subprocess.check_output(["docker", "inspect", deployment["old_roles_preserved_stopped"][role]]))[0]
        assert info["HostConfig"] == old["HostConfig"] and info["Mounts"] == old["Mounts"]
        assert all(info["Config"][k] == old["Config"][k] for k in ("Env", "User", "Entrypoint", "Cmd", "WorkingDir"))
        code = "import hashlib,json,sys;v=json.loads(sys.argv[1]);assert all(hashlib.sha256(open('/app/'+p,'rb').read()).hexdigest()==h for p,h in v.items())"
        subprocess.run(["docker", "exec", value["id"], "python", "-c", code, json.dumps(manifest["files"])], check=True)
    return intent, deployment, upstream


def basis_checks(intent: dict, state: dict, view: dict) -> None:
    assert not view["busy"] and state["counts"]["all_active_jobs"] == 0
    assert state["model_profiles"] == view["model_profiles"] == intent["model_profiles"]
    assert state["model_identities"] == intent["model_identities"]
    assert view["model_binding_hash"] == intent["model_binding_hash"]
    assert view["context"]["base_sha"] == intent["target"]["base_sha"]
    assert view["context"]["record_hash"] == "5f9b41facf3773266149daf5466d275792c6c23af665bcada4162121084fef7c"
    old = json.loads((ROOT.parent / "plan13-preparation/execution-preview.json").read_text())
    old_env, current_env = old["environment"], view["execution"]["environment"]
    for key in ("ecosystem", "mode", "preparation_version", "project_directory", "manifest_hashes", "node_environment", "capability_version", "ci_toolchains", "environment_engine"):
        assert current_env[key] == old_env[key], key
    contract = current_env["verification_contract"]
    assert contract["commands"] == old_env["verification_contract"]["commands"]
    for key in ("snapshot_hash", "source_archive_hash", "base_commit_sha", "approved_commands_hash", "target_platform", "runner_image_digest", "sandbox_policy_hash"):
        assert contract["binding"][key] == old_env["verification_contract"]["binding"][key], key
    assert contract["binding"]["runner_image_digest"] == state["runner_image"] == intent["runner_image_digest"]
    assert view["execution"]["agent_runtime_v2"]["checkpoint"] in state["checkpoints"].values()
    for key in ("record_hash", "limits", "usage", "remaining", "reserved", "unknown_calls"):
        assert view["execution"]["planning_budget"][key] == intent["planning_budget"][key], key


def check_preserved(before: dict, after: dict) -> dict:
    assert before["checkpoints"] == after["checkpoints"]
    assert after["history"][:len(before["history"])] == before["history"]
    changes = {}
    for name, table in before["tables"].items():
        current = after["tables"][name]
        assert not (Counter(table["row_hashes"]) - Counter(current["row_hashes"])), name
        if table != current:
            assert name in {"plan_versions", "contribution_task_state_versions", "workbench_events"}, name
            changes[name] = {"before_count": table["count"], "after_count": current["count"], "preexisting_rows_unchanged": True}
    for key in ("all_active_jobs", "active_jobs", "model_starts", "execution_authorizations", "plan_approvals", "artifacts"):
        assert before["counts"][key] == after["counts"][key], key
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialize", action="store_true")
    args = parser.parse_args()
    intent, deployment, upstream = local_checks()
    container = deployment["new_roles"]["serve"]["id"]
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        before, view = docker_read(container), get_view(client)
        basis_checks(intent, before, view)
        assert before["counts"] == intent["historical_counts"]
        assert before["latest_plan"]["id"] == intent["candidate_parent_reference"]["id"]
        assert before["latest_plan"]["record_hash"] == intent["candidate_parent_reference"]["record_hash"]
        assert before["latest_plan"]["version_number"] == 13
        assert before["latest_plan"]["content_hash"] == intent["candidate_plan_content_hash"]
        assert intent["old_Plan13_checkpoint"] in before["checkpoints"].values()
        assert view["state"] == "executing"
        if not args.materialize:
            value = {"version": "approved-missing-plan14-prerequisites-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
                "intent_hash": INTENT, "source_hash": intent["runtime_source_hash"], "upstream_hash": upstream["record_hash"],
                "all_four_conditions_passed": True, "full_regression_passed": 2025, "full_regression_skipped": 77,
                "real_OCI_passed": 24, "real_OCI_platform": "macOS arm64 / OrbStack", "Linux_amd64": "not_run_still_open",
                "offline_new_file_gate_hash": checked_json(ROOT / "new-file-gate-check.json", "report_hash")["report_hash"],
                "all_frozen_inputs_match": True, "counts": before["counts"], "plan_created": False, "model_calls": 0}
            value["report_hash"] = content_hash(value)
            save("prerequisite-check.json", value)
            print(json.dumps(value))
            return
        assert checked_json(ROOT / "prerequisite-check.json", "report_hash")["all_four_conditions_passed"]
        assert not (ROOT / "plan.json").exists() and not (ROOT / "before-state.json").exists()
        save("before-state.json", before)
        response = client.get("/api/v1/meta"); response.raise_for_status()
        headers = {"Origin": "http://127.0.0.1:18090", "X-CSRF-Token": response.json()["csrf_token"]}
        # Append a preparation-only state transition with no active jobs. No old
        # checkpoint, terminal state, approval, spending or reservation is edited.
        response = client.post(f"/api/v1/tasks/{TASK}/workbench/stop", json={}, headers={**headers, "Idempotency-Key": STOP_KEY})
        response.raise_for_status()
        planning = get_view(client)
        assert planning["state"] == "planning" and not planning["busy"] and planning["context"] == view["context"]
        response = client.post(f"/api/v1/tasks/{TASK}/workbench/plans",
            json={"context_hash": planning["context"]["record_hash"], "plan": intent["candidate_materialization_request_not_sent"]},
            headers={**headers, "Idempotency-Key": PLAN_KEY})
        response.raise_for_status()
        plan = response.json()
        save("plan.json", plan)
        assert plan["version_number"] == 14 and plan["parent_version_id"] == before["latest_plan"]["id"]
        assert plan["parent_record_hash"] == before["latest_plan"]["record_hash"]
        assert plan["content_hash"] == intent["candidate_plan_content_hash"]
        assert plan_content(PlanVersionCreateRequest.model_validate(plan)).hash_payload() == intent["candidate_plan_content"]
        marker = """import json
from app.database import Database
from app.config import Settings
from app.workbench import Workbench
from app.agent_runtime import mark_maintenance
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 wb=Workbench(session,s.artifact_root);binding=wb.binding(%r,%r)
 mark_maintenance(wb,%r,binding,reason='approved_missing_plan14_user_authorized_binding_only')
 session.commit();print(json.dumps({'binding_hash':binding.record_hash}))
db.close()
""" % (TASK, plan["id"], TASK)
        binding = json.loads(subprocess.check_output(["docker", "exec", container, "python", "-c", marker]))
        current, after = get_view(client), docker_read(container)
        basis_checks(intent, after, current)
        assert current["state"] == "planning" and not current["busy"]
        assert current["execution"]["model_transport"] == intent["native_transport"]
        assert current["execution"]["model_transport_hash"] == intent["native_transport_hash"]
        assert current["execution"]["preflight"]["status"] == "ready"
        check_preserved(before, after)
        assert after["latest_binding"]["record_hash"] == binding["binding_hash"]
        save("after-state.json", after)
        body = ExecutePlanRequest(plan_id=plan["id"], plan_hash=plan["record_hash"], approve_plan=True,
            start_execution=True, max_repairs=2, model_binding_hash=current["model_binding_hash"],
            model_transport_hash=current["execution"]["model_transport_hash"], conversation=True).model_dump()
        report_contract = checked_json(ROOT / "acceptance-report-contract.json", "record_hash")
        preview = {"version": "approved-missing-bound-execution-preview-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": "awaiting_new_exact_execution_and_budget_confirmation", "parent_intent_hash": INTENT,
            "preparation_authorization": "user_authorized_independent_plan_binding_and_preview_only_no_execution",
            "target": intent["target"], "baseline_source_archive_hash": intent["baseline_source_archive_hash"],
            "git_HEAD": intent["git_HEAD"], "worktree_uncommitted": True, "runtime_source_hash": intent["runtime_source_hash"],
            "runtime_source_files": intent["runtime_source_files"], "deployed_images": intent["deployed_images"],
            "plan": plan, "plan_content_hash": plan["content_hash"], "plan_record_hash": plan["record_hash"],
            "plan_binding_hash": binding["binding_hash"], "plan_binding": after["latest_binding"],
            "write_paths": intent["write_paths"], "commands": intent["commands"],
            "model_profiles": current["model_profiles"], "model_identities": after["model_identities"],
            "model_binding_hash": current["model_binding_hash"], "model_transport": current["execution"]["model_transport"],
            "model_transport_hash": current["execution"]["model_transport_hash"], "scheduler_policy": POLICY,
            "source_observation_version": intent["source_observation_version"], "preflight_version": intent["preflight_version"],
            "sandbox_policy_hash": intent["sandbox_policy_hash"], "runner_image_digest": after["runner_image"],
            "environment": current["execution"]["environment"], "preflight": current["execution"]["preflight"],
            "preflight_hash": current["execution"]["preflight"]["report_hash"], "execution_revision": current["execution"]["revision"],
            "context_hash": current["context"]["record_hash"], "planning_budget": current["execution"]["planning_budget"],
            "additional_run_limits_pending_consent": intent["additional_run_limits_pending_new_consent"], "review_bounds": intent["review_bounds"],
            "allowed_actions_only_after_new_execution_consent": intent["allowed_actions_only_after_new_exact_execution_consent"],
            "success_conditions": intent["success_conditions"], "coverage_condition": intent["coverage_condition"],
            "acceptance_report_contract_hash": report_contract["record_hash"], "acceptance_report_contract_file": "acceptance-report-contract.json",
            "execution_conditions": ["fresh one-time consent bound to this exact preview, actual Plan and Binding",
                "recheck upstream issue/base, source/images/model/transport/policy/scopes/budgets/revision/preflight before dispatch",
                "changed frozen inputs require a new preview and consent",
                "fresh credential-free OCI preparation and all four Baseline commands before native implementation",
                "all four latest-source Verify commands and independent evidence-bound Review before human final acceptance",
                "safe stop on failure, unknown result, stale input, security error or exhausted limits",
                "no paid Planner, old approval reuse, budget reset/refund, automatic extra paid round or terminal restart"],
            "future_execution_request": {"method": "POST", "path": f"/api/v1/tasks/{TASK}/workbench/execute",
                "idempotency_key": "harness-missing-plan14-execute-once", "body": body, "sent": False},
            "historical_checkpoints_unchanged": True, "historical_budget_limits_usage_and_holds_unchanged": True,
            "all_preexisting_database_rows_preserved": True, "counts_before": before["counts"], "counts_after": after["counts"],
            "new_model_calls": 0, "execution_approved": False, "execution_started": False,
            "new_execution_authorization_hash": "not_created_pending_new_human_confirmation", "new_attempt_id": "not_created",
            "new_diff_test_review_hashes": "not_created", "third_party_writes_approved": False, "Push_PR_comments": False,
            "Linux_amd64_gate": "not_run_still_open", "HR4_HR5": "not_passed"}
        preview["proposal_hash"] = content_hash(preview)
        save("execution-preview.json", preview)
        print(json.dumps({"plan_id": plan["id"], "version_number": 14, "plan_record_hash": plan["record_hash"],
            "binding_hash": binding["binding_hash"], "proposal_hash": preview["proposal_hash"], "new_model_calls": 0, "execution_started": False}))


if __name__ == "__main__":
    main()
