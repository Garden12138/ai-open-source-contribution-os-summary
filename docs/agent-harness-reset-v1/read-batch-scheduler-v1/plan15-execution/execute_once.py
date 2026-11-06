"""Recheck the exact consent, durably record intent, and POST Plan15 ONCE."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx

from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import native_model_transport
from app.config import Settings
from app.github import GitHubClient
from app.provenance import content_hash
from app.sandbox_worker.specs import SandboxPolicy
from app.security import ensure_no_sensitive_data
from app.workbench_api import ExecutePlanRequest

ROOT = Path(__file__).resolve().parent
PREP = ROOT.parent / "plan15-preparation"
sys.path.insert(0, str(PREP))
from check_prerequisites import TASK, basis_checks, checked_json, deployment_checks, docker_read, get_view
from prepare_plan import frozen_model_and_archive

APPROVED = "f7c000560f1e2e1bb9bad33ba20b21c81ceb39bbcfd97f843f1c4e745bc41a6e"
PLAN = "626be813-5437-4ad3-8a24-e30d12c7b803"
PLAN_HASH = "448b0a90cd2c9e4d3dfc14c94de8705ee5a7f2ec89487473397c18dc3e654740"
BINDING = "2672bf166060bf9ec58eda0392a55ba0d141c02dad8fc9a4853a7d4b1d29e70b"
SOURCE = "31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95"
CURRENT_HEAD = "8aa905151329f03e32397c3d1e057bf2a0ac2950"


def save(name: str, value: dict, hash_field: str | None = None) -> dict:
    if hash_field:
        value[hash_field] = content_hash(value)
    ensure_no_sensitive_data(value, context="Plan15 " + name)
    with (ROOT / name).open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        stream.flush(); os.fsync(stream.fileno())
    descriptor = os.open(ROOT, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
    return value


async def upstream() -> dict:
    previous = checked_json(PREP / "upstream-check.json", "record_hash")
    settings = Settings(github_read_proxy_url="http://127.0.0.1:18092",
        github_read_proxy_key=bytes.fromhex(Path("/tmp/upeve-isolated-20261029-v2/github-read-proxy-key").read_text().strip()),
        github_max_retries=0, request_timeout_seconds=30)
    async with GitHubClient(settings) as github:
        items = await github.search_issues('repo:patoles/agent-flow is:issue "tool" "duplicate"', 100)
        issue, = [i for i in items if i.get("number") == 107 and i.get("html_url") == previous["issue_url"]]
        branch, head = await github.resolve_repository_head("patoles/agent-flow", "main")
    value = {"recorded_at": datetime.now(timezone.utc).isoformat(), "issue_url": issue["html_url"],
        "state": issue["state"], "title": issue["title"], "updated_at": issue["updated_at"],
        "issue_body_hash": content_hash(issue["body"]),
        "issue_body_raw_utf8_sha256": hashlib.sha256(issue["body"].encode()).hexdigest(), "branch": branch, "HEAD": head}
    for key in ("issue_url", "state", "title", "updated_at", "issue_body_hash", "issue_body_raw_utf8_sha256", "branch", "HEAD"):
        assert value[key] == previous[key], "Frozen upstream changed: " + key
    return save("upstream-before-execution.json", value, "record_hash")


def main() -> None:
    assert not any((ROOT / name).exists() for name in ("approval.json", "dispatch-intent.json", "dispatch-receipt.json")), (
        "Already recorded: read-only reconciliation only; do not submit another execution")
    preview = checked_json(PREP / "execution-preview.json", "proposal_hash")
    assert preview["proposal_hash"] == APPROVED and preview["plan"]["id"] == PLAN
    assert preview["plan_record_hash"] == PLAN_HASH and preview["plan_binding_hash"] == BINDING
    assert preview["runtime_source_hash"] == SOURCE and preview["runtime_source_files"] == 254
    assert preview["additional_run_limits_pending_consent"] == Limits().model_dump()
    assert preview["model_transport"] == native_model_transport().model_dump()
    assert preview["model_transport_hash"] == content_hash(native_model_transport().model_dump())
    assert preview["sandbox_policy_hash"] == SandboxPolicy().policy_hash
    assert preview["new_run_budget_allocated"] is False and preview["execution_approved"] is False
    assert not preview["execution_started"] and not preview["future_execution_request"]["sent"]
    assert subprocess.check_output(["git", "branch", "--show-current"], text=True).strip() == "refactor/agent-runtime-v2"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == CURRENT_HEAD
    changed = subprocess.check_output(["git", "diff", "--name-only", preview["git_HEAD"], CURRENT_HEAD], text=True).splitlines()
    assert len(changed) == 23 and all(p == "docs/tasks.md" or p.startswith(str(PREP.relative_to(Path.cwd())) + "/") for p in changed)
    assert not subprocess.check_output(["git", "diff", "HEAD", "--", "app", "tests"])
    assert not subprocess.check_output(["git", "diff", "--cached", "--name-only"])
    manifest = json.loads((ROOT.parent / "output-capacity-v1/source-manifest.json").read_text())
    files = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path("app").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    assert files == manifest["files"] and len(files) == 254 and content_hash(files) == SOURCE
    for path, digest in files.items():
        assert hashlib.sha256(subprocess.check_output(["git", "show", CURRENT_HEAD + ":" + path])).hexdigest() == digest
    for item in checked_json(PREP / "final-checks.json", "report_hash")["files"]:
        assert hashlib.sha256((PREP / item["file"]).read_bytes()).hexdigest() == item["sha256"]
    deployment = deployment_checks()
    container = deployment["new_roles"]["serve"]["id"]
    assert deployment["images"] == preview["deployed_images"]
    assert frozen_model_and_archive(container, preview) == json.loads((PREP / "model-and-archive-check.json").read_text())
    fresh_upstream = asyncio.run(upstream())
    before = docker_read(container)
    assert before == json.loads((PREP / "after-state.json").read_text()), "Business state changed; stop before consent consumption"
    assert before["counts"]["all_active_jobs"] == 0 and before["counts"]["model_starts"] == 91
    assert before["latest_plan"]["id"] == PLAN and before["latest_plan"]["record_hash"] == PLAN_HASH
    assert before["latest_binding"] == preview["plan_binding"]
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False, follow_redirects=False) as client:
        view = get_view(client); basis_checks(before, view)
        assert view == json.loads((PREP / "after-view.json").read_text())
        assert view["state"] == "planning" and not view["busy"] and view["execution"]["actions"]["can_execute"]
        for live, frozen in (("revision", "execution_revision"), ("preflight", "preflight"),
                ("environment", "environment"), ("planning_budget", "planning_budget")):
            assert view["execution"][live] == preview[frozen], live
        assert view["model_binding_hash"] == preview["model_binding_hash"]
        assert view["execution"]["model_transport_hash"] == preview["model_transport_hash"]
        assert view["execution"]["preflight"]["status"] == "ready"
        assert (datetime.now(timezone.utc) - datetime.fromisoformat(fresh_upstream["recorded_at"])).total_seconds() < 300
        request = preview["future_execution_request"]
        body = ExecutePlanRequest.model_validate(request["body"]).model_dump()
        assert request["method"] == "POST" and request["path"] == f"/api/v1/tasks/{TASK}/workbench/execute"
        assert request["idempotency_key"] == "harness-capacity-plan15-execute-once"
        assert body["plan_id"] == PLAN and body["plan_hash"] == PLAN_HASH and body["max_repairs"] == 2
        save("before-dispatch-state.json", before)
        save("pre-dispatch-check.json", {"recorded_at": datetime.now(timezone.utc).isoformat(),
            "proposal_hash": APPROVED, "runtime_source_hash": SOURCE, "deployed_roles_verified": 5,
            "git_HEAD_frozen": preview["git_HEAD"], "git_HEAD_current": CURRENT_HEAD,
            "documentation_only_changed_files": changed, "production_source_unchanged": True,
            "host_HEAD_is_snapshot_metadata_not_API_admission_input": True,
            "upstream_check_hash": fresh_upstream["record_hash"], "revision": view["execution"]["revision"],
            "preflight_hash": preview["preflight_hash"], "model_binding_hash": preview["model_binding_hash"],
            "all_frozen_execution_inputs_match": True, "before_counts": before["counts"]}, "report_hash")
        confirmation = (PREP / "confirmation-text.txt").read_bytes()
        approval = save("approval.json", {"version": "output-capacity-plan15-human-execution-consent-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "proposal_hash": APPROVED,
            "source": "explicit_current_user_message_exact_confirmation", "actor": "human-user",
            "confirmation_text_sha256": hashlib.sha256(confirmation).hexdigest(),
            "scope": "one_bounded_real_acceptance_and_independent_review_only", "plan_id": PLAN,
            "plan_hash": PLAN_HASH, "binding_hash": BINDING, "runtime_source_hash": SOURCE,
            "deployed_images": preview["deployed_images"], "runner_image_digest": preview["runner_image_digest"],
            "sandbox_policy_hash": preview["sandbox_policy_hash"], "model_identities": preview["model_identities"],
            "model_binding_hash": preview["model_binding_hash"], "model_transport_hash": preview["model_transport_hash"],
            "new_run_limits": preview["additional_run_limits_pending_consent"], "review_bounds": preview["review_bounds"],
            "existing_call_bounds": preview["existing_call_bounds"], "write_paths": preview["write_paths"], "commands": preview["commands"],
            "allowed_actions": preview["allowed_actions_only_after_new_execution_consent"],
            "acceptance_report_contract_hash": preview["acceptance_report_contract_hash"],
            "upstream_hash": fresh_upstream["record_hash"], "execution_revision": view["execution"]["revision"],
            "stop_at": "human_final_acceptance_or_original_failure_or_budget_boundary",
            "subsequent_paid_rounds_approved": False, "old_authority_terminal_or_budget_reuse": False,
            "Push_PR_comments_publication_approved": False}, "approval_hash")
        dispatch = save("dispatch-intent.json", {"version": "output-capacity-plan15-dispatch-intent-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "proposal_hash": APPROVED,
            "approval_hash": approval["approval_hash"], "task_id": TASK, "plan_id": PLAN,
            "idempotency_key": request["idempotency_key"], "body": body,
            "history_start_sequence": len(before["history"]),
            "status": "submission_may_be_in_flight_reconcile_without_resubmission", "automatic_retry": False}, "intent_hash")
        meta = client.get("/api/v1/meta"); meta.raise_for_status()
        assert get_view(client) == view, "State changed immediately before dispatch; do not POST"
        headers = {"Origin": "http://127.0.0.1:18090", "X-CSRF-Token": meta.json()["csrf_token"],
            "Idempotency-Key": request["idempotency_key"]}
        # Exactly one POST. Unknown transport outcome => read-only reconciliation.
        response = client.post(request["path"], json=body, headers=headers)
        if response.status_code not in (200, 202):
            raise RuntimeError(f"Execute HTTP {response.status_code}; reconcile without automatic resubmission")
        job = response.json()
        receipt = save("dispatch-receipt.json", {"version": "output-capacity-plan15-dispatch-receipt-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "proposal_hash": APPROVED,
            "approval_hash": approval["approval_hash"], "intent_hash": dispatch["intent_hash"],
            "job": {k: job[k] for k in ("id", "kind", "state")}, "HTTP_status": response.status_code,
            "single_execution_POST": True, "automatic_extra_run": False, "third_party_writes": 0}, "receipt_hash")
        print(json.dumps(receipt))


if __name__ == "__main__":
    main()
