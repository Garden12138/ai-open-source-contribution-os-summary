"""Real journal/domain authorization, deterministic sandbox/model fixtures."""
import asyncio

import pytest
from sqlalchemy import select

from app.agent_v2.contracts import Decision, ToolCall
from app.agent_v2.journal import AgentJournal
from app.agent_v2.orchestrator import TaskOrchestrator
from app.contribution_workflow import advance_workflows
from app.models import ExecutionAttempt
from app.workbench import Workbench, WorkbenchError
from tests.test_workbench import automatic_fixture


def prepared(tmp_path):
    db, settings, task, workers, code, review = automatic_fixture(tmp_path, runtime_v2=True)
    assert asyncio.run(workers[0].run_once()).state == "succeeded"
    advance_workflows(db, settings)
    assert asyncio.run(workers[4].run_once()).state == "succeeded"
    with db.session() as s:
        attempt = s.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id == task))
        attempt_id = attempt.id
    return db, settings, task, attempt_id, workers


def test_session_freezes_real_domain_authorization_and_idempotent_continue(tmp_path):
    db, settings, task, attempt, _ = prepared(tmp_path)
    with db.session() as s:
        wb = Workbench(s, settings.artifact_root)
        service = TaskOrchestrator(wb, settings)
        journal = service.start(attempt)
        spec = journal.spec()
        cp = journal.checkpoint()
        assert spec.binding.attempt_id == attempt and spec.binding.authorization_hash
        assert service.start(attempt).session_id == journal.session_id
        assert journal.control("continue", cp.revision) == cp
        assert journal.checkpoint().usage == {}
        s.commit()
    db.close()
    from app.database import Database
    db = Database(settings.database_url)
    with db.session() as s:
        reopened = AgentJournal(Workbench(s, settings.artifact_root), task, journal.session_id)
        assert reopened.checkpoint() == cp
        assert reopened.spec() == spec


def test_cas_pause_resume_and_budget_never_reset(tmp_path):
    db, settings, task, attempt, _ = prepared(tmp_path)
    with db.session() as s:
        journal = TaskOrchestrator(Workbench(s, settings.artifact_root), settings).start(attempt)
        cp = journal.checkpoint()
        usage = journal.charge(cp, {"model_calls": 1, "reads": 1})
        running = journal.transition(cp, state="model", usage=usage, key="running")
        with pytest.raises(WorkbenchError, match="事实已变化"):
            journal.transition(cp, state="model", key="stale")
        assert journal.control("pause", running.revision) == running
        drained = journal.transition(running, state="ready", key="drained")
        from app.agent_v2.loop import schedule
        assert schedule(journal) is None
        paused = journal.checkpoint()
        resumed = journal.control("continue", paused.revision)
        assert resumed.usage == usage and resumed.state == "ready"
        with pytest.raises(WorkbenchError):
            journal.charge(resumed, {"model_calls": 100})
        assert journal.remaining()["model_calls"] == journal.spec().limits.model_calls - 1
        s.commit()


def test_cannot_start_without_baseline_or_exact_approval(tmp_path):
    db, settings, task, workers, *_ = automatic_fixture(tmp_path)
    with db.session() as s:
        wb = Workbench(s, settings.artifact_root)
        attempt = s.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id == task))
        with pytest.raises(WorkbenchError):
            TaskOrchestrator(wb, settings).start(attempt.id)
        assert not wb.latest(task, "agent_v2_session")


def test_typed_protocol_allows_multiple_reads_but_never_shell_or_approval():
    d = Decision(calls=[ToolCall(call_id="one", name="read_file", arguments={"path":"a.py"}),
                        ToolCall(call_id="two", name="list_paths", arguments={})])
    assert len(d.calls) == 2
    for name in ("shell", "docker", "approve_plan", "publish", "fork"):
        with pytest.raises(ValueError):
            ToolCall(call_id="malicious", name=name, arguments={})


def test_existing_v1_authorization_stays_v1_after_enabling_rollout(tmp_path):
    from dataclasses import replace
    db,s,task,workers,_,_=automatic_fixture(tmp_path)
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        original=wb.latest(task,'execution_authorized').record_hash
    s=replace(s,agent_runtime_v2_enabled=True)
    for worker in (workers[0],workers[4],workers[1]):
        job=asyncio.run(worker.run_once());assert job.state=='succeeded',job.error_message
        advance_workflows(db,s)
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        assert wb.latest(task,'execution_authorized').record_hash==original
        assert wb.latest(task,'execution_authorized').payload['agent_runtime_version']=='agent-runtime-v1'
        assert wb.latest(task,'agent_v2_session') is None
        assert any(j.kind in {'coding_turn','change_set_proposal'} for j in wb.jobs(task))
