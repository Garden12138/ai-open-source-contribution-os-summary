from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import AuditService
from app.authorizations import (
    AuthorizationActionError,
    UserAction,
    require_user_action,
)
from app.models import DraftPullRequest, PullRequestEvent
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
)


PULL_REQUEST_EVENT_SCHEMA_VERSION = "1"
_EVENT_TYPES = frozenset(
    {
        "opened",
        "review",
        "check",
        "changes_requested",
        "merged",
        "closed",
    }
)
_REMOTE_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_STATE_FOR_EVENT = {
    "changes_requested": ContributionTaskState.CHANGES_REQUESTED,
    "merged": ContributionTaskState.MERGED,
}


class PullRequestEventError(RuntimeError):
    pass


class PullRequestEventNotFoundError(PullRequestEventError):
    pass


class PullRequestEventConflictError(PullRequestEventError):
    pass


class PullRequestEventService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ingest(
        self,
        *,
        draft_pull_request_id: str,
        action: UserAction | str,
        remote_event_id: str,
        event_type: str,
        payload: dict[str, object],
        actor_type: str,
        actor_id: str,
        occurred_at: datetime | None = None,
        now: datetime | None = None,
    ) -> PullRequestEvent:
        try:
            require_user_action(action, expected=UserAction.INGEST_PR_EVENT)
        except AuthorizationActionError as exc:
            raise PullRequestEventConflictError(str(exc)) from exc
        if event_type not in _EVENT_TYPES:
            raise PullRequestEventConflictError(
                "Pull request event type is unknown"
            )
        if not _REMOTE_EVENT_ID.fullmatch(remote_event_id):
            raise PullRequestEventConflictError(
                "Remote event id is invalid"
            )
        if not isinstance(payload, dict):
            raise PullRequestEventConflictError(
                "Pull request event payload must be an object"
            )
        draft = self.session.get(DraftPullRequest, draft_pull_request_id)
        if draft is None:
            raise PullRequestEventNotFoundError(
                "DraftPullRequest was not found"
            )
        replay = self.session.scalar(
            select(PullRequestEvent).where(
                PullRequestEvent.draft_pull_request_id == draft.id,
                PullRequestEvent.remote_event_id == remote_event_id,
            )
        )
        if replay is not None:
            verified = self.get_verified(replay.id)
            if (
                verified.event_type != event_type
                or verified.payload != payload
            ):
                raise PullRequestEventConflictError(
                    "Remote event id belongs to different inputs"
                )
            return verified
        previous = self.session.scalar(
            select(PullRequestEvent)
            .where(PullRequestEvent.draft_pull_request_id == draft.id)
            .order_by(
                desc(PullRequestEvent.created_at),
                desc(PullRequestEvent.id),
            )
            .limit(1)
        )
        created_at = _aware(now)
        occurred = _aware(occurred_at or now)
        payload_hash = content_hash(payload)
        record = pull_request_event_payload(
            draft_pull_request_id=draft.id,
            task_id=draft.task_id,
            remote_event_id=remote_event_id,
            event_type=event_type,
            payload=payload,
            payload_hash=payload_hash,
            previous_event_hash=(
                None if previous is None else previous.record_hash
            ),
            occurred_at=occurred,
        )
        ensure_no_sensitive_data(
            {"pull_request_event": record},
            context="PullRequestEvent",
        )
        event = PullRequestEvent(
            id=str(uuid4()),
            draft_pull_request_id=draft.id,
            task_id=draft.task_id,
            schema_version=PULL_REQUEST_EVENT_SCHEMA_VERSION,
            remote_event_id=remote_event_id,
            event_type=event_type,
            payload=payload,
            payload_hash=payload_hash,
            previous_event_hash=(
                None if previous is None else previous.record_hash
            ),
            record_hash=content_hash(record),
            occurred_at=occurred,
            created_at=created_at,
        )
        self.session.add(event)
        target = _STATE_FOR_EVENT.get(event_type)
        if target is not None:
            states = ContributionTaskStateService(self.session)
            current = states.current(draft.task_id)
            if current.to_state == ContributionTaskState.DRAFT_PR.value:
                try:
                    version = states.prepare_transition(
                        draft.task_id,
                        expected_sequence=current.sequence,
                        expected_record_hash=current.record_hash,
                        to_state=target,
                        reason_code=f"pr_event_{event_type}",
                        now=now,
                    )
                except TaskStateError as exc:
                    raise PullRequestEventConflictError(str(exc)) from exc
                self.session.add(version)
        try:
            AuditService(self.session).prepare(
                event_type="pull_request.event",
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=remote_event_id,
                payload={
                    "pull_request_event_id": event.id,
                    "record_hash": event.record_hash,
                    "draft_pull_request_id": draft.id,
                    "remote_event_id": remote_event_id,
                    "event_type": event_type,
                },
                now=now,
            )
        except Exception as exc:
            self.session.rollback()
            raise PullRequestEventConflictError(
                "Pull request event audit evidence could not be prepared"
            ) from exc
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(PullRequestEvent).where(
                    PullRequestEvent.draft_pull_request_id == draft.id,
                    PullRequestEvent.remote_event_id == remote_event_id,
                )
            )
            if replay is not None:
                return self.get_verified(replay.id)
            raise PullRequestEventConflictError(
                "Pull request event changed concurrently or failed provenance checks"
            ) from exc
        return event

    def get_verified(self, event_id: str) -> PullRequestEvent:
        event = self.session.get(PullRequestEvent, event_id)
        if event is None:
            raise PullRequestEventNotFoundError(
                "PullRequestEvent was not found"
            )
        expected = pull_request_event_payload(
            draft_pull_request_id=event.draft_pull_request_id,
            task_id=event.task_id,
            remote_event_id=event.remote_event_id,
            event_type=event.event_type,
            payload=dict(event.payload),
            payload_hash=event.payload_hash,
            previous_event_hash=event.previous_event_hash,
            occurred_at=event.occurred_at,
        )
        if (
            event.schema_version != PULL_REQUEST_EVENT_SCHEMA_VERSION
            or event.payload_hash != content_hash(dict(event.payload))
            or event.record_hash != content_hash(expected)
        ):
            raise PullRequestEventConflictError(
                "PullRequestEvent content does not match its immutable inputs"
            )
        return event

    def history(self, draft_pull_request_id: str) -> tuple[PullRequestEvent, ...]:
        rows = tuple(
            self.session.scalars(
                select(PullRequestEvent)
                .where(
                    PullRequestEvent.draft_pull_request_id
                    == draft_pull_request_id
                )
                .order_by(
                    PullRequestEvent.created_at,
                    PullRequestEvent.id,
                )
            )
        )
        return tuple(self.get_verified(row.id) for row in rows)


def pull_request_event_payload(
    *,
    draft_pull_request_id: str,
    task_id: str,
    remote_event_id: str,
    event_type: str,
    payload: dict[str, object],
    payload_hash: str,
    previous_event_hash: str | None,
    occurred_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": PULL_REQUEST_EVENT_SCHEMA_VERSION,
        "draft_pull_request_id": draft_pull_request_id,
        "task_id": task_id,
        "remote_event_id": remote_event_id,
        "event_type": event_type,
        "payload": payload,
        "payload_hash": payload_hash,
        "previous_event_hash": previous_event_hash,
        "occurred_at": _aware(occurred_at).isoformat(),
    }


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
