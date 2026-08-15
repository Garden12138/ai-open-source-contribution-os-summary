from __future__ import annotations

import re
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
from app.models import ContributionTaskStateVersion, TaskLifecycleMark
from app.planning import ContributionTaskService
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
)


LIFECYCLE_MARK_SCHEMA_VERSION = "1"
_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_REASON = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_MARKS = {
    "abandon": (UserAction.ABANDON_TASK, "abandoned", "task_abandoned"),
    "fail": (UserAction.FAIL_TASK, "failed", "task_failed"),
    "reject": (UserAction.REJECT_TASK, "rejected", "task_rejected"),
}
_TRANSITIONS = {
    "reward": (
        UserAction.MARK_REWARD,
        ContributionTaskState.REWARDED,
        "reward_recorded",
    ),
    "revise": (
        UserAction.REVISE_PLAN,
        ContributionTaskState.PLANNING,
        "plan_revision_requested",
    ),
}


class LifecycleError(RuntimeError):
    pass


class LifecycleConflictError(LifecycleError):
    pass


class TaskLifecycleService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def current_mark(self, task_id: str) -> TaskLifecycleMark | None:
        return self.session.scalar(
            select(TaskLifecycleMark).where(TaskLifecycleMark.task_id == task_id)
        )

    def apply(
        self,
        *,
        task_id: str,
        action: str,
        actor_id: str,
        reason_code: str | None = None,
        now: datetime | None = None,
    ) -> ContributionTaskStateVersion | TaskLifecycleMark:
        if action not in _MARKS and action not in _TRANSITIONS:
            raise LifecycleConflictError("Lifecycle action is unknown")
        if not _ACTOR_ID.fullmatch(actor_id):
            raise LifecycleConflictError("Lifecycle actor id is invalid")
        ContributionTaskService(self.session).get_verified(task_id)
        if action in _MARKS:
            return self._mark(
                task_id=task_id,
                action=action,
                actor_id=actor_id,
                reason_code=reason_code,
                now=now,
            )
        return self._transition(
            task_id=task_id,
            action=action,
            actor_id=actor_id,
            reason_code=reason_code,
            now=now,
        )

    def _transition(
        self,
        *,
        task_id: str,
        action: str,
        actor_id: str,
        reason_code: str | None,
        now: datetime | None,
    ) -> ContributionTaskStateVersion:
        if self.current_mark(task_id) is not None:
            raise LifecycleConflictError(
                "A terminal lifecycle mark already exists"
            )
        expected_action, target, default_reason = _TRANSITIONS[action]
        try:
            require_user_action(expected_action, expected=expected_action)
        except AuthorizationActionError as exc:
            raise LifecycleConflictError(str(exc)) from exc
        reason = reason_code or default_reason
        if not _REASON.fullmatch(reason):
            raise LifecycleConflictError("Lifecycle reason code is invalid")
        states = ContributionTaskStateService(self.session)
        try:
            current = states.current(task_id)
            version = states.prepare_transition(
                task_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                to_state=target,
                reason_code=reason,
                now=now,
            )
        except TaskStateError as exc:
            raise LifecycleConflictError(str(exc)) from exc
        self.session.add(version)
        AuditService(self.session).prepare(
            event_type=f"task.lifecycle.{action}",
            actor_type="local_user",
            actor_id=actor_id,
            correlation_id=version.id,
            payload={
                "task_id": task_id,
                "from_state": current.to_state,
                "to_state": target.value,
                "reason_code": reason,
                "state_record_hash": version.record_hash,
            },
            now=now,
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise LifecycleConflictError(
                "Lifecycle transition changed concurrently"
            ) from exc
        return version

    def _mark(
        self,
        *,
        task_id: str,
        action: str,
        actor_id: str,
        reason_code: str | None,
        now: datetime | None,
    ) -> TaskLifecycleMark:
        existing = self.current_mark(task_id)
        if existing is not None:
            raise LifecycleConflictError(
                "A terminal lifecycle mark already exists"
            )
        expected_action, mark, default_reason = _MARKS[action]
        try:
            require_user_action(expected_action, expected=expected_action)
        except AuthorizationActionError as exc:
            raise LifecycleConflictError(str(exc)) from exc
        reason = reason_code or default_reason
        if not _REASON.fullmatch(reason):
            raise LifecycleConflictError("Lifecycle reason code is invalid")
        current = ContributionTaskStateService(self.session).current(task_id)
        created_at = _aware(now)
        payload = {
            "schema_version": LIFECYCLE_MARK_SCHEMA_VERSION,
            "task_id": task_id,
            "from_state": current.to_state,
            "mark": mark,
            "reason_code": reason,
            "actor_type": "local_user",
            "actor_id": actor_id,
            "state_version_id": current.id,
            "state_record_hash": current.record_hash,
        }
        ensure_no_sensitive_data(payload, context="TaskLifecycleMark")
        record = TaskLifecycleMark(
            id=str(uuid4()),
            task_id=task_id,
            schema_version=LIFECYCLE_MARK_SCHEMA_VERSION,
            from_state=current.to_state,
            mark=mark,
            reason_code=reason,
            actor_type="local_user",
            actor_id=actor_id,
            state_version_id=current.id,
            state_record_hash=current.record_hash,
            record_hash=content_hash(payload),
            created_at=created_at,
        )
        self.session.add(record)
        AuditService(self.session).prepare(
            event_type=f"task.lifecycle.{action}",
            actor_type="local_user",
            actor_id=actor_id,
            correlation_id=record.id,
            payload={
                "task_id": task_id,
                "from_state": current.to_state,
                "mark": mark,
                "reason_code": reason,
                "record_hash": record.record_hash,
            },
            now=now,
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise LifecycleConflictError(
                "Lifecycle mark changed concurrently or failed provenance checks"
            ) from exc
        return record


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
