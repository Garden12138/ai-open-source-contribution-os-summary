"""Consume the user's exact Plan12 consent once; reconcile any ambiguous result."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.workbench_api import ExecutePlanRequest

RECORDS = Path("docs/agent-harness-reset-v1")
APPROVED = "81e1d70a6cc7433461cc5cee1ea8ba7abb93c859236d6392e2870beea772420f"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_once(path: Path, value: dict) -> None:
    ensure_no_sensitive_data(value, context="exact Harness consent receipt")
    with path.open("x") as out:
        out.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        out.flush()
        os.fsync(out.fileno())


def main() -> None:
    receipt_path = RECORDS / "real-acceptance-execution.json"
    approval_path = RECORDS / "real-acceptance-approval.json"
    assert not receipt_path.exists() and not approval_path.exists(), "Reconcile existing consent/action; never allocate a second root"
    preview = read_json(RECORDS / "real-acceptance-preview.json")
    assert preview["proposal_hash"] == APPROVED
    assert content_hash({k: v for k, v in preview.items() if k != "proposal_hash"}) == APPROVED
    manifest = read_json(RECORDS / "source-manifest-r2.json")
    deployment = read_json(RECORDS / "deployment-report.json")
    assert content_hash(deployment) == preview["deployment_hash"]
    assert manifest["source_hash"] == deployment["source_hash"] == preview["source_manifest_hash"]
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    assert subprocess.check_output(["git", "branch", "--show-current"], text=True).strip() == "refactor/agent-runtime-v2"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == manifest["base_HEAD"]
    upstream = read_json(RECORDS / "upstream-before-execution.json")
    original = read_json(RECORDS / "upstream-observation.json")
    assert upstream["state"] == "open" and upstream["HEAD"] == upstream["baseline_exists"] == preview["base_sha"]
    assert upstream["issue_body_hash"] == original["issue_body_hash"] and upstream["issue_title"] == original["issue_title"]
    assert (datetime.now(timezone.utc) - datetime.fromisoformat(upstream["recorded_at"])).total_seconds() < 1200
    state = read_json(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json"))
    assert state["roles"] == deployment["new_roles"] and state["source_manifest_hash"] == manifest["source_hash"]
    runtime_checks = {}
    for role, item in state["roles"].items():
        current = json.loads(subprocess.check_output(["docker", "inspect", item["id"]]))[0]
        old = json.loads(subprocess.check_output(["docker", "inspect", deployment["old_roles_preserved_stopped"][role]]))[0]
        assert current["State"]["Running"] and not old["State"]["Running"]
        assert current["Image"] == deployment["images"]["worker" if role == "sandbox-worker" else "app"]
        assert current["Config"]["Labels"]["contribos.owner"] == "upeve-acceptance"
        assert current["HostConfig"] == old["HostConfig"]
        for key in ("Env", "User", "Entrypoint", "Cmd", "WorkingDir"):
            assert current["Config"][key] == old["Config"][key]
        assert current["Mounts"] == old["Mounts"]
        code = "import json,hashlib,sys;files=json.loads(sys.argv[1]);assert all(hashlib.sha256(open('/app/'+p,'rb').read()).hexdigest()==h for p,h in files.items());print(len(files))"
        count = int(subprocess.check_output(["docker", "exec", item["id"], "python", "-c", code, json.dumps(manifest["files"])]))
        runtime_checks[role] = {"id": item["id"], "source_files_verified": count, "unchanged_environment_security_resources": True}
    api = state["roles"]["serve"]["id"]
    code = """import sqlite3,json,sys
with sqlite3.connect('file:/data/state.db?mode=ro',uri=True) as db:
 print(json.dumps({'active':db.execute("select count(*) from jobs where state in ('queued','leased','running')").fetchone()[0],
 'history':[{'id':i,'record_hash':h} for i,h in db.execute('select id,record_hash from workbench_events where task_id=? order by sequence',(sys.argv[1],))],
 'authorizations':db.execute("select count(*) from workbench_events where task_id=? and kind='execution_authorized'",(sys.argv[1],)).fetchone()[0],
 'model_starts':db.execute("select count(*) from workbench_events where task_id=? and kind='agent_v2_model_started'",(sys.argv[1],)).fetchone()[0]}))
"""
    observed = json.loads(subprocess.check_output(["docker", "exec", api, "python", "-c", code, preview["task_id"]]))
    assert observed["active"] == 0 and observed["history"] == preview["history_after"]
    assert observed["authorizations"] == 9 and observed["model_starts"] == 78
    route = f'/api/v1/tasks/{preview["task_id"]}/workbench'
    old_proposal = read_json(Path("docs/acceptance/universal-environment-v1/ci-native-plan11-execution-proposal.json"))
    ExecutePlanRequest.model_validate(preview["execution_request_body"])
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        response = client.get(route)
        response.raise_for_status()
        view = response.json()
        execution = view["execution"]
        assert not view["busy"] and view["state"] == "planning" and view["plans"][-1] == preview["plan"]
        assert view["context"] == old_proposal["context"]
        assert view["model_binding_hash"] == preview["model_binding_hash"] and view["model_profiles"] == old_proposal["task_profiles"]
        assert execution["actions"]["can_execute"] and not execution["plan_stale"]
        assert execution["preflight"]["report_hash"] == preview["preflight_hash"]
        assert execution["environment"] == preview["environment"]
        assert execution["model_transport_hash"] == preview["model_transport_hash"]
        assert execution["planning_budget"] == preview["planning_budget"]
        assert execution["agent_runtime_v2"]["checkpoint"] == preview["old_Plan11_checkpoint"]
        approval = {"version": "harness-exact-human-consent-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "approved_proposal_hash": APPROVED, "actor": "human", "decision": "approved", "user_message": "批准",
            "consent_context": "Direct reply to request to approve preview 81e1d70a and additional bounded acceptance budget",
            "execution_approved": True, "additional_bounded_run_approved": True,
            "execution_request_body": preview["execution_request_body"], "execution_idempotency_key": preview["execution_idempotency_key"],
            "new_run_limits": preview["new_run_requested_limits"], "review_bounds": preview["review_bounds"],
            "allowed_actions": preview["allowed_actions"], "write_paths": preview["write_paths"], "commands": preview["commands"],
            "source_manifest_hash": manifest["source_hash"], "model_binding_hash": preview["model_binding_hash"],
            "model_transport_hash": preview["model_transport_hash"], "third_party_writes_approved": False,
            "publication_approved": False, "automatic_batch_splitting": False, "old_usage_preserved": True}
        approval["approval_hash"] = content_hash(approval)
        write_once(approval_path, approval)
        receipt = {"version": "harness-real-execution-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "proposal_hash": APPROVED, "approval_hash": approval["approval_hash"], "task_id": preview["task_id"],
            "plan_id": preview["plan"]["id"], "source_manifest_hash": manifest["source_hash"],
            "model_binding_hash": preview["model_binding_hash"], "model_transport_hash": preview["model_transport_hash"],
            "upstream_record_hash": upstream["record_hash"], "runtime_checks": runtime_checks,
            "preflight_hash": preview["preflight_hash"], "history_start_sequence": len(observed["history"]),
            "authorizations_before": observed["authorizations"], "native_model_starts_before": observed["model_starts"],
            "old_checkpoint": preview["old_Plan11_checkpoint"], "planning_budget_before": preview["planning_budget"],
            "new_run_limits": preview["new_run_requested_limits"], "execution_idempotency_key": preview["execution_idempotency_key"],
            "status": "consent_validated_submission_may_be_in_flight_reconcile_before_retry", "third_party_writes": 0}
        write_once(receipt_path, receipt)
        response = client.get("/api/v1/meta")
        response.raise_for_status()
        headers = {"Origin": "http://127.0.0.1:18090", "X-CSRF-Token": response.json()["csrf_token"],
            "Idempotency-Key": preview["execution_idempotency_key"]}
        response = client.post(route + "/execute", json=preview["execution_request_body"], headers=headers)
        if response.status_code not in (200, 202):
            raise RuntimeError(f"Execute HTTP {response.status_code}; reconcile existing action before retry")
        job = response.json()
        receipt.update(status="native_execution_queued", native_job_receipt={k: job[k] for k in ("id", "kind", "state")})
        ensure_no_sensitive_data(receipt, context="Harness enqueue receipt")
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": receipt["status"], "job": receipt["native_job_receipt"], "approval_hash": approval["approval_hash"]}))


if __name__ == "__main__":
    main()
