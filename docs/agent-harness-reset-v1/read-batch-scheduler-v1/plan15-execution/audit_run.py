"""Read-only provenance audit of the stopped real run; never invoke a model/tool."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data, redact_text

ROOT = Path(__file__).resolve().parent
PREP = ROOT.parent / "plan15-preparation"


def inside(config: dict) -> dict:
    import types
    from sqlalchemy import text
    from app.agent_v2.journal import AgentJournal
    from app.agent_v2.read_capacity import certificate
    from app.config import Settings
    from app.database import Database
    from app.workbench import Workbench
    prior = types.ModuleType("trusted_prior_auditor")
    prior.__dict__["__file__"] = "/trusted-readonly/previous-audit.py"
    exec(compile(config.pop("prior_auditor_source"), "trusted-prior-audit.py", "exec"), prior.__dict__)
    report = prior.inside(config)
    s = Settings.from_env(); db = Database(s.database_url)
    with db.session() as session:
        session.execute(text("BEGIN")); session.execute(text("PRAGMA query_only=ON"))
        wb = Workbench(session, s.artifact_root)
        journal = AgentJournal(wb, config["task_id"], report["session_id"])
        events = journal.events(); cp = journal.checkpoint()
        assert journal.spec().binding.plan_id == config["plan_id"]
        charges = [e for e in events if e.kind == "agent_v2_output_charge"]
        for e in charges:
            raw = wb.artifacts.read_bytes(e.payload["artifact_id"])
            assert len(raw) == e.payload["bytes"] and e.payload["contract"] == "native-read-capacity-v1"
        model_bytes = sum(len(wb.artifacts.read_bytes(e.payload["artifact_id"])) for e in events if e.kind == "agent_v2_model_response")
        assert cp.usage.get("output_bytes", 0) == model_bytes + sum(e.payload["bytes"] for e in charges)
        capacities = []
        for event in events:
            if event.kind != "agent_v2_read_capacity": continue
            cert = wb.read(event.payload["artifact_id"])
            batch = wb.read(cert["batch_artifact"]); proof = wb.read(cert["preflight_artifact"])
            assert certificate(batch, proof, batch_artifact=cert["batch_artifact"], proof_artifact=cert["preflight_artifact"],
                remaining=cert["remaining_output_bytes"]) == cert
            capacities.append({"artifact_id": event.payload["artifact_id"], "sequence": event.sequence, **cert})
        report.update(version="output-capacity-plan15-real-audit-v1", recorded_at=datetime.now(timezone.utc).isoformat(),
            plan_count=15, capacity_certificates=capacities, output_model_envelope_bytes=model_bytes,
            output_charges=[dict(e.payload) for e in charges], exact_output_billing_verified=True,
            all_old_10_checkpoints_unchanged=True, old_terminal_states_and_usage_unchanged=True,
            output_capacity_contract="native-read-capacity-v1", prompt_projection="native-read-capacity-v9")
        ensure_no_sensitive_data(report, context="Plan15 provenance audit")
    db.close()
    return report


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--inside":
        print(json.dumps(inside(json.loads(sys.argv[2])), ensure_ascii=False)); return
    assert not (ROOT / "final-audit.json").exists()
    preview = json.loads((PREP / "execution-preview.json").read_text())
    before = json.loads((ROOT / "before-dispatch-state.json").read_text())
    prefix = [{k: e[k] for k in ("id", "record_hash")} for e in before["history"]]
    scheduler = (ROOT.parent / "plan13-execution/audit_run.py").read_text()
    scheduler = scheduler.replace("== 13", "== 15").replace("plan_count=13", "plan_count=15")
    config = {"task_id": preview["target"]["task_id"], "plan_id": preview["plan"]["id"],
        "plan_record_hash": preview["plan_record_hash"], "plan_binding_hash": preview["plan_binding_hash"],
        "proposal_hash": preview["proposal_hash"], "write_paths": preview["write_paths"], "commands": preview["commands"],
        "model_transport_hash": preview["model_transport_hash"], "model_binding_hash": preview["model_binding_hash"],
        "model_identities": preview["model_identities"], "source_manifest_hash": preview["runtime_source_hash"],
        "limits": preview["additional_run_limits_pending_consent"], "old_checkpoints": before["checkpoints"],
        "old_checkpoint": next(iter(before["checkpoints"].values())),
        "start_sequence": len(prefix), "prefix_hash": content_hash(prefix), "scheduler_auditor_source": scheduler,
        "report_contract_hash": preview["acceptance_report_contract_hash"],
        "previous_auditor_source": (ROOT.parents[1] / "collect_acceptance.py").read_text(),
        "prior_auditor_source": (ROOT.parent / "plan14-execution/audit_run.py").read_text()}
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    result = subprocess.run(["docker", "exec", "-i", roles["roles"]["serve"]["id"], "python", "-B", "-", "--inside", json.dumps(config)],
        input=Path(__file__).read_text(), text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("Plan15 read-only provenance audit failed: " + redact_text(result.stderr)[-2200:])
    report = json.loads(result.stdout); report["report_hash"] = content_hash(report)
    with (ROOT / "final-audit.json").open("x") as stream: stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"session_id": report["session_id"], "checkpoint": report["checkpoint"],
        "patch_count": report["successful_patch_count"], "Review_count": len(report["reviews"]),
        "capacity_certificates": len(report["capacity_certificates"]), "exact_output_billing_verified": True,
        "report_hash": report["report_hash"]}))


if __name__ == "__main__": main()
