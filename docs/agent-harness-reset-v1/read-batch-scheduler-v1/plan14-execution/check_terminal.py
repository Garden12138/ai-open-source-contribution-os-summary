"""Final read-only proof of stopped Plan14 and unchanged history/security caps."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import httpx

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "plan14-preparation"))
from prepare_plan import checked_json, docker_read, get_view, local_checks


def main() -> None:
    assert not (ROOT / "terminal-checks.json").exists()
    intent, deployment, _ = local_checks()
    preview = checked_json(ROOT.parent / "plan14-preparation/execution-preview.json", "proposal_hash")
    audit = checked_json(ROOT / "final-audit.json", "report_hash")
    result = checked_json(ROOT / "result.json", "report_hash")
    index = checked_json(ROOT / "evidence-index.json", "report_hash")
    before = json.loads((ROOT / "before-dispatch-state.json").read_text())
    after = docker_read(deployment["new_roles"]["serve"]["id"])
    assert after == json.loads((ROOT / "after-terminal-state.json").read_text())
    assert after["counts"]["all_active_jobs"] == 0
    assert after["counts"]["model_starts"] == 91 and after["counts"]["execution_authorizations"] == 12
    assert after["counts"]["plan_approvals"] == 12 and after["tables"]["plan_versions"]["count"] == 14
    assert after["history"][:len(before["history"])] == before["history"]
    for name, table in before["tables"].items():
        assert not (Counter(table["row_hashes"]) - Counter(after["tables"][name]["row_hashes"])), name
    for sid, checkpoint in before["checkpoints"].items():
        assert after["checkpoints"][sid] == checkpoint
    assert after["checkpoints"]["session-11901711068ace4c72aa1410a8847453"]["usage"]["reads"] == 12
    assert after["checkpoints"][audit["session_id"]] == audit["checkpoint"]
    assert audit["checkpoint"]["state"] == "blocked" and not audit["checkpoint"]["pending_calls"]
    assert after["model_identities"] == preview["model_identities"]
    assert after["tables"]["model_config_versions"] == before["tables"]["model_config_versions"]
    count = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".py", ".md"}:
            continue
        raw = path.read_bytes(); ensure_no_sensitive_data(raw, context=path.name)
        if path.parent.name == "artifacts":
            assert hashlib.sha256(raw).hexdigest() == path.stem
        count += 1
    for name, key in (("approval.json", "approval_hash"), ("dispatch-intent.json", "intent_hash"),
        ("dispatch-receipt.json", "receipt_hash"), ("upstream-before-execution.json", "record_hash"),
        ("admission-diagnostic.json", "report_hash"), ("latest.json", "observation_hash")):
        checked_json(ROOT / name, key)
    for path in (ROOT / "observations").glob("*.json"):
        checked_json(path, "observation_hash")
    for entry in deployment["new_roles"].values():
        logs = subprocess.run(["docker", "logs", "--tail", "120", entry["id"]], text=True, capture_output=True, check=True)
        ensure_no_sensitive_data(logs.stdout + logs.stderr, context="Plan14 terminal logs")
    health = {}
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        for port in (18090, 18091, 18092, 18080):
            response = client.get(f"http://127.0.0.1:{port}/health"); response.raise_for_status()
            health[str(port)] = response.json(); assert health[str(port)]["status"] == "ok"
        view = get_view(client)
    assert not view["busy"] and view["execution"]["blocked"]["reason_code"] == "agent_read_batch_budget"
    for key in ("record_hash", "limits", "usage", "remaining", "reserved", "unknown_calls"):
        assert view["execution"]["planning_budget"][key] == preview["planning_budget"][key], key
    subprocess.run([".venv/bin/python", "-m", "compileall", "-q", "app", str(ROOT)], check=True)
    subprocess.run(["git", "diff", "--check"], check=True)
    value = {"version": "approved-missing-plan14-terminal-checks-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "proposal_hash": preview["proposal_hash"], "result_hash": result["report_hash"], "audit_hash": audit["report_hash"],
        "evidence_index_hash": index["report_hash"], "runtime_source_hash": intent["runtime_source_hash"],
        "all252_source_files_and_five_images_security_resources_verified": True,
        "all_preexisting_rows_and_checkpoints_preserved": True, "old_Plan13_reads": 12,
        "planning_usage_unknown_holds_caps_unchanged": True, "new_Plan14_terminal_unchanged": True,
        "new_execution_authorizations": 1, "new_model_requests": 3, "no_automatic_retry_or_restart": True,
        "active_jobs": 0, "counts": after["counts"], "plan_count": 14,
        "credential_checked_saved_files": count, "live_artifacts_hash_and_credentials_checked": after["artifacts_hash_and_credentials_verified"],
        "logs_credentials": "passed", "compileall_app_and_helpers": "passed", "git_diff_check": "passed", "health": health,
        "source_or_policy_or_budget_changed_during_run": False, "new_production_patch_or_redeployment": False,
        "Push_PR_comments_publication": False, "HR4_HR5": "not_passed", "Linux_amd64": "not_run_still_open"}
    value["report_hash"] = content_hash(value)
    with (ROOT / "terminal-checks.json").open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"terminal_checks": "passed", "counts": after["counts"], "health": health, "report_hash": value["report_hash"]}))


if __name__ == "__main__":
    main()
