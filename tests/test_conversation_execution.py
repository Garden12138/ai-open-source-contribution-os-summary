from __future__ import annotations
import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.api import create_app
from app.execution_environment import environment_plan
from app.execution_outcome import classify_verification
from app.execution_chat import ask_execution, ExecutionQuestionWorker
from app.contribution_workflow import advance_workflows
from app.jobs import JobService
from app.models import Job, ReviewRun
from app.planner import PlanningService
from app.workbench import WorkbenchError
from app.workbench_view import workbench_view
from app.task_states import ContributionTaskStateService
from tests.test_workbench import seeded, automatic_fixture, populate_round
from tests.test_nvidia_workflows import ScriptedRunner, _identity


def test_unsupported_environment_disables_execution_and_is_in_planner_evidence(tmp_path):
    from tests.test_workbench import run_turn, result
    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence = wb.read(context.payload["artifact_id"])
        evidence["inventory"] += ["requirements.txt", "package.json", "yarn.lock"]
        wb.append(task_id, "context_ready", {**context.payload, "artifact_id": wb.artifact(evidence)}, key="mixed-context")
        wb.request(task_id, text="完善方案", parent_id=None, expected_hash=None, key="plan")
    completed, runner = run_turn(db, settings, result(content))
    assert completed.state == "succeeded", completed.error_message
    status = json.loads(runner.invocations[0].prompt)["execution_status"]
    assert status["environment"]["status"] == "pending_scope"
    assert not status["readiness"]["blockers"]
    assert not status["workflow"]["source_reading_requires_project_dependencies"]
    assert status["repository_snapshot"]["base_sha"] == "a" * 40
    with TestClient(create_app(settings)) as client:
        view = client.get(f"/api/v1/tasks/{task_id}/workbench").json()
        assert view["plans"]
        assert not view["execution"]["actions"]["can_execute"]
        assert not view["execution"]["plan_stale"]
        assert view["execution"]["readiness"]["blockers"][0]["code"] == "environment_unsupported"
        assert not view["execution"]["readiness"]["blocks_repository_reading"]
        assert not view["execution"]["readiness"]["blocks_planning"]
    # A progress question in planning state must not create or change a plan.
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = wb.latest_plan(task_id).record_hash
        question = ask_execution(wb, task_id, text="为什么不能继续", key="why")
        frozen = wb.read(question.payload["evidence_hash"])
        assert frozen["environment"]["status"] == "unsupported"
    runner = ScriptedRunner(json.dumps({"reply": "环境尚未适配，需要补齐测试环境。"}))
    worker = ExecutionQuestionWorker(db, settings, runner, _identity(settings.implementation_model), worker_id="question")
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        assert PlanningService(session, settings.artifact_root).latest_plan(task_id).record_hash == before
        assert ContributionTaskStateService(session).current(task_id).to_state == "planning"


def test_job_completion_is_not_test_success_and_missing_packages_are_environment_errors():
    value = {"succeeded": False, "test_results": [{"outcome": "failed", "exit_code": 1}],
        "command_evidence": [{"command_id": "lint", "exit_code": 1, "status": "failed",
         "stderr_log": "ModuleNotFoundError: No module named 'filetype'"}]}
    result = classify_verification(value)
    assert result["status"] == "environment_blocked"
    assert result["missing_dependencies"] == ["filetype"]
    assert result["passed"] == 0
    value["command_evidence"][0]["stderr_log"] = "AssertionError: incorrect result"
    assert classify_verification(value)["status"] == "failed"
    assert classify_verification({"succeeded": True, "test_results": []})["status"] == "failed"
    assert classify_verification({"succeeded": True, "test_results": [{"outcome":"passed", "exit_code":0}]})["status"] == "passed"


def test_source_availability_answer_uses_facts_without_model_or_execution(tmp_path):
    db, settings, task_id, _, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence = wb.read(context.payload["artifact_id"])
        evidence["inventory"] += ["setup.py", "src/unread.py"]
        wb.append(task_id, "context_ready", {**context.payload, "artifact_id": wb.artifact(evidence)}, key="source")
        job = ask_execution(wb, task_id, text="为什么没有原文证据，你没有拉取到源码吗？", key="source-question")
        frozen = wb.read(job.payload["evidence_hash"])
        assert frozen["repository_snapshot"]["archive_downloaded"]
        assert not any(f["path"] == "src/unread.py" for f in frozen["repository_snapshot"]["files"])
    runner = ScriptedRunner()  # Any model call would fail the test.
    worker = ExecutionQuestionWorker(db, settings, runner, _identity(settings.implementation_model), worker_id="source-reader")
    assert asyncio.run(worker.run_once()).state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        answer = wb.latest(task_id, "execution_answer")
        assert answer.payload["answer_source"] == "workflow-facts-v1"
        assert "完整源码归档已经下载" in answer.payload["text"]
        assert "读取源码不需要先安装项目依赖" in answer.payload["text"]
        assert not runner.invocations
        assert not wb.latest_plan(task_id) and not wb.latest(task_id, "execution_authorized")


def test_clarification_answer_runs_deferred_read_before_model_and_dependencies(tmp_path):
    db, settings, task_id, _, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence = wb.read(context.payload["artifact_id"])
        evidence["inventory"] += ["setup.py", "src/unread.py"]
        wb.append(task_id, "context_ready", {**context.payload, "artifact_id": wb.artifact(evidence)}, key="source")
        wb.append(task_id, "assistant_message", {"reply": "确认范围", "questions": [{"id":"scope"}],
                  "read_paths":["src/unread.py"], "plan_id":None}, key="scope-question")
        job = wb.request(task_id, text="两者都需要", parent_id=None, expected_hash=None, key="answer")
        assert job.kind == "planning_context" and job.payload["paths"] == ["src/unread.py"]
        assert job.payload["archive_hash"] == context.payload["archive_hash"]
        assert wb.request(task_id, text="两者都需要", parent_id=None, expected_hash=None, key="answer").id == job.id
        assert not any(j.kind in {"planning_archive", "planning_turn", "execution_prepare"} for j in wb.jobs(task_id))
        assert not wb.latest(task_id, "execution_authorized")


@pytest.mark.parametrize("paths,status", [
    (["app.py"], "builtin"), (["requirements.lock"], "supported"),
    (["package.json", "package-lock.json"], "supported"),
    (["package.json", "yarn.lock"], "supported"), (["pyproject.toml"], "unsupported"),
    (["requirements.txt", "package.json"], "unsupported"),
])
def test_environment_support_is_explicit(paths, status):
    assert environment_plan(paths)["status"] == status


def test_execution_question_is_persistent_bounded_and_does_not_change_running_task(tmp_path):
    db, settings, task_id, *_ = automatic_fixture(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        before = ContributionTaskStateService(session).current(task_id).record_hash
        active = [j.id for j in wb.jobs(task_id) if j.state == "queued"]
        job = ask_execution(wb, task_id, text="现在执行到哪一步？", key="question-one")
        assert ask_execution(wb, task_id, text="现在执行到哪一步？", key="question-one").id == job.id
        with pytest.raises(WorkbenchError):
            ask_execution(wb, task_id, text="重复但不同的问题", key="question-one")
        with pytest.raises(WorkbenchError):
            ask_execution(wb, task_id, text="第二个问题", key="question-two")
    runner = ScriptedRunner(json.dumps({"reply": "当前等待隔离探索，还没有测试结果。"}))
    worker = ExecutionQuestionWorker(db, settings, runner, _identity(settings.implementation_model), worker_id="question")
    completed = asyncio.run(worker.run_once())
    assert completed.state == "succeeded", completed.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert ContributionTaskStateService(session).current(task_id).record_hash == before
        assert all(session.get(Job, ident).cancel_requested_at is None for ident in active)
        assert wb.latest(task_id, "execution_answer").payload["evidence_hash"] == job.payload["evidence_hash"]
        assert not wb.latest(task_id, "stop_requested")


def test_cancelled_question_cannot_append_late_answer(tmp_path):
    db, settings, task_id, *_ = seeded(tmp_path)
    with db.session() as session:
        job = ask_execution(PlanningService(session, settings.artifact_root), task_id, text="解释进度", key="q")
    class CancellingRunner:
        async def complete(self, invocation):
            with db.session() as session:
                JobService(session).request_cancel(job.id)
            return type("Completion", (), {"content": '{"reply":"late"}'})()
    worker = ExecutionQuestionWorker(db, settings, CancellingRunner(), _identity(settings.implementation_model), worker_id="question")
    assert asyncio.run(worker.run_once()).state == "cancelled"
    with db.session() as session:
        assert not PlanningService(session, settings.artifact_root).latest(task_id, "execution_answer")


def test_question_api_and_explicit_goal_change_have_separate_authorization(tmp_path):
    db, settings, task_id, *_ = automatic_fixture(tmp_path)
    settings = replace(settings, local_access_token="test-local-token")
    route = f"/api/v1/tasks/{task_id}/workbench"
    headers = {"X-ContribOS-Token": "test-local-token", "Idempotency-Key": "question"}
    with TestClient(create_app(settings)) as client:
        assert client.post(route + "/messages", json={"text":"进度", "mode":"question"}).status_code == 401
        assert client.post(route + "/messages", json={"text":"进度", "mode":"question"}, headers=headers).status_code == 202
        view = client.get(route).json()
        assert view["execution"]["question_busy"]
        assert view["state"] == "executing"
        payload = {"text":"只修改核心逻辑", "expected_revision":"old"}
        assert client.post(route + "/replan", json=payload, headers={**headers, "Idempotency-Key":"change"}).status_code == 409
        payload["expected_revision"] = view["execution"]["revision"]
        response = client.post(route + "/replan", json=payload, headers={**headers, "Idempotency-Key":"change"})
        assert response.status_code == 202, response.text
        assert client.post(route + "/replan", json=payload, headers={**headers, "Idempotency-Key":"change"}).status_code == 202
        timeline = client.get(route + "/timeline").json()
        assert timeline["cursor"] > 0
        assert client.get(route + f'/timeline?after={timeline["cursor"]}').json()["events"] == []
    # Repeated coordinator ticks cannot cancel the new planning job.
    for _ in range(2):
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        messages = [e for e in wb.history(task_id) if e.kind == "user_message" and e.payload["text"] == payload["text"]]
        assert len(messages) == 1
        assert session.get(Job, messages[0].job_id).cancel_requested_at is None


def test_realistic_missing_dependencies_stop_before_review_or_repair(tmp_path):
    from app.sandbox_worker.fake import FakeVerifyRuntime
    from app.sandbox_worker.verification import VerifyCommandStatus
    from app.sandbox_worker.verification import VerifyCommandEvidence
    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    class MissingDependency(FakeVerifyRuntime):
        async def run(self, **kwargs):
            value = await super().run(**kwargs)
            import hashlib
            evidence = []
            for e in value.command_evidence:
                message = "ModuleNotFoundError: No module named 'filetype'\n"
                evidence.append(replace(e, status=VerifyCommandStatus.FAILED, exit_code=1,
                    stderr_log=message, stderr_bytes=len(message.encode()), stderr_hash=hashlib.sha256(message.encode()).hexdigest()))
            return replace(value, command_evidence=tuple(evidence))
    workers[0].verify_runtime = MissingDependency()
    populate_round(code, review)
    for _ in range(12):
        for worker in workers:
            completed = asyncio.run(worker.run_once())
            if completed:
                assert completed.state == "succeeded", completed.error_message
        advance_workflows(db, settings)
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            blocked = wb.latest(task_id, "automation_blocked")
            if blocked:
                assert blocked.payload["reason_code"] == "environment_blocked"
                view = workbench_view(wb, task_id)
                assert view["verification"]["status"] == "environment_blocked"
                assert len(view["attempts"]) == 1
                assert session.scalar(select(ReviewRun)) is None
                break
    else:
        pytest.fail("Missing dependency did not stop workflow")


def test_unknown_review_severity_and_missing_evidence_fail_closed():
    from app.review_provider import _parse_review
    for finding in [{"severity":"critical", "evidence":"x"}, {"severity":"info"}]:
        with pytest.raises(ValueError):
            _parse_review(json.dumps({"verdict":"pass", "reason_code":"ok", "findings":[finding]}))


def test_dependency_preparation_restart_and_manifest_change_fail_closed(tmp_path, monkeypatch):
    import hashlib
    from tests import test_workbench as fixtures
    from tests.test_sandbox_dependencies import _FakeDependencyRuntime, _lease
    from app.sandbox_worker.dependency_jobs import DependencyJobWorker, load_dependency
    from app.executions import ExecutionAttemptService
    from app.models import ExecutionAttempt
    original = fixtures._plan_content
    monkeypatch.setattr(fixtures, "_plan_content", lambda: replace(original(),
        files_to_inspect=("requirements.lock",), files_likely_to_change=("requirements.lock",),
        commands_to_run=(replace(original().commands_to_run[0], argv=("python", "-m", "pytest", "-q")),)))
    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    populate_round(code, review)
    proposal = json.loads(code.contents[1])
    proposal["operations"][0].update(path="requirements.lock", content="",
        expected_prior_hash=hashlib.sha256(b"original content for requirements.lock\n").hexdigest())
    code.contents[1] = json.dumps(proposal)
    class Proxy:
        def __init__(self): self.stopped = []
        async def start(self, *, image_digest, policy): return _lease(policy)
        async def stop(self, lease, **kwargs): self.stopped.append(lease)
    runtime, proxy = _FakeDependencyRuntime(), Proxy()
    class Inspector:
        async def inspect(self, **kwargs):
            return {"entries": [{"path": "requirements.lock", "content": "", "prior_hash": hashlib.sha256(b"original content for requirements.lock\n").hexdigest()}]}
    workers[-1].dependencies, workers[-1].proxy, workers[-1].inspector = runtime, proxy, Inspector()
    dependency = DependencyJobWorker(db, settings, runtime, proxy, worker_id="dependencies")
    for _ in range(12):
        for worker in [*workers, dependency]:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", (job.kind, job.error_message)
        advance_workflows(db, settings)
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            if wb.latest(task_id, "automation_blocked"):
                attempt = session.scalar(select(ExecutionAttempt))
                receipt = load_dependency(wb, attempt.id)
                stage = ExecutionAttemptService(session).current(attempt.id)
                assert stage.stage == "implement"
                assert receipt.result.source_kind == "explore"
                assert wb.latest(task_id, "automation_blocked").payload["reason_code"] == "environment_stale"
                assert not wb.latest(task_id, "awaiting_acceptance")
                assert wb.latest(task_id, "dependencies_ready")
                break
    else:
        pytest.fail("Changed manifest did not invalidate the frozen environment")
    # A reconstructed worker recovers cleanup from append-only allocation receipts.
    dependency = DependencyJobWorker(db, settings, runtime, proxy, worker_id="restarted")
    asyncio.run(dependency.cleanup())
    before = len(runtime.commands)
    asyncio.run(dependency.cleanup())
    assert len(runtime.commands) == before
    assert proxy.stopped
    with db.session() as session:
        assert PlanningService(session, settings.artifact_root).latest(task_id, "dependency_workspace_disposed")
