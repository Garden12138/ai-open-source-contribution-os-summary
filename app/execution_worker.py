from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

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

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        started_at = now or datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        with self.database.session() as session:
            control = ExecutionStageControlService(session)
            run = control.lease_next(
                worker_id=self.worker_id,
                lease_seconds=60,
                now=started_at,
            )
            if run is None:
                return None
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
                now=started_at,
            )
            if archive is None:
                return self._fail(
                    session,
                    run.id,
                    "repository_archive_unavailable",
                    started_at,
                )
            if runtime_missing:
                return self._fail(
                    session,
                    run.id,
                    "explore_runtime_unavailable"
                    if stage is SandboxStage.EXPLORE
                    else "stage_runtime_unavailable",
                    started_at,
                )
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
                        started_at,
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
                        started_at,
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
                result = await VerifyService(
                    signer=self.signer,
                    policy=policy,
                    runtime=self.verify_runtime,
                ).run(signed, implement, verify_workspace)
                return self._succeed(
                    session,
                    run.id,
                    result,
                    "verify_succeeded",
                    started_at,
                )
            except Exception as exc:
                return self._fail(
                    session,
                    run.id,
                    _reason_code(exc),
                    started_at,
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
    ) -> Job:
        ExecutionStageControlService(session).complete(
            run_id,
            worker_id=self.worker_id,
            status=ExecutionStageStatus.FAILED,
            reason_code=reason_code,
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
        attempts = ExecutionAttemptService(session)
        current = attempts.current(attempt_id)
        pending = attempts.advance_stage(
            attempt_id,
            expected_sequence=current.sequence,
            expected_record_hash=current.record_hash,
            input_hashes=(implement_result_hash,),
            reason_code="implement_succeeded",
            idempotency_key=f"advance:verify:{attempt_id}:{current.sequence}",
        )
        spec = attempts.build_current_job_spec(
            attempt_id,
            commands=commands,
        )
        ExecutionStageControlService(session).schedule_current(
            attempt_id,
            signed_job_spec=self.signer.sign(spec),
            signer=self.signer,
            sandbox_policy=SandboxPolicy(),
            idempotency_key=(
                f"schedule:{attempt_id}:{pending.stage}:{pending.sequence}"
            ),
        )

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
    name = type(error).__name__.lower()
    return f"sandbox_{name}"[:80]
