"""Audit the live run inside its Linux container; export references, not secrets."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from app.provenance import content_hash
from app.security import ensure_no_sensitive_data


def inside(config: dict) -> dict:
    from sqlalchemy import select, text
    from app.agent_v2.journal import AgentJournal
    from app.agent_v2.model_receipts import original_call
    from app.agent_v2.progress import current_progress
    from app.audit import AuditService
    from app.config import Settings
    from app.database import Database
    from app.models import Artifact, ReviewRun
    from app.reviews import ReviewRunService
    from app.security import ensure_no_sensitive_bytes
    from app.workbench import Workbench

    settings = Settings.from_env()
    db = Database(settings.database_url)
    with db.session() as session:
        session.execute(text("BEGIN"))
        session.execute(text("PRAGMA query_only=ON"))
        assert session.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert not session.execute(text("PRAGMA foreign_key_check")).all()
        wb = Workbench(session, settings.artifact_root)
        history = wb.history(config["task_id"])
        assert content_hash([{k: getattr(e, k) for k in ("id", "record_hash")} for e in history[:config["start_sequence"]]]) == config["prefix_hash"]
        old = AgentJournal(wb, config["task_id"], config["old_checkpoint"]["session_id"])
        assert old.checkpoint().model_dump() == config["old_checkpoint"]
        audit = AuditService(session).verify()
        assert audit.valid
        artifacts = list(session.scalars(select(Artifact)))
        for artifact in artifacts:
            data = (Path(settings.artifact_root) / artifact.storage_key).read_bytes()
            assert hashlib.sha256(data).hexdigest() == artifact.id
            ensure_no_sensitive_bytes(data, context="real acceptance artifact audit")
        events = history[config["start_sequence"]:]
        assert sum(e.kind == "execution_authorized" for e in events) == 1
        root = next(e for e in reversed(events) if e.kind == "agent_v2_session")
        journal = AgentJournal(wb, config["task_id"], root.payload["session_id"])
        spec = journal.spec()
        assert spec.limits.model_dump() == config["limits"]
        assert spec.write_paths == config["write_paths"]
        assert [{k: c[k] for k in ("command_id", "argv", "working_directory")} for c in spec.commands] == config["commands"]
        assert content_hash(spec.model_transport.model_dump()) == config["model_transport_hash"]
        authorization = next(e for e in events if e.kind == "execution_authorized")
        assert {k: authorization.payload[k] for k in ("implementation", "review")} == config["model_identities"]
        assert spec.binding.authorization_hash == authorization.record_hash
        assert spec.binding.model_binding_hash == content_hash(config["model_identities"])
        native = journal.events()
        responses = []
        for e in native:
            if e.kind != "agent_v2_model_response":
                continue
            value = wb.read(e.payload["artifact_id"])
            started = next(s for s in native if s.kind == "agent_v2_model_started" and s.job_id == e.job_id)
            assert started.sequence < e.sequence and value["request_id"] == e.job_id
            assert value["binding_hash"] == content_hash(spec.binding.model_dump())
            terminal = next((s for s in native if s.kind in ("agent_v2_turn", "agent_v2_model_rejected") and s.job_id == e.job_id), None)
            if terminal:
                assert e.sequence < terminal.sequence and terminal.payload["response_artifact"] == e.payload["artifact_id"]
            calls = [c for choice in value["choices"] for c in choice.get("tool_calls") or []]
            responses.append({"sequence": e.sequence, "job_id": e.job_id, "artifact_id": e.payload["artifact_id"],
                "finish_reasons": [c["finish_reason"] for c in value["choices"]], "usage": value["usage"],
                "original_calls": [{"index": i, "provider_id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                    "arguments_hash": content_hash((c.get("function") or {}).get("arguments"))} for i, c in enumerate(calls)],
                "terminal_kind": terminal.kind if terminal else None,
                "rejection": terminal.payload.get("failure") if terminal and terminal.kind == "agent_v2_model_rejected" else None})
            if terminal and terminal.kind == "agent_v2_model_rejected":
                assert not any(t.kind in ("agent_v2_tool_started", "agent_v2_tool_result") and t.payload.get("response_artifact") == e.payload["artifact_id"] for t in native)
        tools = []
        for e in native:
            if e.kind != "agent_v2_tool_result":
                continue
            result = wb.read(e.payload["artifact_id"])
            source = original_call(journal, e.payload["call_id"])
            assert source and all(e.payload[k] == v for k, v in source.items())
            data = result["data"]
            tools.append({"sequence": e.sequence, "job_id": e.job_id, "call_id": e.payload["call_id"],
                "artifact_id": e.payload["artifact_id"], "name": e.payload["name"], "status": result["status"],
                "failure": result["failure"], "source_hash": result["source_hash"], "output_source_hash": result["output_source_hash"],
                "arguments": e.payload.get("request_arguments"), "execution_backend": data.get("execution_backend"),
                "reused_receipt": data.get("reused_receipt"), "outcome": data.get("outcome"),
                "verification": data.get("verification"), **source})
        reviews = []
        for row in session.scalars(select(ReviewRun).where(ReviewRun.task_id == config["task_id"])):
            if row.plan_version_id != config["plan_id"]:
                continue
            review = ReviewRunService(session, wb.artifacts).get_verified(row.id)
            reviews.append({k: getattr(review, k) for k in ("id", "record_hash", "execution_attempt_id", "reviewer_kind", "verdict", "status", "reason_code", "diff_hash", "test_results_hash", "verify_result_hash", "binding_hash", "findings", "reviewer_invocation_id")})
        progress = current_progress(journal)
        report = {"version": "harness-real-acceptance-audit-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "proposal_hash": config["proposal_hash"], "task_id": config["task_id"], "plan_id": config["plan_id"],
            "session_id": journal.session_id, "source_manifest_hash": config["source_manifest_hash"],
            "database_integrity": "ok", "foreign_key_violations": 0, "audit": asdict(audit),
            "old_event_prefix_verified": config["start_sequence"], "task_events_verified": len(history),
            "artifacts_hash_and_credentials_verified": len(artifacts), "old_checkpoint_unchanged": True,
            "exact_new_root_limits_scope_and_model_verified": True, "checkpoint": journal.checkpoint().model_dump(),
            "progress": progress.model_dump() if progress else None, "model_responses": responses,
            "tools": tools, "reviews": reviews, "tool_counts": dict(Counter(t["name"] for t in tools)),
            "original_call_identity_mismatches": 0, "rejected_batches_executed_calls": 0,
            "event_index": [{"sequence": e.sequence, "kind": e.kind, "record_hash": e.record_hash, "job_id": e.job_id} for e in events]}
        ensure_no_sensitive_data(report, context="real acceptance audit report")
    db.close()
    return report


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--inside":
        print(json.dumps(inside(json.loads(sys.argv[2])), ensure_ascii=False))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve prior audit; choose a fresh filename"
    records = Path("docs/agent-harness-reset-v1")
    preview = json.loads((records / "real-acceptance-preview.json").read_text())
    config = {k: preview[k] for k in ("task_id", "proposal_hash", "write_paths", "commands", "model_transport_hash", "model_binding_hash", "model_identities", "source_manifest_hash")}
    config.update(plan_id=preview["plan"]["id"], limits=preview["new_run_requested_limits"],
        old_checkpoint=preview["old_Plan11_checkpoint"], start_sequence=len(preview["history_after"]), prefix_hash=content_hash(preview["history_after"]))
    state = json.loads(Path("/tmp/upeve-isolated-20261029-v2/container-roles.json").read_text())
    result = subprocess.run(["docker", "exec", "-i", state["roles"]["serve"]["id"], "python", "-", "--inside", json.dumps(config)],
        input=Path(__file__).read_text(), text=True, capture_output=True, check=False)
    if result.returncode:
        from app.security import redact_text
        raise RuntimeError("Container audit failed: " + redact_text(result.stderr)[-2500:])
    value = json.loads(result.stdout)
    value["observation_runtime_source_hash"] = state["source_manifest_hash"]
    ensure_no_sensitive_data(value, context="real acceptance exported audit")
    value["report_hash"] = content_hash(value)
    with args.output.open("x") as out:
        out.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: value[k] for k in ("session_id", "task_events_verified", "artifacts_hash_and_credentials_verified", "tool_counts", "checkpoint", "report_hash")}))


if __name__ == "__main__":
    main()
