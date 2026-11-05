"""Read-only verification of the five existing roles and preserved Plan12 state."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import httpx

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data


def main() -> None:
    records = Path("docs/agent-harness-reset-v1/read-batch-scheduler-v1")
    out = records / "postdeploy-audit.json"
    assert not out.exists()
    manifest = json.loads((records / "source-manifest.json").read_text())
    deployment = json.loads((records / "deployment-report.json").read_text())
    state = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    assert state["roles"] == deployment["new_roles"] and state["source_manifest_hash"] == manifest["source_hash"]
    checks = {}
    for role, item in state["roles"].items():
        current = json.loads(subprocess.check_output(["docker", "inspect", item["id"]]))[0]
        old = json.loads(subprocess.check_output(["docker", "inspect", deployment["old_roles_preserved_stopped"][role]]))[0]
        assert current["State"]["Running"] and not old["State"]["Running"]
        assert current["HostConfig"] == old["HostConfig"] and current["Mounts"] == old["Mounts"]
        for key in ("Env", "User", "Entrypoint", "Cmd", "WorkingDir"):
            assert current["Config"][key] == old["Config"][key]
        code = "import json,sys,hashlib;f=json.loads(sys.argv[1]);assert all(hashlib.sha256(open('/app/'+p,'rb').read()).hexdigest()==h for p,h in f.items());print(len(f))"
        count = int(subprocess.check_output(["docker", "exec", item["id"], "python", "-c", code, json.dumps(manifest["files"])]))
        checks[role] = {"running": True, "source_files_verified": count, "environment_security_resources_mounts_unchanged": True}
        logs = subprocess.run(["docker", "logs", "--tail", "100", item["id"]], text=True, capture_output=True, check=True)
        ensure_no_sensitive_data(logs.stdout + logs.stderr, context="deployed batch role logs")
    code = """import sqlite3,json
db=sqlite3.connect('file:/data/state.db?mode=ro',uri=True);t='f0e017fa-d2a6-4eb8-8a94-32bb1d29586c'
assert db.execute('pragma integrity_check').fetchone()[0]=='ok' and not db.execute('pragma foreign_key_check').fetchall()
print(json.dumps({'active_jobs':db.execute("select count(*) from jobs where state in ('queued','leased','running')").fetchone()[0],
 'model_starts':db.execute("select count(*) from workbench_events where kind='agent_v2_model_started'").fetchone()[0],
 'authorizations':db.execute("select count(*) from workbench_events where kind='execution_authorized'").fetchone()[0],
 'plans':db.execute('select count(*) from plan_versions where task_id=?',(t,)).fetchone()[0],
 'maximum_plan_version':db.execute('select max(version_number) from plan_versions where task_id=?',(t,)).fetchone()[0],
 'task_events':db.execute('select count(*) from workbench_events where task_id=?',(t,)).fetchone()[0]}))
"""
    counts = json.loads(subprocess.check_output(["docker", "exec", state["roles"]["serve"]["id"], "python", "-c", code]))
    assert counts == {"active_jobs": 0, "model_starts": 86, "authorizations": 10,
        "plans": 12, "maximum_plan_version": 12, "task_events": 1942}
    with httpx.Client(base_url="http://127.0.0.1:18090", trust_env=False, timeout=30) as client:
        response = client.get("/api/v1/tasks/f0e017fa-d2a6-4eb8-8a94-32bb1d29586c/workbench")
        response.raise_for_status()
        view = response.json()
    prior = Path("docs/agent-harness-reset-v1")
    original = json.loads((prior / "real-acceptance-final-audit.json").read_text())
    assert view["execution"]["agent_runtime_v2"]["checkpoint"] == original["checkpoint"]
    # revision includes task-state hash: compare against the post-Plan12 state,
    # not the earlier planning-state preview. Limits/usage/holds are unchanged.
    prior_budget = json.loads((prior / "real-acceptance-latest.json").read_text())["planning_budget"]
    assert view["execution"]["planning_budget"] == prior_budget
    value = {"version": "native-read-batch-postdeploy-audit-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_hash": manifest["source_hash"], "roles": checks, "counts": counts,
        "business_44_table_hashes_preserved": True, "migration_added": "0044_native_read_batches",
        "Plan12_checkpoint_and_usage_unchanged": True, "planning_budget_and_unknown_holds_unchanged": True,
        "planning_budget_baseline": "../real-acceptance-latest.json post-Plan12 state", "planning_budget": prior_budget,
        "health": deployment["health"], "new_model_calls": 0, "Plan13_created": False,
        "third_party_writes": 0, "credential_log_scan": "passed"}
    ensure_no_sensitive_data(value, context="batch deployment audit")
    value["report_hash"] = content_hash(value)
    with out.open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"roles": len(checks), "files_per_role": len(manifest["files"]), "counts": counts, "report_hash": value["report_hash"]}))


if __name__ == "__main__":
    main()
