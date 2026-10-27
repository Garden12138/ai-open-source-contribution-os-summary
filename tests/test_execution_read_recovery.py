"""Native startup blockers and frozen supplemental reads at worker boundaries."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json

import pytest
from sqlalchemy import select

from app.agent_runtime import activate
from app.contribution_workflow import advance_workflows, stop_workflow
from app.execution_outcome import baseline_outcome, classify_verification
from app.models import ChangeSetProposal, CodingSession, ExecutionAttempt
from app.planner import PlanningContextWorker
from app.sandbox_worker.fake import FakeVerifyRuntime
from app.sandbox_worker.verification import VerifyCommandStatus
from app.workbench import Workbench
from tests.test_node_environments import node_files
from tests.test_node_workflow import node_workflow


@pytest.mark.parametrize("message", [
    "Error: Failed to load native addon. Tried: pi_natives.linux-arm64.node",
    "Error: Cannot find module './build/Release/addon.node'",
    "Error [ERR_DLOPEN_FAILED]: invalid ELF header",
    "Error: Could not locate the bindings file",
    "ImportError: libexample.so: cannot open shared object file",
    "OSError: library.so: undefined symbol: example",
])
@pytest.mark.parametrize("stream", ["stderr_log", "stdout_log"])
def test_native_loader_failure_is_classified_beyond_display_excerpt(message, stream):
    evidence = {"command_id":"unit", "status":"failed", "exit_code":1,
                stream:"ordinary startup output\n" * 800 + message}
    result = baseline_outcome({"inspection":{"command_evidence":[evidence]}})
    assert result["status"] == "environment_blocked"
    assert result["missing_dependencies"] == ["native_addon_unavailable"]
    assert len(result["failures"][0]["output"]) <= 8000


def test_assertion_failure_does_not_become_native_loader_blocker():
    result = classify_verification({"command_evidence":[{"status":"failed", "exit_code":1,
        "stdout_log":"AssertionError: native addon returned incorrect result"}]})
    assert result["status"] == "failed" and not result["missing_dependencies"]


def runtime_node(tmp_path, monkeypatch):
    from tests import test_workbench as fixtures
    original = fixtures.seeded
    def seed(path):
        result = original(path)
        with result[0].session() as session:
            activate(Workbench(session, result[1].artifact_root), result[2])
            session.commit()
        return result
    monkeypatch.setattr(fixtures, "seeded", seed)
    files = node_files("npm", "10.9.4")
    files["packages/lib/helper.js"] = "export const helper = 'frozen source';\n"
    return node_workflow(tmp_path, monkeypatch, "npm", "10.9.4", files_override=files), files


def tick(db, settings, workers):
    for worker in workers:
        job = asyncio.run(worker.run_once())
        if job:
            assert job.state in {"succeeded", "queued"}, (job.kind, job.error_code, job.error_message)
    advance_workflows(db, settings)


def test_native_baseline_stops_before_model_and_reconciles_old_blocker_once(tmp_path, monkeypatch):
    (db, settings, task, workers, code, review), _ = runtime_node(tmp_path, monkeypatch)
    class NativeFailure(FakeVerifyRuntime):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            text = "Error: Failed to load native addon. Tried: pi_natives.linux-arm64.node"
            return replace(result, command_evidence=tuple(replace(e,
                status=VerifyCommandStatus.FAILED, exit_code=1, stderr_log=text,
                stderr_bytes=len(text), stderr_hash=hashlib.sha256(text.encode()).hexdigest())
                for e in result.command_evidence))
    workers[-1].verifier = NativeFailure()
    old_hash = None
    for _ in range(8):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", job.error_message
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            if old_hash is None and wb.latest(task, "execution_baseline_ready"):
                old_hash = wb.append(task, "automation_blocked", {"reason_code":"proposal_structure_invalid",
                    "reason":"Historical failure", "retryable":False}, key="old-failure").record_hash
        advance_workflows(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        blocker = wb.active_blocker(task)
        assert blocker.payload["reason_code"] == "environment_blocked"
        assert blocker.payload["missing_dependencies"] == ["native_addon_unavailable"]
        assert blocker.payload["superseded_blocker_hash"] == old_hash
        assert any(e.record_hash == old_hash for e in wb.history(task))
        assert not code.invocations and not review.invocations
        assert not session.scalar(select(CodingSession))
        assert not session.scalar(select(ChangeSetProposal))
        attempt = session.scalar(select(ExecutionAttempt))
        # Reconcile an older classifier without replacing historical evidence.
        from app.execution_outcome import reconcile_baseline_blocker
        count = len(wb.history(task))
        assert reconcile_baseline_blocker(wb, attempt)
        assert len(wb.history(task)) == count
        from app.execution_outcome import require_coding_environment
        from app.workbench import WorkbenchError
        with pytest.raises(WorkbenchError) as error:
            require_coding_environment(wb, attempt.id)
        assert error.value.code == "environment_blocked"


def supplemental_read(tmp_path, monkeypatch, *, missing=False):
    (db, settings, task, workers, code, review), files = runtime_node(tmp_path, monkeypatch)
    requested = "packages/lib/guessed.js" if missing else "packages/lib/helper.js"
    proposal = code.contents[-1]
    code.contents[:] = [json.dumps({"summary":"Read the source and helper", "operations":[],
        "read_paths":["packages/lib/index.js", requested]}), proposal]
    class Inspector:
        def __init__(self): self.calls = []
        async def inspect(self, **kwargs):
            self.calls.append(kwargs)
            return {"entries":[{"path":p, "content":files[p],
                "prior_hash":hashlib.sha256(files[p].encode()).hexdigest(), "executable":False}
                for p in kwargs["paths"]]}
    inspector = Inspector()
    reader = PlanningContextWorker(db, settings, inspector, worker_id="supplemental-reader")
    for _ in range(10):
        tick(db, settings, workers)
        with db.session() as session:
            if Workbench(session, settings.artifact_root).latest(task, "agent_code_read_requested"):
                return db, settings, task, workers, code, review, reader, inspector, requested
    pytest.fail("Supplemental read was not dispatched")


@pytest.mark.parametrize("missing", [False, True])
def test_helper_or_missing_file_returns_bound_result_without_format_retry(tmp_path, monkeypatch, missing):
    db, settings, task, workers, code, review, reader, inspector, requested = supplemental_read(
        tmp_path, monkeypatch, missing=missing)
    job = asyncio.run(reader.run_once())
    assert job.state == "succeeded", job.error_message
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        receipt = wb.latest(task, "agent_code_tool_result")
        value = wb.read(receipt.payload["artifact_id"])
        assert value["read_only"]
        assert value["allowed_change_paths"] == ["packages/lib/index.js"]
        assert value["missing_paths"] == ([requested] if missing else [])
        assert "packages/lib/helper.js" in value["directory_inventory"]
        assert not wb.latest(task, "proposal_rejected")
        child = next(j for j in wb.jobs(task) if j.id == job.result_data["next_job_id"])
        assert child.payload["root_job_id"]
    assert [c["paths"] for c in inspector.calls] == ([] if missing else [(requested,)])
    for _ in range(12):
        tick(db, settings, workers)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert wb.latest(task, "awaiting_acceptance")
        assert len(code.invocations) == 2
        assert "CONTRIBOS_CODING_TOOL_RESULT=" in code.invocations[-1].prompt


@pytest.mark.parametrize("invalid", ["stop", "changed_context", "unrequested", "duplicate", "hash"])
def test_supplemental_read_rejects_stale_or_invalid_result_without_dispatch(tmp_path, monkeypatch, invalid):
    db, settings, task, workers, code, review, reader, inspector, requested = supplemental_read(tmp_path, monkeypatch)
    if invalid in {"stop", "changed_context"}:
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            if invalid == "stop":
                stop_workflow(wb, task, key="stop-read")
            else:
                context = wb.latest(task, "context_ready")
                wb.append(task, "context_ready", {**context.payload, "archive_hash":"e" * 64}, key="changed")
    else:
        original = inspector.inspect
        async def bad(**kwargs):
            value = await original(**kwargs)
            if invalid == "unrequested": value["entries"][0]["path"] = "unrequested.js"
            if invalid == "duplicate": value["entries"] *= 2
            if invalid == "hash": value["entries"][0]["prior_hash"] = "0" * 64
            return value
        inspector.inspect = bad
    job = asyncio.run(reader.run_once())
    # Stop may cancel the queued child before it can be leased.
    assert job is None or job.state in {"failed", "cancelled"}
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert not wb.latest(task, "agent_code_tool_result")
        assert not session.scalar(select(ChangeSetProposal))
        assert len(code.invocations) == 1


@pytest.mark.parametrize("invalid", ["stop", "source_hash", "regression", "baseline_hash", "busy"])
def test_startup_runtime_repair_rejects_changed_or_unproven_inputs(tmp_path, monkeypatch, invalid):
    from pathlib import Path
    from app.execution_recovery import resume_startup_after_runtime_repair
    from app.sandbox_worker import workspace_jobs
    from app.workbench import WorkbenchError
    (db, settings, task, workers, code, review), _ = runtime_node(tmp_path, monkeypatch)
    class NativeFailure(FakeVerifyRuntime):
        async def run(self, **kwargs):
            value = await super().run(**kwargs)
            return replace(value, command_evidence=tuple(replace(e, status=VerifyCommandStatus.FAILED,
                exit_code=1, stderr_log="Error: Failed to load native addon") for e in value.command_evidence))
    workers[-1].verifier = NativeFailure()
    for _ in range(5): tick(db, settings, workers)
    monkeypatch.setattr(workspace_jobs, "preparation_needs_restore", lambda w, r: True)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        attempt = session.scalar(select(ExecutionAttempt))
        baseline = workspace_jobs.preparation_receipt(w, attempt.id)
        sources = {"app/coding.py", "app/coding_reads.py", "app/node_environments.py", "app/execution_outcome.py",
            "app/sandbox_worker/node_dependencies.py", "app/sandbox_worker/node_execution.py", "app/sandbox_worker/verification.py"}
        proof = {"version":"native-workspace-runtime-repair-v1", "attempt_hash":attempt.record_hash,
            "baseline_hash":baseline.record_hash, "source_hashes":{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources},
            "regression_artifact_id":w.artifact({"exit_code":0, "passed":1, "failed":0})}
        if invalid == "stop": stop_workflow(w, task, key="stop-repair")
        if invalid == "source_hash": proof["source_hashes"]["app/coding.py"] = "0" * 64
        if invalid == "baseline_hash": proof["baseline_hash"] = "0" * 64
        if invalid == "regression": proof["regression_artifact_id"] = w.artifact({"exit_code":1, "passed":1, "failed":1})
        if invalid == "busy": w.enqueue(task, "execution_question", {}, key="busy-repair")
        before = {e.id:e.record_hash for e in w.history(task)}
        with pytest.raises(WorkbenchError): resume_startup_after_runtime_repair(w, attempt.id, settings, proof)
        assert before == {e.id:e.record_hash for e in w.history(task)}
        assert not w.latest(task, "preparation_runtime_retry_requested")
        assert not code.invocations and not review.invocations


@pytest.mark.parametrize("response", ["proposal", "read", "exhausted"])
def test_last_coding_call_reserves_delivery_and_exhaustion_never_retries(tmp_path, monkeypatch, response):
    from tests.test_coding_source_reads import direct_fixture
    from tests.test_workbench import populate_round
    from app.contribution_workflow import retry_execution_job
    from app.jobs import JobTransitionError
    db, settings, task, workers, code, review = direct_fixture(tmp_path, monkeypatch)
    populate_round(code, review)
    code.contents.pop(0)
    if response == "read":
        code.contents[0] = json.dumps({"summary":"Reread captured source", "operations":[], "read_paths":["app/plans.py"]})
    for _ in range(8):
        for i, worker in enumerate(workers):
            if i != 2:
                completed = asyncio.run(worker.run_once())
                if completed: assert completed.state == "succeeded"
        advance_workflows(db, settings)
        with db.session() as session:
            w = Workbench(session, settings.artifact_root)
            job = next((j for j in w.jobs(task) if j.kind == "change_set_proposal" and j.state == "queued"), None)
            if job:
                root = job.id
                for i in range(6 if response == "exhausted" else 5):
                    w.append(task, "agent_code_call", {"root_job_id":root,"attempt_number":i+1},
                        key=f"historical-call:{i}", actor="orchestrator")
                break
    else: pytest.fail("Proposal was not queued")
    completed = asyncio.run(workers[2].run_once())
    assert completed.state == ("succeeded" if response == "proposal" else "failed")
    assert len(code.invocations) == (0 if response == "exhausted" else 1)
    if code.invocations:
        invocation = code.invocations[-1]
        assert invocation.output_schema["properties"]["read_paths"]["maxItems"] == 0
        assert invocation.output_schema["properties"]["operations"]["minItems"] == 1
        assert '"remaining_including_this_call":1' in invocation.prompt
    if response != "proposal":
        assert completed.error_code == "proposal_call_budget_exceeded"
        advance_workflows(db, settings)
        advance_workflows(db, settings)
        with db.session() as session:
            w = Workbench(session, settings.artifact_root)
            assert w.active_blocker(task).payload["retryable"] is False
            assert w.active_blocker(task).payload["used_calls"] == 6
            before = {e.id:e.record_hash for e in w.history(task)}
            with pytest.raises(JobTransitionError, match="调用预算"):
                retry_execution_job(w, completed.id, settings)
            assert before == {e.id:e.record_hash for e in w.history(task)}
            assert len([e for e in w.history(task) if e.kind == "agent_code_call"]) == 6
