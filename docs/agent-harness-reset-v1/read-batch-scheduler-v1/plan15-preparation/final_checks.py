"""Audit the final UNSENT preview and real local records without executing it."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx

from app.agent_v2.contracts import Limits, Page
from app.agent_v2.native_tools import native_model_transport
from app.agent_v2.read_capacity import FAILURE_BYTES
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_bytes, ensure_no_sensitive_data
from app.workbench_api import ExecutePlanRequest
from check_prerequisites import (ROOT, CAP, TASK, HEAD, SOURCE, basis_checks, checked_json,
    check_preserved, deployment_checks, docker_read, get_view, local_checks, save)
from prepare_plan import frozen_model_and_archive


def main() -> None:
    preview = checked_json(ROOT / "execution-preview.json", "proposal_hash")
    initial = checked_json(ROOT / "initial-preview.json", "proposal_hash")
    assert preview["preparation_preview_supersedes"] == initial["proposal_hash"]
    assert {k: v for k, v in preview.items() if k not in {"proposal_hash", "preparation_preview_supersedes", "existing_call_bounds"}} == {
        k: v for k, v in initial.items() if k != "proposal_hash"}
    local = local_checks(); deployment = deployment_checks()
    before = json.loads((ROOT / "before-state.json").read_text())
    after = docker_read(deployment["new_roles"]["serve"]["id"])
    assert after == json.loads((ROOT / "after-state.json").read_text())
    assert before == json.loads((CAP / "legacy-after.json").read_text())
    changes = check_preserved(before, after)
    assert changes == preview["only_appended_tables"]
    assert after["checkpoints"] == before["checkpoints"] and len(after["checkpoints"]) == 10
    assert after["latest_plan"]["id"] == preview["plan"]["id"]
    assert after["latest_plan"]["record_hash"] == preview["plan_record_hash"]
    assert after["latest_binding"] == preview["plan_binding"]
    assert after["latest_binding"]["record_hash"] == preview["plan_binding_hash"]
    assert after["latest_binding"]["payload"]["agent_model_transport"] == native_model_transport().model_dump()
    assert plan_content(PlanVersionCreateRequest.model_validate(preview["plan"])).hash_payload() == plan_content(
        PlanVersionCreateRequest.model_validate(json.loads((ROOT.parent / "plan14-preparation/plan.json").read_text()))).hash_payload()
    assert preview["plan_content_hash"] == after["latest_plan"]["content_hash"]
    assert preview["plan"]["version_number"] == 15 and after["tables"]["plan_versions"]["count"] == 15
    assert preview["runtime_source_hash"] == SOURCE and preview["git_HEAD"] == HEAD
    assert preview["model_transport_hash"] == local["native_transport_hash"]
    assert preview["additional_run_limits_pending_consent"] == Limits().model_dump()
    assert Page.model_json_schema()["properties"]["max_bytes"]["maximum"] == preview["existing_call_bounds"]["page_max_bytes"] == 16000
    assert preview["existing_call_bounds"]["bounded_failure_envelope_bytes"] == FAILURE_BYTES == 8192
    assert preview["existing_call_bounds"]["native_model_output_tokens_per_call"] == 4096
    contract = checked_json(ROOT / "acceptance-report-contract.json", "record_hash")
    template = checked_json(ROOT / "acceptance-report-template.json", "record_hash")
    assert contract["record_hash"] == preview["acceptance_report_contract_hash"] == template["contract_hash"]
    assert preview["real_Coding_observation_metrics"] == contract["required_sections"]["coding_observation_metrics"]
    assert preview["real_results_initial"] == contract["initial_actual_results"]
    request = ExecutePlanRequest.model_validate(preview["future_execution_request"]["body"])
    assert request.plan_id == preview["plan"]["id"] and request.plan_hash == preview["plan_record_hash"]
    assert request.model_transport_hash == local["native_transport_hash"] and request.model_binding_hash == preview["model_binding_hash"]
    assert not preview["future_execution_request"]["sent"] and not preview["execution_approved"] and not preview["execution_started"]
    assert not preview["new_run_budget_allocated"] and preview["new_model_calls"] == 0
    assert preview["new_execution_authorization_hash"] == preview["new_attempt_id"] == "not_created"
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        view = get_view(client); basis_checks(after, view)
        assert view == json.loads((ROOT / "after-view.json").read_text())
        assert view["state"] == "planning" and not view["busy"]
        for key, frozen_key in (("revision", "execution_revision"), ("preflight", "preflight"),
            ("environment", "environment"), ("planning_budget", "planning_budget")):
            assert view["execution"][key] == preview[frozen_key]
        assert view["execution"]["environment"]["readiness"] == {
            "static_admitted": True, "toolchain_prepared": "unverified", "baseline_verified": "unverified"}
        health = {}
        for port in (18090, 18091, 18092, 18080):
            response = client.get(f"http://127.0.0.1:{port}/health"); response.raise_for_status()
            health[str(port)] = response.json(); assert response.json()["status"] == "ok"
    assert frozen_model_and_archive(deployment["new_roles"]["serve"]["id"], preview) == json.loads((ROOT / "model-and-archive-check.json").read_text())
    for name in ("jobs", "execution_attempts", "execution_stage_versions", "execution_stage_runs", "plan_approvals",
        "plan_locks", "provider_invocations", "review_runs", "job_model_bindings", "model_config_versions", "coding_sessions"):
        assert before["tables"][name] == after["tables"][name], name
    assert after["counts"] == preview["counts_after"] == {
        "active_jobs": 0, "all_active_jobs": 0, "model_starts": 91, "execution_authorizations": 12,
        "plan_approvals": 12, "task_events": 2094, "artifacts": 1640}
    # Values from existing environments remain in memory for exact-match scans.
    containers = [item["id"] for item in deployment["new_roles"].values()]
    for port in ("18091", "18092"):
        containers.extend(subprocess.check_output(["docker", "ps", "-q", "--filter", "publish=" + port], text=True).split())
    containers = list(dict.fromkeys(containers))
    values = dict(os.environ)
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1); values[k.strip()] = v.strip().strip("\"'")
    for container in containers:
        info = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
        for item in info["Config"]["Env"]:
            if "=" in item:
                k, v = item.split("=", 1); values[k] = v
    credentials = list({v for k, v in values.items() if len(v) >= 16
        and any(x in k.upper() for x in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "SIGNING_KEY"))
        and not k.endswith(("_FILE", "_PATH", "_ENV_NAME"))})
    files = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts: continue
        raw = path.read_bytes()
        ensure_no_sensitive_bytes(raw, secrets=credentials, context=path.name)
        assert str(Path.home()).encode() not in raw, path.name
        files.append({"file": path.name, "sha256": hashlib.sha256(raw).hexdigest()})
        whitespace = subprocess.run(["git", "diff", "--no-index", "--check", "--", "/dev/null", str(path)], capture_output=True)
        assert not whitespace.stdout and not whitespace.stderr, path.name
    for container in containers:
        logs = subprocess.run(["docker", "logs", "--tail", "2000", container], capture_output=True, check=True)
        ensure_no_sensitive_bytes(logs.stdout + logs.stderr, secrets=credentials, context="owned service logs")
    for path in (ROOT / "confirmation-text.txt", ROOT / "execution-preview.md"):
        assert preview["proposal_hash"] in path.read_text() and preview["plan_binding_hash"] in path.read_text()
    delivery = json.loads((CAP / "git-delivery-check.json").read_text())
    for item in delivery["excluded_untracked_preserved"]:
        assert hashlib.sha256(Path(item["file"]).read_bytes()).hexdigest() == item["sha256"], item["file"]
    assert not subprocess.check_output(["git", "diff", "--cached", "--name-only"])
    assert not subprocess.check_output(["git", "diff", "HEAD", "--", "app", "tests"])
    subprocess.run([".venv/bin/python", "-m", "compileall", "-q", "app", str(ROOT)], check=True)
    subprocess.run(["git", "diff", "--check"], check=True)
    result = {"version": "output-capacity-plan15-final-audit-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "classification": "query_only_domain_and_hash_audit_no_real_execution", "proposal_hash": preview["proposal_hash"],
        "plan_id": preview["plan"]["id"], "plan_record_hash": preview["plan_record_hash"], "plan_binding_hash": preview["plan_binding_hash"],
        "plan_and_binding_verified_with_domain_readers": True, "all_frozen_inputs_match": True,
        "only_appended_tables": changes, "all_old_rows_and_10_checkpoints_unchanged": True,
        "all_1640_artifact_hashes_and_credentials_verified": True, "all_2090_old_task_events_preserved": True,
        "all_old_approvals_usage_and_unknown_holds_unchanged": True, "counts": after["counts"],
        "runtime_source_hash": SOURCE, "production_source_254_matches_HEAD_and_5_deployed_roles": True,
        "fresh_offline_regression": 59, "archived_OCI_proof_recomputed": True, "all_archived_JUnit_hashes_and_outcomes_verified": True,
        "model_configuration_and_actual_baseline_archive_hash_verified": True,
        "final_execution_revision_preflight_environment_and_budget_match": True,
        "confirmation_text_and_unsent_request_bound_to_actual_ids": True,
        "new_model_calls": 0, "new_execution_authorizations": 0, "new_plan_approvals": 0,
        "new_jobs": 0, "new_attempts": 0, "new_ReviewRuns": 0, "new_run_budget_allocated": False,
        "execution_request_sent": False, "health": health, "services_redeployed": False,
        "redeployment_reason": "No production source/configuration change; this task authorizes read-only verification and Plan/Binding preparation only",
        "credential_canary_and_exact_configured_values_scan": "passed", "owned_container_logs_scanned": len(containers),
        "credential_values_checked_count": len(credentials), "files": files,
        "compileall_app_and_preparation": "passed", "git_diff_check": "passed", "unrelated_untracked_files_preserved": True,
        "git_index_and_HEAD_unchanged": True, "Push_PR_comments": False, "HR4_HR5": "not_passed", "Linux_amd64": "not_run_still_open",
        "next_action": "Stop; await separate explicit exact one-time consent before any paid execution"}
    ensure_no_sensitive_data(result, context="Plan15 final audit")
    result["report_hash"] = content_hash(result); save("final-checks.json", result)
    print(json.dumps({"audit": "passed", "report_hash": result["report_hash"], "proposal_hash": preview["proposal_hash"],
        "counts": after["counts"], "health": health, "new_paid_model_calls": 0, "execution_approved": False}))


if __name__ == "__main__":
    main()
