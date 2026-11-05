"""Build a filesystem-only future acceptance intent from verified, read-only inputs."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import httpx

from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import native_model_transport
from app.agent_v2.read_batches import POLICY
from app.plans import PlanCommand, PlanContent
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_data


def main() -> None:
    records = Path(__file__).resolve().parent
    out = records / "real-model-acceptance-preview.json"
    assert not out.exists(), "Preserve an already issued preview"
    prior = json.loads((records.parent / "real-acceptance-preview.json").read_text())
    manifest = json.loads((records / "source-manifest.json").read_text())
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    audit = json.loads((records / "postdeploy-audit.json").read_text())
    build = json.loads((records / "build-report.json").read_text())
    upstream = json.loads((records / "upstream-before-preview.json").read_text())
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        response = client.get(f"/api/v1/tasks/{prior['task_id']}/workbench")
        response.raise_for_status()
        view = response.json()
    transport = native_model_transport().model_dump()
    assert view["model_binding_hash"] == prior["model_binding_hash"]
    # The workbench displays the immutable latest plan binding, not a new
    # approval for the deployed code. Its historical transport must stay old.
    assert view["execution"]["model_transport"] == prior["model_transport"]
    assert view["execution"]["model_transport_hash"] == prior["model_transport_hash"]
    assert prior["model_transport"] != transport
    assert view["execution"]["planning_budget"] == audit["planning_budget"]
    assert Limits().model_dump() == prior["new_run_requested_limits"]
    candidate = {k: prior["plan"][k] for k in PlanVersionCreateRequest.model_fields if k != "parent_version_id"}
    candidate["parent_version_id"] = prior["plan"]["id"]
    request = PlanVersionCreateRequest.model_validate(candidate)
    content = PlanContent(**{k: tuple(PlanCommand(**{**c, "argv": tuple(c["argv"])}) for c in v)
        if k == "commands_to_run" else tuple(v) if isinstance(v, list) else v
        for k, v in request.model_dump().items() if k != "parent_version_id"})
    assert content.content_hash == prior["plan"]["content_hash"]
    value = {
        "version": "native-read-batch-real-model-preview-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "filesystem_preview_only_requires_new_authorization_and_fresh_exact_binding",
        "execution_approved": False, "new_model_calls": 0, "new_plan_created": False,
        "target": {k: prior[k] for k in ("task_id", "repository", "issue_url", "base_sha", "snapshot_id", "snapshot_hash")},
        "source_archive_hash": prior["environment"]["verification_contract"]["binding"]["source_archive_hash"],
        "upstream_observation": {"file": "upstream-before-preview.json", "record_hash": upstream["record_hash"]},
        "runtime_source_hash": manifest["source_hash"], "runtime_source_files": len(manifest["files"]),
        "images": build["images"], "sandbox_policy_hash": prior["sandbox_policy_hash"],
        "native_transport": transport, "native_transport_hash": content_hash(transport), "scheduler_policy": POLICY,
        "historical_Plan12_transport": prior["model_transport"],
        "historical_Plan12_transport_hash": prior["model_transport_hash"],
        "model_profiles": view["model_profiles"], "model_identities": prior["model_identities"],
        "model_binding_hash": view["model_binding_hash"],
        "candidate_plan_content": content.hash_payload(), "candidate_plan_content_hash": content.content_hash,
        "candidate_parent_reference": {"id": prior["plan"]["id"], "record_hash": prior["plan"]["record_hash"],
            "use": "read_only_parent_reference_never_reuse_execution_authority"},
        "candidate_plan_materialization_request": request.model_dump(),
        "write_paths": prior["write_paths"], "commands": prior["commands"],
        "candidate_additional_run_limits": Limits().model_dump(), "review_bounds": prior["review_bounds"],
        "allowed_actions_after_fresh_exact_execution_consent": prior["allowed_actions"],
        "expected_batch_behavior": "only_all_valid_read_file_5_to_8_in_children_max4_no_model_between_children",
        "success_conditions": ["new preparation and four Baseline checks pass",
            "native Investigation reaches real same-scope Patch",
            "all four approved checks pass on current patched source",
            "independent evidence-bound Review completes", "stop at Human Acceptance"],
        "batch_coverage_condition": "If the model emits no 5-to-8 read_file proposal, report real batch coverage as not_observed; never buy an automatic retry to force it.",
        "freshness_requirements": ["recheck upstream issue body and exact base SHA",
            "same source manifest, images, policy, transport, model identity, candidate content, scopes and limits",
            "new plan binding and one-time execution consent; terminal Plan12 is never resumed",
            "new preparation/Baseline/verification contract bound to the new immutable plan record",
            "all unknown historical holds and already charged usage retained; no automatic paid planning"],
        "authorization_staging": {
            "reason": "The native transport hash changed. freeze_spec rejects the old Plan12 transport and authorization; terminal checkpoints cannot be resumed. The user also forbids automatic Plan13 creation.",
            "next_user_decision": "Whether to permit materializing exactly this candidate content under the new transport. This file alone grants no authority.",
            "after_materialization": "Generate an exact execution preview containing the actual new plan/binding/authorization inputs and revision; paid dispatch waits for its fresh one-time confirmation.",
            "materialization_itself_uses_model": False,
            "new_plan_id": "not_created", "new_plan_record_hash": "not_created",
            "new_plan_binding_hash": "not_created", "new_authorization_hash": "not_created",
            "new_attempt_id": "not_created", "new_verification_contract_hash": "not_created",
        },
        "execution_request_body": None,
        "future_diff_test_review_hashes": "not_created_until_future_execution",
        "historical_budget": audit["planning_budget"], "historical_counts": audit["counts"],
        "budget_note": "The candidate is an additional bounded run requiring fresh consent, not a reset/refund/top-up of Plan12 or planning. Existing root/profile limits remain unchanged; independent Review retains its separate existing bounds.",
        "forbidden_actions": ["reuse Plan12 approval", "resume terminal Plan12", "automatic Plan13 creation",
            "paid calls before new exact consent", "automatic extra paid reruns", "planning budget increase",
            "write outside the eight paths", "new check commands", "sandbox network or credential expansion",
            "Push", "PR", "comments", "publication"],
        "Linux_amd64_acceptance": "not_run_original_gate_still_open",
        "original_HR4_HR5": "not_passed",
    }
    ensure_no_sensitive_data(value, context="independent real-model acceptance preview")
    value["proposal_hash"] = content_hash(value)
    with out.open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"proposal_hash": value["proposal_hash"], "new_model_calls": 0,
        "new_plan_created": False, "executable_request": False}))


if __name__ == "__main__":
    main()
