"""Dispatch the user's exact Plan14 consent once, with durable reconciliation intent."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parent
PREP = ROOT.parent / "plan14-preparation"
sys.path.insert(0, str(PREP))
from prepare_plan import basis_checks, checked_json, docker_read, get_view, local_checks, TASK
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.workbench_api import ExecutePlanRequest

APPROVED = "3825f2d60954527af8276556fdff71235a185554177a3d5159da2aed06769250"


def write_once(name: str, value: dict) -> None:
    ensure_no_sensitive_data(value, context="Plan14 execution receipt")
    with (ROOT / name).open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        target.flush()
        os.fsync(target.fileno())


def main() -> None:
    assert not any((ROOT / name).exists() for name in ("approval.json", "dispatch-intent.json", "dispatch-receipt.json")), (
        "Consent or dispatch already recorded: read-only reconciliation only; never POST another execution")
    intent, deployment, _ = local_checks()
    preview = checked_json(PREP / "execution-preview.json", "proposal_hash")
    assert preview["proposal_hash"] == APPROVED
    assert preview["plan"]["id"] == "e8503bc5-9b61-4e51-95c6-c4eb33bb919e"
    assert preview["plan_record_hash"] == "1006e34393baf6a239c3f4e001af5726baaf3c69e00065a25d5934531b693d06"
    assert preview["plan_binding_hash"] == "6222c965647cdf54e039dad1c41544cdd5dd54245e0b59c6ee528bbd584c36bf"
    assert preview["additional_run_limits_pending_consent"] == intent["additional_run_limits_pending_new_consent"]
    upstream = checked_json(ROOT / "upstream-before-execution.json", "record_hash")
    previous_upstream = checked_json(PREP / "upstream-check.json", "record_hash")
    for key in ("issue_url", "state", "title", "updated_at", "issue_body_hash", "branch", "HEAD"):
        assert upstream[key] == previous_upstream[key], key
    assert (datetime.now(timezone.utc) - datetime.fromisoformat(upstream["recorded_at"])).total_seconds() < 1200
    container = deployment["new_roles"]["serve"]["id"]
    before = docker_read(container)
    assert before == json.loads((PREP / "after-state.json").read_text()), "Live frozen data changed; stop before dispatch"
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False, follow_redirects=False) as client:
        view = get_view(client)
        basis_checks(intent, before, view)
        execution = view["execution"]
        assert view["state"] == "planning" and not view["busy"]
        assert before["latest_plan"]["id"] == preview["plan"]["id"]
        assert before["latest_plan"]["record_hash"] == preview["plan_record_hash"]
        assert before["latest_binding"] == preview["plan_binding"]
        assert execution["revision"] == preview["execution_revision"]
        assert execution["environment"] == preview["environment"]
        assert execution["preflight"] == preview["preflight"] and execution["preflight"]["status"] == "ready"
        assert execution["planning_budget"] == preview["planning_budget"]
        assert execution["model_transport_hash"] == preview["model_transport_hash"]
        request = preview["future_execution_request"]
        body = ExecutePlanRequest.model_validate(request["body"]).model_dump()
        assert request["path"] == f"/api/v1/tasks/{TASK}/workbench/execute" and request["method"] == "POST"
        assert request["idempotency_key"] == "harness-missing-plan14-execute-once" and request["sent"] is False
        write_once("before-dispatch-state.json", before)
        approval = {"version": "approved-missing-plan14-human-execution-consent-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "proposal_hash": APPROVED,
            "actor": "human-user", "source": "explicit_current_user_message",
            "scope": "one_additional_bounded_real_acceptance_and_independent_review_only",
            "plan_id": preview["plan"]["id"], "plan_hash": preview["plan_record_hash"],
            "binding_hash": preview["plan_binding_hash"], "model_identities": preview["model_identities"],
            "model_binding_hash": preview["model_binding_hash"], "model_transport_hash": preview["model_transport_hash"],
            "new_run_limits": preview["additional_run_limits_pending_consent"], "review_bounds": preview["review_bounds"],
            "write_paths": preview["write_paths"], "commands": preview["commands"],
            "allowed_actions": preview["allowed_actions_only_after_new_execution_consent"],
            "stop_at": "human_final_acceptance_or_existing_failure_or_budget_boundary",
            "subsequent_paid_rounds_approved": False, "old_authority_or_budget_reuse": False,
            "Push_PR_comments_publication_approved": False, "upstream_hash": upstream["record_hash"],
            "runtime_source_hash": preview["runtime_source_hash"], "execution_revision": execution["revision"]}
        approval["approval_hash"] = content_hash(approval)
        write_once("approval.json", approval)
        dispatch = {"version": "approved-missing-plan14-dispatch-intent-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "proposal_hash": APPROVED,
            "approval_hash": approval["approval_hash"], "task_id": TASK, "plan_id": preview["plan"]["id"],
            "idempotency_key": request["idempotency_key"], "body": body,
            "history_start_sequence": len(before["history"]),
            "status": "submission_may_be_in_flight_reconcile_without_resubmission",
            "automatic_retry": False, "third_party_writes": 0}
        dispatch["intent_hash"] = content_hash(dispatch)
        write_once("dispatch-intent.json", dispatch)
        response = client.get("/api/v1/meta")
        response.raise_for_status()
        headers = {"Origin": "http://127.0.0.1:18090", "X-CSRF-Token": response.json()["csrf_token"],
            "Idempotency-Key": request["idempotency_key"]}
        # Exactly one POST. A timeout/error requires read-only reconciliation,
        # not another request, root allocation or automatic paid retry.
        response = client.post(request["path"], json=body, headers=headers)
        if response.status_code not in (200, 202):
            raise RuntimeError(f"Execute HTTP {response.status_code}: reconcile without automatic resubmission")
        job = response.json()
        receipt = {"version": "approved-missing-plan14-dispatch-receipt-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "proposal_hash": APPROVED,
            "approval_hash": approval["approval_hash"], "intent_hash": dispatch["intent_hash"],
            "job": {k: job[k] for k in ("id", "kind", "state")}, "HTTP_status": response.status_code,
            "single_execution_POST": True, "automatic_extra_run": False, "third_party_writes": 0}
        receipt["receipt_hash"] = content_hash(receipt)
        write_once("dispatch-receipt.json", receipt)
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()
