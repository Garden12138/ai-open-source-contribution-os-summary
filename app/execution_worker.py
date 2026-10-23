from __future__ import annotations

import asyncio
from contextlib import suppress
from collections.abc import Callable
import json
import logging
from datetime import datetime, timezone, timedelta
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.archives import RepositoryArchiveStore
from app.artifacts import ArtifactStore, ExecutionArtifactBundle
from app.changesets import ChangeSetStore, plan_sandbox_commands
from app.database import Database
from app.execution_control import ExecutionStageControlService
from app.executions import ExecutionAttemptService, ExecutionStageStatus
from app.models import Job
from app.plans import PlanVersionService
from app.sandbox_worker.specs import JobSpecSigner, SandboxPolicy, SandboxStage


class ExecutionStageWorker:
    """Lease sandbox-stage Jobs and run the current Explore/Implement/Verify."""

    def __init__(
        self,
        database: Database,
        signer: JobSpecSigner,
        *,
        worker_id: str,
        archive_store: RepositoryArchiveStore | None = None,
        change_set_store: ChangeSetStore | None = None,
        artifact_root: str | Path | None = None,
        secrets: tuple[str | None, ...] = (),
        explore_runtime: Any | None = None,
        implement_runtime: Any | None = None,
        verify_runtime: Any | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        self.database = database
        self.signer = signer
        self.worker_id = worker_id
        self.archive_store = archive_store
        self.change_set_store = change_set_store
        self.artifact_root = Path(artifact_root) if artifact_root else None
        self.secrets = secrets
        self.explore_runtime = explore_runtime
        self.implement_runtime = implement_runtime
        self.verify_runtime = verify_runtime
        self.lease_seconds = 60
        self.heartbeat_interval_seconds = 10

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        started_at = now or datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        started = time.monotonic()
        clock = lambda: started_at + timedelta(seconds=time.monotonic() - started)
        with self.database.session() as session:
            control = ExecutionStageControlService(session)
            run = control.lease_next(
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                now=started_at,
            )
            if run is None:
                return None
            run_id, timeout = run.id, run.timeout_seconds
        work = asyncio.create_task(self._run_leased(run_id, started_at, clock))
        heartbeat = asyncio.create_task(self._heartbeat(run_id, work, clock))
        try:
            async with asyncio.timeout(timeout):
                return await work
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("Sandbox worker interrupted: exception=%s", type(exc).__name__)
            with self.database.session() as session:
                control = ExecutionStageControlService(session)
                control.reconcile_expired(now=clock())
                current = control.get_verified(run_id)
                if current.job.state not in {"succeeded", "failed", "cancelled", "timed_out"}:
                    if current.job.lease_owner != self.worker_id:
                        return current.job
                    if current.job.state == "leased":
                        from app.jobs import JobService
                        JobService(session).fail(current.job_id, worker_id=self.worker_id,
                            error_code="worker_interrupted", error_message="隔离执行初始化已中断。", now=clock())
                        control.reconcile_expired(now=clock())
                        return self._job(session, run_id)
                    status = (ExecutionStageStatus.CANCELLED if current.job.cancel_requested_at else
                        ExecutionStageStatus.TIMED_OUT if isinstance(exc, TimeoutError) else ExecutionStageStatus.FAILED)
                    control.complete(run_id, worker_id=self.worker_id, status=status,
                        reason_code="cancelled" if status is ExecutionStageStatus.CANCELLED else "worker_interrupted",
                        error_message="隔离执行已中断，已有改动与证据保留。", now=clock())
                return self._job(session, run_id)
        finally:
            work.cancel()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception): await work
            with suppress(asyncio.CancelledError): await heartbeat

    async def _heartbeat(self, run_id: str, work: asyncio.Task, clock: Callable[[], datetime]) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            try:
                with self.database.session() as session:
                    control = ExecutionStageControlService(session)
                    run = control.get_verified(run_id)
                    if run.job.cancel_requested_at or run.job.state in {"succeeded", "failed", "cancelled", "timed_out"}:
                        work.cancel()
                        return
                    control.heartbeat(run_id, worker_id=self.worker_id, lease_seconds=self.lease_seconds, now=clock())
            except Exception:
                work.cancel()
                return

    async def _run_leased(self, run_id: str, started_at: datetime, clock: Callable[[], datetime]) -> Job:
        with self.database.session() as session:
            control = ExecutionStageControlService(session)
            run = control.get_verified(run_id)
            attempts = ExecutionAttemptService(session)
            policy = SandboxPolicy()
            attempt = attempts.get_verified(run.execution_attempt_id)
            pending = attempts.current(attempt.id)
            stage = SandboxStage(pending.stage)
            archive = None
            if self.archive_store is not None:
                archive = self.archive_store.lookup(
                    attempt.repository_archive_hash,
                    repository_full_name=attempt.repository_full_name,
                    base_commit_sha=attempt.base_commit_sha,
                )
            plan = PlanVersionService(session).get_verified(
                attempt.plan_version_id
            )
            allowed_paths = tuple(plan.files_likely_to_change)
            commands = plan_sandbox_commands(plan)
            signed = self.signer.sign(
                attempts.build_current_job_spec(
                    attempt.id,
                    allowed_change_paths=(
                        allowed_paths if stage is SandboxStage.IMPLEMENT else ()
                    ),
                    commands=(
                        commands if stage is SandboxStage.VERIFY else ()
                    ),
                )
            )
            workspace_id = None
            workspace_ref = None
            live_workspace = None
            runtime_missing = (
                (
                    stage is SandboxStage.EXPLORE
                    and self.explore_runtime is None
                )
                or (
                    stage is SandboxStage.IMPLEMENT
                    and self.implement_runtime is None
                )
                or (
                    stage is SandboxStage.VERIFY
                    and self.verify_runtime is None
                )
            )
            if stage is SandboxStage.IMPLEMENT:
                if self.implement_runtime is not None and archive is not None:
                    live_workspace = (
                        await self.implement_runtime.create_workspace(
                            image_digest=attempt.runner_image_digest,
                            policy=policy,
                        )
                    )
                    workspace_id = live_workspace.workspace_id
                    workspace_ref = live_workspace.volume_name
                else:
                    workspace_id = f"workspace:unavailable-{uuid4().hex[:8]}"
                    workspace_ref = "contribos-workspace-" + uuid4().hex
            control.begin(
                run.id,
                worker_id=self.worker_id,
                signed_job_spec=signed,
                signer=self.signer,
                sandbox_policy=policy,
                workspace_id=workspace_id,
                workspace_ref=workspace_ref,
                now=clock(),
            )
            if archive is None:
                return self._fail(
                    session,
                    run.id,
                    "repository_archive_unavailable",
                    clock(),
                    error_message="仓库归档不可用或校验未通过，请检查代码仓库。",
                )
            if runtime_missing:
                return self._fail(
                    session,
                    run.id,
                    "explore_runtime_unavailable"
                    if stage is SandboxStage.EXPLORE
                    else "stage_runtime_unavailable",
                    clock(),
                    error_message="沙箱执行环境不可用，请检查沙箱容器配置。",
                )
            dependency = None
            try:
                if stage is SandboxStage.EXPLORE:
                    from app.sandbox_worker.explore import ExploreService

                    result = await ExploreService(
                        signer=self.signer,
                        policy=policy,
                        runtime=self.explore_runtime,
                    ).run(signed, archive)
                    return self._succeed(
                        session,
                        run.id,
                        result,
                        "explore_succeeded",
                        clock(),
                    )
                if stage is SandboxStage.IMPLEMENT:
                    if self.change_set_store is None:
                        raise RuntimeError("ChangeSet store is required")
                    explore = self._load_explore(
                        session,
                        pending.input_hashes[0],
                    )
                    change_set = self.change_set_store.lookup(
                        pending.input_hashes[1]
                    )
                    from app.sandbox_worker.implementation import ImplementService

                    execution = await ImplementService(
                        signer=self.signer,
                        policy=policy,
                        runtime=self.implement_runtime,
                    ).run(
                        signed,
                        archive,
                        explore,
                        change_set,
                        workspace=live_workspace,
                    )
                    completed = self._succeed(
                        session,
                        run.id,
                        execution.result,
                        "implement_succeeded",
                        clock(),
                        workspace_inventory_hash=(
                            execution.result.result_inventory_hash
                        ),
                    )
                    self._schedule_verify(
                        session,
                        attempt.id,
                        execution.result.result_hash,
                        commands,
                    )
                    return completed
                from app.sandbox_worker.implementation import DisposableWorkspace
                from app.sandbox_worker.verification import VerifyService

                implement = self._load_implement(
                    session,
                    pending.input_hashes[0],
                )
                verify_workspace = DisposableWorkspace(
                    workspace_id=str(pending.workspace_id),
                    volume_name=str(pending.workspace_ref),
                    runner_image_digest=attempt.runner_image_digest,
                    sandbox_policy_hash=attempt.sandbox_policy_hash,
                    inventory_hash=pending.workspace_inventory_hash,
                )
                from app.workbench import Workbench
                from app.sandbox_worker.dependency_jobs import load_dependency
                dependency = (load_dependency(Workbench(session, str(self.artifact_root)), attempt.id)
                              if len(pending.input_hashes) > 1 else None)
                result = await VerifyService(
                    signer=self.signer,
                    policy=policy,
                    runtime=self.verify_runtime,
                ).run(signed, implement, verify_workspace, dependency=dependency)
                return self._succeed(
                    session,
                    run.id,
                    result,
                    "verify_succeeded",
                    clock(),
                )
            except Exception as exc:
                if self._job(session, run.id).state == "succeeded":
                    # The coordinator will retry scheduling from the durable completed
                    # stage, or record an environment blocker without rewriting it.
                    return self._job(session, run.id)
                logger.warning("Sandbox stage failed: stage=%s exception=%s", stage.value, type(exc).__name__)
                safe_msg = {
                    "exploreerror": "沙箱探索仓库代码时失败，请检查沙箱容器配置后重试。",
                    "implementationerror": "沙箱实施代码修改时失败，请检查修改方案后重试。",
                    "verifyerror": "沙箱运行验证测试时失败，请检查测试命令后重试。",
                }.get(type(exc).__name__.lower(), f"沙箱隔离执行失败：{type(exc).__name__}")
                from app.workbench import WorkbenchError
                from app.sandbox_worker.verification import VerificationEnvironmentUnavailable
                if isinstance(exc, VerificationEnvironmentUnavailable) and dependency is not None:
                    wb = Workbench(session, str(self.artifact_root))
                    allocation = next((e for e in reversed(wb.history(attempt.task_id))
                        if e.kind == "dependency_workspace"
                        and e.payload["workspace"]["workspace_id"] == dependency.workspace.workspace_id), None)
                    if allocation is not None:
                        wb.append(attempt.task_id, "dependency_workspace_invalidated", {
                            "allocation_hash": allocation.record_hash,
                            "dependency_result_hash": dependency.result.result_hash,
                            "reason_code": "dependency_inventory_unavailable"},
                            key="dependency-invalidated:" + allocation.id, job_id=run.job_id)
                if ((isinstance(exc, WorkbenchError) and exc.code == "environment_stale")
                        or isinstance(exc, VerificationEnvironmentUnavailable)):
                    safe_msg = "冻结验证环境已失效，正在检查隔离恢复条件；已有改动保留。"
                return self._fail(
                    session,
                    run.id,
                    _reason_code(exc),
                    clock(),
                    error_message=safe_msg,
                )

    def _succeed(
        self,
        session,
        run_id: str,
        result: object,
        reason_code: str,
        now: datetime,
        *,
        workspace_inventory_hash: str | None = None,
    ) -> Job:
        session.expire_all()
        if self.artifact_root is None:
            raise RuntimeError("Execution artifact root is required")
        store = ArtifactStore(
            session,
            self.artifact_root,
            secrets=self.secrets,
        )
        bundle = ExecutionArtifactBundle.from_result(result)
        control = ExecutionStageControlService(session)
        control.complete(
            run_id,
            worker_id=self.worker_id,
            status=ExecutionStageStatus.SUCCEEDED,
            reason_code=reason_code,
            result_hash=bundle.result_hash,
            workspace_inventory_hash=workspace_inventory_hash,
            artifact_store=store,
            artifact_bundle=bundle,
            now=now,
        )
        return self._job(session, run_id)

    def _fail(
        self,
        session,
        run_id: str,
        reason_code: str,
        now: datetime,
        *,
        error_message: str | None = None,
    ) -> Job:
        session.expire_all()
        ExecutionStageControlService(session).complete(
            run_id,
            worker_id=self.worker_id,
            status=ExecutionStageStatus.FAILED,
            reason_code=reason_code,
            error_message=error_message,
            now=now,
        )
        return self._job(session, run_id)

    def _schedule_verify(
        self,
        session,
        attempt_id: str,
        implement_result_hash: str,
        commands: tuple[Any, ...],
    ) -> None:
        if not commands:
            return
        from app.workbench import Workbench
        from app.execution_environment import schedule_verification
        schedule_verification(Workbench(session, str(self.artifact_root)), attempt_id, self.signer)

    def _load_explore(self, session, result_hash: str):
        from app.sandbox_worker.explore import ExploreResult

        payload = json.loads(
            ArtifactStore(
                session,
                self.artifact_root or ".",
                secrets=self.secrets,
            ).read_bytes(result_hash)
        )
        return ExploreResult.from_payload(payload)

    def _load_implement(self, session, result_hash: str):
        from app.sandbox_worker.implementation import ImplementResult

        payload = json.loads(
            ArtifactStore(
                session,
                self.artifact_root or ".",
                secrets=self.secrets,
            ).read_bytes(result_hash)
        )
        return ImplementResult.from_payload(payload)

    def _job(self, session, run_id: str) -> Job:
        run = ExecutionStageControlService(session).get_verified(run_id)
        job = session.get(Job, run.job_id)
        if job is None:
            raise RuntimeError("Sandbox stage Job disappeared after completion")
        session.refresh(job)
        return job


def _reason_code(error: Exception) -> str:
    from app.workbench import WorkbenchError
    from app.sandbox_worker.verification import VerificationEnvironmentUnavailable
    if isinstance(error, VerificationEnvironmentUnavailable):
        return "environment_stale"
    if isinstance(error, WorkbenchError) and error.code == "environment_stale":
        return "environment_stale"
    name = type(error).__name__.lower()
    return f"sandbox_{name}"[:80]
