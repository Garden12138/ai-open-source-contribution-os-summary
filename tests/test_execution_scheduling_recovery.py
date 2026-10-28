from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.contribution_workflow import advance_workflows, _advance
from app.execution_control import ExecutionStageControlService
from app.execution_environment import schedule_verification
from app.executions import ExecutionAttemptService
from app.models import ExecutionAttempt, ExecutionStageRun, Job
from app.planner import PlanningService
from app.provenance import content_hash
from app.providers.runtime import resolve_job_spec_signer
from app.workbench import Workbench
from tests.test_workbench import automatic_fixture, populate_round


def implemented(tmp_path, monkeypatch, *, dependencies=False):
    from tests import test_workbench as fixtures
    manifest = "pyproject.toml"
    text = '[project]\nname="fixture"\nversion="0.1.0"\ndependencies=[]\n'
    digest = hashlib.sha256(text.encode()).hexdigest()
    if dependencies:
        original = fixtures._plan_content()
        monkeypatch.setattr(fixtures, "_plan_content", lambda: replace(original,
            files_to_inspect=(*original.files_to_inspect, manifest)))

    def context(db, settings, tid):
        if not dependencies:
            return
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            old = wb.latest(tid, "context_ready")
            evidence = wb.read(old.payload["artifact_id"])
            for entry in evidence["entries"]:
                if entry["path"] == manifest:
                    entry.update(content=text, prior_hash=digest)
            wb.append(tid, "context_ready", {**old.payload, "artifact_id": wb.artifact(evidence)}, key="python-context")

    db, settings, tid, workers, code, review = automatic_fixture(tmp_path, configure_models=context)
    populate_round(code, review)
    if dependencies:
        from tests.test_sandbox_dependencies import _FakeDependencyRuntime, _lease
        from app.sandbox_worker.implementation import FileInventoryEntry
        from app.sandbox_worker.fake import FakeImplementRuntime

        class Inspector:
            async def inspect(self, **kwargs):
                return {"entries": [{"path":manifest, "content":text, "prior_hash":digest}]}

        class Proxy:
            async def start(self, *, image_digest, policy):
                return _lease(policy)
            async def stop(self, *args, **kwargs):
                pass

        class Implement(FakeImplementRuntime):
            async def apply(self, **kwargs):
                inspection = await super().apply(**kwargs)
                entry = FileInventoryEntry(path=manifest, kind="file", size=len(text), sha256=digest, executable=False)
                baseline = tuple(sorted((*inspection.baseline_inventory, entry), key=lambda f:f.path))
                result = tuple(sorted((*inspection.result_inventory, entry), key=lambda f:f.path))
                return replace(inspection, baseline_inventory=baseline, result_inventory=result,
                    baseline_inventory_hash=content_hash([f.to_wire() for f in baseline]),
                    result_inventory_hash=content_hash([f.to_wire() for f in result]),
                    baseline_file_count=len(baseline), result_file_count=len(result),
                    baseline_total_bytes=inspection.baseline_total_bytes+len(text),
                    result_total_bytes=inspection.result_total_bytes+len(text))

        workers[0].implement_runtime = Implement()
        workers[-1].dependencies, workers[-1].proxy, workers[-1].inspector = _FakeDependencyRuntime(), Proxy(), Inspector()
    monkeypatch.setattr(workers[0], "_schedule_verify", lambda *args: None)
    for _ in range(10):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", job.error_message
        with db.session() as session:
            attempt = session.scalar(select(ExecutionAttempt))
            stage = ExecutionAttemptService(session).current(attempt.id)
            if stage.stage == "implement" and stage.status == "succeeded":
                return db, settings, tid, attempt.id, workers, code, review
            _advance(PlanningService(session, settings.artifact_root), tid, settings)
    raise AssertionError("Implement did not complete")


def test_two_schedulers_share_one_verify_and_atomic_enqueue(tmp_path, monkeypatch):
    db, settings, tid, aid, *_ = implemented(tmp_path, monkeypatch)
    signer = resolve_job_spec_signer(settings)
    barrier = threading.Barrier(2)

    def schedule():
        barrier.wait(timeout=5)
        with db.session() as session:
            schedule_verification(Workbench(session, settings.artifact_root), aid, signer)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(schedule) for _ in range(2)]
        for future in futures:
            future.result(timeout=15)
    with db.session() as session:
        runs = list(session.scalars(select(ExecutionStageRun).where(ExecutionStageRun.stage == "verify")))
        assert len(runs) == 1
        assert session.get(Job, runs[0].job_id).state == "queued"
        assert ExecutionAttemptService(session).current(aid).stage == "verify"
        assert Workbench(session, settings.artifact_root).active_blocker(tid) is None


@pytest.mark.parametrize("mode", ["renew", "cancel", "lost"])
def test_long_stage_renews_lease_and_rejects_cancelled_or_expired_results(tmp_path, monkeypatch, mode):
    from datetime import datetime, timezone
    from app.jobs import JobService
    from app.sandbox_worker.fake import FakeVerifyRuntime
    db, settings, tid, aid, workers, *_ = implemented(tmp_path, monkeypatch)
    with db.session() as session:
        schedule_verification(Workbench(session, settings.artifact_root), aid, resolve_job_spec_signer(settings))
    worker = workers[0]
    worker.lease_seconds = 1
    worker.heartbeat_interval_seconds = 2 if mode == "lost" else .05
    async def exercise():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        class Slow(FakeVerifyRuntime):
            async def run(self, **kwargs):
                entered.set()
                try:
                    await asyncio.sleep(1.4 if mode != "cancel" else 10)
                    return await super().run(**kwargs)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        worker.verify_runtime = Slow()
        task = asyncio.create_task(worker.run_once())
        await entered.wait()
        with db.session() as session:
            run = session.scalar(select(ExecutionStageRun).where(ExecutionStageRun.stage == "verify"))
            job_id = run.job_id
            if mode == "cancel": JobService(session).request_cancel(job_id)
        for _ in range(20):
            await asyncio.sleep(.1)
            with db.session() as session:
                JobService(session).recover_expired(now=datetime.now(timezone.utc))
                if mode == "lost":
                    # The generic reaper consumed the expiry before the stage
                    # coordinator sees it; both durable states must converge.
                    ExecutionStageControlService(session).reconcile_expired()
            if task.done(): break
        result = await task
        assert result.state == {"renew":"succeeded", "cancel":"cancelled", "lost":"timed_out"}[mode]
        with db.session() as session:
            stage = ExecutionAttemptService(session).current(aid)
            assert stage.status == result.state
            if mode != "renew": assert stage.result_hash is None
            else:
                job = session.get(Job, job_id)
                assert (job.completed_at - job.started_at).total_seconds() >= 1
                assert job.heartbeat_at > job.started_at
        if mode == "cancel": assert cancelled.is_set()
    asyncio.run(exercise())
    db.close()


def test_enqueue_crash_rolls_back_verify_transition_and_retries(tmp_path, monkeypatch):
    db, settings, tid, aid, *_ = implemented(tmp_path, monkeypatch)
    signer = resolve_job_spec_signer(settings)
    with monkeypatch.context() as patch:
        patch.setattr(ExecutionStageControlService, "schedule_current", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash")))
        with db.session() as session, pytest.raises(RuntimeError, match="crash"):
            schedule_verification(Workbench(session, settings.artifact_root), aid, signer)
    with db.session() as session:
        assert ExecutionAttemptService(session).current(aid).stage == "implement"
        schedule_verification(Workbench(session, settings.artifact_root), aid, signer)
    with db.session() as session:
        assert ExecutionAttemptService(session).current(aid).stage == "verify"
        assert len(list(session.scalars(select(ExecutionStageRun).where(ExecutionStageRun.stage == "verify")))) == 1


def test_legacy_pending_verify_gets_commands_and_one_job(tmp_path, monkeypatch):
    db, settings, tid, aid, *_ = implemented(tmp_path, monkeypatch)
    with db.session() as session:
        attempts = ExecutionAttemptService(session)
        stage = attempts.current(aid)
        attempts.advance_stage(aid, expected_sequence=stage.sequence, expected_record_hash=stage.record_hash,
            input_hashes=(stage.result_hash,), reason_code="implement_succeeded", idempotency_key="legacy-pending")
        from app.contribution_workflow import schedule
        schedule(Workbench(session, settings.artifact_root), aid, settings)
        schedule(Workbench(session, settings.artifact_root), aid, settings)
        run = session.scalar(select(ExecutionStageRun).where(ExecutionStageRun.stage == "verify"))
        assert run and session.get(Job, run.job_id).state == "queued"


def test_concurrent_event_appends_preserve_hash_chain(tmp_path, monkeypatch):
    from tests.test_workbench import seeded
    db, settings, tid, *_ = seeded(tmp_path)
    original = Workbench.history
    first_read = threading.Event()

    def delayed_history(self, task_id):
        rows = original(self, task_id)
        if threading.current_thread().name.startswith("writer") and not first_read.is_set():
            first_read.set()
            time.sleep(0.1)
        return rows

    monkeypatch.setattr(Workbench, "history", delayed_history)
    def append(key):
        with db.session() as session:
            Workbench(session, settings.artifact_root).append(tid, "diagnostic", {"key":key}, key=key)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="writer") as executor:
        first = executor.submit(append, "writer-first")
        assert first_read.wait(timeout=5)
        second = executor.submit(append, "writer-second")
        first.result(timeout=5)
        second.result(timeout=5)
    with db.session() as session:
        assert len([e for e in Workbench(session, settings.artifact_root).history(tid) if e.kind == "diagnostic"]) == 2


def disposed_verify(tmp_path, monkeypatch):
    db, settings, tid, aid, workers, code, review = implemented(tmp_path, monkeypatch, dependencies=True)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        schedule_verification(wb, aid, resolve_job_spec_signer(settings))
        allocated = wb.latest(tid, "dependency_workspace")
        # Reproduce the historical blocker that incorrectly disposed a consumer's inputs.
        start = wb.latest(tid, "automation_started")
        wb.append(tid, "automation_blocked", {"reason_code":"workflow_failed", "reason":"执行推进失败", "retryable":False},
            key="blocked:"+start.id+":initial")
        wb.append(tid, "dependency_workspace_disposed", {"allocation_hash":allocated.record_hash}, key="dependency-disposed:"+allocated.id)
    job = asyncio.run(workers[0].run_once())
    assert job.state == "failed" and job.error_code == "environment_stale"
    return db, settings, tid, aid, workers, code, review


def test_disposed_verify_rebuilds_baseline_and_resumes_same_approved_code(tmp_path, monkeypatch):
    db, settings, tid, aid, workers, code, review = disposed_verify(tmp_path, monkeypatch)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        old_hashes = [e.record_hash for e in wb.history(tid)]
        old_stage = ExecutionAttemptService(session).current(aid)
        approval = wb.latest(tid, "execution_authorized").record_hash
    advance_workflows(db, settings)
    advance_workflows(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        recovery_jobs = [j for j in wb.jobs(tid) if j.payload.get("recovery_stage_hash")]
        assert len(recovery_jobs) == 1 and recovery_jobs[0].state == "queued"
        assert not wb.active_blocker(tid)
    prepared = asyncio.run(workers[-1].run_once())
    assert prepared.state == "succeeded", prepared.error_message
    advance_workflows(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        current = ExecutionAttemptService(session).current(aid)
        assert current.stage == "verify" and current.status == "pending"
        assert current.input_hashes[0] == old_stage.input_hashes[0]
        assert current.input_hashes[1] != old_stage.input_hashes[1]
        assert current.workspace_ref == old_stage.workspace_ref
        assert [e.record_hash for e in wb.history(tid)][:len(old_hashes)] == old_hashes
        assert wb.latest(tid, "execution_authorized").record_hash == approval
        assert len(list(session.scalars(select(ExecutionAttempt)))) == 1
        assert len(list(session.scalars(select(ExecutionStageRun).where(ExecutionStageRun.stage == "implement")))) == 1
    for _ in range(4):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", job.error_message
        advance_workflows(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert wb.latest(tid, "awaiting_acceptance")
        assert not wb.latest(tid, "publication_confirmed")
        assert not wb.active_blocker(tid)


def test_cleanup_keeps_dependencies_while_verify_is_queued(tmp_path, monkeypatch):
    from app.sandbox_worker.dependency_jobs import DependencyJobWorker
    db, settings, tid, aid, workers, *_ = implemented(tmp_path, monkeypatch, dependencies=True)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        schedule_verification(wb, aid, resolve_job_spec_signer(settings))
        wb.append(tid, "automation_blocked", {"reason":"fixture"}, key="cleanup-blocker")
    runtime = workers[-1].dependencies
    before = len(runtime.commands)
    asyncio.run(DependencyJobWorker(db, settings, runtime, workers[-1].proxy, worker_id="cleanup").cleanup())
    assert len(runtime.commands) == before
    with db.session() as session:
        assert Workbench(session, settings.artifact_root).latest(tid, "dependency_workspace_disposed") is None


@pytest.mark.parametrize("failure", ["preparation", "baseline", "recipe"])
def test_recovery_failure_stays_blocked_and_preserves_approved_code(tmp_path, monkeypatch, failure):
    db, settings, tid, aid, workers, *_ = disposed_verify(tmp_path, monkeypatch)
    advance_workflows(db, settings)
    runtime = workers[-1].dependencies
    prepares = len(runtime.prepares)
    if failure == "preparation":
        runtime.fail_prepare = True
    elif failure == "recipe":
        import app.sandbox_worker.workspace_jobs as preparation
        original = preparation.task_environment
        monkeypatch.setattr(preparation, "task_environment", lambda *args: {
            **original(*args), "preparation_version":"python-workspace-preparation-v1"})
    else:
        from app.sandbox_worker.fake import FakeVerifyRuntime
        from app.sandbox_worker.verification import VerifyCommandStatus
        class FailedBaseline(FakeVerifyRuntime):
            async def run(self, **kwargs):
                value = await super().run(**kwargs)
                return replace(value, command_evidence=tuple(replace(e, status=VerifyCommandStatus.FAILED, exit_code=1)
                                                            for e in value.command_evidence))
        workers[-1].verifier = FailedBaseline()
    job = asyncio.run(workers[-1].run_once())
    assert job.state == ("succeeded" if failure == "baseline" else "failed")
    if failure == "recipe":
        assert len(runtime.prepares) == prepares
    advance_workflows(db, settings)
    advance_workflows(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert wb.active_blocker(tid)
        assert ExecutionAttemptService(session).current(aid).status == "failed"
        assert len([j for j in wb.jobs(tid) if j.payload.get("recovery_stage_hash")]) == 1
        assert len(list(session.scalars(select(ExecutionStageRun).where(ExecutionStageRun.stage == "verify")))) == 1
        assert not wb.latest(tid, "verification_environment_restored")


def test_stop_revokes_queued_environment_recovery(tmp_path, monkeypatch):
    from app.contribution_workflow import stop_workflow
    from app.execution_recovery import require_recovery_authorization
    from app.workbench import WorkbenchError
    db, settings, tid, aid, workers, *_ = disposed_verify(tmp_path, monkeypatch)
    advance_workflows(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        stop_workflow(wb, tid, key="stop-restore")
        with pytest.raises(WorkbenchError, match="已停止"):
            require_recovery_authorization(wb, session.get(ExecutionAttempt, aid), settings)
    assert asyncio.run(workers[-1].run_once()) is None
    with db.session() as session:
        assert ExecutionAttemptService(session).current(aid).status == "failed"


def test_recovery_upgrade_preserves_hashes_and_database_rejects_unproven_input(tmp_path, monkeypatch):
    import app.migrations.versions as versions
    migrations = versions.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(versions, "MIGRATIONS", tuple(m for m in migrations if m.revision < "0036"))
        db, settings, tid, aid, workers, *_ = disposed_verify(tmp_path, monkeypatch)
        with db.session() as session:
            hashes = [e.record_hash for e in Workbench(session, settings.artifact_root).history(tid)]
    assert db.create_schema().applied == ("0036_verification_environment_recovery", "0037_baseline_feedback", "0038_agent_runtime_v2")
    assert db.create_schema().applied == ()
    with db.session() as session:
        assert [e.record_hash for e in Workbench(session, settings.artifact_root).history(tid)] == hashes
        attempts = ExecutionAttemptService(session)
        current = attempts.current(aid)
        from app.executions import _build_stage_version, ExecutionStageStatus
        from app.sandbox_worker.specs import SandboxStage
        version = _build_stage_version(attempt=session.get(ExecutionAttempt, aid), sequence=current.sequence+1,
            stage=SandboxStage.VERIFY, status=ExecutionStageStatus.PENDING,
            reason_code="dependency_environment_restored", idempotency_key="forged-restore", job_spec_hash=None,
            input_hashes=(current.input_hashes[0], "f"*64), result_hash=None,
            workspace_id=current.workspace_id, workspace_ref=current.workspace_ref,
            workspace_inventory_hash=current.workspace_inventory_hash, previous_stage_state_hash=current.record_hash, now=None)
        session.add(version)
        with pytest.raises(IntegrityError, match="provenance"):
            session.flush()


def test_generic_blocker_does_not_reuse_an_old_unbound_diagnostic(tmp_path):
    from tests.test_workbench import seeded
    from app.conversation import user_status
    from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure
    db, settings, tid, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        wb.append(tid, "execution_environment_diagnostic", {"diagnostic":summarize_dependency_failure(
            "runtime_resolve", 1, "No matching distribution found for old_package")}, key="old-diagnostic")
        wb.append(tid, "automation_started", {"authorization_hash":"a"*64}, key="status-start")
        wb.append(tid, "automation_blocked", {"reason":"当前调度错误"}, key="status-blocker")
        assert user_status(wb, tid)["next_step"] == "当前调度错误"


def test_recovery_cannot_extend_verify_run_budget(tmp_path, monkeypatch):
    db, settings, tid, aid, workers, *_ = disposed_verify(tmp_path, monkeypatch)
    for _ in range(2):
        advance_workflows(db, settings)
        assert asyncio.run(workers[-1].run_once()).state == "succeeded"
        advance_workflows(db, settings)
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            allocation = wb.latest(tid, "dependency_workspace")
            wb.append(tid, "dependency_workspace_disposed", {"allocation_hash":allocation.record_hash},
                      key="dependency-disposed:"+allocation.id)
        assert asyncio.run(workers[0].run_once()).state == "failed"
    advance_workflows(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert wb.active_blocker(tid).payload["reason_code"] == "environment_recovery_exhausted"
        assert len([j for j in wb.jobs(tid) if j.payload.get("recovery_stage_hash")]) == 2
        assert len(list(session.scalars(select(ExecutionStageRun).where(ExecutionStageRun.stage == "verify")))) == 3


def test_coordinator_diagnostic_keeps_locations_without_raw_exception(tmp_path, monkeypatch):
    import app.contribution_workflow as workflow
    db, settings, tid, *_ = automatic_fixture(tmp_path)
    def fail(*args):
        raise RuntimeError("api_key=workflowdiagnosticcanary123456")
    monkeypatch.setattr(workflow, "_advance", fail)
    workflow.advance_workflows(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        receipt = wb.latest(tid, "workflow_diagnostic")
        diagnostic = wb.read(receipt.payload["artifact_id"])
        assert diagnostic["exception_type"] == "RuntimeError" and diagnostic["frames"]
        assert "workflowdiagnosticcanary" not in str(diagnostic)
        assert "workflowdiagnosticcanary" not in str(wb.active_blocker(tid).payload)


def test_transport_repair_requires_scoped_public_tls_proof_and_new_allocation_job(tmp_path, monkeypatch):
    from app.execution_recovery import record_transport_recovery
    from app.execution_preflight import task_preflight
    from app.sandbox_worker.dependencies import DependencyPreparationError, DependencyProxyPolicy
    from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure
    from app.workbench import WorkbenchError
    db, settings, tid, aid, workers, *_ = disposed_verify(tmp_path, monkeypatch)
    advance_workflows(db, settings)
    runtime = workers[-1].dependencies
    with monkeypatch.context() as patch:
        async def fail(**kwargs):
            raise DependencyPreparationError("fixture", code="environment_dependency_resolution_failed",
                diagnostic=summarize_dependency_failure("runtime_resolve",1,"403 Forbidden"))
        patch.setattr(runtime, "prepare", fail)
        failed = asyncio.run(workers[-1].run_once())
        assert failed.state == "failed"
    advance_workflows(db, settings)
    proof={"version":"package-dns-transport-v1",
           "proxy_policy_hash":DependencyProxyPolicy.for_ecosystem("python").policy_hash,
           "hosts":{host:{"addresses":["151.101.0.223"],"status":200,"tls_verified":True}
                    for host in ("pypi.org","files.pythonhosted.org")}}
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert task_preflight(wb, tid)["status"] == "blocked"
        invalid={**proof,"hosts":{**proof["hosts"],"pypi.org":{
            **proof["hosts"]["pypi.org"],"addresses":["198.18.0.5"]}}}
        with pytest.raises(WorkbenchError,match="证据"):
            record_transport_recovery(wb,failed.id,invalid)
        receipt=record_transport_recovery(wb,failed.id,proof)
        assert record_transport_recovery(wb,failed.id,proof).record_hash==receipt.record_hash
        assert task_preflight(wb,tid)["report_hash"]==wb.latest(tid,"execution_authorized").payload["preflight_hash"]
    advance_workflows(db,settings)
    with db.session() as session:
        wb = Workbench(session,settings.artifact_root)
        new=[j for j in wb.jobs(tid) if j.payload.get("recovery_stage_hash")][-1]
        assert new.id!=failed.id and new.payload["replaces_preparation_job_id"]==failed.id
    assert asyncio.run(workers[-1].run_once()).state=="succeeded"
    advance_workflows(db,settings)
    for _ in range(4):
        for worker in workers:
            job=asyncio.run(worker.run_once())
            if job:assert job.state=="succeeded",job.error_message
        advance_workflows(db,settings)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        assert not wb.active_blocker(tid)
        assert wb.latest(tid,"awaiting_acceptance")
        assert wb.latest(tid,"verification_environment_restored").payload["superseded_preparation_job_ids"]==[failed.id]
