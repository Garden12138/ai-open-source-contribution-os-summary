from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approvals import ApprovalInputFingerprint, PlanApprovalService
from app.audit import AuditService
from app.authorizations import (
    AuthorizationActionError,
    UserAction,
    require_user_action,
)
from app.execution_readiness import (
    ExecutionReadinessError,
    ExecutionReadinessService,
)
from app.models import (
    AuditEvent,
    ContributionTaskStateVersion,
    ExecutionAttempt,
    ExecutionStageVersion,
    OpportunitySnapshot,
)
from app.planning import ContributionTaskError, ContributionTaskService
from app.provenance import content_hash
from app.sandbox_worker.specs import (
    JobSpec,
    JobSpecSigner,
    SandboxCommand,
    SandboxPolicy,
    SandboxStage,
    SignedJobSpec,
)
from app.security import contains_sensitive_text, ensure_no_sensitive_data
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
    task_state_record_payload,
)


EXECUTION_ATTEMPT_SCHEMA_VERSION = "1"
EXECUTION_STAGE_SCHEMA_VERSION = "1"
MAX_EXECUTION_ATTEMPTS = 4
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ACTOR_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class ExecutionStageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


_TERMINAL_STAGE_STATUSES = frozenset(
    {
        ExecutionStageStatus.SUCCEEDED,
        ExecutionStageStatus.FAILED,
        ExecutionStageStatus.CANCELLED,
        ExecutionStageStatus.TIMED_OUT,
    }
)
_NEXT_STAGE = {
    SandboxStage.EXPLORE: SandboxStage.IMPLEMENT,
    SandboxStage.IMPLEMENT: SandboxStage.VERIFY,
}


class ExecutionAttemptError(RuntimeError):
    pass


class ExecutionAttemptNotFoundError(ExecutionAttemptError):
    pass


class ExecutionAttemptConflictError(ExecutionAttemptError):
    def __init__(
        self,
        message: str,
        *,
        reason_codes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason_codes = reason_codes


class ExecutionStageTransitionError(ExecutionAttemptError):
    pass


class ExecutionAttemptService:
    """Persist exact execution provenance and append-only stage transitions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def start(
        self,
        *,
        approval_id: str,
        observed: ApprovalInputFingerprint,
        action: UserAction | str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        repository_archive_hash: str,
        runner_image_digest: str,
        sandbox_policy: SandboxPolicy,
        now: datetime | None = None,
    ) -> ExecutionAttempt:
        try:
            require_user_action(action, expected=UserAction.START_EXECUTION)
        except AuthorizationActionError as exc:
            raise ExecutionAttemptConflictError(str(exc)) from exc
        key = _idempotency_key(idempotency_key)
        normalized_actor_type = _actor_type(actor_type)
        normalized_actor_id = _actor_id(actor_id)
        archive_hash = _hash(
            repository_archive_hash,
            name="repository archive hash",
        )
        image_digest = _image_digest(runner_image_digest)
        if not isinstance(sandbox_policy, SandboxPolicy):
            raise ValueError("Execution sandbox policy is invalid")
        observed_hash = content_hash(observed.hash_payload())

        replay = self.session.scalar(
            select(ExecutionAttempt).where(
                ExecutionAttempt.idempotency_key == key
            )
        )
        if replay is not None:
            self._assert_start_replay(
                replay,
                approval_id=approval_id,
                actor_type=normalized_actor_type,
                actor_id=normalized_actor_id,
                repository_archive_hash=archive_hash,
                runner_image_digest=image_digest,
                sandbox_policy=sandbox_policy,
                observed_fingerprint_hash=observed_hash,
            )
            return self.get_verified(replay.id)

        try:
            readiness = ExecutionReadinessService(self.session).assert_ready(
                approval_id=approval_id,
                observed=observed,
                action=action,
            )
        except ExecutionReadinessError as exc:
            raise ExecutionAttemptConflictError(
                str(exc),
                reason_codes=exc.reason_codes,
            ) from exc
        approval = PlanApprovalService(self.session).get_verified(
            readiness.approval_id
        )
        lock = approval.plan_lock
        try:
            task = ContributionTaskService(self.session).get_verified(
                readiness.task_id
            )
        except ContributionTaskError as exc:
            raise ExecutionAttemptConflictError(str(exc)) from exc
        snapshot = self.session.get(OpportunitySnapshot, task.snapshot_id)
        if snapshot is None:
            raise ExecutionAttemptConflictError(
                "Execution repository snapshot was not found"
            )
        repository_full_name = _snapshot_repository(snapshot)
        states = ContributionTaskStateService(self.session)
        try:
            current = states.current(task.id)
            executing = states.prepare_transition(
                task.id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                to_state=ContributionTaskState.EXECUTING,
                reason_code="execution_started",
                now=now,
            )
        except TaskStateError as exc:
            raise ExecutionAttemptConflictError(str(exc)) from exc
        if (
            current.id != readiness.task_state_version_id
            or current.record_hash != readiness.task_state_record_hash
        ):
            raise ExecutionAttemptConflictError(
                "Execution readiness state changed concurrently"
            )

        attempt_number = (
            self.session.scalar(
                select(func.max(ExecutionAttempt.attempt_number)).where(
                    ExecutionAttempt.task_id == task.id
                )
            )
            or 0
        ) + 1
        payload = execution_attempt_payload(
            task_id=task.id,
            plan_version_id=approval.plan_version_id,
            plan_approval_id=approval.id,
            approved_state_version_id=approval.approved_state_version_id,
            executing_state_version_id=executing.id,
            attempt_number=attempt_number,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            repository_full_name=repository_full_name,
            base_commit_sha=lock.base_commit_sha,
            repository_archive_hash=archive_hash,
            runner_image_digest=image_digest,
            sandbox_policy_version=sandbox_policy.version,
            sandbox_policy_hash=sandbox_policy.policy_hash,
            task_record_hash=task.record_hash,
            analysis_version_id=lock.analysis_version_id,
            analysis_record_hash=lock.analysis_record_hash,
            analysis_output_hash=lock.analysis_output_hash,
            snapshot_id=lock.snapshot_id,
            snapshot_inputs_hash=lock.snapshot_inputs_hash,
            provider_contract_hash=lock.provider_contract_hash,
            plan_content_hash=approval.plan_content_hash,
            plan_record_hash=approval.plan_record_hash,
            approval_hash=approval.approval_hash,
            approved_state_record_hash=approval.approved_state_record_hash,
            executing_state_record_hash=executing.record_hash,
            observed_fingerprint_hash=observed_hash,
        )
        ensure_no_sensitive_data(
            {"idempotency_key": key, "execution_attempt": payload},
            context="ExecutionAttempt",
        )
        attempt = ExecutionAttempt(
            id=str(uuid4()),
            task_id=task.id,
            plan_version_id=approval.plan_version_id,
            plan_approval_id=approval.id,
            approved_state_version_id=approval.approved_state_version_id,
            executing_state_version_id=executing.id,
            schema_version=EXECUTION_ATTEMPT_SCHEMA_VERSION,
            attempt_number=attempt_number,
            idempotency_key=key,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            action=UserAction.START_EXECUTION.value,
            repository_full_name=repository_full_name,
            base_commit_sha=lock.base_commit_sha,
            repository_archive_hash=archive_hash,
            runner_image_digest=image_digest,
            sandbox_policy_version=sandbox_policy.version,
            sandbox_policy_hash=sandbox_policy.policy_hash,
            task_record_hash=task.record_hash,
            analysis_version_id=lock.analysis_version_id,
            analysis_record_hash=lock.analysis_record_hash,
            analysis_output_hash=lock.analysis_output_hash,
            snapshot_id=lock.snapshot_id,
            snapshot_inputs_hash=lock.snapshot_inputs_hash,
            provider_contract_hash=lock.provider_contract_hash,
            plan_content_hash=approval.plan_content_hash,
            plan_record_hash=approval.plan_record_hash,
            approval_hash=approval.approval_hash,
            approved_state_record_hash=approval.approved_state_record_hash,
            executing_state_record_hash=executing.record_hash,
            observed_fingerprint_hash=observed_hash,
            record_hash=content_hash(payload),
            created_at=_aware(now),
        )
        initial = _build_stage_version(
            attempt=attempt,
            sequence=1,
            stage=SandboxStage.EXPLORE,
            status=ExecutionStageStatus.PENDING,
            reason_code="execution_started",
            idempotency_key=f"initial:{attempt.id}",
            job_spec_hash=None,
            input_hashes=(),
            result_hash=None,
            workspace_id=None,
            workspace_ref=None,
            workspace_inventory_hash=None,
            previous_stage_state_hash=None,
            now=attempt.created_at,
        )
        self.session.add_all((executing, attempt, initial))
        try:
            AuditService(self.session).prepare(
                event_type="execution.started",
                actor_type=normalized_actor_type,
                actor_id=normalized_actor_id,
                correlation_id=key,
                payload=execution_started_audit_payload(attempt),
                now=now,
            )
        except Exception as exc:
            self.session.rollback()
            raise ExecutionAttemptConflictError(
                "Execution start audit evidence could not be prepared"
            ) from exc
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(ExecutionAttempt).where(
                    ExecutionAttempt.idempotency_key == key
                )
            )
            if replay is not None:
                self._assert_start_replay(
                    replay,
                    approval_id=approval_id,
                    actor_type=normalized_actor_type,
                    actor_id=normalized_actor_id,
                    repository_archive_hash=archive_hash,
                    runner_image_digest=image_digest,
                    sandbox_policy=sandbox_policy,
                    observed_fingerprint_hash=observed_hash,
                )
                return self.get_verified(replay.id)
            raise ExecutionAttemptConflictError(
                "Execution start changed concurrently or failed provenance checks"
            ) from exc
        return attempt

    def start_repair(
        self,
        *,
        previous_attempt_id: str,
        action: UserAction | str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> ExecutionAttempt:
        try:
            require_user_action(action, expected=UserAction.START_REPAIR)
        except AuthorizationActionError as exc:
            raise ExecutionAttemptConflictError(str(exc)) from exc
        key = _idempotency_key(idempotency_key)
        normalized_actor_type = _actor_type(actor_type)
        normalized_actor_id = _actor_id(actor_id)
        replay = self.session.scalar(
            select(ExecutionAttempt).where(
                ExecutionAttempt.idempotency_key == key
            )
        )
        if replay is not None:
            if (
                replay.actor_type != normalized_actor_type
                or replay.actor_id != normalized_actor_id
                or replay.attempt_number < 2
            ):
                raise ExecutionAttemptConflictError(
                    "Repair idempotency key belongs to different inputs"
                )
            return self.get_verified(replay.id)

        previous = self.get_verified(previous_attempt_id)
        try:
            task = ContributionTaskService(self.session).get_verified(
                previous.task_id
            )
        except ContributionTaskError as exc:
            raise ExecutionAttemptConflictError(str(exc)) from exc
        approval = PlanApprovalService(self.session).get_verified(
            previous.plan_approval_id
        )
        lock = approval.plan_lock
        snapshot = self.session.get(OpportunitySnapshot, task.snapshot_id)
        if snapshot is None:
            raise ExecutionAttemptConflictError(
                "Execution repository snapshot was not found"
            )
        attempt_number = (
            self.session.scalar(
                select(func.max(ExecutionAttempt.attempt_number)).where(
                    ExecutionAttempt.task_id == task.id
                )
            )
            or 0
        ) + 1
        if attempt_number > MAX_EXECUTION_ATTEMPTS:
            raise ExecutionAttemptConflictError(
                "Repair attempts are capped at three"
            )
        if previous.attempt_number + 1 != attempt_number:
            raise ExecutionAttemptConflictError(
                "Repair must continue from the latest ExecutionAttempt"
            )
        states = ContributionTaskStateService(self.session)
        try:
            current = states.current(task.id)
            executing = states.prepare_transition(
                task.id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                to_state=ContributionTaskState.EXECUTING,
                reason_code="repair_started",
                now=now,
            )
        except TaskStateError as exc:
            raise ExecutionAttemptConflictError(str(exc)) from exc
        if current.to_state != ContributionTaskState.REVIEWING.value:
            raise ExecutionAttemptConflictError(
                "Repair requires the current reviewing task state"
            )
        sandbox_policy = SandboxPolicy()
        if (
            sandbox_policy.version != previous.sandbox_policy_version
            or sandbox_policy.policy_hash != previous.sandbox_policy_hash
        ):
            raise ExecutionAttemptConflictError(
                "Repair sandbox policy does not match the previous attempt"
            )
        observed_hash = previous.observed_fingerprint_hash
        repository_full_name = _snapshot_repository(snapshot)
        payload = execution_attempt_payload(
            task_id=task.id,
            plan_version_id=approval.plan_version_id,
            plan_approval_id=approval.id,
            approved_state_version_id=approval.approved_state_version_id,
            executing_state_version_id=executing.id,
            attempt_number=attempt_number,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            repository_full_name=repository_full_name,
            base_commit_sha=lock.base_commit_sha,
            repository_archive_hash=previous.repository_archive_hash,
            runner_image_digest=previous.runner_image_digest,
            sandbox_policy_version=previous.sandbox_policy_version,
            sandbox_policy_hash=previous.sandbox_policy_hash,
            task_record_hash=task.record_hash,
            analysis_version_id=lock.analysis_version_id,
            analysis_record_hash=lock.analysis_record_hash,
            analysis_output_hash=lock.analysis_output_hash,
            snapshot_id=lock.snapshot_id,
            snapshot_inputs_hash=lock.snapshot_inputs_hash,
            provider_contract_hash=lock.provider_contract_hash,
            plan_content_hash=approval.plan_content_hash,
            plan_record_hash=approval.plan_record_hash,
            approval_hash=approval.approval_hash,
            approved_state_record_hash=approval.approved_state_record_hash,
            executing_state_record_hash=executing.record_hash,
            observed_fingerprint_hash=observed_hash,
        )
        ensure_no_sensitive_data(
            {"idempotency_key": key, "execution_attempt": payload},
            context="ExecutionAttempt",
        )
        attempt = ExecutionAttempt(
            id=str(uuid4()),
            task_id=task.id,
            plan_version_id=approval.plan_version_id,
            plan_approval_id=approval.id,
            approved_state_version_id=approval.approved_state_version_id,
            executing_state_version_id=executing.id,
            schema_version=EXECUTION_ATTEMPT_SCHEMA_VERSION,
            attempt_number=attempt_number,
            idempotency_key=key,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            action=UserAction.START_EXECUTION.value,
            repository_full_name=repository_full_name,
            base_commit_sha=lock.base_commit_sha,
            repository_archive_hash=previous.repository_archive_hash,
            runner_image_digest=previous.runner_image_digest,
            sandbox_policy_version=previous.sandbox_policy_version,
            sandbox_policy_hash=previous.sandbox_policy_hash,
            task_record_hash=task.record_hash,
            analysis_version_id=lock.analysis_version_id,
            analysis_record_hash=lock.analysis_record_hash,
            analysis_output_hash=lock.analysis_output_hash,
            snapshot_id=lock.snapshot_id,
            snapshot_inputs_hash=lock.snapshot_inputs_hash,
            provider_contract_hash=lock.provider_contract_hash,
            plan_content_hash=approval.plan_content_hash,
            plan_record_hash=approval.plan_record_hash,
            approval_hash=approval.approval_hash,
            approved_state_record_hash=approval.approved_state_record_hash,
            executing_state_record_hash=executing.record_hash,
            observed_fingerprint_hash=observed_hash,
            record_hash=content_hash(payload),
            created_at=_aware(now),
        )
        initial = _build_stage_version(
            attempt=attempt,
            sequence=1,
            stage=SandboxStage.EXPLORE,
            status=ExecutionStageStatus.PENDING,
            reason_code="execution_started",
            idempotency_key=f"initial:{attempt.id}",
            job_spec_hash=None,
            input_hashes=(),
            result_hash=None,
            workspace_id=None,
            workspace_ref=None,
            workspace_inventory_hash=None,
            previous_stage_state_hash=None,
            now=attempt.created_at,
        )
        self.session.add_all((executing, attempt, initial))
        try:
            AuditService(self.session).prepare(
                event_type="execution.started",
                actor_type=normalized_actor_type,
                actor_id=normalized_actor_id,
                correlation_id=key,
                payload=execution_started_audit_payload(attempt),
                now=now,
            )
        except Exception as exc:
            self.session.rollback()
            raise ExecutionAttemptConflictError(
                "Execution repair audit evidence could not be prepared"
            ) from exc
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(ExecutionAttempt).where(
                    ExecutionAttempt.idempotency_key == key
                )
            )
            if replay is not None:
                return self.get_verified(replay.id)
            raise ExecutionAttemptConflictError(
                "Execution repair changed concurrently or failed provenance checks"
            ) from exc
        return attempt

    def get_verified(self, attempt_id: str) -> ExecutionAttempt:
        attempt = self.session.get(ExecutionAttempt, attempt_id)
        if attempt is None:
            raise ExecutionAttemptNotFoundError(
                "ExecutionAttempt was not found"
            )
        approval = PlanApprovalService(self.session).get_verified(
            attempt.plan_approval_id
        )
        lock = approval.plan_lock
        try:
            task = ContributionTaskService(self.session).get_verified(
                attempt.task_id
            )
        except ContributionTaskError as exc:
            raise ExecutionAttemptConflictError(str(exc)) from exc
        approved = self.session.get(
            ContributionTaskStateVersion,
            attempt.approved_state_version_id,
        )
        executing = self.session.get(
            ContributionTaskStateVersion,
            attempt.executing_state_version_id,
        )
        snapshot = self.session.get(OpportunitySnapshot, attempt.snapshot_id)
        if approved is None or executing is None or snapshot is None:
            raise ExecutionAttemptConflictError(
                "ExecutionAttempt provenance was not found"
            )
        approved_hash = _verified_task_state_hash(approved)
        executing_hash = _verified_task_state_hash(executing)
        expected_observed_hash = content_hash(
            ApprovalInputFingerprint.from_lock(lock).hash_payload()
        )
        payload = execution_attempt_payload(
            task_id=task.id,
            plan_version_id=approval.plan_version_id,
            plan_approval_id=approval.id,
            approved_state_version_id=approved.id,
            executing_state_version_id=executing.id,
            attempt_number=attempt.attempt_number,
            actor_type=attempt.actor_type,
            actor_id=attempt.actor_id,
            repository_full_name=_snapshot_repository(snapshot),
            base_commit_sha=lock.base_commit_sha,
            repository_archive_hash=attempt.repository_archive_hash,
            runner_image_digest=attempt.runner_image_digest,
            sandbox_policy_version=attempt.sandbox_policy_version,
            sandbox_policy_hash=attempt.sandbox_policy_hash,
            task_record_hash=task.record_hash,
            analysis_version_id=lock.analysis_version_id,
            analysis_record_hash=lock.analysis_record_hash,
            analysis_output_hash=lock.analysis_output_hash,
            snapshot_id=lock.snapshot_id,
            snapshot_inputs_hash=lock.snapshot_inputs_hash,
            provider_contract_hash=lock.provider_contract_hash,
            plan_content_hash=approval.plan_content_hash,
            plan_record_hash=approval.plan_record_hash,
            approval_hash=approval.approval_hash,
            approved_state_record_hash=approved.record_hash,
            executing_state_record_hash=executing.record_hash,
            observed_fingerprint_hash=attempt.observed_fingerprint_hash,
        )
        try:
            _actor_type(attempt.actor_type)
            _actor_id(attempt.actor_id)
            _hash(
                attempt.repository_archive_hash,
                name="repository archive hash",
            )
            _image_digest(attempt.runner_image_digest)
            _hash(attempt.sandbox_policy_hash, name="sandbox policy hash")
        except ValueError as exc:
            raise ExecutionAttemptConflictError(str(exc)) from exc
        if (
            attempt.schema_version != EXECUTION_ATTEMPT_SCHEMA_VERSION
            or attempt.action != UserAction.START_EXECUTION.value
            or attempt.task_id != approval.task_id
            or attempt.plan_version_id != approval.plan_version_id
            or attempt.approved_state_version_id
            != approval.approved_state_version_id
            or attempt.approval_hash != approval.approval_hash
            or attempt.plan_content_hash != approval.plan_content_hash
            or attempt.plan_record_hash != approval.plan_record_hash
            or task.analysis_version_id != attempt.analysis_version_id
            or task.record_hash != attempt.task_record_hash
            or lock.analysis_version_id != attempt.analysis_version_id
            or lock.snapshot_id != attempt.snapshot_id
            or lock.base_commit_sha != attempt.base_commit_sha
            or lock.provider_contract_hash != attempt.provider_contract_hash
            or lock.analysis_record_hash != attempt.analysis_record_hash
            or lock.analysis_output_hash != attempt.analysis_output_hash
            or lock.snapshot_inputs_hash != attempt.snapshot_inputs_hash
            or approved.task_id != attempt.task_id
            or approved.to_state
            != ContributionTaskState.PLAN_APPROVED.value
            or approved.record_hash != approved_hash
            or approved.record_hash
            != attempt.approved_state_record_hash
            or not _valid_execution_start_state(
                attempt=attempt,
                approved=approved,
                executing=executing,
                executing_hash=executing_hash,
            )
            or attempt.observed_fingerprint_hash
            != expected_observed_hash
            or attempt.record_hash != content_hash(payload)
        ):
            raise ExecutionAttemptConflictError(
                "ExecutionAttempt content does not match its immutable inputs"
            )
        self._verify_start_audit(attempt)
        self._verified_history(attempt)
        return attempt

    def history(self, attempt_id: str) -> tuple[ExecutionStageVersion, ...]:
        attempt = self.get_verified(attempt_id)
        return self._verified_history(attempt)

    def current(self, attempt_id: str) -> ExecutionStageVersion:
        history = self.history(attempt_id)
        return history[-1]

    def build_current_job_spec(
        self,
        attempt_id: str,
        *,
        allowed_change_paths: Sequence[str] = (),
        commands: Sequence[SandboxCommand] = (),
    ) -> JobSpec:
        attempt = self.get_verified(attempt_id)
        current = self._verified_history(attempt)[-1]
        try:
            status = ExecutionStageStatus(current.status)
            stage = SandboxStage(current.stage)
        except ValueError as exc:
            raise ExecutionAttemptConflictError(
                "Execution stage value is invalid"
            ) from exc
        if status is not ExecutionStageStatus.PENDING:
            raise ExecutionStageTransitionError(
                "Only a pending execution stage can build a JobSpec"
            )
        return JobSpec(
            spec_id=(
                f"{attempt.id}:{stage.value}:{current.sequence}"
            ),
            execution_attempt_id=attempt.id,
            correlation_id=attempt.id,
            stage=stage,
            repository_full_name=attempt.repository_full_name,
            base_commit_sha=attempt.base_commit_sha,
            task_id=attempt.task_id,
            task_record_hash=attempt.task_record_hash,
            analysis_version_id=attempt.analysis_version_id,
            analysis_record_hash=attempt.analysis_record_hash,
            analysis_output_hash=attempt.analysis_output_hash,
            snapshot_id=attempt.snapshot_id,
            snapshot_inputs_hash=attempt.snapshot_inputs_hash,
            plan_version_id=attempt.plan_version_id,
            plan_content_hash=attempt.plan_content_hash,
            plan_record_hash=attempt.plan_record_hash,
            plan_approval_id=attempt.plan_approval_id,
            approval_hash=attempt.approval_hash,
            approved_state_version_id=attempt.approved_state_version_id,
            approved_state_record_hash=attempt.approved_state_record_hash,
            provider_contract_hash=attempt.provider_contract_hash,
            repository_archive_hash=attempt.repository_archive_hash,
            runner_image_digest=attempt.runner_image_digest,
            sandbox_policy_version=attempt.sandbox_policy_version,
            sandbox_policy_hash=attempt.sandbox_policy_hash,
            input_artifact_hashes=tuple(current.input_hashes),
            allowed_change_paths=tuple(allowed_change_paths),
            commands=tuple(commands),
        )

    def mark_running(
        self,
        attempt_id: str,
        *,
        expected_sequence: int,
        expected_record_hash: str,
        signed_job_spec: SignedJobSpec,
        signer: JobSpecSigner,
        sandbox_policy: SandboxPolicy,
        idempotency_key: str,
        workspace_id: str | None = None,
        workspace_ref: str | None = None,
        now: datetime | None = None,
        commit: bool = True,
    ) -> ExecutionStageVersion:
        key = _idempotency_key(idempotency_key)
        if not isinstance(signer, JobSpecSigner):
            raise ValueError("Execution JobSpec signer is invalid")
        if not isinstance(sandbox_policy, SandboxPolicy):
            raise ValueError("Execution sandbox policy is invalid")
        try:
            job = signer.verify(
                signed_job_spec,
                expected_policy=sandbox_policy,
            )
        except ValueError as exc:
            raise ExecutionStageTransitionError(str(exc)) from exc
        attempt = self.get_verified(attempt_id)
        if (
            sandbox_policy.version != attempt.sandbox_policy_version
            or sandbox_policy.policy_hash != attempt.sandbox_policy_hash
        ):
            raise ExecutionStageTransitionError(
                "Execution JobSpec policy does not match the attempt"
            )
        stage = SandboxStage(job.stage)
        normalized_workspace_id = (
            _opaque_ref(workspace_id, name="workspace ID")
            if workspace_id is not None
            else None
        )
        normalized_workspace_ref = (
            _opaque_ref(workspace_ref, name="workspace reference")
            if workspace_ref is not None
            else None
        )
        replay = self._stage_replay(key)
        if replay is not None:
            replay_inventory = (
                replay.workspace_inventory_hash
                if stage is SandboxStage.VERIFY
                else None
            )
            if stage is SandboxStage.VERIFY:
                normalized_workspace_id = replay.workspace_id
                normalized_workspace_ref = replay.workspace_ref
            self._assert_stage_replay(
                replay,
                attempt_id=attempt.id,
                expected_sequence=expected_sequence,
                expected_record_hash=expected_record_hash,
                stage=stage,
                status=ExecutionStageStatus.RUNNING,
                reason_code="stage_started",
                job_spec_hash=signed_job_spec.spec_hash,
                input_hashes=job.input_artifact_hashes,
                result_hash=None,
                workspace_id=normalized_workspace_id,
                workspace_ref=normalized_workspace_ref,
                workspace_inventory_hash=replay_inventory,
            )
            self.get_verified(attempt.id)
            return replay
        expected = self.build_current_job_spec(
            attempt.id,
            allowed_change_paths=job.allowed_change_paths,
            commands=job.commands,
        )
        if job != expected:
            raise ExecutionStageTransitionError(
                "Signed JobSpec does not match the durable pending stage"
            )
        current = self._current_for_transition(
            attempt,
            expected_sequence=expected_sequence,
            expected_record_hash=expected_record_hash,
        )
        if (
            current.stage != stage.value
            or current.status != ExecutionStageStatus.PENDING.value
        ):
            raise ExecutionStageTransitionError(
                "Only the current pending stage can start"
            )
        if stage is SandboxStage.IMPLEMENT:
            if (
                normalized_workspace_id is None
                or normalized_workspace_ref is None
            ):
                raise ExecutionStageTransitionError(
                    "Implement must persist its disposable workspace before run"
                )
        elif normalized_workspace_id is not None or normalized_workspace_ref is not None:
            raise ExecutionStageTransitionError(
                "Only Implement accepts a new workspace identity"
            )
        if stage is SandboxStage.VERIFY:
            normalized_workspace_id = current.workspace_id
            normalized_workspace_ref = current.workspace_ref
        return self._append(
            attempt=attempt,
            current=current,
            stage=stage,
            status=ExecutionStageStatus.RUNNING,
            reason_code="stage_started",
            idempotency_key=key,
            job_spec_hash=signed_job_spec.spec_hash,
            input_hashes=tuple(current.input_hashes),
            result_hash=None,
            workspace_id=normalized_workspace_id,
            workspace_ref=normalized_workspace_ref,
            workspace_inventory_hash=current.workspace_inventory_hash,
            now=now,
            commit=commit,
        )

    def finish_stage(
        self,
        attempt_id: str,
        *,
        expected_sequence: int,
        expected_record_hash: str,
        status: ExecutionStageStatus | str,
        reason_code: str,
        idempotency_key: str,
        result_hash: str | None = None,
        workspace_inventory_hash: str | None = None,
        now: datetime | None = None,
        commit: bool = True,
    ) -> ExecutionStageVersion:
        try:
            target = ExecutionStageStatus(status)
        except ValueError as exc:
            raise ExecutionStageTransitionError(
                "Execution terminal status is invalid"
            ) from exc
        if target not in _TERMINAL_STAGE_STATUSES:
            raise ExecutionStageTransitionError(
                "Execution finish requires a terminal status"
            )
        key = _idempotency_key(idempotency_key)
        reason = _reason_code(reason_code)
        normalized_result = (
            _hash(result_hash, name="stage result hash")
            if result_hash is not None
            else None
        )
        normalized_inventory = (
            _hash(
                workspace_inventory_hash,
                name="workspace inventory hash",
            )
            if workspace_inventory_hash is not None
            else None
        )
        if target is ExecutionStageStatus.SUCCEEDED and normalized_result is None:
            raise ExecutionStageTransitionError(
                "A succeeded stage requires an immutable result hash"
            )
        attempt = self.get_verified(attempt_id)
        replay = self._stage_replay(key)
        if replay is not None:
            replay_stage = SandboxStage(replay.stage)
            self._assert_stage_replay(
                replay,
                attempt_id=attempt.id,
                expected_sequence=expected_sequence,
                expected_record_hash=expected_record_hash,
                stage=replay_stage,
                status=target,
                reason_code=reason,
                job_spec_hash=replay.job_spec_hash,
                input_hashes=tuple(replay.input_hashes),
                result_hash=normalized_result,
                workspace_id=replay.workspace_id,
                workspace_ref=replay.workspace_ref,
                workspace_inventory_hash=(
                    normalized_inventory
                    if replay_stage is SandboxStage.IMPLEMENT
                    else replay.workspace_inventory_hash
                ),
            )
            self.get_verified(attempt.id)
            return replay
        current = self._current_for_transition(
            attempt,
            expected_sequence=expected_sequence,
            expected_record_hash=expected_record_hash,
        )
        allowed_current = (
            current.status == ExecutionStageStatus.RUNNING.value
            or (
                current.status == ExecutionStageStatus.PENDING.value
                and target
                in {
                    ExecutionStageStatus.FAILED,
                    ExecutionStageStatus.CANCELLED,
                    ExecutionStageStatus.TIMED_OUT,
                }
            )
        )
        if not allowed_current:
            raise ExecutionStageTransitionError(
                "Only the current pending/running stage can finish explicitly"
            )
        stage = SandboxStage(current.stage)
        final_inventory = current.workspace_inventory_hash
        if stage is SandboxStage.IMPLEMENT:
            final_inventory = normalized_inventory
            if (
                target is ExecutionStageStatus.SUCCEEDED
                and final_inventory is None
            ):
                raise ExecutionStageTransitionError(
                    "Succeeded Implement requires the final workspace inventory"
                )
        elif normalized_inventory is not None:
            raise ExecutionStageTransitionError(
                "Only Implement can record a new workspace inventory"
            )
        return self._append(
            attempt=attempt,
            current=current,
            stage=stage,
            status=target,
            reason_code=reason,
            idempotency_key=key,
            job_spec_hash=current.job_spec_hash,
            input_hashes=tuple(current.input_hashes),
            result_hash=normalized_result,
            workspace_id=current.workspace_id,
            workspace_ref=current.workspace_ref,
            workspace_inventory_hash=final_inventory,
            now=now,
            commit=commit,
        )

    def advance_stage(
        self,
        attempt_id: str,
        *,
        expected_sequence: int,
        expected_record_hash: str,
        input_hashes: Sequence[str],
        reason_code: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ExecutionStageVersion:
        inputs = _hashes(input_hashes, name="next-stage input hashes")
        key = _idempotency_key(idempotency_key)
        reason = _reason_code(reason_code)
        attempt = self.get_verified(attempt_id)
        replay = self._stage_replay(key)
        if replay is not None:
            self._assert_stage_replay(
                replay,
                attempt_id=attempt.id,
                expected_sequence=expected_sequence,
                expected_record_hash=expected_record_hash,
                stage=SandboxStage(replay.stage),
                status=ExecutionStageStatus.PENDING,
                reason_code=reason,
                job_spec_hash=None,
                input_hashes=inputs,
                result_hash=None,
                workspace_id=replay.workspace_id,
                workspace_ref=replay.workspace_ref,
                workspace_inventory_hash=replay.workspace_inventory_hash,
            )
            self.get_verified(attempt.id)
            return replay
        current = self._current_for_transition(
            attempt,
            expected_sequence=expected_sequence,
            expected_record_hash=expected_record_hash,
        )
        stage = SandboxStage(current.stage)
        if (
            current.status != ExecutionStageStatus.SUCCEEDED.value
            or stage not in _NEXT_STAGE
            or current.result_hash is None
        ):
            raise ExecutionStageTransitionError(
                "Only a succeeded Explore or Implement stage can advance"
            )
        next_stage = _NEXT_STAGE[stage]
        if next_stage is SandboxStage.IMPLEMENT:
            if len(inputs) != 2 or inputs[0] != current.result_hash:
                raise ExecutionStageTransitionError(
                    "Implement inputs must bind Explore result then ChangeSet"
                )
            workspace_id = None
            workspace_ref = None
            inventory_hash = None
        else:
            if (
                len(inputs) not in {1, 2}
                or inputs[0] != current.result_hash
            ):
                raise ExecutionStageTransitionError(
                    "Verify inputs must bind Implement and at most one "
                    "dependency result"
                )
            if (
                current.workspace_id is None
                or current.workspace_ref is None
                or current.workspace_inventory_hash is None
            ):
                raise ExecutionStageTransitionError(
                    "Verify requires the completed Implement workspace"
                )
            workspace_id = current.workspace_id
            workspace_ref = current.workspace_ref
            inventory_hash = current.workspace_inventory_hash
        return self._append(
            attempt=attempt,
            current=current,
            stage=next_stage,
            status=ExecutionStageStatus.PENDING,
            reason_code=reason,
            idempotency_key=key,
            job_spec_hash=None,
            input_hashes=inputs,
            result_hash=None,
            workspace_id=workspace_id,
            workspace_ref=workspace_ref,
            workspace_inventory_hash=inventory_hash,
            now=now,
            commit=True,
        )

    def retry_stage(
        self,
        attempt_id: str,
        *,
        expected_sequence: int,
        expected_record_hash: str,
        reason_code: str,
        idempotency_key: str,
        now: datetime | None = None,
        commit: bool = True,
    ) -> ExecutionStageVersion:
        key = _idempotency_key(idempotency_key)
        reason = _reason_code(reason_code)
        attempt = self.get_verified(attempt_id)
        replay = self._stage_replay(key)
        if replay is not None:
            self._assert_stage_replay(
                replay,
                attempt_id=attempt.id,
                expected_sequence=expected_sequence,
                expected_record_hash=expected_record_hash,
                stage=SandboxStage(replay.stage),
                status=ExecutionStageStatus.PENDING,
                reason_code=reason,
                job_spec_hash=None,
                input_hashes=tuple(replay.input_hashes),
                result_hash=None,
                workspace_id=replay.workspace_id,
                workspace_ref=replay.workspace_ref,
                workspace_inventory_hash=replay.workspace_inventory_hash,
            )
            self.get_verified(attempt.id)
            return replay
        current = self._current_for_transition(
            attempt,
            expected_sequence=expected_sequence,
            expected_record_hash=expected_record_hash,
        )
        status = ExecutionStageStatus(current.status)
        if status not in {
            ExecutionStageStatus.FAILED,
            ExecutionStageStatus.TIMED_OUT,
        }:
            raise ExecutionStageTransitionError(
                "Only failed or timed-out stages can be retried"
            )
        stage = SandboxStage(current.stage)
        if stage is SandboxStage.IMPLEMENT:
            workspace_id = None
            workspace_ref = None
            inventory_hash = None
        else:
            workspace_id = current.workspace_id
            workspace_ref = current.workspace_ref
            inventory_hash = current.workspace_inventory_hash
        return self._append(
            attempt=attempt,
            current=current,
            stage=stage,
            status=ExecutionStageStatus.PENDING,
            reason_code=reason,
            idempotency_key=key,
            job_spec_hash=None,
            input_hashes=tuple(current.input_hashes),
            result_hash=None,
            workspace_id=workspace_id,
            workspace_ref=workspace_ref,
            workspace_inventory_hash=inventory_hash,
            now=now,
            commit=commit,
        )

    def _append(
        self,
        *,
        attempt: ExecutionAttempt,
        current: ExecutionStageVersion,
        stage: SandboxStage,
        status: ExecutionStageStatus,
        reason_code: str,
        idempotency_key: str,
        job_spec_hash: str | None,
        input_hashes: tuple[str, ...],
        result_hash: str | None,
        workspace_id: str | None,
        workspace_ref: str | None,
        workspace_inventory_hash: str | None,
        now: datetime | None,
        commit: bool,
    ) -> ExecutionStageVersion:
        version = _build_stage_version(
            attempt=attempt,
            sequence=current.sequence + 1,
            stage=stage,
            status=status,
            reason_code=reason_code,
            idempotency_key=idempotency_key,
            job_spec_hash=job_spec_hash,
            input_hashes=input_hashes,
            result_hash=result_hash,
            workspace_id=workspace_id,
            workspace_ref=workspace_ref,
            workspace_inventory_hash=workspace_inventory_hash,
            previous_stage_state_hash=current.record_hash,
            now=now,
        )
        self.session.add(version)
        try:
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self._stage_replay(idempotency_key)
            if replay is not None:
                self._assert_stage_replay(
                    replay,
                    attempt_id=attempt.id,
                    expected_sequence=current.sequence,
                    expected_record_hash=current.record_hash,
                    stage=stage,
                    status=status,
                    reason_code=reason_code,
                    job_spec_hash=job_spec_hash,
                    input_hashes=input_hashes,
                    result_hash=result_hash,
                    workspace_id=workspace_id,
                    workspace_ref=workspace_ref,
                    workspace_inventory_hash=workspace_inventory_hash,
                )
                self.get_verified(attempt.id)
                return replay
            raise ExecutionAttemptConflictError(
                "Execution stage changed concurrently or failed provenance checks"
            ) from exc
        return version

    def _current_for_transition(
        self,
        attempt: ExecutionAttempt,
        *,
        expected_sequence: int,
        expected_record_hash: str,
    ) -> ExecutionStageVersion:
        current = self._verified_history(attempt)[-1]
        if (
            current.sequence != expected_sequence
            or current.record_hash != expected_record_hash
        ):
            raise ExecutionAttemptConflictError(
                "Execution stage compare-and-swap input is stale"
            )
        return current

    def _verified_history(
        self,
        attempt: ExecutionAttempt,
    ) -> tuple[ExecutionStageVersion, ...]:
        versions = tuple(
            self.session.scalars(
                select(ExecutionStageVersion)
                .where(
                    ExecutionStageVersion.execution_attempt_id == attempt.id
                )
                .order_by(ExecutionStageVersion.sequence)
            )
        )
        if not versions:
            raise ExecutionAttemptConflictError(
                "ExecutionAttempt has no initial stage"
            )
        previous: ExecutionStageVersion | None = None
        for expected_sequence, version in enumerate(versions, start=1):
            try:
                stage = SandboxStage(version.stage)
                status = ExecutionStageStatus(version.status)
                reason = _reason_code(version.reason_code)
                key = _idempotency_key(version.idempotency_key)
                inputs = _hashes(
                    version.input_hashes,
                    name="stage input hashes",
                )
                job_hash = (
                    _hash(version.job_spec_hash, name="JobSpec hash")
                    if version.job_spec_hash is not None
                    else None
                )
                result_hash = (
                    _hash(version.result_hash, name="stage result hash")
                    if version.result_hash is not None
                    else None
                )
                workspace_id = (
                    _opaque_ref(version.workspace_id, name="workspace ID")
                    if version.workspace_id is not None
                    else None
                )
                workspace_ref = (
                    _opaque_ref(
                        version.workspace_ref,
                        name="workspace reference",
                    )
                    if version.workspace_ref is not None
                    else None
                )
                inventory_hash = (
                    _hash(
                        version.workspace_inventory_hash,
                        name="workspace inventory hash",
                    )
                    if version.workspace_inventory_hash is not None
                    else None
                )
            except ValueError as exc:
                raise ExecutionAttemptConflictError(str(exc)) from exc
            _validate_stage_shape(
                stage=stage,
                status=status,
                job_spec_hash=job_hash,
                input_hashes=inputs,
                result_hash=result_hash,
                workspace_id=workspace_id,
                workspace_ref=workspace_ref,
                workspace_inventory_hash=inventory_hash,
            )
            previous_hash = None if previous is None else previous.record_hash
            expected_hash = content_hash(
                execution_stage_payload(
                    execution_attempt_id=attempt.id,
                    attempt_record_hash=attempt.record_hash,
                    sequence=expected_sequence,
                    stage=stage,
                    status=status,
                    reason_code=reason,
                    job_spec_hash=job_hash,
                    input_hashes=inputs,
                    result_hash=result_hash,
                    workspace_id=workspace_id,
                    workspace_ref=workspace_ref,
                    workspace_inventory_hash=inventory_hash,
                    previous_stage_state_hash=previous_hash,
                )
            )
            if (
                version.schema_version != EXECUTION_STAGE_SCHEMA_VERSION
                or version.sequence != expected_sequence
                or version.execution_attempt_id != attempt.id
                or version.attempt_record_hash != attempt.record_hash
                or version.previous_stage_state_hash != previous_hash
                or version.record_hash != expected_hash
                or key != version.idempotency_key
            ):
                raise ExecutionAttemptConflictError(
                    "Execution stage hash chain does not match"
                )
            _validate_stage_edge(previous, version)
            previous = version
        return versions

    def _verify_start_audit(self, attempt: ExecutionAttempt) -> None:
        verification = AuditService(self.session).verify()
        expected = execution_started_audit_payload(attempt)
        matches = [
            event
            for event in self.session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "execution.started"
                )
            )
            if event.payload.get("execution_attempt_id") == attempt.id
        ]
        if (
            not verification.valid
            or len(matches) != 1
            or matches[0].actor_type != attempt.actor_type
            or matches[0].actor_id != attempt.actor_id
            or matches[0].correlation_id != attempt.idempotency_key
            or matches[0].payload != expected
            or matches[0].payload_hash != content_hash(expected)
        ):
            raise ExecutionAttemptConflictError(
                "Execution start audit evidence does not match"
            )

    def _stage_replay(
        self,
        idempotency_key: str,
    ) -> ExecutionStageVersion | None:
        return self.session.scalar(
            select(ExecutionStageVersion).where(
                ExecutionStageVersion.idempotency_key == idempotency_key
            )
        )

    @staticmethod
    def _assert_start_replay(
        replay: ExecutionAttempt,
        *,
        approval_id: str,
        actor_type: str,
        actor_id: str,
        repository_archive_hash: str,
        runner_image_digest: str,
        sandbox_policy: SandboxPolicy,
        observed_fingerprint_hash: str,
    ) -> None:
        if (
            replay.plan_approval_id != approval_id
            or replay.actor_type != actor_type
            or replay.actor_id != actor_id
            or replay.repository_archive_hash != repository_archive_hash
            or replay.runner_image_digest != runner_image_digest
            or replay.sandbox_policy_version != sandbox_policy.version
            or replay.sandbox_policy_hash != sandbox_policy.policy_hash
            or replay.observed_fingerprint_hash
            != observed_fingerprint_hash
        ):
            raise ExecutionAttemptConflictError(
                "Execution idempotency key belongs to different inputs"
            )

    @staticmethod
    def _assert_stage_replay(
        replay: ExecutionStageVersion,
        *,
        attempt_id: str,
        expected_sequence: int,
        expected_record_hash: str,
        stage: SandboxStage,
        status: ExecutionStageStatus,
        reason_code: str,
        job_spec_hash: str | None,
        input_hashes: tuple[str, ...],
        result_hash: str | None,
        workspace_id: str | None,
        workspace_ref: str | None,
        workspace_inventory_hash: str | None,
    ) -> None:
        if (
            replay.execution_attempt_id != attempt_id
            or replay.sequence != expected_sequence + 1
            or replay.previous_stage_state_hash != expected_record_hash
            or replay.stage != stage.value
            or replay.status != status.value
            or replay.reason_code != reason_code
            or replay.job_spec_hash != job_spec_hash
            or tuple(replay.input_hashes) != input_hashes
            or replay.result_hash != result_hash
            or replay.workspace_id != workspace_id
            or replay.workspace_ref != workspace_ref
            or replay.workspace_inventory_hash != workspace_inventory_hash
        ):
            raise ExecutionAttemptConflictError(
                "Execution stage idempotency key belongs to different inputs"
            )


def execution_attempt_payload(
    *,
    task_id: str,
    plan_version_id: str,
    plan_approval_id: str,
    approved_state_version_id: str,
    executing_state_version_id: str,
    attempt_number: int,
    actor_type: str,
    actor_id: str,
    repository_full_name: str,
    base_commit_sha: str,
    repository_archive_hash: str,
    runner_image_digest: str,
    sandbox_policy_version: str,
    sandbox_policy_hash: str,
    task_record_hash: str,
    analysis_version_id: str,
    analysis_record_hash: str,
    analysis_output_hash: str,
    snapshot_id: str,
    snapshot_inputs_hash: str,
    provider_contract_hash: str,
    plan_content_hash: str,
    plan_record_hash: str,
    approval_hash: str,
    approved_state_record_hash: str,
    executing_state_record_hash: str,
    observed_fingerprint_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": EXECUTION_ATTEMPT_SCHEMA_VERSION,
        "task_id": task_id,
        "plan_version_id": plan_version_id,
        "plan_approval_id": plan_approval_id,
        "approved_state_version_id": approved_state_version_id,
        "executing_state_version_id": executing_state_version_id,
        "attempt_number": attempt_number,
        "authorization": {
            "action": UserAction.START_EXECUTION.value,
            "actor_type": actor_type,
            "actor_id": actor_id,
        },
        "repository_full_name": repository_full_name,
        "base_commit_sha": base_commit_sha,
        "repository_archive_hash": repository_archive_hash,
        "runner_image_digest": runner_image_digest,
        "sandbox_policy_version": sandbox_policy_version,
        "sandbox_policy_hash": sandbox_policy_hash,
        "task_record_hash": task_record_hash,
        "analysis_version_id": analysis_version_id,
        "analysis_record_hash": analysis_record_hash,
        "analysis_output_hash": analysis_output_hash,
        "snapshot_id": snapshot_id,
        "snapshot_inputs_hash": snapshot_inputs_hash,
        "provider_contract_hash": provider_contract_hash,
        "plan_content_hash": plan_content_hash,
        "plan_record_hash": plan_record_hash,
        "approval_hash": approval_hash,
        "approved_state_record_hash": approved_state_record_hash,
        "executing_state_record_hash": executing_state_record_hash,
        "observed_fingerprint_hash": observed_fingerprint_hash,
    }


def execution_stage_payload(
    *,
    execution_attempt_id: str,
    attempt_record_hash: str,
    sequence: int,
    stage: SandboxStage,
    status: ExecutionStageStatus,
    reason_code: str,
    job_spec_hash: str | None,
    input_hashes: tuple[str, ...],
    result_hash: str | None,
    workspace_id: str | None,
    workspace_ref: str | None,
    workspace_inventory_hash: str | None,
    previous_stage_state_hash: str | None,
) -> dict[str, object]:
    return {
        "schema_version": EXECUTION_STAGE_SCHEMA_VERSION,
        "execution_attempt_id": execution_attempt_id,
        "attempt_record_hash": attempt_record_hash,
        "sequence": sequence,
        "stage": stage.value,
        "status": status.value,
        "reason_code": reason_code,
        "job_spec_hash": job_spec_hash,
        "input_hashes": list(input_hashes),
        "result_hash": result_hash,
        "workspace": {
            "id": workspace_id,
            "reference": workspace_ref,
            "inventory_hash": workspace_inventory_hash,
        },
        "previous_stage_state_hash": previous_stage_state_hash,
    }


def execution_started_audit_payload(
    attempt: ExecutionAttempt,
) -> dict[str, object]:
    return {
        "action": UserAction.START_EXECUTION.value,
        "execution_attempt_id": attempt.id,
        "execution_attempt_hash": attempt.record_hash,
        "attempt_number": attempt.attempt_number,
        "task_id": attempt.task_id,
        "task_record_hash": attempt.task_record_hash,
        "analysis_version_id": attempt.analysis_version_id,
        "analysis_record_hash": attempt.analysis_record_hash,
        "analysis_output_hash": attempt.analysis_output_hash,
        "snapshot_id": attempt.snapshot_id,
        "snapshot_inputs_hash": attempt.snapshot_inputs_hash,
        "repository_full_name": attempt.repository_full_name,
        "base_commit_sha": attempt.base_commit_sha,
        "repository_archive_hash": attempt.repository_archive_hash,
        "plan_version_id": attempt.plan_version_id,
        "plan_content_hash": attempt.plan_content_hash,
        "plan_record_hash": attempt.plan_record_hash,
        "plan_approval_id": attempt.plan_approval_id,
        "approval_hash": attempt.approval_hash,
        "approved_state_record_hash": (
            attempt.approved_state_record_hash
        ),
        "executing_state_record_hash": (
            attempt.executing_state_record_hash
        ),
        "provider_contract_hash": attempt.provider_contract_hash,
        "runner_image_digest": attempt.runner_image_digest,
        "sandbox_policy_version": attempt.sandbox_policy_version,
        "sandbox_policy_hash": attempt.sandbox_policy_hash,
    }


def _build_stage_version(
    *,
    attempt: ExecutionAttempt,
    sequence: int,
    stage: SandboxStage,
    status: ExecutionStageStatus,
    reason_code: str,
    idempotency_key: str,
    job_spec_hash: str | None,
    input_hashes: tuple[str, ...],
    result_hash: str | None,
    workspace_id: str | None,
    workspace_ref: str | None,
    workspace_inventory_hash: str | None,
    previous_stage_state_hash: str | None,
    now: datetime | None,
) -> ExecutionStageVersion:
    reason = _reason_code(reason_code)
    key = _idempotency_key(idempotency_key)
    inputs = _hashes(input_hashes, name="stage input hashes")
    payload = execution_stage_payload(
        execution_attempt_id=attempt.id,
        attempt_record_hash=attempt.record_hash,
        sequence=sequence,
        stage=stage,
        status=status,
        reason_code=reason,
        job_spec_hash=job_spec_hash,
        input_hashes=inputs,
        result_hash=result_hash,
        workspace_id=workspace_id,
        workspace_ref=workspace_ref,
        workspace_inventory_hash=workspace_inventory_hash,
        previous_stage_state_hash=previous_stage_state_hash,
    )
    ensure_no_sensitive_data(
        {"idempotency_key": key, "execution_stage": payload},
        context="Execution stage",
    )
    return ExecutionStageVersion(
        id=str(uuid4()),
        execution_attempt_id=attempt.id,
        schema_version=EXECUTION_STAGE_SCHEMA_VERSION,
        sequence=sequence,
        stage=stage.value,
        status=status.value,
        reason_code=reason,
        idempotency_key=key,
        job_spec_hash=job_spec_hash,
        input_hashes=list(inputs),
        result_hash=result_hash,
        workspace_id=workspace_id,
        workspace_ref=workspace_ref,
        workspace_inventory_hash=workspace_inventory_hash,
        attempt_record_hash=attempt.record_hash,
        previous_stage_state_hash=previous_stage_state_hash,
        record_hash=content_hash(payload),
        created_at=_aware(now),
    )


def _validate_stage_shape(
    *,
    stage: SandboxStage,
    status: ExecutionStageStatus,
    job_spec_hash: str | None,
    input_hashes: tuple[str, ...],
    result_hash: str | None,
    workspace_id: str | None,
    workspace_ref: str | None,
    workspace_inventory_hash: str | None,
) -> None:
    if status is ExecutionStageStatus.PENDING and (
        job_spec_hash is not None or result_hash is not None
    ):
        raise ExecutionAttemptConflictError(
            "Pending execution stage contains run evidence"
        )
    if status is ExecutionStageStatus.RUNNING and (
        job_spec_hash is None or result_hash is not None
    ):
        raise ExecutionAttemptConflictError(
            "Running execution stage evidence is invalid"
        )
    if status is ExecutionStageStatus.SUCCEEDED and (
        job_spec_hash is None or result_hash is None
    ):
        raise ExecutionAttemptConflictError(
            "Succeeded execution stage evidence is invalid"
        )
    workspace_present = workspace_id is not None or workspace_ref is not None
    if workspace_present and (
        workspace_id is None or workspace_ref is None
    ):
        raise ExecutionAttemptConflictError(
            "Execution workspace identity is incomplete"
        )
    if stage is SandboxStage.EXPLORE:
        if input_hashes or workspace_present or workspace_inventory_hash is not None:
            raise ExecutionAttemptConflictError(
                "Explore stage contains forbidden inputs or workspace"
            )
    elif stage is SandboxStage.IMPLEMENT:
        if len(input_hashes) != 2:
            raise ExecutionAttemptConflictError(
                "Implement stage input count is invalid"
            )
        if status is ExecutionStageStatus.PENDING and workspace_present:
            raise ExecutionAttemptConflictError(
                "Pending Implement already owns a workspace"
            )
        if (
            status is not ExecutionStageStatus.PENDING
            and job_spec_hash is not None
            and not workspace_present
        ):
            raise ExecutionAttemptConflictError(
                "Started Implement has no durable workspace"
            )
        if (
            status is ExecutionStageStatus.SUCCEEDED
            and workspace_inventory_hash is None
        ):
            raise ExecutionAttemptConflictError(
                "Succeeded Implement has no workspace inventory"
            )
    else:
        if len(input_hashes) not in {1, 2}:
            raise ExecutionAttemptConflictError(
                "Verify stage input count is invalid"
            )
        if not workspace_present or workspace_inventory_hash is None:
            raise ExecutionAttemptConflictError(
                "Verify stage has no completed Implement workspace"
            )


def _validate_stage_edge(
    previous: ExecutionStageVersion | None,
    current: ExecutionStageVersion,
) -> None:
    if previous is None:
        if (
            current.sequence != 1
            or current.stage != SandboxStage.EXPLORE.value
            or current.status != ExecutionStageStatus.PENDING.value
            or current.reason_code != "execution_started"
        ):
            raise ExecutionAttemptConflictError(
                "Execution initial stage is invalid"
            )
        return
    previous_stage = SandboxStage(previous.stage)
    current_stage = SandboxStage(current.stage)
    previous_status = ExecutionStageStatus(previous.status)
    current_status = ExecutionStageStatus(current.status)
    if (
        previous_stage is current_stage
        and previous_status is ExecutionStageStatus.PENDING
        and current_status is ExecutionStageStatus.RUNNING
        and previous.input_hashes == current.input_hashes
    ):
        if (
            current_stage is SandboxStage.VERIFY
            and (
                previous.workspace_id != current.workspace_id
                or previous.workspace_ref != current.workspace_ref
                or previous.workspace_inventory_hash
                != current.workspace_inventory_hash
            )
        ):
            raise ExecutionAttemptConflictError(
                "Verify workspace changed when the stage started"
            )
        return
    if (
        previous_stage is current_stage
        and previous_status is ExecutionStageStatus.PENDING
        and current_status
        in {
            ExecutionStageStatus.FAILED,
            ExecutionStageStatus.CANCELLED,
            ExecutionStageStatus.TIMED_OUT,
        }
        and current.job_spec_hash is None
        and previous.input_hashes == current.input_hashes
        and previous.workspace_id == current.workspace_id
        and previous.workspace_ref == current.workspace_ref
        and previous.workspace_inventory_hash
        == current.workspace_inventory_hash
    ):
        return
    if (
        previous_stage is current_stage
        and previous_status is ExecutionStageStatus.RUNNING
        and current_status in _TERMINAL_STAGE_STATUSES
        and previous.job_spec_hash == current.job_spec_hash
        and previous.input_hashes == current.input_hashes
        and previous.workspace_id == current.workspace_id
        and previous.workspace_ref == current.workspace_ref
    ):
        if (
            previous_stage is not SandboxStage.IMPLEMENT
            and previous.workspace_inventory_hash
            != current.workspace_inventory_hash
        ):
            raise ExecutionAttemptConflictError(
                "Execution workspace inventory changed outside Implement"
            )
        return
    if (
        previous_stage is SandboxStage.EXPLORE
        and previous_status is ExecutionStageStatus.SUCCEEDED
        and current_stage is SandboxStage.IMPLEMENT
        and current_status is ExecutionStageStatus.PENDING
        and len(current.input_hashes) == 2
        and current.input_hashes[0] == previous.result_hash
    ):
        return
    if (
        previous_stage is SandboxStage.IMPLEMENT
        and previous_status is ExecutionStageStatus.SUCCEEDED
        and current_stage is SandboxStage.VERIFY
        and current_status is ExecutionStageStatus.PENDING
        and len(current.input_hashes) in {1, 2}
        and current.input_hashes[0] == previous.result_hash
        and current.workspace_id == previous.workspace_id
        and current.workspace_ref == previous.workspace_ref
        and current.workspace_inventory_hash
        == previous.workspace_inventory_hash
    ):
        return
    if (
        previous_stage is current_stage
        and previous_status
        in {
            ExecutionStageStatus.FAILED,
            ExecutionStageStatus.TIMED_OUT,
        }
        and current_status is ExecutionStageStatus.PENDING
        and previous.input_hashes == current.input_hashes
        and (
            current_stage is SandboxStage.IMPLEMENT
            and current.workspace_id is None
            and current.workspace_ref is None
            and current.workspace_inventory_hash is None
            or current_stage is not SandboxStage.IMPLEMENT
            and previous.workspace_id == current.workspace_id
            and previous.workspace_ref == current.workspace_ref
            and previous.workspace_inventory_hash
            == current.workspace_inventory_hash
        )
    ):
        return
    raise ExecutionAttemptConflictError(
        "Execution stage transition is illegal"
    )


def _valid_execution_start_state(
    *,
    attempt: ExecutionAttempt,
    approved: ContributionTaskStateVersion,
    executing: ContributionTaskStateVersion,
    executing_hash: str,
) -> bool:
    if (
        executing.task_id != attempt.task_id
        or executing.to_state != ContributionTaskState.EXECUTING.value
        or executing.record_hash != executing_hash
        or executing.record_hash != attempt.executing_state_record_hash
    ):
        return False
    if (
        attempt.attempt_number == 1
        and executing.sequence == approved.sequence + 1
        and executing.from_state == ContributionTaskState.PLAN_APPROVED.value
        and executing.reason_code == "execution_started"
        and executing.previous_state_hash == approved.record_hash
    ):
        return True
    return (
        2 <= attempt.attempt_number <= MAX_EXECUTION_ATTEMPTS
        and executing.from_state == ContributionTaskState.REVIEWING.value
        and executing.reason_code == "repair_started"
    )


def _verified_task_state_hash(
    state: ContributionTaskStateVersion,
) -> str:
    try:
        source = (
            ContributionTaskState(state.from_state)
            if state.from_state is not None
            else None
        )
        target = ContributionTaskState(state.to_state)
    except ValueError as exc:
        raise ExecutionAttemptConflictError(
            "Execution task state value is invalid"
        ) from exc
    return content_hash(
        task_state_record_payload(
            task_id=state.task_id,
            task_record_hash=state.task_record_hash,
            sequence=state.sequence,
            from_state=source,
            to_state=target,
            reason_code=state.reason_code,
            previous_state_hash=state.previous_state_hash,
        )
    )


def _snapshot_repository(snapshot: OpportunitySnapshot) -> str:
    repository = snapshot.repository_data
    value = (
        repository.get("full_name")
        if isinstance(repository, dict)
        else None
    )
    if (
        not isinstance(value, str)
        or not _REPOSITORY.fullmatch(value)
        or contains_sensitive_text(value)
    ):
        raise ExecutionAttemptConflictError(
            "Execution repository snapshot is invalid"
        )
    return value


def _idempotency_key(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not _IDEMPOTENCY_KEY.fullmatch(normalized)
        or contains_sensitive_text(normalized)
    ):
        raise ValueError("Execution idempotency key is invalid")
    return normalized


def _actor_type(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not _ACTOR_TYPE.fullmatch(normalized)
        or contains_sensitive_text(normalized)
    ):
        raise ValueError("Execution actor type is invalid")
    return normalized


def _actor_id(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not _ACTOR_ID.fullmatch(normalized)
        or contains_sensitive_text(normalized)
    ):
        raise ValueError("Execution actor ID is invalid")
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
        raise ValueError(f"Execution {name} is invalid")
    return value


def _hashes(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"Execution {name} are invalid")
    normalized = tuple(_hash(value, name=name) for value in values)
    if len(normalized) > 100 or len(normalized) != len(set(normalized)):
        raise ValueError(f"Execution {name} are invalid")
    return normalized


def _image_digest(value: str) -> str:
    if not isinstance(value, str) or not _IMAGE.fullmatch(value):
        raise ValueError("Execution runner image digest is invalid")
    return value


def _opaque_ref(value: str, *, name: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not _OPAQUE_REF.fullmatch(normalized)
        or contains_sensitive_text(normalized)
    ):
        raise ValueError(f"Execution {name} is invalid")
    return normalized


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
