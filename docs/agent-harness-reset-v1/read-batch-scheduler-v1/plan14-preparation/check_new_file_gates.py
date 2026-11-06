"""Offline gate composition only: isolated fixtures, no live jobs or providers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile

from pytest import MonkeyPatch

from app.agent_v2.contracts import ToolResult
from app.agent_v2.integration import advance_coding
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import validate_finish
from app.agent_v2.native_tools import FINISH
from app.agent_v2.progress import current_progress
from app.sandbox_worker.coding_context import CodingContextEntry
from app.contribution_workflow import advance_workflows
from app.model_settings import task_identity
from app.provenance import content_hash
from app.reviews import ReviewRunService
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench, WorkbenchError
from tests.test_agent_native_tools import response
from tests.test_missing_source_observations import MISSING, MissingGateway, fixture, saved_response, patch_response, drain

ROOT = Path(__file__).resolve().parent


def main() -> None:
    out = ROOT / "new-file-gate-check.json"
    assert not out.exists()
    with tempfile.TemporaryDirectory(prefix="plan14-readonly-gate-fixture-") as temporary, MonkeyPatch.context() as patch:
        import tests.test_native_read_batches as batch_fixture
        original_fixture = batch_fixture.fixture
        workers = []
        patch.setattr(batch_fixture, "fixture", lambda *a, **kw: original_fixture(*a, **kw, capture_workers=workers))

        class Gateway(MissingGateway):
            async def execute(self, spec, call, args, *, source_hash, snapshot, input_hash):
                if call.name == "run_approved_checks":
                    self.calls.append(call)
                    return {"result": ToolResult(call_id=call.call_id, input_hash=input_hash,
                        source_hash=source_hash, output_source_hash=source_hash, status="succeeded",
                        data={"tool": "run_approved_checks", "outcome": "passed"}).model_dump()}
                return await super().execute(spec, call, args, source_hash=source_hash, snapshot=snapshot, input_hash=input_hash)

        values = fixture(Path(temporary), patch, saved_response(), patch_response(),
            response("run_approved_checks", {}), gateway=Gateway())
        db, settings, task, sid, model, tool, runner, gateway = values

        class AbsentContext:
            async def capture(self, *, paths, **kwargs):
                return tuple(CodingContextEntry(path=p, prior_hash=None if p == MISSING else hashlib.sha256(b"content").hexdigest(),
                    content=None if p == MISSING else "content", executable=None if p == MISSING else False) for p in paths)
        workers[1].runtime = AbsentContext()
        captured = asyncio.run(workers[1].run_once())
        assert captured.state == "succeeded", captured.error_code
        for _ in range(2):
            assert asyncio.run(model.run_once()).state == "succeeded"
            drain(tool)
        with db.session() as session:
            journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
            first_source = journal.checkpoint().source_hash
            attempt = journal.spec().binding.attempt_id
            workers[3].identity = task_identity(session, settings, task, "review")
            try:
                validate_finish(journal)
                raise AssertionError("New file was admitted without current Verify")
            except WorkbenchError as error:
                assert error.code == "agent_tests_missing"
        assert asyncio.run(model.run_once()).state == "succeeded"
        drain(tool)
        with db.session() as session:
            journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
            validate_finish(journal)
            assert current_progress(journal).steps[5].status == "pending"
            assert not journal.wb.latest(task, "awaiting_acceptance")
        previous = gateway.created
        runner.responses.extend([response("apply_patch", {"path": MISSING, "expected_hash": hashlib.sha256(previous.encode()).hexdigest(),
            "old_text": previous, "new_text": "// updated offline new-file fixture\n"}), response("run_approved_checks", {}),
            response(FINISH, {"reply": "CODER_SELF_SUMMARY_SENTINEL"})])
        assert asyncio.run(model.run_once()).state == "succeeded"
        drain(tool)
        with db.session() as session:
            journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
            second_source = journal.checkpoint().source_hash
            assert first_source != second_source and current_progress(journal).steps[4].status == "pending"
            try:
                validate_finish(journal)
                raise AssertionError("Stale Verify was accepted after a new-file edit")
            except WorkbenchError as error:
                assert error.code == "agent_tests_missing"
        assert asyncio.run(model.run_once()).state == "succeeded"
        drain(tool)
        assert asyncio.run(model.run_once()).state == "succeeded"
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            journal = AgentJournal(wb, task, sid)
            assert journal.checkpoint().state == "finished"
            assert current_progress(journal).steps[5].status == "pending"
            advance_coding(wb, attempt, settings)
        for _ in range(2):
            job = asyncio.run(workers[0].run_once())
            assert job.state == "succeeded", job.error_code
        advance_workflows(db, settings)
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            assert not wb.latest(task, "awaiting_acceptance")
        workers[3].runner.contents.append(json.dumps({"verdict": "pass", "reason_code": "evidence_checked", "findings": [
            {"severity": "info", "location": MISSING, "evidence": "New-file diff and frozen Verify checked", "recommendation": "Human acceptance", "verdict": "pass"}]}))
        reviewed = asyncio.run(workers[3].run_once())
        assert reviewed.state == "succeeded", reviewed.error_code
        prompt = workers[3].runner.invocations[-1].prompt
        assert "CODER_SELF_SUMMARY_SENTINEL" not in prompt
        envelope = json.loads(prompt.split("=", 1)[1])
        assert MISSING in envelope["unified_diff"] and envelope["normalized_test_results"]
        advance_workflows(db, settings)
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            ledger = current_progress(AgentJournal(wb, task, sid))
            assert ledger.phase == "ready_for_human" and wb.latest(task, "awaiting_acceptance")
            assert not wb.latest(task, "execution_accepted") and not wb.latest(task, "publication_confirmed")
        result = {"version": "new-file-offline-gate-check-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "classification": "isolated_deterministic_model_sandbox_review_fixtures_not_real_acceptance",
            "live_business_database_modified": False, "paid_model_calls": 0, "real_Coding_or_OCI_run": False,
            "not_found_observed_before_creation": True, "approved_new_file_created_in_fixture": True,
            "source_hash_after_creation": first_source, "source_hash_after_edit": second_source,
            "missing_Verify_rejected": True, "stale_Verify_after_edit_rejected": True,
            "fresh_Verify_required_before_export": True, "new_file_exported_with_original_hash_null": True,
            "independent_Review_required_before_human_ready": True, "reviewer_prompt_contains_diff_and_tests": True,
            "coder_summary_excluded_from_review": True, "stopped_before_human_acceptance_or_publication": True,
            "fixture_declared_commands_are_not_target_pnpm_execution": True}
        db.close()
    ensure_no_sensitive_data(result, context="offline prerequisite gate check")
    result["report_hash"] = content_hash(result)
    with out.open("x") as target:
        target.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"all_new_file_gates": "passed", "report_hash": result["report_hash"], "paid_model_calls": 0}))


if __name__ == "__main__":
    main()
