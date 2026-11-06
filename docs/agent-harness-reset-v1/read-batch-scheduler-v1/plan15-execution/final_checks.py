"""Final no-write audit of the stopped Plan15, history, deployed source and secrets."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx

from app.agent_v2.contracts import Limits
from app.provenance import content_hash
from app.security import ensure_no_sensitive_bytes, ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent
PREP = ROOT.parent / "plan15-preparation"
sys.path.insert(0, str(PREP))
from check_prerequisites import TASK, checked_json, deployment_checks, docker_read, get_view


def main() -> None:
    assert not (ROOT / "terminal-checks.json").exists()
    preview = checked_json(PREP / "execution-preview.json", "proposal_hash")
    audit = checked_json(ROOT / "final-audit.json", "report_hash")
    metrics = checked_json(ROOT / "coding-metrics.json", "report_hash")
    index = checked_json(ROOT / "evidence-index.json", "report_hash")
    before = json.loads((ROOT / "before-dispatch-state.json").read_text())
    deployment = deployment_checks(); container = deployment["new_roles"]["serve"]["id"]
    after = docker_read(container)
    assert after == json.loads((ROOT / "after-terminal-state.json").read_text())
    assert after["counts"]["all_active_jobs"] == 0
    assert after["counts"]["model_starts"] == before["counts"]["model_starts"] + metrics["model_requests_started"]
    assert after["counts"]["execution_authorizations"] == before["counts"]["execution_authorizations"] + 1
    assert after["counts"]["plan_approvals"] == before["counts"]["plan_approvals"] + 1
    assert after["tables"]["plan_versions"] == before["tables"]["plan_versions"]
    assert after["history"][:len(before["history"])] == before["history"]
    for sid, cp in before["checkpoints"].items(): assert after["checkpoints"][sid] == cp
    assert after["checkpoints"][audit["session_id"]] == audit["checkpoint"]
    for name in ("plan_versions", "plan_approvals", "workbench_events", "contribution_task_state_versions"):
        assert not (Counter(before["tables"][name]["row_hashes"]) - Counter(after["tables"][name]["row_hashes"]))
    assert after["model_identities"] == preview["model_identities"]
    assert after["tables"]["model_config_versions"] == before["tables"]["model_config_versions"]
    assert after["tables"]["plan_versions"]["count"] == 15
    assert audit["checkpoint"]["state"] in {"blocked", "finished", "cancelled"}
    assert not audit["checkpoint"]["pending_calls"]
    for key, value in audit["checkpoint"]["usage"].items(): assert value <= getattr(Limits(), key)
    assert metrics["model_requests_started"] <= 24
    assert metrics["charged_usage"]["output_bytes"] <= 500000
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        view = get_view(client); assert not view["busy"]
        for key in ("record_hash", "limits", "usage", "remaining", "reserved", "unknown_calls"):
            assert view["execution"]["planning_budget"][key] == preview["planning_budget"][key]
        health = {}
        for port in (18090, 18091, 18092, 18080):
            response = client.get(f"http://127.0.0.1:{port}/health"); response.raise_for_status()
            health[str(port)] = response.json(); assert response.json()["status"] == "ok"
    for name, field in (("approval.json", "approval_hash"), ("dispatch-intent.json", "intent_hash"),
        ("dispatch-receipt.json", "receipt_hash"), ("pre-dispatch-check.json", "report_hash"),
        ("upstream-before-execution.json", "record_hash"), ("latest.json", "observation_hash")):
        checked_json(ROOT / name, field)
    for path in (ROOT / "observations").glob("*.json"): checked_json(path, "observation_hash")
    containers = [v["id"] for v in deployment["new_roles"].values()]
    for port in ("18091", "18092"):
        containers.extend(subprocess.check_output(["docker", "ps", "-q", "--filter", "publish=" + port], text=True).split())
    containers = list(dict.fromkeys(containers)); values = dict(os.environ)
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1); values[k.strip()] = v.strip().strip("\"'")
    for c in containers:
        for item in json.loads(subprocess.check_output(["docker", "inspect", c]))[0]["Config"]["Env"]:
            if "=" in item:
                k, v = item.split("=", 1); values[k] = v
    credentials = list({v for k, v in values.items() if len(v) >= 16
        and any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "SIGNING_KEY"))
        and not k.endswith(("_FILE", "_PATH", "_ENV_NAME"))})
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts: continue
        raw = path.read_bytes(); ensure_no_sensitive_bytes(raw, secrets=credentials, context=path.name)
        assert str(Path.home()).encode() not in raw, path.name
        if path.parent.name == "artifacts": assert hashlib.sha256(raw).hexdigest() == path.stem
        files.append({"file": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest()})
    for c in containers:
        logs = subprocess.run(["docker", "logs", "--tail", "2000", c], capture_output=True, check=True)
        ensure_no_sensitive_bytes(logs.stdout + logs.stderr, secrets=credentials, context="Plan15 owned service logs")
    assert not subprocess.check_output(["git", "diff", "HEAD", "--", "app", "tests"])
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == "8aa905151329f03e32397c3d1e057bf2a0ac2950"
    subprocess.run([".venv/bin/python", "-m", "compileall", "-q", "app", str(ROOT)], check=True)
    subprocess.run(["git", "diff", "--check"], check=True)
    report = {"version": "output-capacity-plan15-terminal-checks-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "proposal_hash": preview["proposal_hash"], "audit_hash": audit["report_hash"], "metrics_hash": metrics["report_hash"],
        "evidence_index_hash": index["report_hash"], "runtime_source_hash": preview["runtime_source_hash"],
        "source_254_files_five_roles_images_security_unchanged": True,
        "all_old_10_checkpoints_and_2094_event_prefix_unchanged": True,
        "all_old_approvals_usage_and_unknown_holds_unchanged": True,
        "new_execution_authorizations": 1, "new_real_model_requests": metrics["model_requests_started"],
        "new_plans_created": 0, "automatic_extra_runs_or_terminal_restarts": 0,
        "checkpoint_state": audit["checkpoint"]["state"], "usage": audit["checkpoint"]["usage"], "counts": after["counts"],
        "health": health, "all_registered_artifact_hashes_and_canaries_verified": after["artifacts_hash_and_credentials_verified"],
        "files": files, "credential_and_exact_configured_secret_scan": "passed", "logs_scanned": len(containers),
        "compileall_app_and_helpers": "passed", "git_diff_check": "passed", "production_code_changed_or_redeployed": False,
        "Push_PR_comments_publication": False, "human_final_acceptance_performed": False,
        "native_Linux_amd64": "not_run_still_open"}
    ensure_no_sensitive_data(report, context="Plan15 terminal checks")
    report["report_hash"] = content_hash(report)
    with (ROOT / "terminal-checks.json").open("x") as stream: stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"terminal_checks": "passed", "counts": after["counts"], "health": health, "report_hash": report["report_hash"]}))


if __name__ == "__main__": main()
