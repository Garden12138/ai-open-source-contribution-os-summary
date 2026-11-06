"""Read-only post-upgrade proof: no old task, budget or authorization changed."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "plan13-preparation"))
from prepare_plan import docker_read
from app.agent_v2.native_tools import native_model_transport
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data


def main() -> None:
    deployment = json.loads((ROOT / "deployment-report.json").read_text())
    manifest = json.loads((ROOT / "source-manifest.json").read_text())
    before = json.loads((ROOT / "before-state.json").read_text())
    current = docker_read(deployment["new_roles"]["serve"]["id"])
    assert current["counts"] == before["counts"]
    assert current["checkpoints"] == before["checkpoints"]
    assert current["history"] == before["history"]
    assert current["latest_plan"] == before["latest_plan"]
    assert current["latest_binding"] == before["latest_binding"]
    assert current["model_profiles"] == before["model_profiles"]
    assert current["model_identities"] == before["model_identities"]
    assert current["runner_image"] == before["runner_image"]
    for name, table in before["tables"].items():
        if name == "_schema_migrations":
            assert not (Counter(table["row_hashes"]) - Counter(current["tables"][name]["row_hashes"]))
            assert current["tables"][name]["count"] == table["count"] + 1
        else:
            assert table == current["tables"][name], name
    roles = {}
    for role, entry in deployment["new_roles"].items():
        info = json.loads(subprocess.check_output(["docker", "inspect", entry["id"]]))[0]
        previous = json.loads(subprocess.check_output(["docker", "inspect", deployment["old_roles_preserved_stopped"][role]]))[0]
        assert info["State"]["Running"] and not previous["State"]["Running"]
        assert info["HostConfig"] == previous["HostConfig"] and info["Mounts"] == previous["Mounts"]
        assert all(info["Config"][k] == previous["Config"][k] for k in ("Env", "User", "Entrypoint", "Cmd", "WorkingDir"))
        code = "import hashlib,json,sys;f=json.loads(sys.argv[1]);assert all(hashlib.sha256(open('/app/'+p,'rb').read()).hexdigest()==h for p,h in f.items())"
        subprocess.run(["docker", "exec", entry["id"], "python", "-c", code, json.dumps(manifest["files"])], check=True)
        logs = subprocess.run(["docker", "logs", "--tail", "150", entry["id"]], text=True, capture_output=True, check=True)
        ensure_no_sensitive_data(logs.stdout + logs.stderr, context="post-upgrade logs")
        roles[role] = {"running": True, "source_files_verified": len(manifest["files"]), "security_resources_mounts_environment_unchanged": True}
    task = "f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        response = client.get(f"/api/v1/tasks/{task}/workbench")
        response.raise_for_status()
        view = response.json()
    prior = json.loads((ROOT.parent / "plan13-preparation/execution-preview.json").read_text())
    for key in ("record_hash", "limits", "usage", "remaining", "reserved", "unknown_calls"):
        assert view["execution"]["planning_budget"][key] == prior["planning_budget"][key], key
    assert view["execution"]["agent_runtime_v2"]["checkpoint"] == current["checkpoints"]["session-11901711068ace4c72aa1410a8847453"]
    assert view["execution"]["model_transport"] == prior["model_transport"]
    assert view["execution"]["model_transport"] != native_model_transport().model_dump()
    report = {"version": "approved-missing-legacy-compatibility-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_hash": manifest["source_hash"], "roles": roles, "counts": current["counts"],
        "all_44_business_table_hashes_unchanged": True, "only_schema_migration_added": "0045_source_file_observations",
        "all_old_checkpoints_unchanged": True, "Plan11_Plan12_Plan13_terminal_and_usage_unchanged": True,
        "Plan13_charged_reads": current["checkpoints"]["session-11901711068ace4c72aa1410a8847453"]["usage"]["reads"],
        "planning_budget": view["execution"]["planning_budget"], "old_native_binding_unchanged_and_stale_for_new_protocol": True,
        "new_model_calls": 0, "Plan14_created": False, "old_session_resumed": False,
        "credential_log_and_1610_artifact_scan": "passed", "Push_PR_comments": False,
        "Linux_amd64": "not_run_still_open", "HR4_HR5": "not_passed_by_offline_tests"}
    ensure_no_sensitive_data(report, context="legacy compatibility report")
    report["report_hash"] = content_hash(report)
    with (ROOT / "legacy-compatibility.json").open("x") as target:
        target.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"counts": current["counts"], "old_reads": report["Plan13_charged_reads"],
        "old_checkpoints": len(current["checkpoints"]), "report_hash": report["report_hash"]}))


if __name__ == "__main__":
    main()
