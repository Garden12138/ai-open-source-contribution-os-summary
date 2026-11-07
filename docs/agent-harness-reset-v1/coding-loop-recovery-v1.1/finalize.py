"""Export local review material; never invokes services, models or Git writes."""
from __future__ import annotations

import base64
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import native_input_bound, native_model_transport, project_native_prompt
from app.agent_v2.coding_context import observe
from app.provenance import content_hash
from app.security import ensure_no_sensitive_bytes

ROOT = Path(__file__).resolve().parent
OLD = ROOT.parent / "coding-loop-recovery-v1/review-closeout-v1"
BASE = "b404a92326ca01efa7e64ab91ff9831b548c6562"
NEW = ["tests/test_coding_delivery_boundaries.py", "tests/test_coding_stop_boundaries.py",
    "tests/test_coding_stop_boundaries_oci.py", "tests/test_coding_working_set_boundaries.py",
    "tests/test_coding_recovery_v11.py", "tests/test_coding_recovery_v11_oci.py"]


def save(name: str, value: dict) -> None:
    value["report_hash"] = content_hash(value)
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare() -> tuple[dict, bytes]:
    manifest = json.loads((ROOT / "source-manifest.json").read_text())
    assert len(manifest["files"]) == 255
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    prior = json.loads((OLD / "source-manifest.json").read_text())
    assert Limits().model_dump() == prior["limits_unchanged"] == manifest["limits_unchanged"]
    assert subprocess.check_output(["git", "branch", "--show-current"], text=True).strip() == "refactor/agent-runtime-v2"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == manifest["starting_HEAD"]
    paths = subprocess.check_output(["git", "diff", "--name-only", BASE, "--", "app", "tests"], text=True).splitlines() + NEW
    paths = sorted(set(paths))
    diff = subprocess.check_output(["git", "diff", BASE, "--", "app", "tests"])
    for p in NEW:
        result = subprocess.run(["git", "diff", "--no-index", "--", "/dev/null", p], capture_output=True)
        assert result.returncode == 1
        diff += result.stdout
    (ROOT / "code.diff").write_bytes(diff)
    subprocess.run(["git", "apply", "--reverse", "--check", str(ROOT / "code.diff")], check=True)
    # Reconstruct the exact previous review snapshot without a checkout or index.
    with tempfile.TemporaryDirectory(prefix="coding-v11-closeout-") as temp:
        base = Path(temp)
        for p in sorted(set(paths) | set(prior["files"])):
            raw = subprocess.run(["git", "show", BASE + ":" + p], capture_output=True)
            if raw.returncode == 0:
                target = base / p; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw.stdout)
        subprocess.run(["git", "apply", str(OLD / "code.diff")], cwd=base, check=True)
        assert all(hashlib.sha256((base / p).read_bytes()).hexdigest() == h for p, h in prior["files"].items())
        delta = []
        for p in paths:
            old = (base / p).read_text() if (base / p).exists() else ""
            new = Path(p).read_text()
            if old != new:
                delta.append("diff --git a/" + p + " b/" + p + "\n")
                if not (base / p).exists(): delta.append("new file mode 100644\n")
                delta.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                    fromfile="a/" + p if (base / p).exists() else "/dev/null", tofile="b/" + p))
        (ROOT / "delta-from-review-closeout.diff").write_text("".join(delta))
        subprocess.run(["git", "apply", "--check", str(ROOT / "delta-from-review-closeout.diff")], cwd=base, check=True)
    for p in manifest["preserved_delivery_files"]:
        assert manifest["files"][p] == prior["files"][p]
    for command in ([".venv/bin/python", "-m", "compileall", "-q", "app"], ["git", "diff", "--check"]):
        subprocess.run(command, check=True)
    for p in NEW:
        check = subprocess.run(["git", "diff", "--no-index", "--check", "--", "/dev/null", p], capture_output=True)
        assert check.returncode == 1 and not check.stdout and not check.stderr
    return manifest, diff


def main() -> None:
    manifest, diff = prepare()
    export_path = ROOT.parent / "read-batch-scheduler-v1/output-capacity-v1/export_tests.py"
    spec = importlib.util.spec_from_file_location("v11_junit", export_path)
    exporter = importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)
    (ROOT / "test-results").mkdir(exist_ok=True)
    reports = []
    for name, category in [("focused-final", "deterministic_offline"), ("oci-final", "real_OCI_with_two_pure_units"),
            ("oci-compatibility", "real_OCI_plan_bound_volume"),
            ("final-full-0", "default_suite_shard"), ("final-full-1", "default_suite_shard")]:
        raw = (Path("/tmp") / ("coding-v11-" + name + ".xml")).read_bytes()
        tree = ET.fromstring(raw)
        for elem in tree.iter():
            elem.attrib.update({k: "local-test-host" if k == "hostname" else exporter.redacted(v) for k, v in elem.attrib.items()})
            if elem.text: elem.text = exporter.redacted(elem.text)
            if elem.tail: elem.tail = exporter.redacted(elem.tail)
        public = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
        assert exporter.stats(raw) == exporter.stats(public)
        a, b = ET.fromstring(raw).findall(".//testcase"), ET.fromstring(public).findall(".//testcase")
        assert [(exporter.redacted(x.get("classname", "")), exporter.redacted(x.get("name", ""))) for x in a] == [(x.get("classname", ""), x.get("name", "")) for x in b]
        assert exporter.stats(public)["errors"] == exporter.stats(public)["failed"] == 0
        if name.startswith("final-full-"):
            shard = json.loads((ROOT / ("regression-shard-" + name[-1] + ".json")).read_text())
            assert exporter.stats(public)["tests"] == shard["collected_tests"]
            assert shard["source_hash"] == manifest["source_hash"]
        target = ROOT / "test-results" / ("coding-v11-" + name + ".xml"); target.write_bytes(public)
        reports.append({"file": str(target.relative_to(ROOT)), "classification": category,
            "source_hash": manifest["source_hash"], "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "public_sha256": hashlib.sha256(public).hexdigest(), "outcomes_order_preserved": True, **exporter.stats(public)})
    shards = [json.loads((ROOT / ("regression-shard-" + str(i) + ".json")).read_text()) for i in (0, 1)]
    assert set(shards[0]["files"]).isdisjoint(shards[1]["files"])
    assert sum(s["collected_tests"] for s in shards) == shards[0]["all_default_tests"] == shards[1]["all_default_tests"]
    save("test-report-index.json", {"reports": reports, "overlapping_not_summed": True, "paid_model_calls": 0})
    scenarios = [json.loads(p.read_text()) for p in sorted(Path("/tmp/coding-v11-scenarios").glob("*.json"))]
    assert all(v["report_hash"] == content_hash({k:x for k,x in v.items() if k != "report_hash"}) for v in scenarios)
    save("scenarios.json", {"classification": "scripted_decisions_not_real_model_success", "cases": scenarios})
    from tests.test_coding_loop_recovery import archived_journal
    from types import SimpleNamespace
    frames = []
    for n in (7, 13):
        j, p, read = archived_journal(n); coding, pages = observe(j)
        p["implementation_progress"]["coding"] = coding
        p["feedback"] = [{**read(r["receipt_artifact"]), "receipt_artifact": r["receipt_artifact"]} for r in p["feedback"]]
        frame = project_native_prompt(p, SimpleNamespace(model_transport=native_model_transport()),
            task_input=read(p["task_context_artifact"]), source_pages=pages, journal=j,
            history_artifact=p.get("history_evidence", {}).get("artifact_id"))
        assert native_input_bound(frame) <= 32000
        frames.append({"round": n, "actual_model_dispatched": False, "input_bound": native_input_bound(frame),
            "inline_source_bytes": sum(len(r["text"].encode()) for r in frame["source_working_set"]),
            "selection": [{k:r[k] for k in ("path", "module", "priority", "receipt_artifact", "offset", "next_offset")} for r in frame["source_working_set"]]})
    previous_frames = json.loads((OLD / "saved-plan15-selection.json").read_text())["comparisons"]
    save("saved-plan15-selection.json", {"classification": "saved_prompt_projection_only", "frames": frames,
        "previous_closeout": previous_frames, "capacity_tradeoff": "per_page_provenance_and_fairness_within_unchanged_32000"})
    capacity = json.loads(Path("/tmp/coding-v11-oci-capacity.json").read_text())
    assert capacity["report_hash"] == content_hash({k:v for k,v in capacity.items() if k != "report_hash"})
    (ROOT / "oci-capacity.json").write_text(json.dumps(capacity, ensure_ascii=False, indent=2) + "\n")
    artifacts = list((ROOT / "saved-envelope-replay/artifacts").iterdir())
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == p.stem for p in artifacts)
    history = json.loads((ROOT / "history-plan11-15.json").read_text())
    preview = json.loads((ROOT.parent / "read-batch-scheduler-v1/plan15-preparation/execution-preview.json").read_text())
    assert preview["additional_run_limits_pending_consent"] == manifest["limits_unchanged"]
    for profile in preview["model_configuration"].values():
        assert profile["model"] == "deepseek-v4-pro" and profile["reasoning_effort"] == "high" and profile["max_tokens"] == 16384
    profile_sources_unchanged = all(Path(p).read_bytes() == subprocess.check_output(["git", "show", BASE + ":" + p])
        for p in ["app/model_settings.py", "app/providers/studio.py", "app/providers/nvidia_nim.py"])
    assert profile_sources_unchanged
    save("model-and-budget-check.json", {"profile_source_byte_identical_to_core": profile_sources_unchanged,
        "frozen_model_configuration": preview["model_configuration"], "native_output_per_call_unchanged": 4096,
        "native_input_bound_unchanged": 32000, "limits_unchanged": manifest["limits_unchanged"],
        "review_bounds_unchanged": preview["review_bounds"], "additional_conditional_call_max": 1,
        "additional_input_upper_bound": 32000, "additional_output_upper_bound": 4096,
        "allocation_of_new_budget": False, "old_Plan15_authorization_compatible": False,
        "fresh_freeze_binding_and_independent_exact_consent_required": True,
        "live_model_profiles_unchanged_by_45_table_query_only_audit": True})
    save("validation-report.json", {"source_hash": manifest["source_hash"], "reports": reports,
        "code_diff_sha256": hashlib.sha256(diff).hexdigest(), "closeout_reconstruction_verified": True,
        "shared_delivery_fix_unchanged": True, "profile_source_unchanged": True, "limits_unchanged": True,
        "history_report_hash": history["report_hash"], "saved_replay_artifacts_hash_verified": len(artifacts),
        "compileall_app": "passed", "diff_check": "passed", "database_schema_change": False, "native_protocol_changed": True,
        "new_paid_model_calls": 0, "new_live_Plans_or_approvals": 0, "restored_terminal_states": 0,
        "deployments_commits_push_PR_comments": 0, "linux_amd64": "not_run", "deployment": "NO_GO_pending_review_and_fresh_freeze"})
    print(json.dumps({"source_hash": manifest["source_hash"], "reports": [{"file":r["file"], **{k:r[k] for k in ("passed", "skipped", "failed", "errors")}} for r in reports]}))


def scan() -> None:
    """Match local secret values in memory; print paths/counts, never secrets."""
    values = dict(os.environ)
    env = Path(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); values[k.strip()] = v.strip().strip("\"'")
    needles = set()
    for k, v in values.items():
        if re.search(r"(?:API_KEY|TOKEN|SECRET|PASSWORD)", k, re.I) and len(v) >= 8:
            raw = v.encode(); needles.update([raw, base64.b64encode(raw), raw.hex().encode()])
    targets = [p for p in ROOT.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    targets += [Path(p) for p in NEW]
    targets += [Path(p) for p in json.loads((ROOT / "source-manifest.json").read_text())["changed_vs_closeout"]]
    errors = []
    for p in targets:
        raw = p.read_bytes()
        if any(n in raw for n in needles): errors.append(str(p))
        try: ensure_no_sensitive_bytes(raw, context="v11 scoped delivery")
        except ValueError: errors.append(str(p))
        if re.search(rb"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b", raw): errors.append(str(p))
    task_delta = subprocess.check_output(["git", "diff", "--", "docs/tasks.md"], text=True)
    added = "\n".join(line[1:] for line in task_delta.splitlines() if line.startswith("+") and not line.startswith("+++"))
    ensure_no_sensitive_bytes(added.encode(), context="changed roadmap lines")
    assert not any(n in added.encode() for n in needles)
    assert not errors, "Sensitive scan requires review: " + repr(sorted(set(errors)))
    save("sensitive-scan.json", {"scanned_files": len(targets), "local_secret_variants_checked": len(needles),
        "matched_credentials": 0, "raw_databases_in_delivery": 0, "unredacted_Provider_response_or_runtime_logs": 0,
        "fixture_source_pages": "exact_previously_sanitized_source_only"})


if __name__ == "__main__": main(); scan()
