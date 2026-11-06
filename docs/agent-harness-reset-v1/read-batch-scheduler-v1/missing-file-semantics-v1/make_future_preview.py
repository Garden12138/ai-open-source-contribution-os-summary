"""Create a filesystem-only acceptance proposal after every offline gate passes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import httpx

from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import native_model_transport
from app.agent_v2.read_batches import POLICY
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_data

ROOT = Path(__file__).resolve().parent


def main() -> None:
    out = ROOT / "real-model-acceptance-preview.json"
    assert not out.exists()
    manifest = json.loads((ROOT / "source-manifest.json").read_text())
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    for name in ("missing-source-final-full-0.xml", "missing-source-final-full-1.xml", "missing-source-final2-real-oci.xml"):
        cases = ET.parse(Path("/tmp") / name).findall(".//testcase")
        assert cases and not any(c.find("failure") is not None or c.find("error") is not None for c in cases)
    legacy = json.loads((ROOT / "legacy-compatibility.json").read_text())
    assert legacy["new_model_calls"] == 0 and not legacy["Plan14_created"] and legacy["Plan13_charged_reads"] == 12
    prior = json.loads((ROOT.parent / "plan13-preparation/execution-preview.json").read_text())
    old = json.loads((ROOT / "before-state.json").read_text())
    upstream = json.loads((ROOT / "upstream-before-preview.json").read_text())
    build = json.loads((ROOT / "build-report-final.json").read_text())
    transport = native_model_transport().model_dump()
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        response = client.get(f"/api/v1/tasks/{prior['target']['task_id']}/workbench")
        response.raise_for_status()
        view = response.json()
    assert not view["busy"] and view["model_profiles"] == prior["model_profiles"]
    assert view["model_binding_hash"] == prior["model_binding_hash"]
    assert view["execution"]["model_transport"] == prior["model_transport"] != transport
    assert view["plans"][-1]["id"] == prior["plan"]["id"]
    request = PlanVersionCreateRequest.model_validate({**prior["plan"], "parent_version_id": prior["plan"]["id"]})
    content = plan_content(request)
    assert content.content_hash == prior["plan_content_hash"]
    assert Limits().model_dump() == prior["additional_run_limits_pending_consent"]
    value = {"version": "approved-missing-real-model-preview-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "filesystem_only_waiting_new_plan_binding_and_exact_execution_consent",
        "target": prior["target"], "baseline_source_archive_hash": prior["environment"]["verification_contract"]["binding"]["source_archive_hash"],
        "git_HEAD": manifest["base_HEAD"], "worktree_uncommitted": True,
        "runtime_source_hash": manifest["source_hash"], "runtime_source_files": len(manifest["files"]),
        "deployed_images": build["images"], "sandbox_policy_hash": prior["sandbox_policy_hash"],
        "runner_image_digest": prior["runner_image_digest"], "migration": "0045_source_file_observations",
        "model_profiles": view["model_profiles"], "model_identities": prior["model_identities"],
        "model_binding_hash": view["model_binding_hash"], "native_transport": transport,
        "native_transport_hash": content_hash(transport), "scheduler_policy": POLICY,
        "source_observation_version": "source-file-observation-v1", "preflight_version": "native-read-batch-preflight-v2",
        "candidate_plan_content": content.hash_payload(), "candidate_plan_content_hash": content.content_hash,
        "candidate_parent_reference": {"id": prior["plan"]["id"], "record_hash": prior["plan_record_hash"],
            "use": "read_only_parent_never_terminal_execution_authority"},
        "candidate_materialization_request_not_sent": request.model_dump(),
        "write_paths": prior["write_paths"], "commands": prior["commands"],
        "additional_run_limits_pending_new_consent": Limits().model_dump(), "review_bounds": prior["review_bounds"],
        "allowed_actions_only_after_new_exact_execution_consent": prior["allowed_actions_only_after_new_execution_consent"],
        "success_conditions": ["fresh preparation and all four Baseline commands pass",
            "real native investigation handles an OCI-attested approved missing file without terminal blockage",
            "real same-scope Patch, all four current-source Verify commands and independent evidence-bound Review",
            "stop at human final acceptance"],
        "not_found_scope": "Only OCI-confirmed approved absent paths with no expected file hash; never contents/hash or authority for other writes.",
        "coverage_condition": "If the model emits no applicable missing-path read/batch, record not_observed; do not automatically buy another run.",
        "execution_conditions": ["new human authorization for same-content plan materialization is required; do not auto-create Plan14",
            "then freeze actual new Plan/Binding/Preflight/Revision and present an exact execution preview for a fresh one-time confirmation",
            "recheck issue/title/body/base/source/images/models/transport/scopes/policy/budgets before dispatch",
            "no old Plan11/12/13 authority, checkpoint or spent/unknown budget may be reused/reset/refunded",
            "safe stop on genuine security/unknown/stale/hash failures or exhausted limits; no automatic extra paid round"],
        "new_plan_id": "not_created", "new_plan_hash": "not_created", "new_binding_hash": "not_created",
        "new_execution_authorization_hash": "not_created", "new_attempt_id": "not_created", "execution_request_body": None,
        "old_Plan13_checkpoint": old["checkpoints"]["session-11901711068ace4c72aa1410a8847453"],
        "planning_budget": legacy["planning_budget"], "historical_counts": legacy["counts"],
        "upstream_observation_hash": upstream["record_hash"], "legacy_compatibility_hash": legacy["report_hash"],
        "budget_note": "This only proposes a new separately consented bounded round with unchanged per-run and Review caps. No additional allocation is made; old Plan13 reads12 and all failed/unknown holds remain charged.",
        "new_model_calls": 0, "Plan14_created": False, "old_session_resumed": False,
        "execution_approved": False, "execution_started": False, "Push_PR_comments_publication": False,
        "HR4_HR5": "not_passed_by_offline_tests", "Linux_amd64": "not_run_still_open"}
    ensure_no_sensitive_data(value, context="new file-only real-model preview")
    value["proposal_hash"] = content_hash(value)
    with out.open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"proposal_hash": value["proposal_hash"], "execution_approved": False,
        "Plan14_created": False, "new_model_calls": 0}))


if __name__ == "__main__":
    main()
