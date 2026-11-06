"""Query-only live history check. No service redeploy, approval or job API."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "read-batch-scheduler-v1"


def main() -> None:
    spec = importlib.util.spec_from_file_location("query_only_reader", PRIOR / "plan13-preparation/prepare_plan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())["roles"]
    current = module.docker_read(roles["serve"]["id"])
    expected = json.loads((PRIOR / "plan15-execution/after-terminal-state.json").read_text())
    assert current == expected, "Live business state differs from the saved Plan15 terminal snapshot"
    code = """
import hashlib,json
from pathlib import Path
files={str(p.relative_to('/app')):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/app/app').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
from app.provenance import content_hash
print(json.dumps({'files':len(files),'hash':content_hash(files)}))
"""
    deployed = {}
    for role, config in roles.items():
        value = json.loads(subprocess.check_output(["docker", "exec", config["id"], "python", "-B", "-c", code]))
        assert value == {"files": 254, "hash": "31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95"}
        health = json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .State}}", config["id"]]))
        assert health["Running"] and health.get("Health", {}).get("Status", "healthy") == "healthy"
        deployed[role] = {**value, "running": True, "health": health.get("Health", {}).get("Status", "not_configured")}
    report = {"version": "coding-recovery-readonly-compatibility-v1", "query_only": True,
        "all_45_table_rows_identical_to_saved_terminal_snapshot": True,
        "business_snapshot_hash": content_hash(current), "counts": current["counts"],
        "tables": {k: {x: v[x] for x in ("count", "hash")} for k, v in current["tables"].items()},
        "all_11_checkpoints_unchanged": True, "checkpoint_states": {k: v["state"] for k, v in current["checkpoints"].items()},
        "model_identity_hash": content_hash(current["model_identities"]),
        "deployed_roles": deployed, "deployed_source_is_original_not_local_candidate": True,
        "schema_change": False, "new_paid_requests": 0, "new_plans": 0, "new_approvals": 0,
        "resumed_old_sessions": 0, "budget_changes_or_refunds": 0, "deployments": 0}
    ensure_no_sensitive_data(report, context="read-only compatibility evidence")
    report["report_hash"] = content_hash(report)
    (ROOT / "compatibility-live.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "unchanged", "tables": len(report["tables"]), "counts": report["counts"],
        "report_hash": report["report_hash"]}))


if __name__ == "__main__":
    main()
