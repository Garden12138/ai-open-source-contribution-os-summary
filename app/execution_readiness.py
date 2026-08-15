from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.approvals import (
    ApprovalInputFingerprint,
    PlanApprovalError,
    PlanApprovalService,
)
from app.authorizations import (
    AuthorizationActionError,
    UserAction,
    require_user_action,
)
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
)


class ExecutionReadinessError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason_codes: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.reason_codes = reason_codes


@dataclass(frozen=True, slots=True)
class ExecutionReadiness:
    approval_id: str
    approval_hash: str
    plan_version_id: str
    plan_record_hash: str
    plan_content_hash: str
    task_id: str
    task_state_version_id: str
    task_state_record_hash: str
    base_commit_sha: str
    observed_fingerprint_hash: str


class ExecutionReadinessService:
    """Fail-closed precondition check; this service never starts execution."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def assert_ready(
        self,
        *,
        approval_id: str,
        observed: ApprovalInputFingerprint,
        action: UserAction | str,
    ) -> ExecutionReadiness:
        try:
            require_user_action(
                action,
                expected=UserAction.START_EXECUTION,
            )
        except AuthorizationActionError as exc:
            raise ExecutionReadinessError(
                str(exc),
                reason_codes=("wrong_user_action",),
            ) from exc
        approvals = PlanApprovalService(self.session)
        try:
            approval = approvals.get_verified(approval_id)
        except PlanApprovalError as exc:
            raise ExecutionReadinessError(
                "Execution requires a verified plan approval",
                reason_codes=("approval_missing_or_invalid",),
            ) from exc
        try:
            current = ContributionTaskStateService(
                self.session
            ).current(approval.task_id)
        except TaskStateError as exc:
            raise ExecutionReadinessError(
                "Execution requires a current approved task state",
                reason_codes=("task_state_missing_or_invalid",),
            ) from exc
        if (
            current.id != approval.approved_state_version_id
            or current.record_hash != approval.approved_state_record_hash
            or current.to_state != ContributionTaskState.PLAN_APPROVED.value
        ):
            raise ExecutionReadinessError(
                "Execution requires the current exact approved plan state",
                reason_codes=("plan_not_currently_approved",),
            )
        freshness = approvals.check_freshness(
            approval.id,
            observed=observed,
        )
        if not freshness.valid:
            approvals.revoke_if_stale(
                approval.id,
                observed=observed,
                expected_sequence=current.sequence,
                expected_state_record_hash=current.record_hash,
            )
            raise ExecutionReadinessError(
                "Execution inputs do not match the approved plan",
                reason_codes=freshness.reason_codes,
            )
        lock = approval.plan_lock
        return ExecutionReadiness(
            approval_id=approval.id,
            approval_hash=approval.approval_hash,
            plan_version_id=approval.plan_version_id,
            plan_record_hash=approval.plan_record_hash,
            plan_content_hash=approval.plan_content_hash,
            task_id=approval.task_id,
            task_state_version_id=current.id,
            task_state_record_hash=current.record_hash,
            base_commit_sha=lock.base_commit_sha,
            observed_fingerprint_hash=freshness.observed_fingerprint_hash,
        )
