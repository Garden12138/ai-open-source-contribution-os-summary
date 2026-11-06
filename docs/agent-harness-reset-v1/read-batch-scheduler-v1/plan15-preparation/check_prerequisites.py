"""Read-only Plan15 freeze checks. No approval, model request or execution endpoint."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import httpx

from app.agent_v2.contracts import Limits, TaskSpec, ToolCall, ToolResult
from app.agent_v2.native_tools import NATIVE_INPUT_LIMIT, NATIVE_PROMPT_VERSION, native_model_transport
from app.agent_v2.read_batches import POLICY, parse_preflight
from app.agent_v2.read_capacity import VERSION, certificate, observation_matches, size, validate_claim
from app.agent_v2.tools import RestrictedToolRegistry
from app.planner import plan_content
from app.provenance import content_hash
from app.schemas import PlanVersionCreateRequest
from app.security import ensure_no_sensitive_data
from app.sandbox_worker.specs import SandboxPolicy

ROOT = Path(__file__).resolve().parent
CAP = ROOT.parent / "output-capacity-v1"
TASK = "f0e017fa-d2a6-4eb8-8a94-32bb1d29586c"
HEAD = "6b335c173f2d76f0872d4776f3e5ab87334d5c5f"
SOURCE = "31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95"
spec = importlib.util.spec_from_file_location("plan14_readers", ROOT.parent / "plan14-preparation/prepare_plan.py")
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)
docker_read, get_view, checked_json, check_preserved = prior.docker_read, prior.get_view, prior.checked_json, prior.check_preserved


def save(name: str, value: dict) -> None:
    ensure_no_sensitive_data(value, context=name)
    with (ROOT / name).open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def local_checks() -> dict:
    assert subprocess.check_output(["git", "branch", "--show-current"], text=True).strip() == "refactor/agent-runtime-v2"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == HEAD
    manifest = json.loads((CAP / "source-manifest.json").read_text())
    assert content_hash(manifest["files"]) == manifest["source_hash"] == SOURCE
    actual = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path("app").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    assert actual == manifest["files"] and len(actual) == 254
    for path, digest in actual.items():
        assert hashlib.sha256(subprocess.check_output(["git", "show", HEAD + ":" + path])).hexdigest() == digest, path
    assert not subprocess.check_output(["git", "diff", "928e6b719b1fb3985aee2a78e4973666ecda55e3", HEAD, "--", "app"])
    validation = checked_json(CAP / "validation-report.json", "report_hash")
    index = checked_json(CAP / "test-report-index.json", "report_hash")
    for row in index["reports"]:
        raw = (CAP / row["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["public_sha256"], row["file"]
        cases = ET.fromstring(raw).findall(".//testcase")
        assert len(cases) == row["tests"]
        for element, key in (("failure", "failed"), ("error", "errors"), ("skipped", "skipped")):
            assert sum(c.find(element) is not None for c in cases) == row[key], row["file"]
    assert validation["source_hash"] == SOURCE and validation["app_files"] == 254
    assert validation["full_regression"]["passed"] == 2047 and validation["full_regression"]["failures"] == 0
    assert validation["OCI"]["suite_passed"] == 25 and validation["OCI"]["actual_OCI_cases"] == 23
    for name in validation["full_regression"]["reports"] + validation["supplemental"]["reports"] + [validation["OCI"]["report"]]:
        cases = ET.parse(CAP / name).getroot().findall(".//testcase")
        assert not any(c.find("failure") is not None or c.find("error") is not None for c in cases), name
    replay = checked_json(CAP / "offline-replay.json", "report_hash")
    assert replay["new_paid_model_requests"] == 0
    assert [r["child_sizes"] for r in replay["replays"]] == [[4, 2], [4, 1], [4, 1], [4, 4], [4, 4]]
    assert replay["replays"][3]["model_visible_statuses"] == ["succeeded"] * 7 + ["not_found"]
    for row in replay["replays"]:
        assert row["provider_requests"] == 0 and row["limits"] == Limits().model_dump()
        assert row["billing_conservation_verified"] and not row["historical_approval_reused"]
        assert row["usage"]["output_bytes"] == row["model_envelope_bytes"] + sum(c["bytes"] for c in row["output_charges"])
        for charge in row["output_charges"]:
            aid = charge["artifact_id"]
            extension = ".txt" if charge["role"] == "source_page" else ".json"
            raw = (CAP / "artifacts" / (aid + extension)).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == aid and len(raw) == charge["bytes"]
    oci = checked_json(CAP / "plan14-real-oci-final.json", "report_hash")
    binding = checked_json(CAP / "oci-capacity-binding.json", "report_hash")
    assert binding["actual_OCI_report_hash"] == oci["report_hash"]
    spec = TaskSpec.model_validate(binding["spec"])
    proof = parse_preflight(oci["proof"])
    batch = binding["batch"]
    computed = certificate(batch, proof, batch_artifact=content_hash(batch), proof_artifact=content_hash(proof),
        remaining=oci["certificate"]["remaining_output_bytes"])
    assert computed == oci["certificate"] and computed["status"] == "succeeded"
    assert computed["total_bytes_bound"] == 228743 <= computed["remaining_output_bytes"] == 459508
    assert oci["provider_requests"] == 0 and oci["source_page_bytes"] == 68338
    for call, check, result, entry in zip(batch["calls"], proof["checked"], oci["results"], computed["calls"], strict=True):
        args = RestrictedToolRegistry().validate(ToolCall.model_validate(call), spec).model_dump()
        validate_claim(check, args, spec, proof["source_hash"])
        assert observation_matches(ToolResult.model_validate(result), entry)
        assert size(result) <= entry["result_bytes_bound"]
    coverage = checked_json(CAP / "model-visible-source-coverage.json", "report_hash")
    assert coverage["native_input_bound"] == 31027 <= NATIVE_INPUT_LIMIT == 32000
    assert coverage["inline_source_bytes"] == 1024 and coverage["returned_source_bytes"] == 68338
    assert coverage["focused_artifact_recovery_rounds_total"] == 10 and coverage["remaining_file_tail_read_file_calls"] == 2
    assert VERSION == "native-read-capacity-v1" and NATIVE_PROMPT_VERSION == "native-read-capacity-v9"
    for path in (CAP / "artifacts").iterdir():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem, path.name
    old = checked_json(ROOT.parent / "plan14-preparation/execution-preview.json", "proposal_hash")
    assert Limits().model_dump() == old["additional_run_limits_pending_consent"] == validation["safety"]["all_limits"]
    assert old["write_paths"] == validation["safety"]["write_paths_unchanged"] and len(old["write_paths"]) == 8
    assert old["commands"] == validation["safety"]["commands_unchanged"] and len(old["commands"]) == 4
    assert SandboxPolicy().policy_hash == old["sandbox_policy_hash"] == validation["safety"]["sandbox_policy_hash"]
    plan = PlanVersionCreateRequest.model_validate(old["plan"])
    assert content_hash(plan_content(plan).hash_payload()) == old["plan_content_hash"]
    return {"git_HEAD": HEAD, "runtime_source_hash": SOURCE, "runtime_source_files": 254,
        "archived_validation_hash": validation["report_hash"], "test_report_index_hash": index["report_hash"],
        "archived_replay_hash": replay["report_hash"], "OCI_report_hash": oci["report_hash"],
        "OCI_binding_hash": binding["report_hash"], "capacity_certificate_hash": content_hash(computed),
        "capacity_bytes_bound": 228743, "capacity_remaining_bytes": 459508, "source_page_bytes": 68338,
        "source_coverage_report_hash": coverage["report_hash"], "native_input_bound": 31027,
        "prompt_projection_version": NATIVE_PROMPT_VERSION, "capacity_contract": VERSION,
        "scheduler_policy": POLICY, "native_transport": native_model_transport().model_dump(),
        "native_transport_hash": content_hash(native_model_transport().model_dump()),
        "all_archived_artifact_hashes_verified": True, "all_published_JUnit_hashes_and_outcomes_verified": True}


def deployment_checks() -> dict:
    deployment = json.loads((CAP / "deployment-report.json").read_text())
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())["roles"]
    assert roles == deployment["new_roles"]
    manifest = json.loads((CAP / "source-manifest.json").read_text())
    for role, item in roles.items():
        info = json.loads(subprocess.check_output(["docker", "inspect", item["id"]]))[0]
        old = json.loads(subprocess.check_output(["docker", "inspect", deployment["old_roles_preserved_stopped"][role]]))[0]
        assert info["State"]["Running"] and info["Image"] == deployment["images"]["worker" if role == "sandbox-worker" else "app"]
        assert info["HostConfig"] == old["HostConfig"] and info["Mounts"] == old["Mounts"]
        assert all(info["Config"][k] == old["Config"][k] for k in ("Env", "User", "Entrypoint", "Cmd", "WorkingDir"))
        code = """import hashlib,json,sys
from pathlib import Path
expected=json.loads(sys.argv[1])
actual={str(p.relative_to('/app')):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('/app/app').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
assert actual==expected
"""
        subprocess.run(["docker", "exec", item["id"], "python", "-c", code, json.dumps(manifest["files"])], check=True)
    return deployment


def basis_checks(state: dict, view: dict) -> None:
    old = checked_json(ROOT.parent / "plan14-preparation/execution-preview.json", "proposal_hash")
    prior.basis_checks(old, state, view)
    assert state["model_profiles"] == old["model_profiles"] and state["model_identities"] == old["model_identities"]
    assert view["model_binding_hash"] == old["model_binding_hash"]
    assert state["runner_image"] == old["runner_image_digest"]
    assert view["context"]["record_hash"] == old["context_hash"]


def main() -> None:
    result = local_checks()
    upstream = checked_json(ROOT / "upstream-check.json", "record_hash")
    assert not upstream["changed_fields"] and upstream["issue_body_matches"] and upstream["baseline_matches"]
    deployment = deployment_checks()
    state = docker_read(deployment["new_roles"]["serve"]["id"])
    legacy = json.loads((CAP / "legacy-after.json").read_text())
    assert state == legacy, "Business state changed since offline acceptance; stop before binding"
    with httpx.Client(base_url="http://127.0.0.1:18090", timeout=30, trust_env=False) as client:
        view = get_view(client)
        basis_checks(state, view)
        assert view["state"] == "executing" and not view["busy"]
        health = {}
        for port in (18090, 18091, 18092, 18080):
            r = client.get(f"http://127.0.0.1:{port}/health"); r.raise_for_status()
            health[str(port)] = r.json(); assert r.json()["status"] == "ok"
    raw = Path("/tmp/plan15-preparation-offline.xml").read_bytes()
    tree = ET.fromstring(raw)
    cases = tree.findall(".//testcase")
    assert cases and not any(c.find("failure") is not None or c.find("error") is not None or c.find("skipped") is not None for c in cases)
    # Keep outcomes and case order; omit local paths, hostnames and timestamps.
    for suite in tree.findall(".//testsuite"):
        suite.attrib.pop("hostname", None); suite.attrib.pop("timestamp", None)
    public = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
    ensure_no_sensitive_data(public.decode(), context="Plan15 offline JUnit")
    with (ROOT / "offline-regression.xml").open("xb") as target: target.write(public)
    save("before-state.json", state)
    save("before-view.json", view)
    result.update(version="output-capacity-plan15-prerequisites-v1", recorded_at=datetime.now(timezone.utc).isoformat(),
        all_frozen_inputs_match=True, source_matches_latest_commit_and_five_deployed_roles=True,
        deployed_images=deployment["images"], health=health, counts=state["counts"],
        all_business_rows_identical_to_offline_acceptance=True, old_checkpoints_unchanged=len(state["checkpoints"]),
        upstream_check_hash=upstream["record_hash"], fresh_offline_regression={"passed": len(cases), "failed": 0,
            "skipped": 0, "raw_sha256": hashlib.sha256(raw).hexdigest(), "public_sha256": hashlib.sha256(public).hexdigest(),
            "file": "offline-regression.xml", "classification": "deterministic_no_network_no_real_model"},
        model_calls=0, new_Plan_created=False, new_binding_created=False, new_execution_approval=False,
        native_Linux_amd64="not_run_still_open", HR4_HR5="not_passed")
    result["report_hash"] = content_hash(result)
    save("prerequisite-check.json", result)
    print(json.dumps({k: result[k] for k in ("report_hash", "all_frozen_inputs_match", "counts", "fresh_offline_regression", "native_transport_hash")}))


if __name__ == "__main__":
    main()
