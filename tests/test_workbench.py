from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from app.api import create_app
from app.config import Settings
from app.database import Database
from app.planning import ContributionTaskService
from app.planner import PlanningService, PlanningTurnWorker, frozen_inputs
from app.provenance import content_hash
from app.workbench import WorkbenchError
from app.schemas import PlanVersionCreateRequest
from app.contribution_workflow import authorize_execution, ContributionStartWorker
from tests.test_planning import _seed_analysis, _plan_content
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_runtime_wiring import SIGNING_KEY_HEX
from app.sandbox_worker.specs import SandboxPolicy


def seeded(tmp_path):
    path = tmp_path / "workbench.db"
    analysis_id, _, _ = _seed_analysis(path)
    db = Database(f"sqlite:///{path}")
    db.create_schema()
    settings = Settings(
        database_url=f"sqlite:///{path}",
        artifact_root=str(tmp_path / "artifacts"),
        implementation_provider="nvidia_nim",
        review_provider="nvidia_nim",
        sandbox_stage_runtime="docker",
        workbench_runner_image="sha256:" + "a" * 64,
        sandbox_job_spec_signing_key=bytes.fromhex(SIGNING_KEY_HEX),
    )
    content = replace(_plan_content(), questions_for_maintainer=())
    with db.session() as session:
        task = ContributionTaskService(session).create(
            analysis_version_id=analysis_id, idempotency_key="task"
        )
        wb = PlanningService(session, settings.artifact_root)
        paths = sorted(
            set(content.files_to_inspect) | set(content.files_likely_to_change)
        )
        artifact_id = wb.artifact(
            {
                "inventory": paths,
                "entries": [
                    {
                        "path": p,
                        "prior_hash": hashlib.sha256(b"content").hexdigest(),
                        "content": "content",
                        "executable": False,
                    }
                    for p in paths
                ],
                "truncated": False,
            }
        )
        context = wb.append(
            task.id,
            "context_ready",
            {
                "artifact_id": artifact_id,
                "base_sha": "a" * 40,
                "archive_hash": "b" * 64,
                "branch": "main",
                "image": settings.workbench_runner_image,
                "inputs_hash": content_hash(frozen_inputs(session, task.id)),
                "policy_hash": SandboxPolicy().policy_hash,
            },
            key="context",
        )
    return db, settings, task.id, content, context


def result(content):
    payload = content.hash_payload()
    payload.pop("schema_version")
    return json.dumps(
        {
            "reply": "已阅读代码，这是改造方案。",
            "questions": [],
            "read_paths": [],
            "plan": payload,
        }
    )


def run_turn(db, settings, reply):
    runner = ScriptedRunner(reply)
    job = asyncio.run(
        PlanningTurnWorker(
            db,
            settings,
            runner,
            _identity(settings.implementation_model),
            worker_id="planner",
        ).run_once()
    )
    return job, runner


@pytest.mark.parametrize("legacy_stop", [False, True])
def test_stopping_then_new_message_survives_coordinator_and_restart(tmp_path, legacy_stop):
    from app.contribution_workflow import advance_workflows, stop_workflow
    from app.jobs import JobService

    db, settings, task_id, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        first = wb.request(task_id, text="旧消息", parent_id=None, expected_hash=None, key="old-message")
        if legacy_stop:
            wb.append(task_id, "stop_requested", {}, key="stop:first-stop", actor="local-user")
            JobService(session).request_cancel(first.id)
        else:
            stop_workflow(wb, task_id, key="first-stop")
        assert JobService(session).get(first.id).state == "cancelled"
        second = wb.request(task_id, text="修改方案", parent_id=None, expected_hash=None, key="new-message")
    db.close()
    db = Database(settings.database_url)
    db.create_schema()
    for _ in range(3):
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        # Retrying an acknowledged stop must not cancel a newer user request.
        stop_workflow(wb, task_id, key="first-stop")
        assert JobService(session).get(second.id).state == "queued"
        assert not wb.latest(task_id, "execution_authorized")
    completed, runner = run_turn(db, settings, result(content))
    assert completed.id == second.id and completed.state == "succeeded"
    assert len(runner.invocations) == 1
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.latest_plan(task_id)
        third = wb.request(task_id, text="再修改", parent_id=plan.id, expected_hash=plan.record_hash, key="third-message")
        stop_workflow(wb, task_id, key="second-stop")
        assert JobService(session).get(third.id).state == "cancelled"
        assert wb.latest(task_id, "assistant_message")
    db.close()


def test_stop_event_and_cancellation_roll_back_together(tmp_path, monkeypatch):
    from app.contribution_workflow import stop_workflow
    from app.jobs import JobService

    db, settings, task_id, _, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        pending = wb.request(task_id, text="讨论", parent_id=None, expected_hash=None, key="before-stop")
        def fail(*args, **kwargs):
            raise RuntimeError("injected cancellation failure")
        with monkeypatch.context() as patch:
            patch.setattr(JobService, "request_cancel", fail)
            with pytest.raises(RuntimeError):
                stop_workflow(wb, task_id, key="atomic-stop")
            session.rollback()
        assert wb.latest(task_id, "stop_requested") is None
        assert JobService(session).get(pending.id).state == "queued"
    db.close()


def test_stop_scope_holds_sqlite_writer_lock_before_reading_jobs(tmp_path, monkeypatch):
    import sqlite3
    from app.contribution_workflow import stop_workflow
    from app.jobs import JobService

    db, settings, task_id, _, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        pending = wb.request(task_id, text="讨论", parent_id=None, expected_hash=None, key="locked-stop")
        original_jobs = wb.jobs
        def locked_jobs(identifier):
            with sqlite3.connect(tmp_path / "workbench.db", timeout=0.01) as competitor:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    competitor.execute("UPDATE jobs SET updated_at=updated_at WHERE id=?", (pending.id,))
            return original_jobs(identifier)
        monkeypatch.setattr(wb, "jobs", locked_jobs)
        stop_workflow(wb, task_id, key="writer-lock")
        assert JobService(session).get(pending.id).state == "cancelled"
    db.close()


def test_plan_chat_edit_approve_restart_and_no_writes(tmp_path):
    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        first = wb.request(
            task_id, text="制定方案", parent_id=None, expected_hash=None, key="first"
        )
        assert (
            wb.request(
                task_id,
                text="制定方案",
                parent_id=None,
                expected_hash=None,
                key="first",
            ).id
            == first.id
        )
        with pytest.raises(WorkbenchError):
            wb.request(
                task_id,
                text="重复并发",
                parent_id=None,
                expected_hash=None,
                key="another",
            )
    completed, runner = run_turn(db, settings, result(content))
    assert completed.state == "succeeded", completed.error_message
    assert runner.invocations[0].stage.value == "planning"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.latest_plan(task_id)
        assert plan.goal == content.goal
        assert (
            wb.binding(task_id, plan.id).payload["context_hash"] == context.record_hash
        )
        changed = replace(content, goal="A revised implementation goal")
        request = PlanVersionCreateRequest(
            **{
                k: v for k, v in changed.hash_payload().items() if k != "schema_version"
            },
            parent_version_id=plan.id,
        )
        child = wb.save(
            task_id, request, context_hash=context.record_hash, key="manual"
        )
        session.commit()
        assert child.parent_version_id == plan.id
        job = authorize_execution(
            wb,
            task_id,
            plan_id=child.id,
            plan_hash=child.record_hash,
            key="execute",
            settings=settings,
        )
        assert job.kind == "contribution_start"
        assert not any(e.kind.startswith("publication") for e in wb.history(task_id))
        assert (
            authorize_execution(
                wb,
                task_id,
                plan_id=child.id,
                plan_hash=child.record_hash,
                key="execute",
                settings=settings,
            ).id
            == job.id
        )
    db.close()
    db = Database(settings.database_url)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task_id).goal == changed.goal
        assert len(wb.history(task_id)) >= 6
        with pytest.raises(Exception):
            session.execute(text("UPDATE workbench_events SET kind='forged'"))


def test_questions_block_execution_and_answer_creates_plan(tmp_path):
    db, settings, task_id, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(
            task_id, text="规划", parent_id=None, expected_hash=None, key="q"
        )
    question = json.dumps(
        {
            "reply": "需要确认兼容范围",
            "questions": [
                {
                    "id": "compat",
                    "prompt": "兼容旧接口吗？",
                    "options": ["兼容", "允许变更"],
                }
            ],
            "read_paths": [],
            "plan": None,
        }
    )
    completed, _ = run_turn(db, settings, question)
    assert completed.state == "succeeded", completed.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task_id) is None
        wb.request(
            task_id, text="兼容旧接口", parent_id=None, expected_hash=None, key="answer"
        )
    completed, _ = run_turn(db, settings, result(content))
    assert completed.state == "succeeded", completed.error_message


def test_invalid_provider_and_secret_canary_do_not_create_plan(tmp_path):
    db, settings, task_id, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(task_id, text="规划", parent_id=None, expected_hash=None, key="bad")
    completed, _ = run_turn(
        db,
        settings,
        result(content).replace(
            content.goal, "ghp_workbenchcanary12345678901234567890"
        ),
    )
    assert completed.state == "failed"
    with db.session() as session:
        assert (
            PlanningService(session, settings.artifact_root).latest_plan(task_id)
            is None
        )
    raw = Path(db.engine.url.database).read_bytes()
    assert b"ghp_workbenchcanary" not in raw


def test_unavailable_existing_file_is_not_accepted_as_read_evidence(tmp_path):
    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence = wb.read(context.payload["artifact_id"])
        unavailable = content.files_likely_to_change[0]
        assert unavailable in evidence["inventory"]
        for entry in evidence["entries"]:
            if entry["path"] == unavailable:
                entry.update(content=None, prior_hash=None, executable=None)
        updated = wb.append(
            task_id, "context_ready",
            {**context.payload, "artifact_id": wb.artifact(evidence)},
            key="unavailable-context",
        )
        request = PlanVersionCreateRequest(**{
            key: value for key, value in content.hash_payload().items()
            if key != "schema_version"
        })
        with pytest.raises(WorkbenchError, match="读取"):
            wb.save(task_id, request, context_hash=updated.record_hash, key="unread")
        assert wb.latest_plan(task_id) is None


@pytest.mark.parametrize("kind,stage", [
    ("planning_archive", "下载仓库归档"),
    ("planning_context", "读取仓库代码"),
    ("planning_turn", "规划模型"),
])
def test_planning_failure_reports_stage_without_raw_exception(tmp_path, caplog, kind, stage):
    from app.jobs import JobService
    from app.workbench_worker import WorkbenchJobWorker

    db = Database(f"sqlite:///{tmp_path / 'errors.db'}")
    db.create_schema()
    canary = "ghp_workbencherrorcanary123456789012345"

    class FailingWorker(WorkbenchJobWorker):
        kinds = (kind,)

        async def execute(self, job):
            raise RuntimeError("raw upstream exception " + canary)

    with db.session() as session:
        JobService(session).enqueue(
            kind=kind, payload={"task_id": "fixture"},
            idempotency_key="failure", max_attempts=1,
        )
    failed = asyncio.run(FailingWorker(db, worker_id="fixture").run_once())
    assert failed.state == "failed"
    assert failed.error_code == kind + "_failed"
    assert stage in failed.error_message
    assert canary not in failed.error_message
    with db.engine.connect() as connection:
        dump = "\n".join(connection.connection.driver_connection.iterdump())
    assert canary not in dump and "raw upstream exception" not in dump
    assert canary not in caplog.text and "raw upstream exception" not in caplog.text
    assert "exception=RuntimeError" in caplog.text


def test_model_schema_failure_logs_only_error_types(tmp_path, caplog):
    from app.jobs import JobService
    from app.planner import PlanningReply
    from app.workbench_worker import WorkbenchJobWorker

    db = Database(f"sqlite:///{tmp_path / 'invalid-reply.db'}")
    db.create_schema()
    canary = "ghp_validationinputcanary123456789012345"

    class Worker(WorkbenchJobWorker):
        kinds = ("planning_turn",)

        async def execute(self, job):
            PlanningReply.model_validate({"reply": "done", canary: canary})

    with db.session() as session:
        JobService(session).enqueue(
            kind="planning_turn", payload={}, idempotency_key="invalid-schema",
            max_attempts=1,
        )
    failed = asyncio.run(Worker(db, worker_id="fixture").run_once())
    assert failed.error_code == "planning_output_invalid"
    assert "extra_forbidden" in caplog.text
    assert canary not in caplog.text and canary not in failed.error_message


def test_workbench_api_guards_and_readable_document(tmp_path):
    db, settings, task_id, content, _ = seeded(tmp_path)
    settings = replace(settings, local_access_token="test-local-token")
    app = create_app(settings)
    with TestClient(app) as client:
        route = f"/api/v1/tasks/{task_id}/workbench"
        assert client.get(route).status_code == 200
        payload = {"text": "生成方案"}
        assert (
            client.post(
                route + "/messages", json=payload, headers={"Idempotency-Key": "one"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                route + "/messages",
                json=payload,
                headers={
                    "Idempotency-Key": "one",
                    "X-ContribOS-Token": "test-local-token",
                    "Origin": "http://testserver",
                },
            ).status_code
            == 403
        )
        response = client.post(
            route + "/messages",
            json=payload,
            headers={"Idempotency-Key": "one", "X-ContribOS-Token": "test-local-token"},
        )
        assert response.status_code == 202, response.text
    completed, _ = run_turn(db, settings, result(content))
    assert completed.state == "succeeded", completed.error_message
    with TestClient(app) as client:
        value = client.get(route).json()
        assert value["plans"][0]["goal"] == content.goal
        assert value["context"]["base_sha"] == "a" * 40


class ReadOnlyGitHub:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def resolve_repository_head(self, repository, branch):
        return branch, "a" * 40


def automatic_fixture(tmp_path, configure_models=None):
    from app.archives import RepositoryArchiveStore
    from app.changesets import ChangeSetStore
    from app.coding import CodingContextJobWorker, NvidiaCodingJobWorker
    from app.execution_worker import ExecutionStageWorker
    from app.providers.runtime import resolve_job_spec_signer
    from app.review_provider import NvidiaReviewJobWorker
    from app.sandbox_worker.fake import (
        FakeExploreRuntime,
        FakeImplementRuntime,
        FakeVerifyRuntime,
    )
    from tests.test_execution_pipeline import ARCHIVE_BYTES
    from tests.test_nvidia_workflows import FakeContextRuntime

    db, settings, task_id, content, context = seeded(tmp_path)
    if configure_models:
        configure_models(db, settings, task_id)
    archive = tmp_path / "repo.tar.gz"
    archive.write_bytes(ARCHIVE_BYTES)
    archive_hash = RepositoryArchiveStore(settings.artifact_root).put_file(archive)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.append(
            task_id,
            "context_ready",
            {**context.payload, "archive_hash": archive_hash},
            key="real-archive",
        )
        wb.request(
            task_id,
            text="完整实现",
            parent_id=None,
            expected_hash=None,
            key="auto-plan",
        )
    assert run_turn(db, settings, result(content))[0].state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.latest_plan(task_id)
        authorize_execution(
            wb,
            task_id,
            plan_id=plan.id,
            plan_hash=plan.record_hash,
            key="auto-execute",
            settings=settings,
        )
    job = asyncio.run(
        ContributionStartWorker(
            db, settings, ReadOnlyGitHub, worker_id="start"
        ).run_once()
    )
    assert job.state == "succeeded", job.error_message
    code = ScriptedRunner()
    review = ScriptedRunner()
    workers = [
        ExecutionStageWorker(
            db,
            resolve_job_spec_signer(settings),
            worker_id="sandbox",
            archive_store=RepositoryArchiveStore(settings.artifact_root),
            change_set_store=ChangeSetStore(settings.artifact_root),
            artifact_root=settings.artifact_root,
            explore_runtime=FakeExploreRuntime(),
            implement_runtime=FakeImplementRuntime(),
            verify_runtime=FakeVerifyRuntime(),
        ),
        CodingContextJobWorker(
            db,
            artifact_root=settings.artifact_root,
            runtime=FakeContextRuntime(),
            worker_id="context",
        ),
        NvidiaCodingJobWorker(
            db,
            artifact_root=settings.artifact_root,
            runner=code,
            identity=_identity(settings.implementation_model),
            worker_id="code",
        ),
        NvidiaReviewJobWorker(
            db,
            artifact_root=settings.artifact_root,
            runner=review,
            identity=_identity(settings.review_model),
            worker_id="review",
        ),
    ]
    return db, settings, task_id, workers, code, review


def populate_round(code, review, *, verdict="pass"):
    code.contents.extend(
        [
            json.dumps({"reply": "按批准的方案实施。"}),
            json.dumps(
                {
                    "summary": "Implement approved behavior",
                    "operations": [
                        {
                            "path": "app/plans.py",
                            "kind": "write",
                            "expected_prior_hash": hashlib.sha256(
                                b"original content for app/plans.py\n"
                            ).hexdigest(),
                            "content": "def preserved_api():\n    return True\n",
                            "executable": False,
                        }
                    ],
                }
            ),
        ]
    )
    review.contents.append(
        json.dumps(
            {
                "verdict": verdict,
                "reason_code": "verified" if verdict == "pass" else "needs_repair",
                "findings": [
                    {
                        "severity": "info" if verdict == "pass" else "blocking",
                        "location": "app/plans.py",
                        "evidence": "Verified evidence",
                        "recommendation": (
                            "Accept" if verdict == "pass" else "Fix boundary condition"
                        ),
                        "verdict": verdict,
                    }
                ],
            }
        )
    )


def drive_round(db, settings, task_id, workers):
    from app.contribution_workflow import _advance

    for _ in range(7):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", (job.kind, job.error_message)
        with db.session() as session:
            _advance(
                PlanningService(session, settings.artifact_root), task_id, settings
            )


def test_automatic_execution_stops_at_human_acceptance_and_can_replan(tmp_path):
    from app.contribution_workflow import _advance, stop_workflow
    from app.task_states import ContributionTaskStateService
    from app.models import ExecutionAttempt

    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    populate_round(code, review)
    drive_round(db, settings, task_id, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task_id, "awaiting_acceptance")
        assert not wb.latest(task_id, "publication_confirmed")
        assert (
            ContributionTaskStateService(session).current(task_id).to_state == "ready"
        )
        stop_workflow(wb, task_id, key="replan")
        _advance(wb, task_id, settings)
        assert (
            ContributionTaskStateService(session).current(task_id).to_state
            == "planning"
        )
        plan = wb.latest_plan(task_id)
        content = {
            k: v
            for k, v in replace(
                _plan_content(),
                goal="Revised after acceptance",
                questions_for_maintainer=(),
            )
            .hash_payload()
            .items()
            if k != "schema_version"
        }
        child = wb.save(
            task_id,
            PlanVersionCreateRequest(**content, parent_version_id=plan.id),
            context_hash=wb.latest(task_id, "context_ready").record_hash,
            key="acceptance-edit",
        )
        session.commit()
        authorize_execution(
            wb,
            task_id,
            plan_id=child.id,
            plan_hash=child.record_hash,
            key="second-execute",
            settings=settings,
        )
    job = asyncio.run(
        ContributionStartWorker(
            db, settings, ReadOnlyGitHub, worker_id="second-start"
        ).run_once()
    )
    assert job.state == "succeeded", job.error_message
    populate_round(code, review)
    drive_round(db, settings, task_id, workers)
    with db.session() as session:
        attempts = list(
            session.scalars(
                select(ExecutionAttempt).order_by(ExecutionAttempt.attempt_number)
            )
        )
        assert len(attempts) == 2 and attempts[-1].plan_version_id == child.id
        assert (
            ContributionTaskStateService(session).current(task_id).to_state == "ready"
        )


def test_automatic_repairs_are_limited_to_two(tmp_path):
    from app.contribution_workflow import advance_workflows
    from app.models import ExecutionAttempt

    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    for _ in range(3):
        populate_round(code, review, verdict="block")
    for _ in range(35):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", (job.kind, job.error_message)
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert len(list(session.scalars(select(ExecutionAttempt)))) == 3
        assert wb.latest(task_id, "automation_blocked")
        assert not wb.latest(task_id, "awaiting_acceptance")
        assert len(review.invocations) == 3


def test_stale_edits_cancelled_model_and_upstream_movement_fail_closed(tmp_path):
    from app.jobs import JobService

    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(
            task_id, text="制定方案", parent_id=None, expected_hash=None, key="initial"
        )
    assert run_turn(db, settings, result(content))[0].state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.latest_plan(task_id)
        revised = {
            k: v
            for k, v in replace(content, goal="Edited goal").hash_payload().items()
            if k != "schema_version"
        }
        child = wb.save(
            task_id,
            PlanVersionCreateRequest(**revised, parent_version_id=plan.id),
            context_hash=context.record_hash,
            key="edit",
        )
        session.commit()
        with pytest.raises(WorkbenchError, match="方案已更新"):
            wb.save(
                task_id,
                PlanVersionCreateRequest(**revised, parent_version_id=plan.id),
                context_hash=context.record_hash,
                key="stale-edit",
            )
        pending = wb.request(
            task_id,
            text="进一步优化",
            parent_id=child.id,
            expected_hash=child.record_hash,
            key="cancelled",
        )

    class CancellingRunner(ScriptedRunner):
        async def complete(self, invocation):
            from app.contribution_workflow import stop_workflow
            with db.session() as session:
                stop_workflow(PlanningService(session, settings.artifact_root), task_id, key="during-model")
            return await super().complete(invocation)

    job = asyncio.run(
        PlanningTurnWorker(
            db,
            settings,
            CancellingRunner(result(content)),
            _identity(settings.implementation_model),
            worker_id="cancelled-planner",
        ).run_once()
    )
    assert job.state == "cancelled"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task_id).id == child.id
        authorize_execution(
            wb,
            task_id,
            plan_id=child.id,
            plan_hash=child.record_hash,
            key="moved",
            settings=settings,
        )

    class MovedGitHub(ReadOnlyGitHub):
        async def resolve_repository_head(self, *_):
            return "main", "c" * 40

    job = asyncio.run(
        ContributionStartWorker(db, settings, MovedGitHub, worker_id="moved").run_once()
    )
    assert job.state == "failed" and "上游提交已变化" in job.error_message
    with db.session() as session:
        assert not PlanningService(session, settings.artifact_root).latest(
            task_id, "automation_started"
        )


def test_workbench_migration_preserves_existing_drafts_and_references(tmp_path):
    from tests.test_execution_pipeline import _complete_verified_execution
    from app.models import DraftPullRequest, PublishConfirmation

    app, execution_id, task_id, _ = _complete_verified_execution(tmp_path)
    with TestClient(app) as client:
        review = client.post(
            f"/api/v1/executions/{execution_id}/reviews",
            json={"actor_id": "user-1", "reviewer": "fake"},
            headers={"Idempotency-Key": "migration-review"},
        ).json()
        intent = client.post(
            f"/api/v1/reviews/{review['id']}/publish-intents",
            json={"actor_id": "user-1", "title": "Fixture", "body": "Verified fixture"},
            headers={"Idempotency-Key": "migration-intent"},
        ).json()
        response = client.post(
            f"/api/v1/publish-intents/{intent['id']}/confirm",
            json={
                "actor_id": "user-1",
                "confirmation_nonce": intent["confirmation_nonce"],
            },
        )
        assert response.status_code == 200, response.text
    db = app.state.database
    with db.session() as session:
        draft = session.scalar(select(DraftPullRequest))
        draft_id, draft_hash = draft.id, draft.record_hash
        confirmation_id = session.scalar(select(PublishConfirmation.id))
    # Reconstruct the immediately preceding schema with its preserved immutable rows.
    import sqlite3
    from app.migrations.versions.v0022_publish_intents import _STATEMENTS

    with sqlite3.connect(db.engine.url.database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        from tests.migration_fixtures import remove_task_visibility_schema
        remove_task_visibility_schema(connection)
        connection.execute("DROP TABLE job_model_bindings")
        connection.execute("DROP TABLE model_config_versions")
        connection.execute("DELETE FROM _schema_migrations WHERE revision='0030_model_settings'")
        connection.executescript(
            "DROP TABLE workbench_events; DROP TRIGGER draft_pull_requests_no_update; DROP TRIGGER draft_pull_requests_no_delete; ALTER TABLE draft_pull_requests RENAME TO drafts_backup;"
        )
        connection.execute(
            next(s for s in _STATEMENTS if "CREATE TABLE draft_pull_requests" in s)
        )
        connection.execute(
            "INSERT INTO draft_pull_requests SELECT * FROM drafts_backup"
        )
        connection.execute("DROP TABLE drafts_backup")
        for sql in _STATEMENTS:
            if "CREATE" in sql and (
                "INDEX ix_draft_pull_requests_" in sql
                or "TRIGGER draft_pull_requests_no_" in sql
            ):
                connection.execute(sql)
        connection.execute(
            "DELETE FROM _schema_migrations WHERE revision='0029_workbench'"
        )
    report = db.create_schema()
    assert report.applied == (
        "0029_workbench",
        "0030_model_settings",
        "0031_task_visibility",
        "0032_minimax_reviews",
    )
    with db.session() as session:
        assert session.get(DraftPullRequest, draft_id).record_hash == draft_hash
        assert (
            session.get(PublishConfirmation, confirmation_id).draft_pull_request_id
            == draft_id
        )
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        with pytest.raises(Exception):
            session.execute(text("UPDATE draft_pull_requests SET provider='github'"))


def test_failed_tests_override_model_pass_and_prevent_acceptance(tmp_path):
    from app.contribution_workflow import advance_workflows
    from app.models import ReviewRun
    from app.sandbox_worker.fake import FakeVerifyRuntime
    from app.sandbox_worker.verification import VerifyCommandStatus

    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)

    class FailedVerify(FakeVerifyRuntime):
        async def run(self, **kwargs):
            inspection = await super().run(**kwargs)
            return replace(
                inspection,
                command_evidence=tuple(
                    replace(e, status=VerifyCommandStatus.FAILED, exit_code=1)
                    for e in inspection.command_evidence
                ),
            )

    workers[0].verify_runtime = FailedVerify()
    populate_round(code, review, verdict="pass")
    for _ in range(12):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", (job.kind, job.error_message)
        advance_workflows(db, settings)
        with db.session() as session:
            reviewed = session.scalar(select(ReviewRun))
            if reviewed:
                assert reviewed.verdict == "block"
                assert any(f["location"] == "verification" for f in reviewed.findings)
                assert not PlanningService(session, settings.artifact_root).latest(
                    task_id, "awaiting_acceptance"
                )
                break
    else:
        raise AssertionError("Failed-test review was not reached")


def test_unchanged_plan_gets_new_version_when_code_evidence_changes(tmp_path):
    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(
            task_id, text="规划", parent_id=None, expected_hash=None, key="basis-first"
        )
    assert run_turn(db, settings, result(content))[0].state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        parent = wb.latest_plan(task_id)
        context = wb.append(
            task_id,
            "context_ready",
            {**context.payload, "base_sha": "c" * 40},
            key="refreshed-basis",
        )
        request = PlanVersionCreateRequest(
            **{
                k: v for k, v in content.hash_payload().items() if k != "schema_version"
            },
            parent_version_id=parent.id,
        )
        child = wb.save(
            task_id,
            request,
            context_hash=context.record_hash,
            key="same-text-new-basis",
        )
        session.commit()
        assert child.id != parent.id and child.content_hash == parent.content_hash
        assert (
            child.parent_version_id == parent.id
            and child.record_hash != parent.record_hash
        )
        assert (
            wb.binding(task_id, child.id).payload["context_hash"] == context.record_hash
        )
        with pytest.raises(WorkbenchError, match="已过期"):
            wb.binding(task_id, parent.id)
