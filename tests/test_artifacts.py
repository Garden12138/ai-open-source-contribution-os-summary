from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.artifacts import ArtifactIntegrityError, ArtifactStore
from app.database import Database
from app.jobs import JobService


NOW = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    current = Database(f"sqlite+pysqlite:///{tmp_path / 'artifacts.db'}")
    current.create_schema()
    try:
        yield current
    finally:
        current.close()


def test_artifact_storage_is_content_addressed_and_attach_is_idempotent(
    database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifact-root"
    payload = b'{"result":"verified"}'

    with database.session() as session:
        job, _ = JobService(session).enqueue(
            kind="verification",
            idempotency_key="artifact-job",
            payload={},
            now=NOW,
        )
        store = ArtifactStore(session, root)
        first = store.store_bytes(
            payload,
            media_type="application/json",
            now=NOW,
        )
        repeated = store.store_bytes(
            payload,
            media_type="application/json",
            now=NOW,
        )
        first_link = store.attach(
            job_id=job.id,
            artifact_id=first.id,
            role="verification-result",
            now=NOW,
        )
        repeated_link = store.attach(
            job_id=job.id,
            artifact_id=first.id,
            role="verification-result",
            now=NOW,
        )

        expected_hash = hashlib.sha256(payload).hexdigest()
        assert first.id == expected_hash
        assert repeated.id == first.id
        assert first.storage_key == f"sha256/{expected_hash[:2]}/{expected_hash}"
        assert first_link.id == repeated_link.id
        assert store.read_bytes(first.id) == payload


def test_artifact_read_detects_file_tampering(
    database: Database, tmp_path: Path
) -> None:
    root = tmp_path / "artifact-root"
    with database.session() as session:
        store = ArtifactStore(session, root)
        artifact = store.store_bytes(b"trusted bytes", now=NOW)
        path = root / artifact.storage_key
        path.write_bytes(b"tampered bytes")

        with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
            store.read_bytes(artifact.id)


def test_atomic_write_failure_leaves_no_final_or_database_artifact(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifact-root"
    payload = b"must not be partially finalized"
    digest = hashlib.sha256(payload).hexdigest()
    target = root / f"sha256/{digest[:2]}/{digest}"

    def fail_replace(_: object, __: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with database.session() as session:
        store = ArtifactStore(session, root)
        with pytest.raises(OSError, match="injected"):
            store.store_bytes(payload, now=NOW)

        assert not target.exists()
        assert not list(target.parent.glob(".artifact-*.tmp"))


def test_artifact_metadata_is_database_immutable(
    database: Database, tmp_path: Path
) -> None:
    with database.session() as session:
        artifact = ArtifactStore(session, tmp_path / "root").store_bytes(
            b"immutable",
            now=NOW,
        )
        artifact.media_type = "text/plain"
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
