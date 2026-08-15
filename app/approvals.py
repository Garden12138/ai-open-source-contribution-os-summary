from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import AuditService
from app.authorizations import (
    AuthorizationActionError,
    UserAction,
    require_user_action,
)
from app.models import (
    AuditEvent,
    ContributionTaskStateVersion,
    PlanApproval,
    PlanLock,
)
from app.plan_locks import (
    PlanLockError,
    PlanLockService,
)
from app.provenance import content_hash
from app.security import contains_sensitive_text, ensure_no_sensitive_data
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
    task_state_record_payload,
)


PLAN_APPROVAL_SCHEMA_VERSION = "1"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ACTOR_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_BASE_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class PlanApprovalError(RuntimeError):
    pass


class PlanApprovalNotFoundError(PlanApprovalError):
    pass


class PlanApprovalConflictError(PlanApprovalError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalInputFingerprint:
    analysis_version_id: str
    snapshot_id: str
    base_commit_sha: str
    provider_contract_hash: str
    plan_version_id: str
    plan_content_hash: str
    plan_record_hash: str

    def __post_init__(self) -> None:
        for name in (
            "analysis_version_id",
            "snapshot_id",
            "plan_version_id",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 128
                or contains_sensitive_text(value)
            ):
                raise ValueError(
                    f"Approval input {name.replace('_', ' ')} is invalid"
                )
        if not _BASE_COMMIT_SHA.fullmatch(self.base_commit_sha):
            raise ValueError("Approval input base commit SHA is invalid")
        for name in (
            "provider_contract_hash",
            "plan_content_hash",
            "plan_record_hash",
        ):
            if not _HASH.fullmatch(getattr(self, name)):
                raise ValueError(
                    f"Approval input {name.replace('_', ' ')} is invalid"
                )
        ensure_no_sensitive_data(
            self.hash_payload(),
            context="Approval input fingerprint",
        )

    @classmethod
    def from_lock(cls, lock: PlanLock) -> "ApprovalInputFingerprint":
        return cls(
            analysis_version_id=lock.analysis_version_id,
            snapshot_id=lock.snapshot_id,
            base_commit_sha=lock.base_commit_sha,
            provider_contract_hash=lock.provider_contract_hash,
            plan_version_id=lock.plan_version_id,
            plan_content_hash=lock.plan_content_hash,
            plan_record_hash=lock.plan_record_hash,
        )

    def hash_payload(self) -> dict[str, str]:
        return {
            "analysis_version_id": self.analysis_version_id,
            "snapshot_id": self.snapshot_id,
            "base_commit_sha": self.base_commit_sha,
            "provider_contract_hash": self.provider_contract_hash,
            "plan_version_id": self.plan_version_id,
            "plan_content_hash": self.plan_content_hash,
            "plan_record_hash": self.plan_record_hash,
        }


@dataclass(frozen=True, slots=True)
class ApprovalFreshness:
    valid: bool
    reason_codes: tuple[str, ...]
    expected_fingerprint_hash: str
    observed_fingerprint_hash: str


@dataclass(frozen=True, slots=True)
class ApprovalRevocation:
    freshness: ApprovalFreshness
    state_version: ContributionTaskStateVersion | None


class PlanApprovalService:
    """Approve one immutable PlanLock and append the approved task state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def approve(
        self,
        *,
        plan_lock_id: str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        action: UserAction | str,
        now: datetime | None = None,
    ) -> PlanApproval:
        try:
            require_user_action(
                action,
                expected=UserAction.APPROVE_PLAN,
            )
        except AuthorizationActionError as exc:
            raise PlanApprovalConflictError(str(exc)) from exc
        key = _idempotency_key(idempotency_key)
        normalized_actor_type = _actor_type(actor_type)
        normalized_actor_id = _actor_id(actor_id)
        try:
            lock = PlanLockService(self.session).get_verified(plan_lock_id)
        except PlanLockError as exc:
            raise PlanApprovalNotFoundError(str(exc)) from exc

        replay = self.session.scalar(
            select(PlanApproval).where(
                PlanApproval.idempotency_key == key
            )
        )
        if replay is not None:
            self._assert_same_actor_and_lock(
                replay,
                plan_lock_id=lock.id,
                actor_type=normalized_actor_type,
                actor_id=normalized_actor_id,
            )
            return self.get_verified(replay.id)
        existing = self.session.scalar(
            select(PlanApproval).where(
                PlanApproval.plan_lock_id == lock.id
            )
        )
        if existing is not None:
            self._assert_same_actor_and_lock(
                existing,
                plan_lock_id=lock.id,
                actor_type=normalized_actor_type,
                actor_id=normalized_actor_id,
            )
            return self.get_verified(existing.id)

        states = ContributionTaskStateService(self.session)
        try:
            current = states.current(lock.task_id)
        except TaskStateError as exc:
            raise PlanApprovalConflictError(str(exc)) from exc
        if (
            current.id != lock.task_state_version_id
            or current.record_hash != lock.task_state_record_hash
            or current.to_state != ContributionTaskState.PLANNING.value
        ):
            raise PlanApprovalConflictError(
                "PlanLock is stale against the current task state"
            )
        try:
            approved_state = states.prepare_transition(
                lock.task_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                to_state=ContributionTaskState.PLAN_APPROVED,
                reason_code="plan_approved",
                now=now,
            )
        except TaskStateError as exc:
            raise PlanApprovalConflictError(str(exc)) from exc

        payload = plan_approval_payload(
            plan_lock_id=lock.id,
            plan_version_id=lock.plan_version_id,
            task_id=lock.task_id,
            approved_state_version_id=approved_state.id,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            lock_hash=lock.lock_hash,
            plan_content_hash=lock.plan_content_hash,
            plan_record_hash=lock.plan_record_hash,
            prior_state_record_hash=current.record_hash,
            approved_state_record_hash=approved_state.record_hash,
        )
        ensure_no_sensitive_data(
            {
                "idempotency_key": key,
                "approval": payload,
            },
            context="PlanApproval",
        )
        approval = PlanApproval(
            id=str(uuid4()),
            plan_lock_id=lock.id,
            plan_version_id=lock.plan_version_id,
            task_id=lock.task_id,
            approved_state_version_id=approved_state.id,
            schema_version=PLAN_APPROVAL_SCHEMA_VERSION,
            idempotency_key=key,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            lock_hash=lock.lock_hash,
            plan_content_hash=lock.plan_content_hash,
            plan_record_hash=lock.plan_record_hash,
            prior_state_record_hash=current.record_hash,
            approved_state_record_hash=approved_state.record_hash,
            approval_hash=content_hash(payload),
            created_at=_aware(now),
        )
        self.session.add_all((approved_state, approval))
        try:
            AuditService(self.session).prepare(
                event_type="plan.approved",
                actor_type=normalized_actor_type,
                actor_id=normalized_actor_id,
                correlation_id=key,
                payload=plan_approval_audit_payload(
                    approval=approval,
                    lock=lock,
                ),
                now=now,
            )
        except Exception as exc:
            self.session.rollback()
            raise PlanApprovalConflictError(
                "Plan approval audit evidence could not be prepared"
            ) from exc
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(PlanApproval).where(
                    PlanApproval.idempotency_key == key
                )
            )
            if replay is not None:
                self._assert_same_actor_and_lock(
                    replay,
                    plan_lock_id=lock.id,
                    actor_type=normalized_actor_type,
                    actor_id=normalized_actor_id,
                )
                return self.get_verified(replay.id)
            raise PlanApprovalConflictError(
                "Plan approval changed concurrently or failed provenance checks"
            ) from exc
        return approval

    def get_verified(self, approval_id: str) -> PlanApproval:
        approval = self.session.get(PlanApproval, approval_id)
        if approval is None:
            raise PlanApprovalNotFoundError("PlanApproval was not found")
        try:
            lock = PlanLockService(
                self.session
            ).get_verified(approval.plan_lock_id)
        except PlanLockError as exc:
            raise PlanApprovalConflictError(str(exc)) from exc
        prior = self.session.get(
            ContributionTaskStateVersion,
            lock.task_state_version_id,
        )
        approved = self.session.get(
            ContributionTaskStateVersion,
            approval.approved_state_version_id,
        )
        if prior is None or approved is None:
            raise PlanApprovalConflictError(
                "PlanApproval task state provenance was not found"
            )
        try:
            prior_source = (
                ContributionTaskState(prior.from_state)
                if prior.from_state is not None
                else None
            )
            prior_target = ContributionTaskState(prior.to_state)
            approved_source = ContributionTaskState(approved.from_state)
            approved_target = ContributionTaskState(approved.to_state)
        except ValueError as exc:
            raise PlanApprovalConflictError(
                "PlanApproval task state is invalid"
            ) from exc
        expected_prior_hash = content_hash(
            task_state_record_payload(
                task_id=approval.task_id,
                task_record_hash=prior.task_record_hash,
                sequence=prior.sequence,
                from_state=prior_source,
                to_state=prior_target,
                reason_code=prior.reason_code,
                previous_state_hash=prior.previous_state_hash,
            )
        )
        expected_approved_hash = content_hash(
            task_state_record_payload(
                task_id=approval.task_id,
                task_record_hash=approved.task_record_hash,
                sequence=approved.sequence,
                from_state=approved_source,
                to_state=approved_target,
                reason_code=approved.reason_code,
                previous_state_hash=approved.previous_state_hash,
            )
        )
        payload = plan_approval_payload(
            plan_lock_id=lock.id,
            plan_version_id=lock.plan_version_id,
            task_id=lock.task_id,
            approved_state_version_id=approved.id,
            actor_type=approval.actor_type,
            actor_id=approval.actor_id,
            lock_hash=lock.lock_hash,
            plan_content_hash=lock.plan_content_hash,
            plan_record_hash=lock.plan_record_hash,
            prior_state_record_hash=prior.record_hash,
            approved_state_record_hash=approved.record_hash,
        )
        if (
            approval.schema_version != PLAN_APPROVAL_SCHEMA_VERSION
            or approval.plan_version_id != lock.plan_version_id
            or approval.task_id != lock.task_id
            or prior.task_id != lock.task_id
            or prior.id != lock.task_state_version_id
            or prior.record_hash != expected_prior_hash
            or prior.record_hash != lock.task_state_record_hash
            or prior.to_state != ContributionTaskState.PLANNING.value
            or approved.task_id != lock.task_id
            or approved.sequence != prior.sequence + 1
            or approved.from_state != ContributionTaskState.PLANNING.value
            or approved.to_state
            != ContributionTaskState.PLAN_APPROVED.value
            or approved.reason_code != "plan_approved"
            or approved.previous_state_hash != prior.record_hash
            or approved.record_hash != expected_approved_hash
            or approval.lock_hash != lock.lock_hash
            or approval.plan_content_hash != lock.plan_content_hash
            or approval.plan_record_hash != lock.plan_record_hash
            or approval.prior_state_record_hash != prior.record_hash
            or approval.approved_state_record_hash != approved.record_hash
            or approval.approval_hash != content_hash(payload)
        ):
            raise PlanApprovalConflictError(
                "PlanApproval content does not match its immutable inputs"
            )
        try:
            _actor_type(approval.actor_type)
            _actor_id(approval.actor_id)
        except ValueError as exc:
            raise PlanApprovalConflictError(str(exc)) from exc
        audit_verification = AuditService(self.session).verify()
        expected_audit_payload = plan_approval_audit_payload(
            approval=approval,
            lock=lock,
        )
        matching_audits = [
            event
            for event in self.session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "plan.approved"
                )
            )
            if event.payload.get("approval_id") == approval.id
        ]
        if (
            not audit_verification.valid
            or len(matching_audits) != 1
            or matching_audits[0].actor_type != approval.actor_type
            or matching_audits[0].actor_id != approval.actor_id
            or matching_audits[0].correlation_id
            != approval.idempotency_key
            or matching_audits[0].payload != expected_audit_payload
            or matching_audits[0].payload_hash
            != content_hash(expected_audit_payload)
        ):
            raise PlanApprovalConflictError(
                "PlanApproval audit evidence does not match"
            )
        return approval

    def check_freshness(
        self,
        approval_id: str,
        *,
        observed: ApprovalInputFingerprint,
    ) -> ApprovalFreshness:
        approval = self.get_verified(approval_id)
        lock = PlanLockService(self.session).get_verified(
            approval.plan_lock_id
        )
        expected = ApprovalInputFingerprint.from_lock(lock)
        checks = (
            (
                "analysis_version_changed",
                observed.analysis_version_id,
                expected.analysis_version_id,
            ),
            (
                "snapshot_changed",
                observed.snapshot_id,
                expected.snapshot_id,
            ),
            (
                "base_commit_changed",
                observed.base_commit_sha,
                expected.base_commit_sha,
            ),
            (
                "provider_policy_changed",
                observed.provider_contract_hash,
                expected.provider_contract_hash,
            ),
            (
                "plan_version_changed",
                observed.plan_version_id,
                expected.plan_version_id,
            ),
            (
                "plan_content_changed",
                observed.plan_content_hash,
                expected.plan_content_hash,
            ),
            (
                "plan_record_changed",
                observed.plan_record_hash,
                expected.plan_record_hash,
            ),
        )
        reasons = tuple(
            reason for reason, actual, wanted in checks if actual != wanted
        )
        return ApprovalFreshness(
            valid=not reasons,
            reason_codes=reasons,
            expected_fingerprint_hash=content_hash(expected.hash_payload()),
            observed_fingerprint_hash=content_hash(observed.hash_payload()),
        )

    def revoke_if_stale(
        self,
        approval_id: str,
        *,
        observed: ApprovalInputFingerprint,
        expected_sequence: int,
        expected_state_record_hash: str,
        now: datetime | None = None,
    ) -> ApprovalRevocation:
        approval = self.get_verified(approval_id)
        freshness = self.check_freshness(
            approval_id,
            observed=observed,
        )
        if freshness.valid:
            return ApprovalRevocation(
                freshness=freshness,
                state_version=None,
            )
        states = ContributionTaskStateService(self.session)
        try:
            current = states.current(approval.task_id)
        except TaskStateError as exc:
            raise PlanApprovalConflictError(str(exc)) from exc
        if (
            current.id != approval.approved_state_version_id
            or current.to_state != ContributionTaskState.PLAN_APPROVED.value
            or current.sequence != expected_sequence
            or current.record_hash != expected_state_record_hash
        ):
            raise PlanApprovalConflictError(
                "Plan approval revocation compare-and-swap input is stale"
            )
        try:
            revoked = states.transition(
                approval.task_id,
                expected_sequence=expected_sequence,
                expected_record_hash=expected_state_record_hash,
                to_state=ContributionTaskState.PLANNING,
                reason_code="approval_stale_inputs",
                now=now,
            )
        except TaskStateError as exc:
            raise PlanApprovalConflictError(str(exc)) from exc
        return ApprovalRevocation(
            freshness=freshness,
            state_version=revoked,
        )

    @staticmethod
    def _assert_same_actor_and_lock(
        approval: PlanApproval,
        *,
        plan_lock_id: str,
        actor_type: str,
        actor_id: str,
    ) -> None:
        if (
            approval.plan_lock_id != plan_lock_id
            or approval.actor_type != actor_type
            or approval.actor_id != actor_id
        ):
            raise PlanApprovalConflictError(
                "Plan approval already belongs to different inputs"
            )


def plan_approval_payload(
    *,
    plan_lock_id: str,
    plan_version_id: str,
    task_id: str,
    approved_state_version_id: str,
    actor_type: str,
    actor_id: str,
    lock_hash: str,
    plan_content_hash: str,
    plan_record_hash: str,
    prior_state_record_hash: str,
    approved_state_record_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": PLAN_APPROVAL_SCHEMA_VERSION,
        "plan_lock_id": plan_lock_id,
        "plan_version_id": plan_version_id,
        "task_id": task_id,
        "approved_state_version_id": approved_state_version_id,
        "actor": {
            "type": actor_type,
            "id": actor_id,
        },
        "lock_hash": lock_hash,
        "plan_content_hash": plan_content_hash,
        "plan_record_hash": plan_record_hash,
        "prior_state_record_hash": prior_state_record_hash,
        "approved_state_record_hash": approved_state_record_hash,
    }


def plan_approval_audit_payload(
    *,
    approval: PlanApproval,
    lock: PlanLock,
) -> dict[str, object]:
    return {
        "action": UserAction.APPROVE_PLAN.value,
        "approval_id": approval.id,
        "approval_hash": approval.approval_hash,
        "plan_lock_id": lock.id,
        "lock_hash": lock.lock_hash,
        "task_id": approval.task_id,
        "analysis_version_id": lock.analysis_version_id,
        "analysis_record_hash": lock.analysis_record_hash,
        "analysis_output_hash": lock.analysis_output_hash,
        "snapshot_id": lock.snapshot_id,
        "snapshot_inputs_hash": lock.snapshot_inputs_hash,
        "base_commit_sha": lock.base_commit_sha,
        "provider_contract_hash": lock.provider_contract_hash,
        "plan_version_id": approval.plan_version_id,
        "plan_content_hash": approval.plan_content_hash,
        "plan_record_hash": approval.plan_record_hash,
        "prior_state_record_hash": approval.prior_state_record_hash,
        "approved_state_version_id": approval.approved_state_version_id,
        "approved_state_record_hash": approval.approved_state_record_hash,
    }


def _idempotency_key(value: str) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not _IDEMPOTENCY_KEY.fullmatch(key) or contains_sensitive_text(key):
        raise ValueError("PlanApproval idempotency key is invalid")
    return key


def _actor_type(value: str) -> str:
    actor = value.strip() if isinstance(value, str) else ""
    if not _ACTOR_TYPE.fullmatch(actor) or contains_sensitive_text(actor):
        raise ValueError("PlanApproval actor type is invalid")
    return actor


def _actor_id(value: str) -> str:
    actor = value.strip() if isinstance(value, str) else ""
    if not _ACTOR_ID.fullmatch(actor) or contains_sensitive_text(actor):
        raise ValueError("PlanApproval actor ID is invalid")
    return actor


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
