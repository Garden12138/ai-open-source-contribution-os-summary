"""Replay saved provider envelopes using deterministic local tools, never a provider."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import prompt_for
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench
from tests.test_native_read_batches import SAVED, saved_response, setup, drain


def main() -> None:
    records = Path("docs/agent-harness-reset-v1/read-batch-scheduler-v1")
    out = records / "offline-replay.json"
    assert not out.exists(), "Preserve existing evidence"
    manifest = json.loads((records / "source-manifest.json").read_text())
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in manifest["files"].items())
    replay = []
    for index, original in enumerate(SAVED["batches"]):
        with tempfile.TemporaryDirectory(prefix="native-read-batch-replay-") as temporary:
            db, settings, task, sid, model, tool, runner, gateway = setup(Path(temporary), saved_response(index))
            assert asyncio.run(model.run_once()).state == "succeeded"
            drain(tool)
            with db.session() as session:
                journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
                cp, events = journal.checkpoint(), journal.events()
                assert cp.state == "ready" and len(runner.prompts) == 1
                children = [e for e in events if e.kind == "agent_v2_read_sub_batch_completed"]
                results = [e for e in events if e.kind == "agent_v2_tool_result"]
                aggregate = next(e for e in events if e.kind == "agent_v2_read_batch_completed")
                assert aggregate.payload["receipts"] == [e.payload["artifact_id"] for e in results]
                original_ids = [c["id"] for c in original["envelope"]["choices"][0]["tool_calls"]]
                assert [e.payload["provider_call_id"] for e in results] == original_ids
                prompt = prompt_for(journal)
                assert len(prompt["feedback"]) == len(original_ids)
                replay.append({"original_response_artifact": original["response_artifact"],
                    "original_model_job_id": original["model_job_id"], "classification": "fresh_synthetic_authority_not_Plan12_approval",
                    "child_sizes": [len(e.payload["call_ids"]) for e in children],
                    "child_receipts": [{"id": e.payload["sub_batch_id"], "artifact": e.payload["artifact_id"]} for e in children],
                    "ordered_provider_ids": original_ids, "ordered_tool_results": [e.payload["artifact_id"] for e in results],
                    "aggregate_artifact": aggregate.payload["artifact_id"], "checkpoint_calls_max": max(
                        len(e.payload["checkpoint"]["pending_calls"]) for e in events if e.kind == "agent_v2_checkpoint"),
                    "charged_usage": cp.usage, "sandbox_preflights": gateway.preflights,
                    "gateway_executions": len(gateway.calls), "model_decisions_between_children": 0,
                    "coding_completed": False, "visible_feedback": [{"receipt_artifact": r["receipt_artifact"],
                        "offset": r["data"]["offset"], "visible_bytes": len(r["data"]["text"].encode()),
                        "next_offset": r["data"]["next_offset"], "total_bytes": r["data"]["total_bytes"]} for r in prompt["feedback"]]})
            db.close()
    value = {"version": "native-read-batch-offline-replay-v1", "source_hash": manifest["source_hash"],
        "original_fixture_hash": content_hash(SAVED), "classification": "real_saved_inputs_deterministic_fixture_tools",
        "model_provider_requests": 0, "live_business_database_modified": False, "Plan13_created": False,
        "replays": replay, "actual_OCI_evidence": "/tmp/native-read-batch-real-oci.xml", "coding_acceptance_claim": False}
    ensure_no_sensitive_data(value, context="offline replay evidence")
    value["report_hash"] = content_hash(value)
    with out.open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"child_sizes": [r["child_sizes"] for r in replay], "new_provider_requests": 0,
        "report_hash": value["report_hash"]}))


if __name__ == "__main__":
    main()
