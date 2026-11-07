"""Local evidence export only; no service, model, approval or Git mutation."""
from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

from app.agent_v2.coding_context import POLICY
from app.agent_v2.contracts import Limits
from app.agent_v2.native_tools import native_model_transport, native_input_bound, project_native_prompt
from app.provenance import content_hash
from app.security import ensure_no_sensitive_bytes

ROOT = Path(__file__).resolve().parent
BASE = "b404a92326ca01efa7e64ab91ff9831b548c6562"
NEW = ["tests/test_coding_delivery_boundaries.py", "tests/test_coding_stop_boundaries.py",
    "tests/test_coding_stop_boundaries_oci.py", "tests/test_coding_working_set_boundaries.py"]
REPORTS = ["delivery-before", "delivery-after", "stops", "projection", "stops-oci", "stops-oci-final", "regression", "final-boundaries"]


def save(name: str, value: dict) -> None:
    value["report_hash"] = content_hash(value)
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    assert subprocess.check_output(["git", "branch", "--show-current"], text=True).strip() == "refactor/agent-runtime-v2"
    old = json.loads((ROOT.parent / "source-manifest.json").read_text())
    files = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path("app").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    changed = [p for p, h in files.items() if old["files"].get(p) != h]
    assert set(changed) == {"app/agent_v2/coding_context.py", "app/agent_v2/model_receipts.py", "app/agent_v2/read_progress.py"}
    prior = subprocess.check_output(["git", "show", BASE + ":app/agent_v2/coding_context.py"], text=True)
    node = next(n for n in ast.parse(prior).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "POLICY" for t in n.targets))
    assert ast.literal_eval(node.value) == POLICY and Limits().model_dump() == old["limits"]
    for name in ("app/agent_v2/loop.py", "app/agent_v2/native_tools.py", "app/agent_v2/contracts.py"):
        assert Path(name).read_bytes() == subprocess.check_output(["git", "show", BASE + ":" + name])
    for cmd in ([".venv/bin/python", "-m", "compileall", "-q", "app"], ["git", "diff", "--check"]):
        subprocess.run(cmd, check=True)
    diff = subprocess.check_output(["git", "diff", BASE, "--", "app", "tests"])
    for name in NEW:
        value = subprocess.run(["git", "diff", "--no-index", "--", "/dev/null", name], capture_output=True)
        assert value.returncode == 1
        diff += value.stdout
        whitespace = subprocess.run(["git", "diff", "--no-index", "--check", "--", "/dev/null", name], capture_output=True)
        assert not whitespace.stdout and not whitespace.stderr
    (ROOT / "code.diff").write_bytes(diff)
    subprocess.run(["git", "apply", "--reverse", "--check", str(ROOT / "code.diff")], check=True)
    spec = importlib.util.spec_from_file_location("closeout_junit", ROOT.parent.parent / "read-batch-scheduler-v1/output-capacity-v1/export_tests.py")
    exporter = importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)
    reports = []
    for suffix in REPORTS:
        name = "coding-closeout-" + suffix + ".xml"
        raw = (Path("/tmp") / name).read_bytes(); tree = ET.fromstring(raw)
        for elem in tree.iter():
            elem.attrib.update({k: "local-test-host" if k == "hostname" else exporter.redacted(v) for k, v in elem.attrib.items()})
            if elem.text: elem.text = exporter.redacted(elem.text)
            if elem.tail: elem.tail = exporter.redacted(elem.tail)
        public = ET.tostring(tree, encoding="utf-8", xml_declaration=True)
        assert exporter.stats(raw) == exporter.stats(public)
        a, b = ET.fromstring(raw).findall(".//testcase"), ET.fromstring(public).findall(".//testcase")
        assert [(exporter.redacted(x.get("classname", "")), exporter.redacted(x.get("name", ""))) for x in a] == [(x.get("classname", ""), x.get("name", "")) for x in b]
        (ROOT / "test-results" / name).write_bytes(public)
        reports.append({"file": "test-results/" + name, "classification":
            "pre_fix_expected_failures" if suffix == "delivery-before" else "development_fixture_root_mismatch" if suffix == "stops-oci" else
            "real_offline_OCI_synthetic_authority" if suffix == "stops-oci-final" else "deterministic_no_real_model",
            "source_hash": content_hash(files) if suffix not in {"delivery-before", "stops-oci"} else None,
            "raw_sha256": hashlib.sha256(raw).hexdigest(), "public_sha256": hashlib.sha256(public).hexdigest(),
            "outcomes_order_preserved": True, **exporter.stats(public)})
    for suffix, count in (("regression", 179), ("final-boundaries", 24), ("stops-oci-final", 2)):
        row = next(r for r in reports if r["file"].endswith("-" + suffix + ".xml"))
        assert row["passed"] == row["tests"] == count and row["failed"] == row["errors"] == row["skipped"] == 0
    save("test-report-index.json", {"reports": reports, "overlap_not_summed": True, "paid_model_calls": 0})
    scenarios = []
    for p in sorted(Path("/tmp/coding-closeout-boundaries").glob("*.json")):
        value = json.loads(p.read_text())
        assert value["report_hash"] == content_hash({k:v for k,v in value.items() if k != "report_hash"})
        scenarios.append(value)
    assert len(scenarios) == 10
    save("scenarios.json", {"classification": "offline_reproductions_scripted_decisions_no_real_model_success", "cases": scenarios})
    # Re-evaluate the actual saved Plan15 frames without changing their records.
    from tests.test_coding_loop_recovery import archived_journal
    from app.agent_v2.coding_context import observe
    from types import SimpleNamespace
    archived = []
    for ordinal in (7, 13):
        j, prompt, read = archived_journal(ordinal); coding, pages = observe(j)
        prompt["implementation_progress"]["coding"] = coding
        prompt["feedback"] = [{**read(r["receipt_artifact"]), "receipt_artifact": r["receipt_artifact"]} for r in prompt["feedback"]]
        frame = project_native_prompt(prompt, SimpleNamespace(model_transport=native_model_transport()),
            task_input=read(prompt["task_context_artifact"]), source_pages=pages,
            history_artifact=prompt.get("history_evidence", {}).get("artifact_id"))
        assert native_input_bound(frame) <= 32000
        archived.append({"saved_round": ordinal, "classification": "diagnostic_only_no_dispatch", "paths": [p["path"] for p in frame["source_working_set"]],
            "input_bound": native_input_bound(frame), "inline_source_bytes": sum(len(p["text"].encode()) for p in frame["source_working_set"])})
    save("saved-plan15-selection.json", {"comparisons": archived})
    save("source-manifest.json", {"starting_HEAD": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "reviewed_core_commit": BASE, "files": files, "source_hash": content_hash(files), "changed_production_paths": changed,
        "native_transport": native_model_transport().model_dump(), "native_transport_hash": content_hash(native_model_transport().model_dump()),
        "coding_policy_unchanged": POLICY, "limits_unchanged": Limits().model_dump()})
    history = json.loads((ROOT / "history-plan11-15.json").read_text())
    save("validation-report.json", {"source_hash": content_hash(files), "code_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "related_regression_passed": 179, "final_boundary_passed": 24, "overlapping_not_summed": True, "actual_OCI_passed": 2,
        "delivery_witness_fix": "verified", "hard_stop_and_selection_changed": False, "coding_policy_6_8_unchanged": True,
        "limits_unchanged": True, "unknown_outcome_safety_stop": "retained_with_and_without_response",
        "history_report_hash": history["report_hash"], "all_45_tables_and_11_checkpoints_unchanged": True,
        "new_paid_model_calls": 0, "new_live_Plans_or_approvals": 0, "restored_old_terminal_states": 0,
        "deployments": 0, "git_commits_push_PR_comments": False, "compileall_app": "passed", "diff_check": "passed",
        "Linux_amd64": "not_run", "deployment": "NO_GO_pending_policy_decision_and_review",
        "real_autonomous_Coding_success": "not_proven", "candidate_grace_policy": "design_only_not_implemented"})
    values = dict(os.environ)
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1); values[k.strip()] = v.strip().strip("\"'")
    secrets = {v for k,v in values.items() if len(v) >= 16 and any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "SIGNING_KEY")) and not k.endswith(("_FILE", "_PATH", "_ENV_NAME"))}
    variants = {s for v in secrets for s in (v, base64.b64encode(v.encode()).decode(), base64.urlsafe_b64encode(v.encode()).decode().rstrip("="), v.encode().hex())}
    credential = re.compile(rb'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,})|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
    inventory = []
    for path in [*ROOT.rglob("*"), *map(Path, NEW), *map(Path, changed), Path("tests/test_coding_loop_recovery.py")]:
        if not path.is_file() or "__pycache__" in path.parts or path.name == "delivery-check.json": continue
        raw = path.read_bytes()
        ensure_no_sensitive_bytes(raw, secrets=list(variants), context=path.name)
        assert not credential.search(raw), path.name
        inventory.append({"path": str(path.relative_to(Path.cwd())) if path.is_absolute() else str(path),
            "sha256": hashlib.sha256(raw).hexdigest()})
    save("delivery-check.json", {"source_hash": content_hash(files), "files": inventory, "configured_secret_and_variant_scan": "passed",
        "credential_patterns": "passed", "raw_DB_or_private_Provider_bodies_exported": False, "source_remains_uncommitted": True})
    print(json.dumps({"source_hash": content_hash(files), "related": 179, "final_boundary": 24, "actual_OCI": 2, "credential_scan": "passed", "deployment": "NO_GO"}))


if __name__ == "__main__": main()
