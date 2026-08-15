from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.artifacts import (
    ArtifactError,
    ArtifactStore,
    ExecutionArtifactBundle,
)
from app.executions import (
    ExecutionAttemptConflictError,
    ExecutionAttemptService,
    ExecutionStageStatus,
    ExecutionStageTransitionError,
)
from app.jobs import (
    JobService,
    JobState,
    JobTransitionError,
    TERMINAL_STATES,
)
from app.models import (
    ExecutionStageRun,
    ExecutionStageVersion,
    Job,
)
from app.provenance import content_hash
from app.sandbox_worker.specs import (
    JobSpecSigner,
    SandboxPolicy,
    SandboxStage,
    SignedJobSpec,
)
from app.security import contains_sensitive_text, ensure_no_sensitive_data
from app.workspace_disposals import (
    ExecutionWorkspaceDisposalService,
    WorkspaceDisposalError,
)


EXECUTION_STAGE_RUN_SCHEMA_VERSION = "1"
EXECUTION_STAGE_JOB_SCHEMA_VERSION = "execution-stage-job-v1"
EXECUTION_STAGE_JOB_KIND = "sandbox_stage"
MAX_STAGE_RUNS = 3
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class ExecutionControlError(RuntimeError):
    pass


class ExecutionStageRunNotFoundError(ExecutionControlError):
    pass


class ExecutionControlConflictError(ExecutionControlError):
    pass


class ExecutionRetryExhaustedError(ExecutionControlError):
    pass


class ExecutionStageControlService:
    """Own bounded stage Jobs, leases, cancellation, and reconciliation."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def schedule_current(
        self,
        attempt_id: str,
        *,
        signed_job_spec: SignedJobSpec,
        signer: JobSpecSigner,
        sandbox_policy: SandboxPolicy,
        idempotency_key: str,
        max_stage_runs: int = MAX_STAGE_RUNS,
        now: datetime | None = None,
    ) -> ExecutionStageRun:
        key = _idempotency_key(idempotency_key)
        maximum = _stage_run_budget(max_stage_runs)
        if not isinstance(signer, JobSpecSigner):
            raise ValueError("Execution stage signer is invalid")
        if not isinstance(sandbox_policy, SandboxPolicy):
            raise ValueError("Execution stage SandboxPolicy is invalid")
        try:
            spec = signer.verify(
                signed_job_spec,
                expected_policy=sandbox_policy,
            )
        except ValueError as exc:
            raise ExecutionControlConflictError(str(exc)) from exc
        attempts = ExecutionAttemptService(self.session)
        attempt = attempts.get_verified(attempt_id)
        if (
            sandbox_policy.version != attempt.sandbox_policy_version
            or sandbox_policy.policy_hash != attempt.sandbox_policy_hash
        ):
            raise ExecutionControlConflictError(
                "Scheduled SandboxPolicy does not match the attempt"
            )

        replay = self.session.scalar(
            select(ExecutionStageRun).where(
                ExecutionStageRun.idempotency_key == key
            )
        )
        if replay is not None:
            pending = self.session.get(
                ExecutionStageVersion,
                replay.pending_stage_version_id,
            )
            if pending is None:
                raise ExecutionControlConflictError(
                    "Scheduled pending stage was not found"
                )
            self._assert_schedule_replay(
                replay,
                attempt_id=attempt.id,
                pending=pending,
                spec_hash=signed_job_spec.spec_hash,
                max_stage_runs=maximum,
                timeout_seconds=sandbox_policy.timeout_seconds,
            )
            return self.get_verified(replay.id)

        pending = attempts.current(attempt.id)
        if pending.status != ExecutionStageStatus.PENDING.value:
            raise ExecutionControlConflictError(
                "Only the current pending stage can be scheduled"
            )
        expected = attempts.build_current_job_spec(
            attempt.id,
            allowed_change_paths=spec.allowed_change_paths,
            commands=spec.commands,
        )
        if spec != expected:
            raise ExecutionControlConflictError(
                "Scheduled JobSpec does not match the durable pending stage"
            )

        prior_runs = tuple(
            self.session.scalars(
                select(ExecutionStageRun)
                .where(
                    ExecutionStageRun.execution_attempt_id == attempt.id,
                    ExecutionStageRun.stage == pending.stage,
                )
                .order_by(ExecutionStageRun.stage_run_number)
            )
        )
        if prior_runs:
            previous = self.get_verified(prior_runs[-1].id)
            if previous.max_stage_runs != maximum:
                raise ExecutionControlConflictError(
                    "Stage retry budget cannot change after scheduling"
                )
            if JobState(previous.job.state) not in {
                JobState.FAILED,
                JobState.TIMED_OUT,
            }:
                raise ExecutionControlConflictError(
                    "A new stage run requires an explicit retryable terminal Job"
                )
            run_number = previous.stage_run_number + 1
        else:
            run_number = 1
        if run_number > maximum:
            raise ExecutionRetryExhaustedError(
                "Execution stage exhausted its bounded retry budget"
            )

        payload = execution_stage_job_payload(
            execution_attempt_id=attempt.id,
            attempt_record_hash=attempt.record_hash,
            pending_stage_version_id=pending.id,
            pending_stage_record_hash=pending.record_hash,
            stage=SandboxStage(pending.stage),
            stage_run_number=run_number,
            max_stage_runs=maximum,
            timeout_seconds=sandbox_policy.timeout_seconds,
            job_spec_hash=signed_job_spec.spec_hash,
            input_hashes=tuple(pending.input_hashes),
        )
        ensure_no_sensitive_data(
            {"idempotency_key": key, "job": payload},
            context="Execution stage Job",
        )
        try:
            job, _ = JobService(self.session).enqueue(
                kind=EXECUTION_STAGE_JOB_KIND,
                idempotency_key=key,
                payload=payload,
                max_attempts=1,
                timeout_seconds=sandbox_policy.timeout_seconds,
                now=now,
                commit=False,
            )
            record_payload = execution_stage_run_payload(
                job_id=job.id,
                execution_attempt_id=attempt.id,
                pending_stage_version_id=pending.id,
                stage=SandboxStage(pending.stage),
                stage_run_number=run_number,
                max_stage_runs=maximum,
                timeout_seconds=sandbox_policy.timeout_seconds,
                job_spec_hash=signed_job_spec.spec_hash,
                input_hashes=tuple(pending.input_hashes),
                attempt_record_hash=attempt.record_hash,
                pending_stage_record_hash=pending.record_hash,
            )
            run = ExecutionStageRun(
                id=str(uuid4()),
                job_id=job.id,
                execution_attempt_id=attempt.id,
                pending_stage_version_id=pending.id,
                schema_version=EXECUTION_STAGE_RUN_SCHEMA_VERSION,
                stage=pending.stage,
                stage_run_number=run_number,
                max_stage_runs=maximum,
                timeout_seconds=sandbox_policy.timeout_seconds,
                idempotency_key=key,
                job_spec_hash=signed_job_spec.spec_hash,
                input_hashes=list(pending.input_hashes),
                attempt_record_hash=attempt.record_hash,
                pending_stage_record_hash=pending.record_hash,
                record_hash=content_hash(record_payload),
                created_at=_aware(now),
            )
            self.session.add(run)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(ExecutionStageRun).where(
                    ExecutionStageRun.idempotency_key == key
                )
            )
            if replay is not None:
                self._assert_schedule_replay(
                    replay,
                    attempt_id=attempt.id,
                    pending=pending,
                    spec_hash=signed_job_spec.spec_hash,
                    max_stage_runs=maximum,
                    timeout_seconds=sandbox_policy.timeout_seconds,
                )
                return self.get_verified(replay.id)
            raise ExecutionControlConflictError(
                "Execution stage scheduling changed concurrently "
                "or failed provenance checks"
            ) from exc
        return run

    def get_verified(self, run_id: str) -> ExecutionStageRun:
        run = self.session.get(ExecutionStageRun, run_id)
        if run is None:
            raise ExecutionStageRunNotFoundError(
                "ExecutionStageRun was not found"
            )
        try:
            stage = SandboxStage(run.stage)
            key = _idempotency_key(run.idempotency_key)
            spec_hash = _hash(run.job_spec_hash, name="JobSpec hash")
            attempt_hash = _hash(
                run.attempt_record_hash,
                name="attempt record hash",
            )
            pending_hash = _hash(
                run.pending_stage_record_hash,
                name="pending stage record hash",
            )
            inputs = _hashes(run.input_hashes)
            maximum = _stage_run_budget(run.max_stage_runs)
            if (
                isinstance(run.stage_run_number, bool)
                or not isinstance(run.stage_run_number, int)
                or run.stage_run_number < 1
                or run.stage_run_number > maximum
            ):
                raise ValueError("Execution stage run number is invalid")
            if (
                isinstance(run.timeout_seconds, bool)
                or not isinstance(run.timeout_seconds, int)
                or not 1 <= run.timeout_seconds <= 600
            ):
                raise ValueError("Execution stage timeout is invalid")
        except ValueError as exc:
            raise ExecutionControlConflictError(str(exc)) from exc
        attempt = ExecutionAttemptService(self.session).get_verified(
            run.execution_attempt_id
        )
        pending = self.session.get(
            ExecutionStageVersion,
            run.pending_stage_version_id,
        )
        job = self.session.get(Job, run.job_id)
        if pending is None or job is None:
            raise ExecutionControlConflictError(
                "ExecutionStageRun provenance was not found"
            )
        expected_job_payload = execution_stage_job_payload(
            execution_attempt_id=attempt.id,
            attempt_record_hash=attempt.record_hash,
            pending_stage_version_id=pending.id,
            pending_stage_record_hash=pending.record_hash,
            stage=stage,
            stage_run_number=run.stage_run_number,
            max_stage_runs=maximum,
            timeout_seconds=run.timeout_seconds,
            job_spec_hash=spec_hash,
            input_hashes=inputs,
        )
        expected_record_payload = execution_stage_run_payload(
            job_id=job.id,
            execution_attempt_id=attempt.id,
            pending_stage_version_id=pending.id,
            stage=stage,
            stage_run_number=run.stage_run_number,
            max_stage_runs=maximum,
            timeout_seconds=run.timeout_seconds,
            job_spec_hash=spec_hash,
            input_hashes=inputs,
            attempt_record_hash=attempt_hash,
            pending_stage_record_hash=pending_hash,
        )
        prior_runs = tuple(
            self.session.scalars(
                select(ExecutionStageRun)
                .where(
                    ExecutionStageRun.execution_attempt_id == attempt.id,
                    ExecutionStageRun.stage == stage.value,
                    ExecutionStageRun.stage_run_number
                    <= run.stage_run_number,
                )
                .order_by(ExecutionStageRun.stage_run_number)
            )
        )
        if (
            run.schema_version != EXECUTION_STAGE_RUN_SCHEMA_VERSION
            or run.execution_attempt_id != attempt.id
            or attempt_hash != attempt.record_hash
            or pending.execution_attempt_id != attempt.id
            or pending.status != ExecutionStageStatus.PENDING.value
            or pending.stage != stage.value
            or pending.record_hash != pending_hash
            or tuple(pending.input_hashes) != inputs
            or job.kind != EXECUTION_STAGE_JOB_KIND
            or job.idempotency_key != key
            or job.payload != expected_job_payload
            or job.payload_hash != content_hash(expected_job_payload)
            or job.max_attempts != 1
            or job.timeout_seconds != run.timeout_seconds
            or run.record_hash != content_hash(expected_record_payload)
            or len(prior_runs) != run.stage_run_number
            or tuple(
                item.stage_run_number for item in prior_runs
            ) != tuple(range(1, run.stage_run_number + 1))
            or any(item.max_stage_runs != maximum for item in prior_runs)
        ):
            raise ExecutionControlConflictError(
                "ExecutionStageRun does not match its immutable inputs"
            )
        return run

    def lease_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> ExecutionStageRun | None:
        self.reconcile_expired(now=now)
        job = JobService(self.session).lease_next(
            worker_id=worker_id,
            kinds=(EXECUTION_STAGE_JOB_KIND,),
            lease_seconds=lease_seconds,
            now=now,
        )
        if job is None:
            return None
        run = self.session.scalar(
            select(ExecutionStageRun).where(
                ExecutionStageRun.job_id == job.id
            )
        )
        if run is None:
            raise ExecutionControlConflictError(
                "Leased sandbox stage Job has no immutable run binding"
            )
        return self.get_verified(run.id)

    def begin(
        self,
        run_id: str,
        *,
        worker_id: str,
        signed_job_spec: SignedJobSpec,
        signer: JobSpecSigner,
        sandbox_policy: SandboxPolicy,
        workspace_id: str | None = None,
        workspace_ref: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionStageVersion:
        run = self.get_verified(run_id)
        job = run.job
        attempts = ExecutionAttemptService(self.session)
        current = attempts.current(run.execution_attempt_id)
        if (
            job.state == JobState.RUNNING.value
            and job.lease_owner == worker_id
            and current.status == ExecutionStageStatus.RUNNING.value
            and current.previous_stage_state_hash
            == run.pending_stage_record_hash
            and current.job_spec_hash == run.job_spec_hash
        ):
            return current
        if job.state != JobState.LEASED.value:
            raise ExecutionControlConflictError(
                "Execution stage Job must be leased before begin"
            )
        if (
            current.id != run.pending_stage_version_id
            or current.record_hash != run.pending_stage_record_hash
        ):
            raise ExecutionControlConflictError(
                "Execution stage pending state changed before begin"
            )
        if signed_job_spec.spec_hash != run.job_spec_hash:
            raise ExecutionControlConflictError(
                "Execution stage signed JobSpec hash changed"
            )
        try:
            JobService(self.session).start(
                job.id,
                worker_id=worker_id,
                now=now,
                commit=False,
            )
            running = attempts.mark_running(
                run.execution_attempt_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                signed_job_spec=signed_job_spec,
                signer=signer,
                sandbox_policy=sandbox_policy,
                idempotency_key=f"run:{run.id}:running",
                workspace_id=workspace_id,
                workspace_ref=workspace_ref,
                now=now,
                commit=False,
            )
            self.session.commit()
            return running
        except Exception:
            self.session.rollback()
            raise

    def heartbeat(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> Job:
        run = self.get_verified(run_id)
        return JobService(self.session).heartbeat(
            run.job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            now=now,
        )

    def request_cancel(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> Job:
        run = self.get_verified(run_id)
        job = run.job
        previous_state = JobState(job.state)
        if previous_state in TERMINAL_STATES:
            self._reconcile_terminal(run, now=now)
            return job
        attempts = ExecutionAttemptService(self.session)
        current = attempts.current(run.execution_attempt_id)
        try:
            requested = JobService(self.session).request_cancel(
                job.id,
                now=now,
                commit=False,
            )
            if previous_state is JobState.QUEUED:
                if current.id != run.pending_stage_version_id:
                    raise ExecutionControlConflictError(
                        "Queued cancellation no longer owns the pending stage"
                    )
                attempts.finish_stage(
                    run.execution_attempt_id,
                    expected_sequence=current.sequence,
                    expected_record_hash=current.record_hash,
                    status=ExecutionStageStatus.CANCELLED,
                    reason_code="cancellation_requested",
                    idempotency_key=f"run:{run.id}:cancelled",
                    now=now,
                    commit=False,
                )
            self.session.commit()
            return requested
        except Exception:
            self.session.rollback()
            raise

    def complete(
        self,
        run_id: str,
        *,
        worker_id: str,
        status: ExecutionStageStatus | str,
        reason_code: str,
        result_hash: str | None = None,
        workspace_inventory_hash: str | None = None,
        artifact_store: ArtifactStore | None = None,
        artifact_bundle: ExecutionArtifactBundle | None = None,
        now: datetime | None = None,
    ) -> ExecutionStageVersion:
        try:
            target = ExecutionStageStatus(status)
        except ValueError as exc:
            raise ExecutionControlConflictError(
                "Execution completion status is invalid"
            ) from exc
        if target not in {
            ExecutionStageStatus.SUCCEEDED,
            ExecutionStageStatus.FAILED,
            ExecutionStageStatus.CANCELLED,
            ExecutionStageStatus.TIMED_OUT,
        }:
            raise ExecutionControlConflictError(
                "Execution completion requires a terminal status"
            )
        reason = _reason_code(reason_code)
        run = self.get_verified(run_id)
        attempts = ExecutionAttemptService(self.session)
        current = attempts.current(run.execution_attempt_id)
        job = run.job
        if JobState(job.state) in TERMINAL_STATES:
            if (
                current.stage == run.stage
                and current.status == target.value
                and current.reason_code == reason
                and current.result_hash == result_hash
            ):
                if target is ExecutionStageStatus.SUCCEEDED:
                    if artifact_store is None or artifact_bundle is None:
                        raise ExecutionControlConflictError(
                            "Succeeded execution replay requires its exact "
                            "artifact bundle"
                        )
                    try:
                        artifact_store.finalize_execution_bundle(
                            stage_run_id=run.id,
                            bundle=artifact_bundle,
                            now=now,
                            commit=False,
                        )
                    except ArtifactError as exc:
                        raise ExecutionControlConflictError(str(exc)) from exc
                return current
            raise ExecutionControlConflictError(
                "Execution stage Job already has a different terminal outcome"
            )
        if (
            job.state != JobState.RUNNING.value
            or job.lease_owner != worker_id
            or current.status != ExecutionStageStatus.RUNNING.value
            or current.previous_stage_state_hash
            != run.pending_stage_record_hash
        ):
            raise ExecutionControlConflictError(
                "Only the owning live Worker can complete this stage run"
            )
        jobs = JobService(self.session)
        try:
            if target is ExecutionStageStatus.SUCCEEDED:
                if result_hash is None:
                    raise ExecutionControlConflictError(
                        "Succeeded execution requires a result hash"
                    )
                if job.cancel_requested_at is not None:
                    raise JobTransitionError(
                        "Cancelled execution stage Job cannot succeed"
                    )
                if (
                    artifact_store is None
                    or artifact_bundle is None
                    or artifact_store.session is not self.session
                ):
                    raise ExecutionControlConflictError(
                        "Succeeded execution requires atomic artifact "
                        "finalization in the stage transaction"
                    )
                if (
                    artifact_bundle.stage.value != run.stage
                    or artifact_bundle.result_hash != result_hash
                ):
                    raise ExecutionControlConflictError(
                        "Execution artifact bundle does not match the result"
                    )
                try:
                    manifest = artifact_store.finalize_execution_bundle(
                        stage_run_id=run.id,
                        bundle=artifact_bundle,
                        now=now,
                        commit=False,
                    )
                    if run.stage == SandboxStage.VERIFY.value:
                        ExecutionWorkspaceDisposalService(
                            self.session
                        ).prepare(
                            verify_stage_run_id=run.id,
                            artifact_manifest_id=manifest.id,
                            now=now,
                            commit=False,
                        )
                except IntegrityError as exc:
                    raise ExecutionControlConflictError(
                        "Execution artifacts could not be finalized atomically"
                    ) from exc
                except ArtifactError as exc:
                    raise ExecutionControlConflictError(str(exc)) from exc
                except WorkspaceDisposalError as exc:
                    raise ExecutionControlConflictError(str(exc)) from exc
                jobs.succeed(
                    job.id,
                    worker_id=worker_id,
                    result_data={
                        "result_hash": result_hash,
                        "artifact_manifest_hash": manifest.manifest_hash,
                    },
                    now=now,
                    commit=False,
                )
            elif target is ExecutionStageStatus.FAILED:
                jobs.fail(
                    job.id,
                    worker_id=worker_id,
                    error_code=reason,
                    error_message="Sandbox stage failed explicitly",
                    now=now,
                    commit=False,
                )
            elif target is ExecutionStageStatus.TIMED_OUT:
                jobs.time_out(
                    job.id,
                    worker_id=worker_id,
                    error_message="Sandbox stage exceeded its bounded timeout",
                    now=now,
                    commit=False,
                )
            else:
                jobs.cancel(
                    job.id,
                    worker_id=worker_id,
                    now=now,
                    commit=False,
                )
            terminal = attempts.finish_stage(
                run.execution_attempt_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                status=target,
                reason_code=reason,
                idempotency_key=f"run:{run.id}:{target.value}",
                result_hash=result_hash,
                workspace_inventory_hash=workspace_inventory_hash,
                now=now,
                commit=False,
            )
            self.session.commit()
            return terminal
        except Exception:
            self.session.rollback()
            raise

    def reconcile_expired(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        job_ids = JobService(self.session).recover_expired(now=now)
        reconciled: list[str] = []
        for job_id in job_ids:
            run = self.session.scalar(
                select(ExecutionStageRun).where(
                    ExecutionStageRun.job_id == job_id
                )
            )
            if run is None:
                continue
            verified = self.get_verified(run.id)
            self._reconcile_terminal(verified, now=now)
            reconciled.append(verified.id)
        return tuple(reconciled)

    def prepare_retry(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ExecutionStageVersion:
        run = self.get_verified(run_id)
        if run.stage_run_number >= run.max_stage_runs:
            raise ExecutionRetryExhaustedError(
                "Execution stage exhausted its bounded retry budget"
            )
        job_state = JobState(run.job.state)
        if job_state not in {JobState.FAILED, JobState.TIMED_OUT}:
            raise ExecutionControlConflictError(
                "Only failed or timed-out stage Jobs can retry"
            )
        attempts = ExecutionAttemptService(self.session)
        current = attempts.current(run.execution_attempt_id)
        if (
            current.stage != run.stage
            or current.status
            not in {
                ExecutionStageStatus.FAILED.value,
                ExecutionStageStatus.TIMED_OUT.value,
            }
        ):
            replay = self.session.scalar(
                select(ExecutionStageVersion).where(
                    ExecutionStageVersion.idempotency_key
                    == _idempotency_key(idempotency_key)
                )
            )
            if (
                replay is not None
                and replay.execution_attempt_id == run.execution_attempt_id
                and replay.stage == run.stage
                and replay.status == ExecutionStageStatus.PENDING.value
            ):
                return replay
            raise ExecutionControlConflictError(
                "Retryable Job does not match the current terminal stage"
            )
        try:
            return attempts.retry_stage(
                run.execution_attempt_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                reason_code="retry_scheduled",
                idempotency_key=idempotency_key,
                now=now,
            )
        except (
            ExecutionAttemptConflictError,
            ExecutionStageTransitionError,
        ) as exc:
            raise ExecutionControlConflictError(str(exc)) from exc

    def _reconcile_terminal(
        self,
        run: ExecutionStageRun,
        *,
        now: datetime | None,
    ) -> None:
        job_state = JobState(run.job.state)
        mapping = {
            JobState.CANCELLED: (
                ExecutionStageStatus.CANCELLED,
                "cancelled_after_worker_loss",
            ),
            JobState.TIMED_OUT: (
                ExecutionStageStatus.TIMED_OUT,
                "worker_lease_expired",
            ),
            JobState.FAILED: (
                ExecutionStageStatus.FAILED,
                "worker_failed",
            ),
        }
        outcome = mapping.get(job_state)
        if outcome is None:
            return
        attempts = ExecutionAttemptService(self.session)
        current = attempts.current(run.execution_attempt_id)
        if (
            current.status
            in {
                ExecutionStageStatus.FAILED.value,
                ExecutionStageStatus.CANCELLED.value,
                ExecutionStageStatus.TIMED_OUT.value,
            }
            and current.stage == run.stage
        ):
            return
        owns_pending = (
            current.id == run.pending_stage_version_id
            and current.record_hash == run.pending_stage_record_hash
        )
        owns_running = (
            current.stage == run.stage
            and current.status == ExecutionStageStatus.RUNNING.value
            and current.previous_stage_state_hash
            == run.pending_stage_record_hash
        )
        if not owns_pending and not owns_running:
            raise ExecutionControlConflictError(
                "Terminal stage Job does not match the current stage"
            )
        target, reason = outcome
        attempts.finish_stage(
            run.execution_attempt_id,
            expected_sequence=current.sequence,
            expected_record_hash=current.record_hash,
            status=target,
            reason_code=reason,
            idempotency_key=f"reconcile:{run.id}:{target.value}",
            now=now,
        )

    @staticmethod
    def _assert_schedule_replay(
        replay: ExecutionStageRun,
        *,
        attempt_id: str,
        pending: ExecutionStageVersion,
        spec_hash: str,
        max_stage_runs: int,
        timeout_seconds: int,
    ) -> None:
        if (
            replay.execution_attempt_id != attempt_id
            or replay.pending_stage_version_id != pending.id
            or replay.pending_stage_record_hash != pending.record_hash
            or replay.stage != pending.stage
            or replay.job_spec_hash != spec_hash
            or tuple(replay.input_hashes) != tuple(pending.input_hashes)
            or replay.max_stage_runs != max_stage_runs
            or replay.timeout_seconds != timeout_seconds
        ):
            raise ExecutionControlConflictError(
                "Stage scheduling idempotency key belongs to different inputs"
            )


def execution_stage_job_payload(
    *,
    execution_attempt_id: str,
    attempt_record_hash: str,
    pending_stage_version_id: str,
    pending_stage_record_hash: str,
    stage: SandboxStage,
    stage_run_number: int,
    max_stage_runs: int,
    timeout_seconds: int,
    job_spec_hash: str,
    input_hashes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": EXECUTION_STAGE_JOB_SCHEMA_VERSION,
        "execution_attempt_id": execution_attempt_id,
        "attempt_record_hash": attempt_record_hash,
        "pending_stage_version_id": pending_stage_version_id,
        "pending_stage_record_hash": pending_stage_record_hash,
        "stage": stage.value,
        "stage_run_number": stage_run_number,
        "max_stage_runs": max_stage_runs,
        "timeout_seconds": timeout_seconds,
        "job_spec_hash": job_spec_hash,
        "input_hashes": list(input_hashes),
    }


def execution_stage_run_payload(
    *,
    job_id: str,
    execution_attempt_id: str,
    pending_stage_version_id: str,
    stage: SandboxStage,
    stage_run_number: int,
    max_stage_runs: int,
    timeout_seconds: int,
    job_spec_hash: str,
    input_hashes: tuple[str, ...],
    attempt_record_hash: str,
    pending_stage_record_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": EXECUTION_STAGE_RUN_SCHEMA_VERSION,
        "job_id": job_id,
        "execution_attempt_id": execution_attempt_id,
        "pending_stage_version_id": pending_stage_version_id,
        "stage": stage.value,
        "stage_run_number": stage_run_number,
        "max_stage_runs": max_stage_runs,
        "timeout_seconds": timeout_seconds,
        "job_spec_hash": job_spec_hash,
        "input_hashes": list(input_hashes),
        "attempt_record_hash": attempt_record_hash,
        "pending_stage_record_hash": pending_stage_record_hash,
    }


def _idempotency_key(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not _IDEMPOTENCY_KEY.fullmatch(normalized)
        or contains_sensitive_text(normalized)
    ):
        raise ValueError("Execution stage idempotency key is invalid")
    return normalized


def _reason_code(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not _REASON_CODE.fullmatch(normalized)
        or contains_sensitive_text(normalized)
    ):
        raise ValueError("Execution stage reason code is invalid")
    return normalized


def _hash(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"Execution stage {name} is invalid")
    return value


def _hashes(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("Execution stage input hashes are invalid")
    normalized = tuple(
        _hash(value, name="input hash")
        for value in values
    )
    if len(normalized) > 100 or len(normalized) != len(set(normalized)):
        raise ValueError("Execution stage input hashes are invalid")
    return normalized


def _stage_run_budget(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_STAGE_RUNS
    ):
        raise ValueError(
            f"Execution stage run budget must be between 1 and {MAX_STAGE_RUNS}"
        )
    return value


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
