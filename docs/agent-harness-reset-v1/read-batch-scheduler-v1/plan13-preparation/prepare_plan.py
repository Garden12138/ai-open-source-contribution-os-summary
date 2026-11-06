"""Materialize an explicitly authorized plan binding; never execute or call a model."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

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
PRIOR = ROOT.parent
TASK = "f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"
INTENT = "a7b2f501f7320db06810b37c04816b0d973e3aaee595c8cd00aac94d2c402291"
STOP_KEY = "harness-rb-plan13-preparation-stop"
PLAN_KEY = "harness-rb-plan13-same-content-binding"


def save(name: str, value: dict) -> None:
    ensure_no_sensitive_data(value, context=name)
    with (ROOT / name).open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def checked_json(path: Path, hash_field: str) -> dict:
    value = json.loads(path.read_text())
    without = {k: v for k, v in value.items() if k != hash_field}
    assert content_hash(without) == value[hash_field], path.name
    return value


def docker_read(container: str) -> dict:
    # Execute trusted application readers inside the original Linux business
    # container. Export hashes/identities only; no database on the host.
    code = r'''
from collections import Counter
from dataclasses import asdict
import hashlib,json,sqlite3
from pathlib import Path
from sqlalchemy import text
from app.agent_v2.journal import AgentJournal
from app.audit import AuditService
from app.config import Settings
from app.database import Database
from app.model_settings import ModelSettingsService
from app.plans import PlanVersionService
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench
s=Settings.from_env();db=Database(s.database_url);task="f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"
with db.session() as session:
 session.execute(text("BEGIN"));session.execute(text("PRAGMA query_only=ON"))
 assert session.execute(text("PRAGMA integrity_check")).scalar_one()=="ok"
 assert not session.execute(text("PRAGMA foreign_key_check")).all()
 audit=AuditService(session).verify();assert audit.valid
 wb=Workbench(session,s.artifact_root);history=wb.history(task)
 roots=[e for e in history if e.kind=="agent_v2_session"]
 checkpoints={e.payload["session_id"]:AgentJournal(wb,task,e.payload["session_id"]).checkpoint().model_dump() for e in roots}
 models=ModelSettingsService(session);profiles=models.task_profiles(task,bind=False)
 identities={k:models.identity(profiles[k]).hash_payload() for k in ("implementation","review")}
 plan=PlanVersionService(session).get_verified(wb.latest_plan(task).id)
 binding=wb.binding(task,plan.id)
 tables={}
 for (name,) in session.execute(text("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")):
  assert all(c.isalnum() or c=='_' for c in name)
  rows=session.execute(text('select * from "'+name+'"')).all()
  hashes=sorted(content_hash(list(row)) for row in rows)
  tables[name]={"count":len(rows),"hash":content_hash(hashes),"row_hashes":hashes}
 artifacts=session.execute(text("select id,storage_key from artifacts")).all()
 for aid,key in artifacts:
  raw=(Path(s.artifact_root)/key).read_bytes();assert hashlib.sha256(raw).hexdigest()==aid
  ensure_no_sensitive_data(raw,context="plan preparation artifact audit")
 value={"integrity":"ok","foreign_key_violations":0,"audit_chain_valid":True,"tables":tables,
  "history":[{"id":e.id,"record_hash":e.record_hash,"sequence":e.sequence,"kind":e.kind} for e in history],
  "counts":{"active_jobs":sum(j.state in {"queued","leased","running"} for j in wb.jobs(task)),
    "all_active_jobs":session.execute(text("select count(*) from jobs where state in ('queued','leased','running')")).scalar_one(),
    "model_starts":session.execute(text("select count(*) from workbench_events where kind='agent_v2_model_started'")).scalar_one(),
    "execution_authorizations":sum(e.kind=="execution_authorized" for e in history),
    "plan_approvals":tables["plan_approvals"]["count"],"task_events":len(history),"artifacts":len(artifacts)},
  "checkpoints":checkpoints,"model_profiles":profiles,"model_identities":identities,
  "latest_plan":{"id":plan.id,"record_hash":plan.record_hash,"content_hash":plan.content_hash,"version_number":plan.version_number},
  "latest_binding":{"id":binding.id,"record_hash":binding.record_hash,"payload":binding.payload},
  "runner_image":s.workbench_runner_image,"artifacts_hash_and_credentials_verified":len(artifacts)}
 print(json.dumps(value))
db.close()
'''
    return json.loads(subprocess.check_output(["docker", "exec", container, "python", "-c", code]))


def get_view(client: httpx.Client) -> dict:
    response = client.get(f"/api/v1/tasks/{TASK}/workbench")
    response.raise_for_status()
    return response.json()


def local_checks() -> tuple[dict, dict, dict]:
    intent = checked_json(PRIOR / "real-model-acceptance-preview.json", "proposal_hash")
    assert intent["proposal_hash"] == INTENT
    assert subprocess.check_output(["git", "branch", "--show-current"], text=True).strip() == "refactor/agent-runtime-v2"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == "e5cc92a8e3154acf79025cb0e235357b514024fd"
    manifest = json.loads((PRIOR / "source-manifest.json").read_text())
    assert content_hash(manifest["files"]) == intent["runtime_source_hash"]
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    assert plan_content(PlanVersionCreateRequest.model_validate(intent["candidate_plan_materialization_request"])).hash_payload() == intent["candidate_plan_content"]
    assert intent["candidate_plan_content_hash"] == content_hash(intent["candidate_plan_content"])
    assert intent["write_paths"] == intent["candidate_plan_content"]["files_likely_to_change"]
    assert intent["commands"] == [{k: c[k] for k in ("command_id", "argv", "working_directory")}
        for c in intent["candidate_plan_content"]["commands_to_run"]]
    assert intent["candidate_additional_run_limits"] == Limits().model_dump()
    assert intent["native_transport"] == native_model_transport().model_dump()
    assert intent["native_transport_hash"] == content_hash(intent["native_transport"])
    assert intent["scheduler_policy"] == POLICY and intent["sandbox_policy_hash"] == SandboxPolicy().policy_hash
    upstream = checked_json(ROOT / "upstream-check.json", "record_hash")
    old_upstream = checked_json(PRIOR / "upstream-before-preview.json", "record_hash")
    assert upstream["HEAD"] == intent["target"]["base_sha"] and upstream["issue_body_hash"] == old_upstream["issue_body_hash"]
    assert upstream["state"] == "open" and upstream["baseline_matches"] and upstream["issue_body_matches"]
    deployment = json.loads((PRIOR / "deployment-report.json").read_text())
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    assert roles["roles"] == deployment["new_roles"]
    for role, value in roles["roles"].items():
        info = json.loads(subprocess.check_output(["docker", "inspect", value["id"]]))[0]
        assert info["State"]["Running"]
        assert info["Image"] == intent["images"]["worker" if role == "sandbox-worker" else "app"]
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
    assert view["execution"]["environment"]["verification_contract"]["binding"]["runner_image_digest"] == state["runner_image"]
    old = json.loads((PRIOR.parent / "real-acceptance-preview.json").read_text())
    old_env = old["environment"]
    current_env = view["execution"]["environment"]
    for key in ("ecosystem", "mode", "preparation_version", "project_directory", "manifest_hashes",
                "node_environment", "capability_version", "ci_toolchains", "environment_engine"):
        assert current_env[key] == old_env[key], key
    contract = current_env["verification_contract"]
    assert contract["commands"] == old_env["verification_contract"]["commands"]
    for key in ("snapshot_hash", "source_archive_hash", "base_commit_sha", "approved_commands_hash",
                "target_platform", "runner_image_digest", "sandbox_policy_hash"):
        assert contract["binding"][key] == old_env["verification_contract"]["binding"][key], key
    assert view["execution"]["agent_runtime_v2"]["checkpoint"] in state["checkpoints"].values()
    for key in ("record_hash", "limits", "usage", "remaining", "reserved", "unknown_calls"):
        assert view["execution"]["planning_budget"][key] == intent["historical_budget"][key], key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialize", action="store_true")
    args = parser.parse_args()
    intent, deployment, upstream = local_checks()
    container = deployment["new_roles"]["serve"]["id"]
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        before = docker_read(container)
        view = get_view(client)
        basis_checks(intent, before, view)
        assert before["latest_plan"]["id"] == intent["candidate_parent_reference"]["id"]
        assert before["latest_plan"]["record_hash"] == intent["candidate_parent_reference"]["record_hash"]
        assert before["latest_plan"]["content_hash"] == intent["candidate_plan_content_hash"]
        assert before["latest_plan"]["version_number"] == 12
        assert view["execution"]["model_transport"] == intent["historical_Plan12_transport"]
        assert view["state"] == "executing"
        if not args.materialize:
            report = {"version": "native-read-batch-plan13-prerequisites-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
                "intent_hash": INTENT, "git_HEAD": "e5cc92a8e3154acf79025cb0e235357b514024fd",
                "source_hash": intent["runtime_source_hash"], "deployed_roles_verified": 5,
                "upstream_hash": upstream["record_hash"], "all_frozen_inputs_match": True,
                "model_calls": 0, "plan_created": False, "counts": before["counts"],
                "model_identities": before["model_identities"], "native_transport_hash": intent["native_transport_hash"]}
            report["report_hash"] = content_hash(report)
            save("prerequisite-check.json", report)
            print(json.dumps({k: report[k] for k in ("all_frozen_inputs_match", "counts", "report_hash")}))
            return
        prerequisites = checked_json(ROOT / "prerequisite-check.json", "report_hash")
        assert prerequisites["all_frozen_inputs_match"]
        assert not (ROOT / "plan.json").exists() and not (ROOT / "execution-preview.json").exists()
        save("before-state.json", before)
        # The only HTTP mutation endpoints in this helper are /stop and /plans.
        # There are no active jobs. This appends a user_replan state transition;
        # it does not resume or rewrite either historical terminal checkpoint.
        response = client.get("/api/v1/meta")
        response.raise_for_status()
        headers = {"Origin": "http://127.0.0.1:18090", "X-CSRF-Token": response.json()["csrf_token"]}
        response = client.post(f"/api/v1/tasks/{TASK}/workbench/stop", json={},
            headers={**headers, "Idempotency-Key": STOP_KEY})
        response.raise_for_status()
        planning = get_view(client)
        assert planning["state"] == "planning" and not planning["busy"]
        assert planning["context"] == view["context"]
        response = client.post(f"/api/v1/tasks/{TASK}/workbench/plans",
            json={"context_hash": planning["context"]["record_hash"], "plan": intent["candidate_plan_materialization_request"]},
            headers={**headers, "Idempotency-Key": PLAN_KEY})
        response.raise_for_status()
        plan = response.json()
        assert plan["version_number"] == 13 and plan["parent_version_id"] == before["latest_plan"]["id"]
        assert plan["content_hash"] == intent["candidate_plan_content_hash"]
        assert plan_content(PlanVersionCreateRequest.model_validate({**plan, "parent_version_id": before["latest_plan"]["id"]})).hash_payload() == intent["candidate_plan_content"]
        save("plan.json", plan)
        # Mark this as mechanical operator preparation, not autonomous model work.
        marker = """import json
from app.database import Database
from app.config import Settings
from app.workbench import Workbench
from app.agent_runtime import mark_maintenance
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 wb=Workbench(session,s.artifact_root);binding=wb.binding(%r,%r)
 mark_maintenance(wb,%r,binding,reason='native_read_batch_same_content_plan13_user_authorized_binding_only')
 session.commit();print(json.dumps({'binding_hash':binding.record_hash}))
db.close()
""" % (TASK, plan["id"], TASK)
        binding = json.loads(subprocess.check_output(["docker", "exec", container, "python", "-c", marker]))
        current = get_view(client)
        after = docker_read(container)
        basis_checks(intent, after, current)
        assert current["state"] == "planning" and not current["busy"]
        assert current["execution"]["model_transport"] == intent["native_transport"]
        assert current["execution"]["model_transport_hash"] == intent["native_transport_hash"]
        assert current["execution"]["preflight"]["status"] == "ready"
        assert before["checkpoints"] == after["checkpoints"]
        assert after["history"][:len(before["history"])] == before["history"]
        allowed_changed = {"plan_versions", "contribution_task_state_versions", "workbench_events"}
        for name, table in before["tables"].items():
            after_table = after["tables"][name]
            if name in allowed_changed:
                from collections import Counter
                assert not (Counter(table["row_hashes"]) - Counter(after_table["row_hashes"])), name
            else:
                assert table == after_table, name
        for key in ("all_active_jobs", "active_jobs", "model_starts", "execution_authorizations", "plan_approvals", "artifacts"):
            assert before["counts"][key] == after["counts"][key], key
        assert after["latest_binding"]["record_hash"] == binding["binding_hash"]
        save("after-state.json", after)
        body = ExecutePlanRequest(plan_id=plan["id"], plan_hash=plan["record_hash"], approve_plan=True,
            start_execution=True, max_repairs=2, model_binding_hash=current["model_binding_hash"],
            model_transport_hash=current["execution"]["model_transport_hash"], conversation=True).model_dump()
        preview = {"version": "native-read-batch-bound-execution-preview-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": "awaiting_new_exact_execution_and_budget_confirmation", "parent_intent_hash": INTENT,
            "preparation_authorization": "user_explicitly_authorized_new_plan_and_binding_only_no_model_or_execution",
            "target": intent["target"], "git_HEAD": "e5cc92a8e3154acf79025cb0e235357b514024fd",
            "runtime_source_hash": intent["runtime_source_hash"], "deployed_images": intent["images"],
            "plan": plan, "plan_content_hash": plan["content_hash"], "plan_record_hash": plan["record_hash"],
            "plan_binding_hash": binding["binding_hash"], "plan_binding": after["latest_binding"],
            "write_paths": intent["write_paths"], "commands": intent["commands"],
            "model_profiles": current["model_profiles"], "model_identities": after["model_identities"],
            "model_binding_hash": current["model_binding_hash"], "model_transport": current["execution"]["model_transport"],
            "model_transport_hash": current["execution"]["model_transport_hash"], "scheduler_policy": POLICY,
            "sandbox_policy_hash": intent["sandbox_policy_hash"], "runner_image_digest": after["runner_image"],
            "environment": current["execution"]["environment"], "preflight": current["execution"]["preflight"],
            "preflight_hash": current["execution"]["preflight"]["report_hash"], "execution_revision": current["execution"]["revision"],
            "context_hash": current["context"]["record_hash"], "planning_budget": current["execution"]["planning_budget"],
            "additional_run_limits_pending_consent": intent["candidate_additional_run_limits"], "review_bounds": intent["review_bounds"],
            "allowed_actions_only_after_new_execution_consent": intent["allowed_actions_after_fresh_exact_execution_consent"],
            "success_conditions": intent["success_conditions"], "batch_coverage_condition": intent["batch_coverage_condition"],
            "execution_conditions": ["fresh one-time human consent bound to this exact preview Hash, actual Plan and Binding",
                "recheck issue body and base SHA, source/images/models/transport/policy/scopes/budgets before dispatch",
                "current preflight/revision must match; changed frozen inputs require another preview",
                "fresh preparation and original four Baseline commands before native implementation",
                "stop safely on failure, unknown result, stale input or any limit; no automatic extra paid rerun",
                "no reuse of old terminal authority, usage or reservations; no paid Planner"],
            "future_execution_request": {"method": "POST", "path": f"/api/v1/tasks/{TASK}/workbench/execute",
                "idempotency_key": "harness-rb-plan13-execute-once", "body": body, "sent": False},
            "historical_checkpoints_unchanged": True, "historical_budget_limits_usage_and_holds_unchanged": True,
            "all_preexisting_database_rows_preserved": True, "history_before_count": len(before["history"]),
            "history_after_count": len(after["history"]), "counts_before": before["counts"], "counts_after": after["counts"],
            "new_model_calls": 0, "execution_approved": False, "execution_started": False,
            "new_execution_authorization_hash": "not_created_pending_new_human_confirmation",
            "new_attempt_id": "not_created", "new_diff_test_review_hashes": "not_created",
            "third_party_writes_approved": False, "Push_PR_comments": False,
            "Linux_amd64_gate": "not_run_still_open", "HR4_HR5": "not_passed"}
        preview["proposal_hash"] = content_hash(preview)
        save("execution-preview.json", preview)
        print(json.dumps({"plan_id": plan["id"], "version_number": plan["version_number"],
            "plan_record_hash": plan["record_hash"], "binding_hash": binding["binding_hash"],
            "proposal_hash": preview["proposal_hash"], "new_model_calls": 0, "execution_started": False}))


if __name__ == "__main__":
    main()
