from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditEvent
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data


class AuditError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuditVerification:
    valid: bool
    checked_events: int
    error: str | None = None


class AuditService:
    def __init__(
        self,
        session: Session,
        *,
        secrets: tuple[str | None, ...] = (),
    ) -> None:
        self.session = session
        self.secrets = secrets

    def append(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        correlation_id: str,
        payload: dict[str, object],
        now: datetime | None = None,
    ) -> AuditEvent:
        for attempt in range(3):
            event = self.prepare(
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload=payload,
                now=now,
            )
            try:
                self.session.commit()
                return event
            except IntegrityError:
                self.session.rollback()
                if attempt == 2:
                    raise AuditError(
                        "Could not append audit event after three sequence conflicts"
                    )
        raise AssertionError("unreachable")

    def prepare(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        correlation_id: str,
        payload: dict[str, object],
        now: datetime | None = None,
    ) -> AuditEvent:
        """Build an event for an owning service's atomic transaction."""
        created_at = self._aware(now)
        fields = {
            "event_type": event_type.strip(),
            "actor_type": actor_type.strip(),
            "actor_id": actor_id.strip(),
            "correlation_id": correlation_id.strip(),
        }
        limits = {
            "event_type": 100,
            "actor_type": 40,
            "actor_id": 128,
            "correlation_id": 128,
        }
        for name, value in fields.items():
            if not value or len(value) > limits[name]:
                raise ValueError(
                    f"{name} must contain between 1 and {limits[name]} characters"
                )

        ensure_no_sensitive_data(
            fields,
            secrets=self.secrets,
            context="audit identity fields",
        )
        ensure_no_sensitive_data(
            payload,
            secrets=self.secrets,
            context="audit payload",
        )
        payload_hash = content_hash(payload)
        previous = self.session.scalar(
            select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1)
        )
        sequence = 1 if previous is None else previous.sequence + 1
        previous_hash = None if previous is None else previous.event_hash
        event_hash = self._event_hash(
            sequence=sequence,
            event_type=fields["event_type"],
            actor_type=fields["actor_type"],
            actor_id=fields["actor_id"],
            correlation_id=fields["correlation_id"],
            payload_hash=payload_hash,
            previous_event_hash=previous_hash,
            created_at=created_at,
        )
        event = AuditEvent(
            id=str(uuid4()),
            sequence=sequence,
            event_type=fields["event_type"],
            actor_type=fields["actor_type"],
            actor_id=fields["actor_id"],
            correlation_id=fields["correlation_id"],
            payload=payload,
            payload_hash=payload_hash,
            previous_event_hash=previous_hash,
            event_hash=event_hash,
            created_at=created_at,
        )
        self.session.add(event)
        return event

    def verify(self) -> AuditVerification:
        events = list(
            self.session.scalars(
                select(AuditEvent).order_by(AuditEvent.sequence)
            )
        )
        previous_hash: str | None = None
        expected_sequence = 1
        for event in events:
            if event.sequence != expected_sequence:
                return AuditVerification(
                    False,
                    expected_sequence - 1,
                    f"Expected audit sequence {expected_sequence}, got {event.sequence}",
                )
            expected_payload_hash = content_hash(event.payload)
            if event.payload_hash != expected_payload_hash:
                return AuditVerification(
                    False,
                    expected_sequence - 1,
                    f"Payload hash mismatch at sequence {event.sequence}",
                )
            if event.previous_event_hash != previous_hash:
                return AuditVerification(
                    False,
                    expected_sequence - 1,
                    f"Previous hash mismatch at sequence {event.sequence}",
                )
            expected_event_hash = self._event_hash(
                sequence=event.sequence,
                event_type=event.event_type,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                correlation_id=event.correlation_id,
                payload_hash=event.payload_hash,
                previous_event_hash=event.previous_event_hash,
                created_at=event.created_at,
            )
            if event.event_hash != expected_event_hash:
                return AuditVerification(
                    False,
                    expected_sequence - 1,
                    f"Event hash mismatch at sequence {event.sequence}",
                )
            previous_hash = event.event_hash
            expected_sequence += 1
        return AuditVerification(True, len(events))

    @staticmethod
    def _event_hash(
        *,
        sequence: int,
        event_type: str,
        actor_type: str,
        actor_id: str,
        correlation_id: str,
        payload_hash: str,
        previous_event_hash: str | None,
        created_at: datetime,
    ) -> str:
        return content_hash(
            {
                "sequence": sequence,
                "event_type": event_type,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "correlation_id": correlation_id,
                "payload_hash": payload_hash,
                "previous_event_hash": previous_event_hash,
                "created_at": created_at,
            }
        )

    @staticmethod
    def _aware(value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
