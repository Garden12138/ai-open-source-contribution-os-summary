"""Replay exact Plan13 input through the persistent scheduler, without a provider."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile

from pytest import MonkeyPatch

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import prompt_for
from app.agent_v2.native_tools import native_input_bound
from app.agent_v2.progress import current_progress
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench
from tests.test_missing_source_observations import MISSING, SAVED, fixture, saved_response, patch_response, drain


def main() -> None:
    out = Path(__file__).resolve().parent / "scheduler-replay.json"
    assert not out.exists(), "Preserve previous replay evidence"
    with tempfile.TemporaryDirectory(prefix="missing-source-scheduler-replay-") as temporary, MonkeyPatch.context() as patch:
        values = fixture(Path(temporary), patch, saved_response(), patch_response())
        db, settings, task, sid, model, tool, runner, gateway = values
        original = runner.complete_native_tools

        async def next_decision(invocation, tools, instruction):
            if runner.prompts:
                observed = json.loads(invocation.prompt)
                assert observed["feedback"][-1]["status"] == "not_found"
                assert observed["feedback"][-1]["data"]["creation_allowed"] is True
                assert MISSING in observed["source_files"]["approved_new_paths_currently_absent"]
            return await original(invocation, tools, instruction)
        runner.complete_native_tools = next_decision
        assert asyncio.run(model.run_once()).state == "succeeded"
        drain(tool)
        with db.session() as session:
            journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
            cp = journal.checkpoint()
            events = journal.events()
            assert cp.state == "ready" and cp.usage["reads"] == 8
            results = [e for e in events if e.kind == "agent_v2_tool_result"]
            receipts = [journal.wb.read(e.payload["artifact_id"]) for e in results]
            observed = prompt_for(journal)
            assert native_input_bound(observed) <= 32000
            assert [e.payload["provider_call_id"] for e in results] == [c["id"] for c in SAVED["envelope"]["choices"][0]["tool_calls"]]
            assert [r["status"] for r in receipts] == ["succeeded"] * 7 + ["not_found"]
            value = {"version": "approved-missing-scheduler-replay-v1",
                "classification": "real_saved_provider_input_deterministic_fixture_tools_synthetic_fresh_authority",
                "tools_are_real_OCI": False, "provider_requests": 0, "live_business_database_used": False,
                "historical_approval_reused": False, "new_Plan14_created": False,
                "original_response_artifact": SAVED["response_artifact"],
                "ordered_provider_ids": [e.payload["provider_call_id"] for e in results],
                "ordered_results": receipts, "receipt_artifacts": [e.payload["artifact_id"] for e in results],
                "child_receipts": [e.payload for e in events if e.kind == "agent_v2_read_sub_batch_completed"],
                "aggregate_receipt": next(e.payload for e in events if e.kind == "agent_v2_read_batch_completed"),
                "maximum_executable_checkpoint_calls": max(len(e.payload["checkpoint"]["pending_calls"])
                    for e in events if e.kind == "agent_v2_checkpoint"), "model_decisions_between_children": 0,
                "next_model_visible_statuses": [r["status"] for r in observed["feedback"]],
                "next_model_file_context": observed["source_files"], "next_model_input_bound": native_input_bound(observed),
                "fixture_usage_after_reads": cp.usage, "coding_completed_by_reads": current_progress(journal).steps[3].status == "completed"}
        assert asyncio.run(model.run_once()).state == "succeeded"
        drain(tool)
        with db.session() as session:
            journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
            cp = journal.checkpoint()
            value.update(simulated_next_decision="create_the_approved_absent_path_only_after_observing_not_found",
                fixture_usage_after_creation=cp.usage, resulting_source_hash=cp.source_hash,
                snapshot_artifact=cp.snapshot_artifact, created_paths=list(journal.wb.read(cp.snapshot_artifact)["files"]),
                real_Coding_Verify_Review_acceptance_claim=False)
            assert cp.usage["reads"] == 8 and cp.usage["writes"] == 1
        db.close()
    ensure_no_sensitive_data(value, context="offline scheduler replay")
    value["report_hash"] = content_hash(value)
    with out.open("x") as target:
        target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"statuses": value["next_model_visible_statuses"], "created_paths": value["created_paths"],
        "provider_requests": 0, "report_hash": value["report_hash"]}))


if __name__ == "__main__":
    main()
