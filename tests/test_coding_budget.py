"""Exact one-time budget decisions preserve original usage and edit authority."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json

import pytest
from sqlalchemy import select

from app.coding_budget import CodingBudgetDecision, EVENT, extend_and_resume, proposal_budget
from app.contribution_workflow import advance_workflows, retry_execution_job, stop_workflow
from app.database import Database
from app.jobs import JobTransitionError
from app.models import Job
from app.workbench import Workbench, WorkbenchError
from tests.test_coding_source_reads import direct_fixture
from tests.test_workbench import populate_round


def exhausted(tmp_path, monkeypatch):
    db, settings, task, workers, code, review = direct_fixture(tmp_path, monkeypatch)
    populate_round(code, review)
    valid = code.contents[1]
    for _ in range(8):
        for i, worker in enumerate(workers):
            if i != 2:
                job = asyncio.run(worker.run_once())
                if job: assert job.state == "succeeded", job.error_message
        advance_workflows(db, settings)
        with db.session() as session:
            w = Workbench(session, settings.artifact_root)
            root = next((j for j in w.jobs(task) if j.kind == "change_set_proposal" and j.state == "queued"), None)
            if root:
                for i in range(6):
                    w.append(task, "agent_code_call", {"root_job_id":root.id,"attempt_number":i+1},
                        key=f"past-call:{i}", actor="orchestrator")
                root_id = root.id
                break
    else: pytest.fail("Proposal was not queued")
    failed = asyncio.run(workers[2].run_once())
    assert failed.id == root_id and failed.state == "failed" and not code.invocations
    advance_workflows(db, settings)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        budget = proposal_budget(w, task, root_id)
        request = CodingBudgetDecision(task_id=task, root_job_id=root_id, failed_job_id=root_id,
            expected_revision=budget["revision"], requested_limit=8, instruction_hash="a"*64)
    return db, settings, task, workers, code, review, root_id, request, valid


@pytest.mark.parametrize("last_read", [False, True])
def test_exact_extension_uses_only_two_remaining_calls_and_preserves_records(tmp_path, monkeypatch, last_read):
    db, settings, task, workers, code, review, root, request, valid = exhausted(tmp_path, monkeypatch)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        originals = {e.id:e.record_hash for e in w.history(task)}
        authorization = w.latest(task, "execution_authorized").record_hash
        job = extend_and_resume(w, request, settings)
        assert extend_and_resume(w, request, settings).id == job.id
        assert proposal_budget(w, task, root)["remaining"] == 2
        assert w.latest(task, "agent_maintenance_recorded")
    read = json.dumps({"summary":"Read exact source", "operations":[], "read_paths":["app/plans.py"]})
    code.contents[:] = [read, read if last_read else valid]
    for _ in range(10):
        for worker in workers:
            completed = asyncio.run(worker.run_once())
            if completed:
                assert completed.state in {"succeeded", "failed"}, completed.error_message
        advance_workflows(db, settings)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        budget = proposal_budget(w, task, root)
        assert budget["used"] == budget["limit"] == 8 and budget["remaining"] == 0
        assert len(code.invocations) == 2
        assert '"remaining_including_this_call":1' in code.invocations[-1].prompt
        assert code.invocations[-1].output_schema["properties"]["read_paths"]["maxItems"] == 0
        assert all({e.id:e.record_hash for e in w.history(task)}.get(i) == h for i,h in originals.items())
        assert len([e for e in w.history(task) if e.kind == EVENT]) == 1
        assert w.latest(task, "execution_authorized").record_hash == authorization
        assert extend_and_resume(w, request, settings).id == job.id
        assert bool(w.latest(task, "awaiting_acceptance")) is not last_read
        if last_read:
            assert w.active_blocker(task).payload["call_limit"] == 8
            failed = next(j for j in reversed(w.jobs(task)) if j.error_code == "proposal_call_budget_exceeded")
            with pytest.raises(JobTransitionError, match="调用预算"):
                retry_execution_job(w, failed.id, settings)
    # Opening an old database retains the grant and all original counters.
    reopened = Database(settings.database_url)
    with reopened.session() as session:
        assert proposal_budget(Workbench(session, settings.artifact_root), task, root)["used"] == 8
    reopened.close()


@pytest.mark.parametrize("invalid", ["stale", "stop", "busy", "wrong_task", "wrong_failed", "model_changed"])
def test_changed_or_unbound_decisions_cannot_grant_budget(tmp_path, monkeypatch, invalid):
    db, settings, task, workers, code, review, root, request, valid = exhausted(tmp_path, monkeypatch)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        if invalid == "stale": request = request.model_copy(update={"expected_revision":"0"*64})
        if invalid == "wrong_task": request = request.model_copy(update={"task_id":"another-task"})
        if invalid == "wrong_failed": request = request.model_copy(update={"failed_job_id":"absent-job"})
        if invalid == "stop": stop_workflow(w, task, key="stop-budget")
        if invalid == "busy": w.enqueue(task, "execution_question", {}, key="busy-budget")
        if invalid == "model_changed":
            from app import model_settings
            original = model_settings.task_identity
            monkeypatch.setattr(model_settings, "task_identity", lambda *args:replace(original(*args), model="changed-model"))
        before = {e.id:e.record_hash for e in w.history(task)}
        with pytest.raises(WorkbenchError): extend_and_resume(w, request, settings)
        assert not w.latest(task, EVENT)
        assert {e.id:e.record_hash for e in w.history(task)} == before
        assert proposal_budget(w, task, root)["limit"] == 6
        assert not code.invocations


def test_failed_retry_rolls_back_grant_and_second_decision_cannot_extend_again(tmp_path, monkeypatch):
    from app import contribution_workflow
    db, settings, task, workers, code, review, root, request, valid = exhausted(tmp_path, monkeypatch)
    retry = contribution_workflow.retry_execution_job
    def fail(*args, **kwargs): raise JobTransitionError("Injected retry failure")
    monkeypatch.setattr(contribution_workflow, "retry_execution_job", fail)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        with pytest.raises(JobTransitionError): extend_and_resume(w, request, settings)
        assert not w.latest(task, EVENT) and proposal_budget(w, task, root)["limit"] == 6
    monkeypatch.setattr(contribution_workflow, "retry_execution_job", retry)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        extend_and_resume(w, request, settings)
        with pytest.raises(WorkbenchError, match="重复请求不同"):
            extend_and_resume(w, request.model_copy(update={"instruction_hash":"b"*64}), settings)
        assert len([e for e in w.history(task) if e.kind == EVENT]) == 1


@pytest.mark.parametrize("invalid", ["actor", "binding", "limit", "version"])
def test_malformed_or_untrusted_grant_fails_closed(tmp_path, monkeypatch, invalid):
    db, settings, task, workers, code, review, root, request, valid = exhausted(tmp_path, monkeypatch)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        state = proposal_budget(w, task, root)
        payload = {"version":"coding-budget-extension-v1","root_job_id":root,"request":request.model_dump(),
            "binding":state["binding"],"previous_limit":6,"new_limit":8,"used_calls_at_grant":6,
            "consent_source":"explicit_user_instruction"}
        if invalid == "binding": payload["binding"] = {**payload["binding"],"attempt_hash":"0"*64}
        if invalid == "limit": payload["new_limit"] = 10
        if invalid == "version": payload["version"] = "unknown"
        w.append(task, EVENT, payload, key="forged-grant", actor="model" if invalid == "actor" else "maintenance",
            job_id=root, plan_id=state["binding"]["plan_id"])
        with pytest.raises(WorkbenchError, match="预算授权"):
            proposal_budget(w, task, root)


def test_supplemental_read_cache_retains_only_same_lineage_and_editing_view(tmp_path, monkeypatch):
    from app.coding import CodingConversationService, _load_coding_context
    from app.coding_reads import read_cache
    db, settings, task, workers, code, review, root, request, valid = exhausted(tmp_path, monkeypatch)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        root_job = session.get(Job, root)
        coding = CodingConversationService(session, artifact_root=settings.artifact_root).require_session(root_job.payload["execution_attempt_id"])
        view = _load_coding_context(session, artifact_root=settings.artifact_root, coding_session=coding)
        for i, (path, context_hash, root_id) in enumerate([
            ("app/first-helper.py", view.context_hash, root), ("app/second-helper.py", view.context_hash, root),
            ("app/stale-helper.py", "0"*64, root), ("app/other-helper.py", view.context_hash, "other-root")]):
            job = w.enqueue(task, "coding_source_read", {**root_job.payload, "root_job_id":root_id}, key=f"cache-job:{i}")
            # Controlled completed-read fixtures; no repository code executes.
            from app.jobs import JobService
            jobs = JobService(session)
            leased = jobs.lease_next(worker_id="cache", kinds=("coding_source_read",))
            assert leased.id == job.id
            jobs.start(job.id, worker_id="cache")
            text = f"# exact helper {i}\n"
            artifact = w.artifact({"context_hash":context_hash,"entries":[{"path":path,"content":text,
                "prior_hash":hashlib.sha256(text.encode()).hexdigest(),"executable":False}]})
            w.append(task, "agent_code_tool_result", {"context_hash":context_hash,"artifact_id":artifact},
                key=f"cache-receipt:{i}", actor="orchestrator", job_id=job.id)
            jobs.succeed(job.id, worker_id="cache")
        cache = read_cache(w, task, root, view)
        assert {entry["path"] for entry in cache["entries"]} == {"app/first-helper.py", "app/second-helper.py"}
        assert cache["read_only"]
