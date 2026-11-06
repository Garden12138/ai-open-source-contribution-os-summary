"""Read-only Plan14 evidence audit; no model/tool invocation or state mutation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data, redact_text

ROOT = Path(__file__).resolve().parent
PREP = ROOT.parent / "plan14-preparation"


def inside(config: dict) -> dict:
    import types
    from sqlalchemy import select, text
    from app.agent_v2.journal import AgentJournal
    from app.config import Settings
    from app.database import Database
    from app.models import ReviewRun
    from app.workbench import Workbench

    scheduler = types.ModuleType("scheduler_readonly_auditor")
    scheduler.__dict__["__file__"] = "scheduler_readonly_auditor.py"
    # Reuse the existing raw-call/receipt, whole-batch, old-checkpoint and
    # independent Review provenance checks with the new expected Plan count.
    source = config.pop("scheduler_auditor_source")
    exec(compile(source, "scheduler_readonly_auditor.py", "exec"), scheduler.__dict__)
    report = scheduler.inside(config)
    settings = Settings.from_env(); db = Database(settings.database_url)
    with db.session() as session:
        session.execute(text("BEGIN")); session.execute(text("PRAGMA query_only=ON"))
        wb = Workbench(session, settings.artifact_root)
        journal = AgentJournal(wb, config["task_id"], report["session_id"])
        native = journal.events(); cp = journal.checkpoint(); spec = journal.spec()
        assert spec.binding.plan_id == config["plan_id"]
        assert spec.binding.plan_hash == config["plan_record_hash"]
        authority = next(e for e in wb.history(config["task_id"])[config["start_sequence"]:] if e.kind == "execution_authorized")
        assert spec.binding.authorization_hash == authority.record_hash
        started = [e for e in native if e.kind == "agent_v2_model_started"]
        responses = {e.job_id: e for e in native if e.kind == "agent_v2_model_response"}
        observed = []
        results = [e for e in native if e.kind == "agent_v2_tool_result"]
        requested = []
        for event in native:
            if event.kind != "agent_v2_model_response":
                continue
            raw = wb.read(event.payload["artifact_id"])
            calls = [c for choice in raw["choices"] for c in choice.get("tool_calls") or []]
            if not calls:
                continue
            rows = []
            for index, call in enumerate(calls):
                matches = [e for e in results if e.payload.get("response_artifact") == event.payload["artifact_id"]
                    and e.payload.get("provider_call_id") == call.get("id")]
                assert len(matches) <= 1
                rows.append({"order": index, "provider_call_id": call.get("id"),
                    "name": call.get("function", {}).get("name"), "arguments": call.get("function", {}).get("arguments"),
                    "result_status": matches[0].payload["status"] if matches else "not_dispatched",
                    "receipt_artifact": matches[0].payload["artifact_id"] if matches else None})
            requested.append({"response_artifact": event.payload["artifact_id"], "event_sequence": event.sequence,
                "finish_reasons": [c["finish_reason"] for c in raw["choices"]], "usage": raw["usage"], "calls": rows})
        for event in results:
            result = wb.read(event.payload["artifact_id"])
            if result["status"] != "not_found":
                continue
            assert result["data"]["creation_allowed"] and result["data"]["path"] in spec.write_paths
            assert not ({"text", "content_hash", "total_bytes", "next_offset"} & result["data"].keys())
            later = []
            for request in started:
                if request.sequence <= event.sequence:
                    continue
                prompt = wb.read(request.payload["prompt_artifact"])
                matches = [f for f in prompt.get("feedback", []) if f.get("receipt_artifact") == event.payload["artifact_id"]
                    and f.get("status") == "not_found" and f.get("data", {}).get("path") == result["data"]["path"]]
                if matches:
                    assert all(f["source_hash"] == result["source_hash"] for f in matches)
                    later.append({"sequence": request.sequence, "job_id": request.job_id,
                        "prompt_artifact": request.payload["prompt_artifact"], "provider_response_artifact":
                            responses[request.job_id].payload["artifact_id"] if request.job_id in responses else None})
            observed.append({"status": "observed" if any(v["provider_response_artifact"] for v in later) else "not_observed",
                "path": result["data"]["path"], "provider_call_id": event.payload["provider_call_id"],
                "receipt_artifact": event.payload["artifact_id"], "source_hash": result["source_hash"],
                "receipt_event_sequence": event.sequence, "later_real_model_inputs": later})
        for batch in report["read_batches"]:
            raw = wb.read(batch["original_response_artifact"])
            batch["original_calls"] = [{"order": i, "provider_call_id": c["id"], "name": c["function"]["name"],
                "arguments": c["function"]["arguments"]} for i, c in enumerate(raw["choices"][0]["tool_calls"])]
            batch["finish_reasons"] = [c["finish_reason"] for c in raw["choices"]]
            batch["provider_usage"] = raw["usage"]
            matched = [e for e in results if e.payload.get("read_batch_artifact") == batch["batch_artifact"]]
            batch["ordered_results"] = [{"provider_call_id": e.payload["provider_call_id"],
                "artifact_id": e.payload["artifact_id"], "status": e.payload["status"],
                "actual_or_reused": "reused" if wb.read(e.payload["artifact_id"])["data"].get("reused_receipt") else "executed",
                "source_hash": wb.read(e.payload["artifact_id"])["source_hash"]} for e in matched]
            batch["not_dispatched_provider_ids"] = [c["provider_call_id"] for c in batch["original_calls"]
                if c["provider_call_id"] not in {e.payload["provider_call_id"] for e in matched}]
            batch["child_starts"] = [e.payload for e in native if e.kind == "agent_v2_read_sub_batch_started"
                and e.payload["batch_artifact"] == batch["batch_artifact"]]
        patches = []
        for event in results:
            if event.payload["name"] != "apply_patch":
                continue
            result = wb.read(event.payload["artifact_id"])
            patches.append({"event_sequence": event.sequence, "provider_call_id": event.payload["provider_call_id"],
                "response_artifact": event.payload["response_artifact"], "receipt_artifact": event.payload["artifact_id"],
                "status": result["status"], "failure": result["failure"], "path": event.payload["request_arguments"]["path"],
                "before_source_hash": result["source_hash"], "after_source_hash": result["output_source_hash"],
                "execution_backend": result["data"].get("execution_backend")})
        assert all(p["path"] in spec.write_paths for p in patches)
        verifies = []
        for event in results:
            if event.payload["name"] == "run_approved_checks":
                result = wb.read(event.payload["artifact_id"])
                verifies.append({"receipt_artifact": event.payload["artifact_id"], "status": result["status"],
                    "failure": result["failure"], "source_hash": result["source_hash"],
                    "output_source_hash": result["output_source_hash"], "data": result["data"],
                    "is_latest_source": result["source_hash"] == result["output_source_hash"] == cp.source_hash})
        for review in report["reviews"]:
            row = session.get(ReviewRun, review["id"])
            review["findings_hash"] = row.findings_hash
        failures = [{"sequence": e.sequence, "kind": e.kind, "failure": e.payload["failure"]}
            for e in native if e.payload.get("failure")]
        report.update(version="approved-missing-plan14-real-audit-v1", plan_record_hash=config["plan_record_hash"],
            plan_binding_hash=config["plan_binding_hash"], execution_authorization_hash=authority.record_hash,
            acceptance_report_contract_hash=config["report_contract_hash"], not_found_observations=observed,
            requested_tool_batches=requested, requested_read_batches=[b for b in requested if any(c["name"] == "read_file" for c in b["calls"])],
            not_found_observation_status="observed" if any(v["status"] == "observed" for v in observed) else "not_observed",
            patch_attempts=patches, successful_patch_count=sum(p["status"] == "succeeded" for p in patches),
            latest_source_Verify=verifies, failures=failures, snapshot_artifact=cp.snapshot_artifact,
            recoverability={"checkpoint_state": cp.state, "failure": failures[-1]["failure"] if failures else None,
                "automatic_terminal_restart_authorized": False, "another_paid_round_authorized": False},
            executed_stage="awaiting_human" if wb.latest(config["task_id"], "awaiting_acceptance") and cp.state == "finished"
                else (report["progress"]["phase"] if report["progress"] else "unknown"))
        ensure_no_sensitive_data(report, context="Plan14 real audit")
    db.close()
    return report


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--inside":
        print(json.dumps(inside(json.loads(sys.argv[2])), ensure_ascii=False)); return
    preview = json.loads((PREP / "execution-preview.json").read_text())
    before = json.loads((ROOT / "before-dispatch-state.json").read_text())
    history = [{k: e[k] for k in ("id", "record_hash")} for e in before["history"]]
    source = (ROOT.parent / "plan13-execution/audit_run.py").read_text()
    source = source.replace("== 13", "== 14").replace("plan_count=13", "plan_count=14")
    config = {"task_id": preview["target"]["task_id"], "plan_id": preview["plan"]["id"],
        "plan_record_hash": preview["plan_record_hash"], "plan_binding_hash": preview["plan_binding_hash"],
        "proposal_hash": preview["proposal_hash"], "write_paths": preview["write_paths"], "commands": preview["commands"],
        "model_transport_hash": preview["model_transport_hash"], "model_binding_hash": preview["model_binding_hash"],
        "model_identities": preview["model_identities"], "source_manifest_hash": preview["runtime_source_hash"],
        "limits": preview["additional_run_limits_pending_consent"], "old_checkpoints": before["checkpoints"],
        "old_checkpoint": before["checkpoints"]["session-11901711068ace4c72aa1410a8847453"],
        "start_sequence": len(history), "prefix_hash": content_hash(history), "scheduler_auditor_source": source,
        "report_contract_hash": preview["acceptance_report_contract_hash"],
        "previous_auditor_source": (ROOT.parents[1] / "collect_acceptance.py").read_text()}
    roles = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    command = ["docker", "exec", "-i", roles["roles"]["serve"]["id"], "python", "-", "--inside", json.dumps(config)]
    result = subprocess.run(command, input=Path(__file__).read_text(), text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Read-only Plan14 audit failed: " + redact_text(result.stderr)[-2200:])
    report = json.loads(result.stdout); report["report_hash"] = content_hash(report)
    with (ROOT / "final-audit.json").open("x") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"session_id": report["session_id"], "checkpoint": report["checkpoint"],
        "not_found_observation_status": report["not_found_observation_status"], "batch_count": len(report["read_batches"]),
        "patch_count": report["successful_patch_count"], "Review_count": len(report["reviews"]), "report_hash": report["report_hash"]}))


if __name__ == "__main__":
    main()
