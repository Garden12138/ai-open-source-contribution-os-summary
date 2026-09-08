from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ContributionTask, ContributionTaskStateVersion
from app.provenance import content_hash
from app.security import contains_sensitive_text, ensure_no_sensitive_data


CONTRIBUTION_TASK_STATE_SCHEMA_VERSION = "1"
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")


class ContributionTaskState(StrEnum):
    PLANNING = "planning"
    PLAN_APPROVED = "plan_approved"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    READY = "ready"
    DRAFT_PR = "draft_pr"
    CHANGES_REQUESTED = "changes_requested"
    MERGED = "merged"
    REWARDED = "rewarded"


LEGAL_TASK_TRANSITIONS: dict[
    ContributionTaskState,
    frozenset[ContributionTaskState],
] = {
    ContributionTaskState.PLANNING: frozenset(
        {ContributionTaskState.PLAN_APPROVED}
    ),
    ContributionTaskState.PLAN_APPROVED: frozenset(
        {
            ContributionTaskState.PLANNING,
            ContributionTaskState.EXECUTING,
        }
    ),
    ContributionTaskState.EXECUTING: frozenset(
        {ContributionTaskState.REVIEWING, ContributionTaskState.PLANNING}
    ),
    ContributionTaskState.REVIEWING: frozenset(
        {
            ContributionTaskState.EXECUTING,
            ContributionTaskState.READY,
            ContributionTaskState.PLANNING,
        }
    ),
    ContributionTaskState.READY: frozenset(
        {
            ContributionTaskState.PLANNING,
            ContributionTaskState.DRAFT_PR,
        }
    ),
    ContributionTaskState.DRAFT_PR: frozenset(
        {
            ContributionTaskState.CHANGES_REQUESTED,
            ContributionTaskState.MERGED,
        }
    ),
    ContributionTaskState.CHANGES_REQUESTED: frozenset(
        {
            ContributionTaskState.PLANNING,
            ContributionTaskState.EXECUTING,
        }
    ),
    ContributionTaskState.MERGED: frozenset(
        {ContributionTaskState.REWARDED}
    ),
    ContributionTaskState.REWARDED: frozenset(),
}


class TaskStateError(RuntimeError):
    pass


class TaskStateNotFoundError(TaskStateError):
    pass


class TaskStateConflictError(TaskStateError):
    pass


class TaskStateTransitionError(TaskStateError):
    pass


class ContributionTaskStateService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def current(self, task_id: str) -> ContributionTaskStateVersion:
        task = self.session.get(ContributionTask, task_id)
        if task is None:
            raise TaskStateNotFoundError("ContributionTask was not found")
        current = self.session.scalar(
            select(ContributionTaskStateVersion)
            .where(ContributionTaskStateVersion.task_id == task_id)
            .order_by(
                desc(ContributionTaskStateVersion.sequence),
                desc(ContributionTaskStateVersion.id),
            )
            .limit(1)
        )
        if current is None:
            raise TaskStateConflictError(
                "ContributionTask has no initial state"
            )
        try:
            source = (
                ContributionTaskState(current.from_state)
                if current.from_state is not None
                else None
            )
            target = ContributionTaskState(current.to_state)
        except ValueError as exc:
            raise TaskStateConflictError(
                "ContributionTask state record is invalid"
            ) from exc
        expected_hash = content_hash(
            task_state_record_payload(
                task_id=task.id,
                task_record_hash=task.record_hash,
                sequence=current.sequence,
                from_state=source,
                to_state=target,
                reason_code=current.reason_code,
                previous_state_hash=current.previous_state_hash,
            )
        )
        if (
            current.task_record_hash != task.record_hash
            or current.record_hash != expected_hash
        ):
            raise TaskStateConflictError(
                "ContributionTask state record hash does not match"
            )
        return current

    def transition(
        self,
        task_id: str,
        *,
        expected_sequence: int,
        expected_record_hash: str,
        to_state: ContributionTaskState | str,
        reason_code: str,
        now: datetime | None = None,
    ) -> ContributionTaskStateVersion:
        version = self.prepare_transition(
            task_id,
            expected_sequence=expected_sequence,
            expected_record_hash=expected_record_hash,
            to_state=to_state,
            reason_code=reason_code,
            now=now,
        )
        self.session.add(version)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise TaskStateConflictError(
                "ContributionTask state changed concurrently"
            ) from exc
        return version

    def prepare_transition(
        self,
        task_id: str,
        *,
        expected_sequence: int,
        expected_record_hash: str,
        to_state: ContributionTaskState | str,
        reason_code: str,
        now: datetime | None = None,
    ) -> ContributionTaskStateVersion:
        """Validate and build a transition for an owning atomic service."""
        task = self.session.get(ContributionTask, task_id)
        if task is None:
            raise TaskStateNotFoundError("ContributionTask was not found")
        current = self.current(task_id)
        if (
            current.sequence != expected_sequence
            or current.record_hash != expected_record_hash
        ):
            raise TaskStateConflictError(
                "ContributionTask state compare-and-swap input is stale"
            )
        try:
            target = ContributionTaskState(to_state)
            source = ContributionTaskState(current.to_state)
        except ValueError as exc:
            raise TaskStateTransitionError(
                "ContributionTask state is invalid"
            ) from exc
        if target not in LEGAL_TASK_TRANSITIONS[source]:
            raise TaskStateTransitionError(
                f"Illegal ContributionTask transition: {source.value} "
                f"to {target.value}"
            )
        reason = _reason_code(reason_code)
        if source in {ContributionTaskState.EXECUTING, ContributionTaskState.REVIEWING} and target == ContributionTaskState.PLANNING and reason != "user_replan":
            raise TaskStateTransitionError("Returning to planning requires user_replan")
        version = _build_state_version(
            task=task,
            sequence=current.sequence + 1,
            from_state=source,
            to_state=target,
            reason_code=reason,
            previous_state_hash=current.record_hash,
            now=now,
        )
        return version


def create_initial_task_state(
    session: Session,
    task: ContributionTask,
    *,
    now: datetime | None = None,
) -> ContributionTaskStateVersion:
    state = _build_state_version(
        task=task,
        sequence=1,
        from_state=None,
        to_state=ContributionTaskState.PLANNING,
        reason_code="created_from_analysis",
        previous_state_hash=None,
        now=now,
    )
    session.add(state)
    return state


def task_state_record_payload(
    *,
    task_id: str,
    task_record_hash: str,
    sequence: int,
    from_state: ContributionTaskState | None,
    to_state: ContributionTaskState,
    reason_code: str,
    previous_state_hash: str | None,
) -> dict[str, object]:
    return {
        "schema_version": CONTRIBUTION_TASK_STATE_SCHEMA_VERSION,
        "task_id": task_id,
        "task_record_hash": task_record_hash,
        "sequence": sequence,
        "from_state": from_state.value if from_state is not None else None,
        "to_state": to_state.value,
        "reason_code": reason_code,
        "previous_state_hash": previous_state_hash,
    }


def _build_state_version(
    *,
    task: ContributionTask,
    sequence: int,
    from_state: ContributionTaskState | None,
    to_state: ContributionTaskState,
    reason_code: str,
    previous_state_hash: str | None,
    now: datetime | None,
) -> ContributionTaskStateVersion:
    payload = task_state_record_payload(
        task_id=task.id,
        task_record_hash=task.record_hash,
        sequence=sequence,
        from_state=from_state,
        to_state=to_state,
        reason_code=reason_code,
        previous_state_hash=previous_state_hash,
    )
    ensure_no_sensitive_data(
        payload,
        context="ContributionTask state",
    )
    return ContributionTaskStateVersion(
        id=str(uuid4()),
        task=task,
        schema_version=CONTRIBUTION_TASK_STATE_SCHEMA_VERSION,
        sequence=sequence,
        from_state=from_state.value if from_state is not None else None,
        to_state=to_state.value,
        reason_code=reason_code,
        task_record_hash=task.record_hash,
        previous_state_hash=previous_state_hash,
        record_hash=content_hash(payload),
        created_at=_aware(now),
    )


def _reason_code(value: str) -> str:
    reason = value.strip() if isinstance(value, str) else ""
    if not _REASON_CODE.fullmatch(reason) or contains_sensitive_text(reason):
        raise ValueError("ContributionTask state reason code is invalid")
    return reason


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
