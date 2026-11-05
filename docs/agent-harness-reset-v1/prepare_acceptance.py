"""Save an unapproved same-scope child plan and exact real-execution preview.

This never submits /execute, allocates model budget, or contacts a model.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

import httpx

from app.agent_v2.native_tools import native_model_transport
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_data
from app.workbench_api import ExecutePlanRequest

RECORDS = Path("docs/agent-harness-reset-v1")
TASK = "f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"


def observe(container: str) -> dict:
    code = """import sqlite3,json
db=sqlite3.connect('file:/data/state.db?mode=ro',uri=True)
task='f0e017fa-d2a6-4eb8-8a94-32bb1d29586c'
print(json.dumps({'history':[{'id':i,'record_hash':h} for i,h in db.execute('select id,record_hash from workbench_events where task_id=? order by sequence',(task,))],
 'active':db.execute("select count(*) from jobs where state in ('queued','leased','running')").fetchone()[0],
 'authorizations':db.execute("select count(*) from workbench_events where task_id=? and kind='execution_authorized'",(task,)).fetchone()[0],
 'native_model_starts':db.execute("select count(*) from workbench_events where task_id=? and kind='agent_v2_model_started'",(task,)).fetchone()[0]}))
"""
    return json.loads(subprocess.check_output(["docker", "exec", container, "python", "-c", code]))


def main() -> None:
    out = RECORDS / "real-acceptance-preview.json"
    if out.exists() or (RECORDS / "acceptance-draft.json").exists():
        raise ValueError("A draft/preview already exists; reconcile instead of creating another plan")
    deployment = json.loads((RECORDS / "deployment-report.json").read_text())
    source = json.loads(Path(deployment.get("source_manifest_file", str(RECORDS / "source-manifest.json"))).read_text())
    assert source["source_hash"] == deployment["source_hash"]
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in source["files"].items())
    equivalence = json.loads((RECORDS / "upstream-equivalence.json").read_text())
    assert equivalence["issue_open"] and equivalence["base_commit_matches"] and equivalence["issue_body_matches"]
    old = json.loads(Path("docs/acceptance/universal-environment-v1/ci-native-plan11-execution-proposal.json").read_text())
    terminal = json.loads(Path("docs/acceptance/universal-environment-v1/ci-native-plan11-execution.json").read_text())
    container = deployment["new_roles"]["serve"]["id"]
    before = observe(container)
    assert before["active"] == 0 and before["authorizations"] == 9
    request = PlanVersionCreateRequest(**{k: v for k, v in old["plan"].items()
        if k in PlanVersionCreateRequest.model_fields and k != "parent_version_id"}, parent_version_id=old["plan_id"])
    assert plan_content(request).content_hash == old["plan_content_hash"]
    route = f"/api/v1/tasks/{TASK}/workbench"
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        response = client.get(route)
        response.raise_for_status()
        view = response.json()
        assert not view["busy"] and view["plans"][-1]["id"] == old["plan_id"]
        assert view["execution"]["agent_runtime_v2"]["checkpoint"] == terminal["checkpoint"]
        budget_before = view["execution"]["planning_budget"]
        response = client.get("/api/v1/meta")
        response.raise_for_status()
        headers = {"Origin": "http://127.0.0.1:18090", "X-CSRF-Token": response.json()["csrf_token"]}
        response = client.post(route + "/stop", json={}, headers={**headers, "Idempotency-Key": "harness-v1-plan12-stop"})
        response.raise_for_status()
        for _ in range(20):
            response = client.get(route)
            response.raise_for_status()
            current = response.json()
            if current["state"] == "planning" and not current["busy"]:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Planning state not ready; reconcile local workflow")
        response = client.post(route + "/plans", json={"context_hash": view["context"]["record_hash"], "plan": request.model_dump()},
            headers={**headers, "Idempotency-Key": "harness-v1-plan12-draft"})
        response.raise_for_status()
        plan = response.json()
        assert plan["content_hash"] == old["plan_content_hash"] and plan["parent_version_id"] == old["plan_id"]
        draft = {"version": "harness-unapproved-child-v1", "classification": "mechanical_same_scope_not_model_generated",
            "plan": plan, "old_history": before["history"], "execution_approved": False, "model_calls": 0}
        ensure_no_sensitive_data(draft, context="unapproved Harness child")
        with (RECORDS / "acceptance-draft.json").open("x") as target:
            target.write(json.dumps(draft, ensure_ascii=False, indent=2) + "\n")
    # Existing platform maintenance marker records the operator-created draft.
    code = """import json
from app.database import Database
from app.config import Settings
from app.workbench import Workbench
from app.agent_runtime import mark_maintenance
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 wb=Workbench(session,s.artifact_root)
 binding=wb.binding(%r,%r)
 mark_maintenance(wb,%r,binding,reason='harness_v6_same_scope_child_after_terminal_Plan11_no_business_patch')
 session.commit()
 print(json.dumps({'binding_hash':binding.record_hash}))
db.close()
""" % (TASK, plan["id"], TASK)
    binding = json.loads(subprocess.check_output(["docker", "exec", container, "python", "-c", code]))
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        response = client.get(route)
        response.raise_for_status()
        current = response.json()
    execution = current["execution"]
    assert current["state"] == "planning" and not current["busy"] and execution["preflight"]["status"] == "ready"
    assert current["model_profiles"] == view["model_profiles"] and current["model_binding_hash"] == view["model_binding_hash"]
    assert current["context"] == view["context"]
    assert execution["model_transport"] == native_model_transport().model_dump()
    assert execution["model_transport"] != old["model_transport"]
    for key in ("limits", "usage", "remaining", "reserved", "unknown_calls"):
        assert execution["planning_budget"][key] == budget_before[key]
    after = observe(container)
    assert after["active"] == 0 and after["authorizations"] == before["authorizations"]
    assert after["native_model_starts"] == before["native_model_starts"]
    assert after["history"][:len(before["history"])] == before["history"]
    assert execution["agent_runtime_v2"]["checkpoint"] == terminal["checkpoint"]
    body = {"plan_id": plan["id"], "plan_hash": plan["record_hash"], "approve_plan": True,
        "start_execution": True, "max_repairs": 2, "model_binding_hash": current["model_binding_hash"],
        "model_transport_hash": execution["model_transport_hash"], "conversation": True}
    ExecutePlanRequest.model_validate(body)
    preview = {"version": "harness-real-acceptance-preview-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "awaiting_exact_human_execution_and_budget_consent", "task_id": TASK,
        "repository": old["repository"], "issue_url": old["issue_url"], "base_sha": current["context"]["base_sha"],
        "snapshot_id": old["snapshot_id"], "snapshot_hash": old["snapshot_hash"], "plan": plan,
        "plan_binding_hash": binding["binding_hash"], "parent_plan_hash": old["plan_record_hash"],
        "write_paths": old["write_paths"], "commands": old["commands"], "model_identities": old["model_identities"],
        "model_binding_hash": current["model_binding_hash"], "model_transport": execution["model_transport"],
        "model_transport_hash": execution["model_transport_hash"], "environment": execution["environment"],
        "preflight_hash": execution["preflight"]["report_hash"], "sandbox_policy_hash": old["sandbox_policy_hash"],
        "source_manifest_hash": source["source_hash"], "deployment_hash": content_hash(deployment),
        "new_run_requested_limits": old["agent_limits"], "review_bounds": old["review_bounds"],
        "additional_bounded_run_requires_consent": True, "prior_limits_not_raised": True,
        "old_Plan11_checkpoint": terminal["checkpoint"], "old_usage_preserved": terminal["checkpoint"]["usage"],
        "old_session_seconds_remaining": execution["agent_runtime_v2"]["remaining_budget"]["seconds"],
        "planning_budget": execution["planning_budget"], "history_before": before["history"], "history_after": after["history"],
        "execution_request_body": body, "execution_idempotency_key": "harness-v1-plan12-execute-once",
        "allowed_actions": ["fresh preparation and original four Baseline commands", "native Investigation and same-scope Patch",
            "all four approved checks on latest source", "at most two code repairs", "independent evidence-bound Review", "stop for Human Acceptance"],
        "budget_note": "This proposes a separate bounded acceptance run with the original per-run caps; it is additional allocation and is NOT approved. Old Plan11 limits, spent usage, failed reservations and terminal state remain intact. No budget is allocated or reset by saving this draft. Frozen native transport changed; old approval cannot authorize it.",
        "baseline_and_real_model": "not_run", "future_diff_test_review_hashes": "not_created_until_real_execution",
        "model_calls_by_preparation": 0, "execution_approved": False, "third_party_writes_approved": False,
        "publication_approved": False, "automatic_batch_splitting": False}
    ensure_no_sensitive_data(preview, context="exact Harness execution preview")
    preview["proposal_hash"] = content_hash(preview)
    with out.open("x") as target:
        target.write(json.dumps(preview, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"plan_id": plan["id"], "plan_number": plan["version_number"],
        "proposal_hash": preview["proposal_hash"], "write_paths": len(old["write_paths"]), "commands": len(old["commands"]),
        "model_calls": 0, "execution_approved": False}))


if __name__ == "__main__":
    main()
