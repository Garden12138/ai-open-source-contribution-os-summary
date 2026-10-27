from __future__ import annotations

import hashlib
import json

import pytest

from app.agent_runtime import activate
from app.workbench import Workbench


def direct_fixture(tmp_path, monkeypatch):
    from tests import test_workbench as fixtures
    original = fixtures.seeded
    def seed(path):
        result = original(path)
        with result[0].session() as session:
            w=Workbench(session,result[1].artifact_root)
            context=w.latest(result[2],"context_ready")
            evidence=w.read(context.payload["artifact_id"])
            evidence["inventory"].append("app/helper.py")
            w.append(result[2],"context_ready",{**context.payload,"artifact_id":w.artifact(evidence)},key="helper-inventory")
            activate(w,result[2]);session.commit()
        return result
    monkeypatch.setattr(fixtures,"seeded",seed)
    return fixtures.automatic_fixture(tmp_path)


def test_uncaptured_helper_and_missing_path_return_real_source_and_inventory_then_implement(tmp_path,monkeypatch):
    from tests import test_workbench as fixtures
    from app.planner import PlanningContextWorker
    from app.archives import RepositoryArchiveStore
    db,s,task,workers,code,review=direct_fixture(tmp_path,monkeypatch)
    fixtures.populate_round(code,review)
    code.contents[0]=json.dumps({"summary":"Read helpers before editing", "operations":[],
        "read_paths":["app/helper.py","app/guessed-helper.py"]})
    class Reader:
        calls=[]
        async def inspect(self,*,paths,**kwargs):
            self.calls.append(paths)
            assert paths==("app/helper.py",)
            text="def helper(): return 'frozen helper'\n"
            return {"inventory":["app/helper.py"],"entries":[{"path":"app/helper.py","content":text,
                "prior_hash":hashlib.sha256(text.encode()).hexdigest(),"executable":False}],"truncated":False}
    reader=Reader()
    workers.append(PlanningContextWorker(db,s,reader,worker_id="source-reader"))
    fixtures.drive_round(db,s,task,workers)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        source=w.latest(task,"agent_code_tool_result")
        value=w.read(source.payload["artifact_id"])
        assert value["entries"][0]["content"]=="def helper(): return 'frozen helper'\n"
        assert value["missing_paths"]==["app/guessed-helper.py"]
        assert "app/helper.py" in value["directory_inventory"]
        assert "app/helper.py" not in value["allowed_change_paths"]
        assert w.latest(task,"awaiting_acceptance")
        assert not w.latest(task,"proposal_rejected")
        assert len(code.invocations)==2
        assert "frozen helper" in code.invocations[1].prompt
        assert not w.latest(task,"publication_confirmed")


@pytest.mark.parametrize("message", [
    "error: Failed to load example native addon for linux-arm64. Cannot find module '/workspace/native/example.linux-arm64.node'",
    "Error: Could not locate the bindings file. Tried native.node",
    "Error [ERR_DLOPEN_FAILED]: libmissing.so: cannot open shared object file",
    "ImportError: example.so: undefined symbol: native_entry",
])
def test_native_startup_failures_are_environment_blockers(message):
    from app.execution_outcome import baseline_outcome
    value={"inspection":{"command_evidence":[{"command_id":"test","status":"failed","exit_code":1,
        "stderr_log":message,"stdout_log":"0 pass, 1 error"}]}}
    result=baseline_outcome(value)
    assert result["status"]=="environment_blocked"
    assert result["missing_dependencies"]==["native_addon_unavailable"]


def test_native_assertion_failure_is_not_a_missing_environment():
    from app.execution_outcome import baseline_outcome
    result=baseline_outcome({"inspection":{"command_evidence":[{"command_id":"native-test","status":"failed",
        "exit_code":1,"stderr_log":"AssertionError: native result differs from expected", "stdout_log":"12 pass, 1 fail"}]}})
    assert result["status"]=="failed" and result["missing_dependencies"]==[]


def test_baseline_blocker_is_append_only_idempotent_and_prevents_coding(tmp_path,monkeypatch):
    from tests import test_workbench as fixtures
    from app.contribution_workflow import advance_workflows
    from app.sandbox_worker.fake import FakeVerifyRuntime
    from dataclasses import replace
    db,s,task,workers,code,review=direct_fixture(tmp_path,monkeypatch)
    class NativeMissing(FakeVerifyRuntime):
        async def run(self,**kwargs):
            value=await super().run(**kwargs)
            evidence=replace(value.command_evidence[0],status="failed",exit_code=1,
                stderr_log="Error: Failed to load fixture native addon for linux-arm64")
            return replace(value,command_evidence=(evidence,))
    workers[-1].verifier=NativeMissing()
    fixtures.drive_round(db,s,task,workers)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        blocked=w.active_blocker(task)
        assert blocked.payload["reason_code"]=="environment_blocked"
        before={e.id:e.record_hash for e in w.history(task)}
        assert not any(j.kind=="change_set_proposal" for j in w.jobs(task))
    advance_workflows(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert {e.id:e.record_hash for e in w.history(task)}==before
    assert code.invocations==[]


def test_runtime_repair_restores_baseline_under_same_exact_approval(tmp_path,monkeypatch):
    from app.contribution_workflow import advance_workflows
    from tests import test_workbench as fixtures
    from app.sandbox_worker.fake import FakeVerifyRuntime
    from app.sandbox_worker import workspace_jobs
    from app.execution_recovery import resume_startup_after_runtime_repair
    from app.executions import ExecutionAttemptService
    from app.provenance import content_hash
    from dataclasses import replace
    from pathlib import Path
    from sqlalchemy import select
    from app.models import ExecutionAttempt
    db,s,task,workers,code,review=direct_fixture(tmp_path,monkeypatch)
    class NativeMissing(FakeVerifyRuntime):
        async def run(self,**kwargs):
            value=await super().run(**kwargs)
            return replace(value,command_evidence=tuple(replace(e,status="failed",exit_code=1,
                stderr_log="Error: Failed to load fixture native addon for linux-arm64") for e in value.command_evidence))
    workers[-1].verifier=NativeMissing()
    fixtures.drive_round(db,s,task,workers)
    monkeypatch.setattr(workspace_jobs,"preparation_needs_restore",lambda w,r:r.id==baseline.id)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        attempt=session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id==task))
        authorization=w.latest(task,"execution_authorized").record_hash
        baseline=workspace_jobs.preparation_receipt(w,attempt.id)
        sources={"app/coding.py","app/coding_reads.py","app/node_environments.py","app/execution_outcome.py",
            "app/sandbox_worker/node_dependencies.py","app/sandbox_worker/node_execution.py",
            "app/sandbox_worker/verification.py"}
        proof={"version":"native-workspace-runtime-repair-v1","attempt_hash":attempt.record_hash,
            "baseline_hash":baseline.record_hash,"source_hashes":{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources},
            "regression_artifact_id":w.artifact({"exit_code":0,"passed":1,"failed":0})}
        job=resume_startup_after_runtime_repair(w,attempt.id,s,proof)
        assert resume_startup_after_runtime_repair(w,attempt.id,s,proof).id==job.id
    # The replacement must own coordinator ticks, even if the old baseline
    # classifier receipt was absent when maintenance resumed the task.
    for _ in range(2): advance_workflows(db,s)
    with db.session() as session:
        assert Workbench(session,s.artifact_root).active_blocker(task) is None
    workers[-1].verifier=FakeVerifyRuntime()
    fixtures.populate_round(code,review);code.contents.pop(0)
    fixtures.drive_round(db,s,task,workers)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.latest(task,"execution_authorized").record_hash==authorization
        assert w.latest(task,"awaiting_acceptance")
        assert w.latest(task,"agent_maintenance_recorded")
        assert len([e for e in w.history(task) if e.kind=="execution_baseline_ready"])==2
        assert resume_startup_after_runtime_repair(w,attempt.id,s,proof).id==job.id
