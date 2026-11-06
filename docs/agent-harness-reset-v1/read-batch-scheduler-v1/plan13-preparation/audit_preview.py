"""Read-only verification of the materialized plan and an unsent exact preview."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json

import httpx

from prepare_plan import ROOT, TASK, basis_checks, checked_json, docker_read, get_view, local_checks, save
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_data
from app.workbench_api import ExecutePlanRequest


def main() -> None:
    intent, deployment, upstream = local_checks()
    preview = checked_json(ROOT / "execution-preview.json", "proposal_hash")
    before = json.loads((ROOT / "before-state.json").read_text())
    saved_after = json.loads((ROOT / "after-state.json").read_text())
    plan = json.loads((ROOT / "plan.json").read_text())
    assert preview["plan"] == plan
    assert plan_content(PlanVersionCreateRequest.model_validate(plan)).hash_payload() == intent["candidate_plan_content"]
    assert plan["content_hash"] == preview["plan_content_hash"] == intent["candidate_plan_content_hash"]
    assert plan["record_hash"] == preview["plan_record_hash"]
    assert plan["parent_version_id"] == intent["candidate_parent_reference"]["id"]
    assert plan["parent_record_hash"] == intent["candidate_parent_reference"]["record_hash"]
    assert plan["version_number"] == 13
    request = ExecutePlanRequest.model_validate(preview["future_execution_request"]["body"])
    assert request.plan_id == plan["id"] and request.plan_hash == plan["record_hash"]
    assert request.model_binding_hash == intent["model_binding_hash"]
    assert request.model_transport_hash == intent["native_transport_hash"]
    assert preview["future_execution_request"]["sent"] is False
    assert preview["additional_run_limits_pending_consent"] == intent["candidate_additional_run_limits"]
    assert preview["review_bounds"] == intent["review_bounds"]
    assert preview["write_paths"] == intent["write_paths"] and preview["commands"] == intent["commands"]
    assert not preview["execution_approved"] and not preview["execution_started"]
    assert not preview["third_party_writes_approved"] and preview["new_model_calls"] == 0
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        current = get_view(client)
    live = docker_read(deployment["new_roles"]["serve"]["id"])
    basis_checks(intent, live, current)
    assert current["state"] == "planning" and not current["busy"]
    assert live == saved_after
    assert current["context"]["record_hash"] == preview["context_hash"]
    assert current["execution"]["revision"] == preview["execution_revision"]
    assert current["execution"]["preflight"] == preview["preflight"]
    assert current["execution"]["environment"] == preview["environment"]
    assert current["execution"]["model_transport_hash"] == intent["native_transport_hash"]
    assert current["execution"]["preflight"]["status"] == "ready"
    assert current["execution"]["environment"]["readiness"] == {
        "static_admitted": True, "toolchain_prepared": "unverified", "baseline_verified": "unverified"}
    assert live["latest_plan"]["id"] == plan["id"]
    assert live["latest_plan"]["record_hash"] == preview["plan_record_hash"]
    assert live["latest_binding"] == preview["plan_binding"]
    assert live["latest_binding"]["record_hash"] == preview["plan_binding_hash"]
    assert before["checkpoints"] == live["checkpoints"]
    assert live["history"][:len(before["history"])] == before["history"]
    assert [e["kind"] for e in live["history"][len(before["history"]):]] == [
        "stop_requested", "plan_bound", "assistant_message", "agent_maintenance_recorded"]
    changes = {}
    for name, table in before["tables"].items():
        after = live["tables"][name]
        assert not (Counter(table["row_hashes"]) - Counter(after["row_hashes"])), name
        if table != after:
            assert name in {"plan_versions", "contribution_task_state_versions", "workbench_events"}, name
            changes[name] = {"before_count": table["count"], "after_count": after["count"],
                "preexisting_rows_unchanged": True, "before_hash": table["hash"], "after_hash": after["hash"]}
    assert live["counts"] == preview["counts_after"]
    for name in ("jobs", "plan_approvals", "plan_locks", "execution_attempts", "execution_stage_versions",
                 "execution_stage_runs", "coding_sessions", "coding_turns", "provider_invocations", "model_config_versions"):
        assert before["tables"][name] == live["tables"][name], name
    for path in ROOT.glob("*.json"):
        ensure_no_sensitive_data(json.loads(path.read_text()), context=path.name)
    value = {"version": "native-read-batch-bound-preview-audit-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "intent_hash": intent["proposal_hash"], "proposal_hash": preview["proposal_hash"],
        "plan_id": plan["id"], "plan_record_hash": plan["record_hash"], "plan_content_hash": plan["content_hash"],
        "plan_binding_hash": preview["plan_binding_hash"], "upstream_record_hash": upstream["record_hash"],
        "read_only_audit": True, "source_and_five_deployed_roles_verified": True,
        "plan_and_binding_verified_with_domain_readers": True, "frozen_scopes_models_budgets_policy_unchanged": True,
        "live_execution_revision_and_preflight_match": True, "future_request_schema_validated_and_unsent": True,
        "historical_checkpoints_hashes": {sid: content_hash(cp) for sid, cp in live["checkpoints"].items()},
        "historical_checkpoints_unchanged": True, "all_preexisting_database_rows_unchanged": True,
        "only_appended_tables": changes, "counts": live["counts"], "artifact_hash_and_credentials_verified": live["artifacts_hash_and_credentials_verified"],
        "integrity_check": "ok", "foreign_key_violations": 0, "audit_chain_valid": True,
        "credential_scan": "passed", "model_calls_by_preparation": 0, "new_execution_authorizations": 0,
        "new_execution_attempts": 0, "new_jobs": 0, "new_business_patch": False,
        "execution_approved": False, "new_run_budget_allocated": False,
        "toolchain_preparation_and_baseline": "not_run_for_Plan13",
        "Coding_Verify_Review": "not_run_for_Plan13", "third_party_writes": 0,
        "Push_PR_comments": False, "Linux_amd64": "not_run_still_open", "HR4_HR5": "not_passed"}
    value["report_hash"] = content_hash(value)
    save("preview-integrity-audit.json", value)
    print(json.dumps({"plan_id": plan["id"], "proposal_hash": preview["proposal_hash"],
        "report_hash": value["report_hash"], "counts": live["counts"], "readonly_checks": "passed"}))


if __name__ == "__main__":
    main()
