"""Export redacted offline evidence and a reviewable local diff; no Git writes."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

from app.agent_v2.native_tools import native_model_transport
from app.provenance import content_hash
from app.security import ensure_no_sensitive_bytes

ROOT = Path(__file__).resolve().parent
FILES = ["focused", "final-delta", "oci", "oci-delta", "warning-compat", "full", "final-full-0", "final-full-1"]
NEW_CODE = ["app/agent_v2/coding_context.py", "tests/test_coding_loop_recovery.py", "tests/test_coding_loop_recovery_oci.py"]


def write(name: str, value: dict) -> None:
    value.pop("report_hash", None)
    value["report_hash"] = content_hash(value)
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    manifest = json.loads((ROOT / "source-manifest.json").read_text())
    actual = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path("app").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    assert actual == manifest["files"] and content_hash(actual) == manifest["source_hash"]
    subprocess.run([".venv/bin/python", "-m", "compileall", "-q", "app"], check=True)
    subprocess.run(["node", "--check", "app/static/workbench.js"], check=True)
    subprocess.run(["git", "diff", "--check"], check=True)
    assert not subprocess.check_output(["git", "diff", "--name-only", "--", "docs/agent-harness-reset-v1/read-batch-scheduler-v1"])
    exporter = ROOT.parent / "read-batch-scheduler-v1/output-capacity-v1/export_tests.py"
    spec = importlib.util.spec_from_file_location("test_export", exporter)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    reports = []
    for suffix in FILES:
        name = "coding-loop-recovery-" + suffix + ".xml"
        raw = (Path("/tmp") / name).read_bytes()
        tree = ET.fromstring(raw)
        for node in tree.iter():
            node.attrib.update({k: ("local-test-host" if k == "hostname" else module.redacted(v)) for k, v in node.attrib.items()})
            if node.text: node.text = module.redacted(node.text)
            if node.tail: node.tail = module.redacted(node.tail)
        public = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
        assert module.stats(raw) == module.stats(public)
        before = ET.fromstring(raw).findall(".//testcase")
        after = ET.fromstring(public).findall(".//testcase")
        assert [(module.redacted(c.get("classname", "")), module.redacted(c.get("name", ""))) for c in before] == [(c.get("classname", ""), c.get("name", "")) for c in after]
        (ROOT / "test-results" / name).write_bytes(public)
        final = suffix.startswith("final-full-") or suffix == "warning-compat"
        reports.append({"file": "test-results/" + name, "classification": "frozen_source_deterministic" if final else
            "real_OCI_fixed_unchanged_runner" if suffix.startswith("oci") else "development_including_preserved_failure",
            "source_hash": manifest["source_hash"] if final else None,
            "raw_sha256": hashlib.sha256(raw).hexdigest(), "public_sha256": hashlib.sha256(public).hexdigest(),
            "outcomes_and_order_unchanged": True, **module.stats(public)})
    frozen = [r for r in reports if "final-full-" in r["file"]]
    assert len(frozen) == 2 and all(r["failed"] == r["errors"] == 0 for r in frozen)
    shards = [json.loads((ROOT / ("regression-shard-" + str(i) + ".json")).read_text()) for i in (0, 1)]
    assert not set(shards[0]["files"]) & set(shards[1]["files"])
    assert sum(r["tests"] for r in frozen) == shards[0]["all_default_tests"] == shards[1]["all_default_tests"]
    write("test-report-index.json", {"reports": reports, "paid_calls": 0,
        "metadata_redactions": ["home", "temporary_directory", "email", "hostname"],
        "development_failure_preserved": "Duplicate warning delivery observer fixed; original failed report retained"})
    diff = subprocess.check_output(["git", "diff", "--", "app", "tests"])
    for name in NEW_CODE:
        result = subprocess.run(["git", "diff", "--no-index", "--", "/dev/null", name], capture_output=True)
        assert result.returncode == 1
        diff += result.stdout
    (ROOT / "code.diff").write_bytes(diff)
    preview = json.loads((ROOT.parent / "read-batch-scheduler-v1/plan15-preparation/execution-preview.json").read_text())
    oci = json.loads(Path("/tmp/coding-recovery-capacity-oci.json").read_text())
    write("oci-capacity.json", oci)
    full = {k: sum(r[k] for r in frozen) for k in ("tests", "passed", "failed", "errors", "skipped")}
    oci_reports = [r for r in reports if r["classification"] == "real_OCI_fixed_unchanged_runner"]
    assert all(r["failed"] == r["errors"] == r["skipped"] == 0 for r in oci_reports)
    assert not subprocess.check_output(["git", "diff", "--", "app/sandbox_worker", "app/agent_v2/read_capacity.py"])
    write("validation-report.json", {"version": "coding-loop-recovery-offline-validation-v1",
        "source_hash": manifest["source_hash"], "app_files": len(actual), "git_HEAD": manifest["git_HEAD"],
        "default_regression": full, "default_suite_partition_complete": True,
        "OCI": {"suite_cases_passed": sum(r["passed"] for r in oci_reports), "actual_OCI_cases": 24,
            "deterministic_unit_cases_in_OCI_suite": 2, "runner_digest": oci["runner_image"],
            "sandbox_and_capacity_source_unchanged_from_HEAD": True,
            "platform": "macOS arm64 / OrbStack Linux arm64", "Linux_amd64": "not_run"},
        "limits_unchanged": manifest["limits"], "native_input_bound": 32000, "per_call_output_limit": 4096,
        "review_bounds_unchanged": preview["review_bounds"], "sandbox_policy_unchanged": preview["sandbox_policy"],
        "model_configuration_unchanged": preview["model_configuration"],
        "native_transport": native_model_transport().model_dump(), "native_transport_hash": content_hash(native_model_transport().model_dump()),
        "old_native_transport_hash": preview["model_transport_hash"],
        "code_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "compileall": "passed", "javascript_syntax": "passed", "git_diff_check": "passed",
        "historical_committed_evidence_unchanged": True, "new_paid_requests": 0,
        "new_plans_or_live_approvals": 0, "resumed_Plan15": False, "deployed_candidate": False,
        "independent_Review": "deterministic_gate_passed_real_model_not_run",
        "real_autonomous_Coding_success_proven": False, "next_paid_acceptance": "NO_GO_pending_code_review_and_new_exact_authorization"})
    values = dict(os.environ)
    if Path(".env").exists():
        for line in Path(".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); values[k.strip()] = v.strip().strip("\"'")
    secrets = {v for k, v in values.items() if len(v) >= 16 and any(s in k.upper() for s in
        ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "SIGNING_KEY")) and not k.endswith(("_FILE", "_PATH", "_ENV_NAME"))}
    variants = {s for value in secrets for s in (value, base64.b64encode(value.encode()).decode(), value.encode().hex())}
    inventory = []
    for path in [*ROOT.rglob("*"), *map(Path, NEW_CODE)]:
        if not path.is_file() or "__pycache__" in path.parts or path.name == "delivery-check.json": continue
        raw = path.read_bytes()
        ensure_no_sensitive_bytes(raw, secrets=list(variants), context=path.name)
        assert not re.search(rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)", raw), path.name
        if path.parent.name == "artifacts": assert hashlib.sha256(raw).hexdigest() == path.stem
        inventory.append({"file": str(path.relative_to(Path.cwd())) if path.is_absolute() else str(path), "sha256": hashlib.sha256(raw).hexdigest()})
    write("delivery-check.json", {"source_hash": manifest["source_hash"], "files": inventory,
        "configured_secret_and_encoded_variant_scan": "passed", "credential_pattern_scan": "passed",
        "original_archived_artifacts_not_modified": True, "Git_commit_push_PR_or_comments": False})
    print(json.dumps({"source_hash": manifest["source_hash"], "default": full, "OCI_actual": 24,
        "credential_scan": "passed", "paid_calls": 0}))


if __name__ == "__main__": main()
