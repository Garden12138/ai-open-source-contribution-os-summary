from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit import AuditService
from app.database import Database


NOW = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    current = Database(f"sqlite+pysqlite:///{tmp_path / 'audit.db'}")
    current.create_schema()
    try:
        yield current
    finally:
        current.close()


def test_audit_events_form_a_verifiable_append_only_hash_chain(
    database: Database,
) -> None:
    with database.session() as session:
        service = AuditService(session)
        first = service.append(
            event_type="job.enqueued",
            actor_type="user",
            actor_id="local-user",
            correlation_id="job-1",
            payload={"job_id": "job-1", "kind": "discovery_scan"},
            now=NOW,
        )
        second = service.append(
            event_type="job.leased",
            actor_type="worker",
            actor_id="worker-a",
            correlation_id="job-1",
            payload={"job_id": "job-1", "attempt": 1},
            now=NOW + timedelta(seconds=1),
        )

        assert first.sequence == 1
        assert first.previous_event_hash is None
        assert second.sequence == 2
        assert second.previous_event_hash == first.event_hash
        assert service.verify().valid is True
        assert service.verify().checked_events == 2

        second.payload = {"tampered": True}
        with pytest.raises(IntegrityError, match="append-only"):
            session.commit()


def test_audit_verifier_detects_out_of_band_corruption(
    database: Database,
) -> None:
    with database.session() as session:
        service = AuditService(session)
        event = service.append(
            event_type="scan.completed",
            actor_type="system",
            actor_id="discovery",
            correlation_id="scan-1",
            payload={"selected": 3},
            now=NOW,
        )

        session.execute(text("DROP TRIGGER audit_events_no_update"))
        session.execute(
            text(
                "UPDATE audit_events "
                "SET payload = :payload "
                "WHERE id = :event_id"
            ),
            {"payload": '{"selected":999}', "event_id": event.id},
        )
        session.commit()
        session.expire_all()

        result = service.verify()
        assert result.valid is False
        assert result.checked_events == 0
        assert result.error == "Payload hash mismatch at sequence 1"
