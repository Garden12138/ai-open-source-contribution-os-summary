from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PlanConversationEntry
from app.planning import ContributionTaskError, ContributionTaskService
from app.plans import PlanVersionError, PlanVersionService
from app.provenance import content_hash
from app.security import contains_sensitive_text, ensure_no_sensitive_data


PLAN_CONVERSATION_SCHEMA_VERSION = "1"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ACTOR_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


class PlanConversationEntryType(StrEnum):
    MESSAGE = "message"
    DECISION = "decision"


class PlanDecisionCode(StrEnum):
    REQUEST_REVISION = "request_revision"
    ACCEPT_FOR_APPROVAL = "accept_for_approval"
    DEFER = "defer"
    REJECT = "reject"


class PlanConversationError(RuntimeError):
    pass


class PlanConversationNotFoundError(PlanConversationError):
    pass


class PlanConversationConflictError(PlanConversationError):
    pass


class PlanConversationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append_message(
        self,
        *,
        task_id: str,
        plan_version_id: str | None,
        actor_type: str,
        actor_id: str,
        text: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> PlanConversationEntry:
        message = _bounded_text(text, name="message", maximum=8_000)
        return self._append(
            task_id=task_id,
            plan_version_id=plan_version_id,
            entry_type=PlanConversationEntryType.MESSAGE,
            actor_type=actor_type,
            actor_id=actor_id,
            content={"text": message},
            idempotency_key=idempotency_key,
            now=now,
        )

    def append_decision(
        self,
        *,
        task_id: str,
        plan_version_id: str,
        actor_type: str,
        actor_id: str,
        decision_code: PlanDecisionCode | str,
        rationale: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> PlanConversationEntry:
        try:
            code = PlanDecisionCode(decision_code)
        except ValueError as exc:
            raise ValueError("Plan decision code is invalid") from exc
        explanation = _bounded_text(
            rationale,
            name="decision rationale",
            maximum=8_000,
        )
        return self._append(
            task_id=task_id,
            plan_version_id=plan_version_id,
            entry_type=PlanConversationEntryType.DECISION,
            actor_type=actor_type,
            actor_id=actor_id,
            content={
                "decision_code": code.value,
                "rationale": explanation,
            },
            idempotency_key=idempotency_key,
            now=now,
        )

    def history(self, task_id: str) -> list[PlanConversationEntry]:
        try:
            ContributionTaskService(self.session).get_verified(task_id)
        except ContributionTaskError as exc:
            raise PlanConversationNotFoundError(str(exc)) from exc
        entries = list(
            self.session.scalars(
                select(PlanConversationEntry)
                .where(PlanConversationEntry.task_id == task_id)
                .order_by(PlanConversationEntry.sequence)
            )
        )
        previous_hash: str | None = None
        for expected_sequence, entry in enumerate(entries, start=1):
            if entry.plan_version_id is not None:
                try:
                    plan = PlanVersionService(
                        self.session
                    ).get_verified(entry.plan_version_id)
                except PlanVersionError as exc:
                    raise PlanConversationConflictError(str(exc)) from exc
                if plan.task_id != task_id:
                    raise PlanConversationConflictError(
                        "Conversation PlanVersion belongs to another task"
                    )
            try:
                entry_type = PlanConversationEntryType(entry.entry_type)
            except ValueError as exc:
                raise PlanConversationConflictError(
                    "Conversation entry type is invalid"
                ) from exc
            expected_content_hash = content_hash(entry.content)
            expected_record_hash = content_hash(
                plan_conversation_record_payload(
                    task_id=task_id,
                    plan_version_id=entry.plan_version_id,
                    sequence=entry.sequence,
                    entry_type=entry_type,
                    actor_type=entry.actor_type,
                    actor_id=entry.actor_id,
                    content_hash_value=expected_content_hash,
                    previous_entry_hash=entry.previous_entry_hash,
                )
            )
            if (
                entry.schema_version != PLAN_CONVERSATION_SCHEMA_VERSION
                or entry.sequence != expected_sequence
                or entry.previous_entry_hash != previous_hash
                or entry.content_hash != expected_content_hash
                or entry.record_hash != expected_record_hash
            ):
                raise PlanConversationConflictError(
                    "Plan conversation hash chain does not match"
                )
            try:
                _actor_type(entry.actor_type)
                _actor_id(entry.actor_id)
                ensure_no_sensitive_data(
                    entry.content,
                    context="Plan conversation history",
                )
            except ValueError as exc:
                raise PlanConversationConflictError(str(exc)) from exc
            previous_hash = entry.record_hash
        return entries

    def _append(
        self,
        *,
        task_id: str,
        plan_version_id: str | None,
        entry_type: PlanConversationEntryType,
        actor_type: str,
        actor_id: str,
        content: dict[str, object],
        idempotency_key: str,
        now: datetime | None,
    ) -> PlanConversationEntry:
        key = _idempotency_key(idempotency_key)
        normalized_actor_type = _actor_type(actor_type)
        normalized_actor_id = _actor_id(actor_id)
        try:
            ContributionTaskService(self.session).get_verified(task_id)
        except ContributionTaskError as exc:
            raise PlanConversationNotFoundError(str(exc)) from exc
        if plan_version_id is not None:
            try:
                plan = PlanVersionService(
                    self.session
                ).get_verified(plan_version_id)
            except PlanVersionError as exc:
                raise PlanConversationNotFoundError(str(exc)) from exc
            if plan.task_id != task_id:
                raise PlanConversationConflictError(
                    "Conversation PlanVersion belongs to another task"
                )
        ensure_no_sensitive_data(
            {
                "idempotency_key": key,
                "actor_type": normalized_actor_type,
                "actor_id": normalized_actor_id,
                "content": content,
            },
            context="Plan conversation entry",
        )
        content_hash_value = content_hash(content)
        replay = self.session.scalar(
            select(PlanConversationEntry).where(
                PlanConversationEntry.idempotency_key == key
            )
        )
        if replay is not None:
            if (
                replay.task_id != task_id
                or replay.plan_version_id != plan_version_id
                or replay.entry_type != entry_type.value
                or replay.actor_type != normalized_actor_type
                or replay.actor_id != normalized_actor_id
                or replay.content_hash != content_hash_value
            ):
                raise PlanConversationConflictError(
                    "Conversation idempotency key belongs to different inputs"
                )
            self.history(task_id)
            return replay
        history = self.history(task_id)
        previous = history[-1] if history else None
        sequence = 1 if previous is None else previous.sequence + 1
        previous_hash = None if previous is None else previous.record_hash
        record_payload = plan_conversation_record_payload(
            task_id=task_id,
            plan_version_id=plan_version_id,
            sequence=sequence,
            entry_type=entry_type,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            content_hash_value=content_hash_value,
            previous_entry_hash=previous_hash,
        )
        entry = PlanConversationEntry(
            id=str(uuid4()),
            task_id=task_id,
            plan_version_id=plan_version_id,
            schema_version=PLAN_CONVERSATION_SCHEMA_VERSION,
            sequence=sequence,
            entry_type=entry_type.value,
            idempotency_key=key,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            content=content,
            content_hash=content_hash_value,
            previous_entry_hash=previous_hash,
            record_hash=content_hash(record_payload),
            created_at=_aware(now),
        )
        self.session.add(entry)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(PlanConversationEntry).where(
                    PlanConversationEntry.idempotency_key == key
                )
            )
            if (
                replay is not None
                and replay.task_id == task_id
                and replay.content_hash == content_hash_value
            ):
                return replay
            raise PlanConversationConflictError(
                "Plan conversation changed concurrently"
            ) from exc
        return entry


def plan_conversation_record_payload(
    *,
    task_id: str,
    plan_version_id: str | None,
    sequence: int,
    entry_type: PlanConversationEntryType,
    actor_type: str,
    actor_id: str,
    content_hash_value: str,
    previous_entry_hash: str | None,
) -> dict[str, object]:
    return {
        "schema_version": PLAN_CONVERSATION_SCHEMA_VERSION,
        "task_id": task_id,
        "plan_version_id": plan_version_id,
        "sequence": sequence,
        "entry_type": entry_type.value,
        "actor": {
            "type": actor_type,
            "id": actor_id,
        },
        "content_hash": content_hash_value,
        "previous_entry_hash": previous_entry_hash,
    }


def _bounded_text(value: str, *, name: str, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > maximum or contains_sensitive_text(text):
        raise ValueError(f"Plan {name} is invalid")
    return text


def _idempotency_key(value: str) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not _IDEMPOTENCY_KEY.fullmatch(key) or contains_sensitive_text(key):
        raise ValueError("Plan conversation idempotency key is invalid")
    return key


def _actor_type(value: str) -> str:
    actor = value.strip() if isinstance(value, str) else ""
    if not _ACTOR_TYPE.fullmatch(actor) or contains_sensitive_text(actor):
        raise ValueError("Plan conversation actor type is invalid")
    return actor


def _actor_id(value: str) -> str:
    actor = value.strip() if isinstance(value, str) else ""
    if not _ACTOR_ID.fullmatch(actor) or contains_sensitive_text(actor):
        raise ValueError("Plan conversation actor ID is invalid")
    return actor


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
