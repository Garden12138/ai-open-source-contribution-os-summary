"""Audit the actual Plan13 run without executing tools, code, models or writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data, redact_text

ROOT = Path(__file__).resolve().parent
PREP = ROOT.parent / "plan13-preparation"


def inside(config: dict) -> dict:
    from collections import Counter
    import difflib
    from sqlalchemy import select, text
    from app.agent_v2 import read_batches
    from app.agent_v2.journal import AgentJournal
    from app.config import Settings
    from app.database import Database
    from app.models import ExecutionAttempt, PlanVersion
    from app.workbench import Workbench
    # Reuse the independently verified prior auditor, parameterized with the
    # new actual plan/consent/prefix, then verify the new batch contract too.
    import types
    previous = types.ModuleType("previous_auditor")
    previous.__dict__["__name__"] = "previous_auditor"
    exec(compile(config.pop("previous_auditor_source"), "previous_readonly_auditor.py", "exec"), previous.__dict__)
    report = previous.inside(config)
    settings = Settings.from_env()
    db = Database(settings.database_url)
    with db.session() as session:
        session.execute(text("BEGIN"))
        session.execute(text("PRAGMA query_only=ON"))
        wb = Workbench(session, settings.artifact_root)
        history = wb.history(config["task_id"])
        events = history[config["start_sequence"]:]
        assert sum(e.kind == "execution_authorized" for e in events) == 1
        assert sum(e.kind == "automation_started" for e in events) == 1
        for sid, checkpoint in config["old_checkpoints"].items():
            assert AgentJournal(wb, config["task_id"], sid).checkpoint().model_dump() == checkpoint
        plans = list(session.scalars(select(PlanVersion).where(PlanVersion.task_id == config["task_id"])))
        assert len(plans) == 13 and max(p.version_number for p in plans) == 13
        roots = [e for e in events if e.kind == "agent_v2_session"]
        batches = []
        patches = []
        prompts = []
        verifications = []
        for root in roots:
            journal = AgentJournal(wb, config["task_id"], root.payload["session_id"])
            spec = journal.spec()
            assert spec.binding.plan_id == config["plan_id"] and spec.limits.model_dump() == config["limits"]
            native = journal.events()
            for event in native:
                if event.kind == "agent_v2_model_started":
                    prompt = wb.read(event.payload["prompt_artifact"])
                    assert content_hash(prompt) == event.payload["prompt_artifact"]
                    prompts.append({"sequence": event.sequence, "job_id": event.job_id, **event.payload})
                if event.kind == "agent_v2_tool_result" and event.payload["name"] == "run_approved_checks":
                    value = wb.read(event.payload["artifact_id"])
                    verifications.append({"sequence": event.sequence, "artifact_id": event.payload["artifact_id"],
                        "source_hash": value["source_hash"], "status": value["status"], "data": value["data"], "failure": value["failure"]})
            for batch in (e for e in native if e.kind == "agent_v2_read_batch"):
                original = read_batches.record(journal, batch)
                raw = wb.read(original["response_artifact"])
                vendors = raw["choices"][0]["tool_calls"]
                assert 5 <= len(vendors) <= 8 and all(v["function"]["name"] == "read_file" for v in vendors)
                proofs = [e for e in native if e.kind == "agent_v2_read_batch_preflight" and e.payload["batch_artifact"] == batch.payload["artifact_id"]]
                starts = [e for e in native if e.kind == "agent_v2_read_sub_batch_started" and e.payload["batch_artifact"] == batch.payload["artifact_id"]]
                completed = [e for e in native if e.kind == "agent_v2_read_sub_batch_completed" and e.payload["batch_artifact"] == batch.payload["artifact_id"]]
                finals = [e for e in native if e.kind in {"agent_v2_read_batch_completed", "agent_v2_read_batch_stopped"}
                    and e.payload["batch_artifact"] == batch.payload["artifact_id"]]
                results = [e for e in native if e.kind == "agent_v2_tool_result" and e.payload.get("read_batch_artifact") == batch.payload["artifact_id"]]
                assert len(proofs) == 1 and len(finals) == 1
                assert all(len(e.payload["call_ids"]) <= 4 for e in starts + completed)
                assert not any(e.kind == "agent_v2_model_started" and batch.sequence < e.sequence < finals[0].sequence for e in native)
                assert [e.payload["provider_call_id"] for e in results] == [v["id"] for v in vendors[:len(results)]]
                if proofs[0].payload["status"] == "succeeded":
                    assert starts and proofs[0].sequence < starts[0].sequence
                else:
                    assert not starts and not results
                if finals[0].kind == "agent_v2_read_batch_completed":
                    assert len(results) == len(vendors) and len(completed) == 2
                    assert finals[0].payload["receipts"] == [e.payload["artifact_id"] for e in results]
                    assert [len(e.payload["call_ids"]) for e in completed] == [4, len(vendors) - 4]
                batches.append({"batch_artifact": batch.payload["artifact_id"], "original_response_artifact": original["response_artifact"],
                    "original_call_count": len(vendors), "provider_ids_in_order": [v["id"] for v in vendors],
                    "preflight": proofs[0].payload, "children": [e.payload for e in completed],
                    "final_kind": finals[0].kind, "final_receipt": finals[0].payload,
                    "executed_provider_ids_in_order": [e.payload["provider_call_id"] for e in results],
                    "new_model_decisions_between_children": 0})
            checkpoints = [e.payload["checkpoint"] for e in native if e.kind == "agent_v2_checkpoint"]
            assert all(len(c["pending_calls"]) <= 4 for c in checkpoints)
            reads = [c["usage"].get("reads", 0) for c in checkpoints]
            assert reads == sorted(reads)
            checkpoint = journal.checkpoint()
            if checkpoint.snapshot_artifact:
                snapshot = wb.read(checkpoint.snapshot_artifact)
                for path, item in sorted(snapshot["files"].items()):
                    if item["original_content"] != item["content"]:
                        assert path in spec.write_paths
                        diff = "".join(difflib.unified_diff(item["original_content"].splitlines(keepends=True),
                            item["content"].splitlines(keepends=True), fromfile="a/" + path, tofile="b/" + path))
                        patches.append({"path": path, "snapshot_artifact": checkpoint.snapshot_artifact,
                            "original_hash": item["original_hash"], "diff": diff})
        attempts = list(session.scalars(select(ExecutionAttempt).where(ExecutionAttempt.plan_version_id == config["plan_id"])))
        report.update(version="native-read-batch-plan13-real-audit-v1", recorded_at=datetime.now(timezone.utc).isoformat(),
            all_old_checkpoints_unchanged=True, plan_count=13, new_plan_created_during_execution=False,
            actual_model_requests=prompts, read_batches=batches, actual_source_patches=patches,
            current_source_verifications=verifications, batch_coverage="observed" if batches else "not_observed",
            original_execution_authorizations_used=0, additional_authorized_rounds=1,
            native_session_count=len(roots), execution_attempts=[{"id": a.id, "record_hash": a.record_hash} for a in attempts])
        ensure_no_sensitive_data(report, context="Plan13 final execution audit")
    db.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--output", default="final-audit.json")
    args = parser.parse_args()
    if args.inside:
        print(json.dumps(inside(json.loads(args.config)), ensure_ascii=False))
        return
    preview = json.loads((PREP / "execution-preview.json").read_text())
    before = json.loads((ROOT / "before-dispatch-state.json").read_text())
    history = [{k: e[k] for k in ("id", "record_hash")} for e in before["history"]]
    checkpoint = before["checkpoints"]["session-345b2c24d307d9e35767d4f34f34d672"]
    config = {"task_id": preview["target"]["task_id"], "plan_id": preview["plan"]["id"],
        "proposal_hash": preview["proposal_hash"], "write_paths": preview["write_paths"], "commands": preview["commands"],
        "model_transport_hash": preview["model_transport_hash"], "model_binding_hash": preview["model_binding_hash"],
        "model_identities": preview["model_identities"], "source_manifest_hash": preview["runtime_source_hash"],
        "limits": preview["additional_run_limits_pending_consent"], "old_checkpoint": checkpoint,
        "old_checkpoints": before["checkpoints"], "start_sequence": len(history), "prefix_hash": content_hash(history),
        "previous_auditor_source": (ROOT.parents[1] / "collect_acceptance.py").read_text()}
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    command = ["docker", "exec", "-i", roles["roles"]["serve"]["id"], "python", "-", "--inside", "--config", json.dumps(config)]
    result = subprocess.run(command, input=Path(__file__).read_text(), text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Read-only audit failed: " + redact_text(result.stderr)[-2000:])
    report = json.loads(result.stdout)
    report["report_hash"] = content_hash(report)
    target = ROOT / args.output
    with target.open("x") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"session_id": report["session_id"], "checkpoint": report["checkpoint"],
        "batch_coverage": report["batch_coverage"], "batches": len(report["read_batches"]),
        "patch_paths": [p["path"] for p in report["actual_source_patches"]],
        "reviews": len(report["reviews"]), "report_hash": report["report_hash"]}))


if __name__ == "__main__":
    main()
