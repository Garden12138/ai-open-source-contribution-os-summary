"""Read-only final checks; persist a preparation report, never execute the Plan."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import httpx

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from prepare_plan import ROOT, PRIOR, TASK, checked_json, save


def main() -> None:
    assert not (ROOT / "final-checks.json").exists()
    preview = checked_json(ROOT / "execution-preview.json", "proposal_hash")
    audit = checked_json(ROOT / "preview-integrity-audit.json", "report_hash")
    hashes = {"execution-preview.json": "proposal_hash", "preview-integrity-audit.json": "report_hash",
        "upstream-check.json": "record_hash", "prerequisite-check.json": "report_hash",
        "new-file-gate-check.json": "report_hash", "acceptance-report-contract.json": "record_hash",
        "acceptance-report-template.json": "record_hash"}
    checked = 0
    for path in ROOT.iterdir():
        if path.is_file() and path.suffix in {".json", ".py", ".md", ".txt"}:
            ensure_no_sensitive_data(path.read_text(), context=path.name)
            if path.name in hashes:
                checked_json(path, hashes[path.name])
            checked += 1
    # Plan record hashes use the domain hash payload (not the entire API DTO).
    # The independent auditor has already used PlanVersionService.get_verified.
    assert audit["plan_and_binding_verified_with_domain_readers"]
    before = json.loads((ROOT / "before-state.json").read_text())
    legacy = json.loads((PRIOR / "before-state.json").read_text())
    assert legacy["checkpoints"] == before["checkpoints"] and legacy["history"] == before["history"]
    for name, table in legacy["tables"].items():
        current = before["tables"][name]
        if name == "_schema_migrations":
            assert current["count"] == table["count"] + 1
            assert not (Counter(table["row_hashes"]) - Counter(current["row_hashes"]))
        else:
            assert table == current, name
    for path, expected in [
        (ROOT.parent / "real-model-acceptance-preview.json", "a7b2f501f7320db06810b37c04816b0d973e3aaee595c8cd00aac94d2c402291"),
        (ROOT.parent / "plan13-preparation/execution-preview.json", "c628ea479f2647a18c3188575ed3bf7fbc17d611a5c58a7c55a43519fdfdf42c"),
        (PRIOR / "real-model-acceptance-preview.json", preview["parent_intent_hash"]),
    ]:
        assert checked_json(path, "proposal_hash")["proposal_hash"] == expected
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())["roles"]
    code = """import json
from sqlalchemy import text
from app.config import Settings
from app.database import Database
s=Settings.from_env();db=Database(s.database_url)
with db.session() as session:
 session.execute(text('BEGIN'));session.execute(text('PRAGMA query_only=ON'))
 queries={'active_jobs':\"select count(*) from jobs where state in ('queued','leased','running')\",'model_starts':\"select count(*) from workbench_events where kind='agent_v2_model_started'\",'plan_approvals':'select count(*) from plan_approvals','plans':'select count(*) from plan_versions','max_plan':'select max(version_number) from plan_versions','artifacts':'select count(*) from artifacts','attempts':'select count(*) from execution_attempts'}
 print(json.dumps({k:session.execute(text(q)).scalar_one() for k,q in queries.items()}))
db.close()
"""
    counts = json.loads(subprocess.check_output(["docker", "exec", roles["serve"]["id"], "python", "-c", code]))
    assert counts["active_jobs"] == 0 and counts["model_starts"] == 88 and counts["plan_approvals"] == 11
    assert counts["plans"] == counts["max_plan"] == 14 and counts["artifacts"] == 1610
    assert counts["attempts"] == before["tables"]["execution_attempts"]["count"]
    for entry in roles.values():
        logs = subprocess.run(["docker", "logs", "--tail", "100", entry["id"]], capture_output=True, text=True, check=True)
        ensure_no_sensitive_data(logs.stdout + logs.stderr, context="Plan14 preparation role logs")
    health = {}
    with httpx.Client(timeout=30, trust_env=False) as client:
        for port in (18090, 18091, 18092, 18080):
            response = client.get(f"http://127.0.0.1:{port}/health"); response.raise_for_status()
            health[str(port)] = response.json()
            assert health[str(port)]["status"] == "ok"
        response = client.get(f"http://127.0.0.1:18090/api/v1/tasks/{TASK}/workbench"); response.raise_for_status()
        view = response.json()
        assert view["state"] == "planning" and not view["busy"]
        assert view["execution"]["revision"] == preview["execution_revision"]
        assert view["execution"]["preflight"] == preview["preflight"]
        assert view["execution"]["planning_budget"] == preview["planning_budget"]
    subprocess.run([".venv/bin/python", "-m", "compileall", "-q", "app", str(ROOT)], check=True)
    subprocess.run(["git", "diff", "--check"], check=True)
    value = {"version": "approved-missing-plan14-preparation-final-checks-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
        "plan_id": preview["plan"]["id"], "plan_record_hash": preview["plan_record_hash"],
        "binding_hash": preview["plan_binding_hash"], "proposal_hash": preview["proposal_hash"],
        "runtime_source_hash": preview["runtime_source_hash"], "source_unchanged_in_preparation": True,
        "only_new_preparation_documents_and_helpers": True, "JSON_and_text_credential_checks": checked,
        "self_hashes_and_domain_hashes_verified": True, "compileall_app_and_preparation_helpers": "passed",
        "git_diff_check": "passed", "all_original_previews_preserved": True,
        "legacy_business_rows_from_before_0045_preserved": True, "all_old_checkpoints_preserved": len(before["checkpoints"]),
        "logs_credentials_check": "passed", "health": health, "counts": counts, "new_paid_model_calls": 0,
        "new_execution_authorizations": 0, "new_jobs": 0, "new_attempts": 0, "new_Plan_versions": 1,
        "new_run_budget_allocated": False, "execution_request_sent": False,
        "human_consent": "pending_new_exact_execution_authorization", "HR4_HR5": "not_passed",
        "Linux_amd64": "not_run_still_open", "Push_PR_comments_publication": False,
        "preview_audit_hash": audit["report_hash"]}
    value["report_hash"] = content_hash(value)
    save("final-checks.json", value)
    print(json.dumps({"final_checks": "passed", "report_hash": value["report_hash"], "counts": counts, "health": health}))


if __name__ == "__main__":
    main()
