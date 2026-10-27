"""Advancing a blocked plan must create work, without granting execution consent."""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from app.conversation import advance_conversations, fast_intent
from app.planner import PlanningContextWorker, PlanningService
from app.planning_continuation import planning_next_step, proceed_requested, recover_failed_forward
from app.workbench import Workbench
from tests.test_agent_runtime import send
from tests.test_workbench import seeded, result, run_turn


def blocked_plan(tmp_path):
    from app.agent_runtime import activate
    db, settings, task, content, context = seeded(tmp_path)
    with db.session() as session:
        w = PlanningService(session, settings.artifact_root)
        w.request(task, text="准备方案", parent_id=None, expected_hash=None, key="initial")
    job, _ = run_turn(db, settings, result(content))
    assert job.state == "succeeded", job.error_message
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        evidence = w.read(context.payload["artifact_id"])
        evidence["inventory"].append("pyproject.toml")
        w.append(task, "context_ready", {**context.payload, "artifact_id":w.artifact(evidence)}, key="unread-metadata")
        activate(w, task)
        session.commit()
    return db, settings, task, content


@pytest.mark.parametrize("text", ["按你的下一步计划进行", "按照下一步计划推进", "按你建议的方案执行。"])
def test_exact_forward_controls_are_not_questions(text):
    assert proceed_requested(text)
    assert fast_intent(text) == "continue"


@pytest.mark.parametrize("text", ["好", "按你的下一步计划进行吗？", "不要按你的计划进行", "先解释下一步，再按计划执行"])
def test_ambiguous_or_compound_text_is_not_a_forward_control(text):
    assert not proceed_requested(text)


def test_forward_prepares_child_plan_instead_of_reentering_evidence_loop(tmp_path, monkeypatch):
    from app.archives import RepositoryArchiveStore
    db, s, task, content = blocked_plan(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        context = w.latest(task, "context_ready")
        evidence = w.read(context.payload["artifact_id"])
        parent = w.latest_plan(task)
        parent_id, parent_hash = parent.id, parent.record_hash
        assert planning_next_step(w, task)["read_paths"] == ["pyproject.toml"]
    send(db, s, task, "按你的下一步计划进行")
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        job = w.jobs(task)[-1]
        assert job.kind == "planning_context" and job.state == "queued"
        assert job.payload["paths"] == ["pyproject.toml"]
        assert not any(j.kind == "conversation_intent" for j in w.jobs(task))
        assert not w.latest(task, "execution_authorized")
    monkeypatch.setattr(RepositoryArchiveStore, "lookup", lambda *a, **kw: object())
    class Metadata:
        async def inspect(self, *, paths, **kwargs):
            assert paths == ("pyproject.toml",)
            text = '[project]\nname="fixture"\nversion="1.0"\n'
            return {"inventory":sorted(evidence["inventory"]), "entries":[{"path":paths[0],
                "content":text, "prior_hash":hashlib.sha256(text.encode()).hexdigest(), "executable":False}], "truncated":False}
    read = asyncio.run(PlanningContextWorker(db, s, Metadata(), worker_id="sandbox").run_once())
    assert read.state == "succeeded", read.error_message
    planned, _ = run_turn(db, s, result(content))
    assert planned.state == "succeeded", planned.error_message
    advance_conversations(db, s)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        child = w.latest_plan(task)
        assert child.id != parent_id and child.parent_version_id == parent_id
        assert next(e for e in w.history(task) if e.kind == "plan_bound" and e.plan_version_id == parent_id)
        assert session.get(type(child), parent_id).record_hash == parent_hash
        assert not w.latest(task, "execution_authorized")
        assert all(j.state == "succeeded" for j in w.jobs(task))
        from app.workbench_view import workbench_view
        assert workbench_view(w, task)["actions"]["can_execute"]


def failed_forward(w, task):
    from app.jobs import JobService
    source = w.append(task, "conversation_received", {"version":"conversation-v1", "text":"按你的下一步计划进行"},
        key="old-forward", actor="local-user", commit=False)
    job = w.enqueue(task, "conversation_intent", {"message_id":source.id}, key="old-failed", commit=False)
    jobs = JobService(w.session)
    lease = jobs.lease_next(worker_id="old-worker", kinds=("conversation_intent",))
    jobs.start(lease.id, worker_id="old-worker")
    jobs.fail(lease.id, worker_id="old-worker",
        error_code="conversation_tool_budget", error_message="read budget reached")
    w.append(task, "conversation_classifying", {"version":"conversation-v1", "message_id":source.id},
        key="old-classified", job_id=job.id, commit=False)
    w.append(task, "conversation_finished", {"version":"conversation-v1", "message_id":source.id,"status":"failed"},
        key="old-finished", job_id=job.id, commit=False)
    return source, job


def recovery_fixture(tmp_path):
    db, s, task, content = blocked_plan(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        source, failed = failed_forward(w, task)
        session.commit()
        ids = source.id, source.record_hash, failed.id
    return db, s, task, ids


def test_recovery_is_once_source_bound_and_preserves_failed_history(tmp_path):
    db, s, task, (source_id, source_hash, failed_id) = recovery_fixture(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        before = {e.id:e.record_hash for e in w.history(task)}
        assert recover_failed_forward(w, task, s)
        session.commit()
        assert not recover_failed_forward(w, task, s)
        request = w.latest(task, "planning_continuation_requested")
        assert request.actor_id == "orchestrator"
        assert request.payload["continuation_source"] == {"id":source_id,"record_hash":source_hash}
        receipt = w.latest(task, "agent_planning_continued")
        assert receipt.payload["failed_job_id"] == failed_id
        assert receipt.payload["execution_authorized"] is False
        assert all(e.record_hash == before[e.id] for e in w.history(task) if e.id in before)
        assert not w.latest(task, "execution_authorized")


@pytest.mark.parametrize("interruption", ["stop_requested", "context_ready", "new_question"])
def test_recovery_never_resurrects_a_stale_user_request(tmp_path, interruption):
    db, s, task, _ = recovery_fixture(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        if interruption == "new_question":
            w.append(task, "conversation_received", {"version":"conversation-v1", "text":"为什么？"}, key="new-question", actor="local-user")
        elif interruption == "context_ready":
            w.append(task, interruption, w.latest(task, "context_ready").payload, key="new-context")
        else:
            w.append(task, interruption, {}, key="stop")
        assert not recover_failed_forward(w, task, s)
        assert not w.latest(task, "agent_planning_continued")
