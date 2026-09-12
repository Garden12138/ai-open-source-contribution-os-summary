import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.artifacts import ArtifactStore
from app.audit import AuditService
from app.jobs import JobService
from app.models import ContributionTask, Job, ModelConfigVersion, WorkbenchEvent
from app.task_erasure import TaskErasureWorker, enqueue_erasure, erase_task
from app.task_visibility import TaskVisibilityService
from app.workbench import Workbench
from tests.test_workbench import seeded


def prepare(db, task_id):
    with db.session() as session:
        TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=0)
        return enqueue_erasure(session, task_id, 1).id


def worker(db, settings):
    return asyncio.run(TaskErasureWorker(db, settings.artifact_root, worker_id="erase-tests").run_once())


def test_erases_task_files_jobs_audits_and_backups_preserving_shared_data(tmp_path):
    db, settings, task_id, _, _ = seeded(tmp_path)
    with db.session() as session:
        shared = ArtifactStore(session, settings.artifact_root).store_bytes(b"shared fixture")
        unrelated, _ = JobService(session).enqueue(kind="unrelated", idempotency_key="shared", payload={})
        ArtifactStore(session, settings.artifact_root).attach(job_id=unrelated.id, artifact_id=shared.id, role="shared")
        JobService(session).request_cancel(unrelated.id)
        wb = Workbench(session, settings.artifact_root)
        wb.append(task_id, "shared_fixture", {"artifact_id": shared.id}, key="shared")
        audit = AuditService(session).append(event_type="unrelated", actor_type="user", actor_id="user",
            correlation_id="unrelated", payload={"preserved": True})
        unchanged_audit = audit.event_hash
        root_count = session.execute(text("SELECT count(*) FROM opportunities")).scalar()
        analysis_count = session.execute(text("SELECT count(*) FROM analysis_versions")).scalar()
        private_artifact = session.scalar(select(WorkbenchEvent).where(WorkbenchEvent.kind == "context_ready")).payload["artifact_id"]
    # Managed backup contains this task; it must not remain a restoration route.
    folder = tmp_path / "backups" / "pre-studio-20260910T000000Z"
    folder.mkdir(parents=True)
    with sqlite3.connect(tmp_path / "workbench.db") as source, sqlite3.connect(folder / "contribos.db") as backup:
        source.backup(backup)
    (folder / "manifest.json").write_text("{}")
    (folder / "artifacts.tar.gz").write_bytes(b"fixture backup")
    job_id = prepare(db, task_id)
    assert worker(db, settings).state == "succeeded"
    assert not folder.exists()
    assert not (Path(settings.artifact_root) / "sha256" / private_artifact[:2] / private_artifact).exists()
    assert (Path(settings.artifact_root) / shared.storage_key).read_bytes() == b"shared fixture"
    with db.session() as session:
        assert session.get(ContributionTask, task_id) is None
        assert session.get(Job, job_id) is None
        assert session.execute(text("SELECT count(*) FROM opportunities")).scalar() == root_count
        assert session.execute(text("SELECT count(*) FROM analysis_versions")).scalar() == analysis_count
        assert session.execute(text("SELECT event_hash FROM audit_events WHERE correlation_id='unrelated'")).scalar() == unchanged_audit
        assert not session.execute(text("PRAGMA foreign_key_check")).all()
        assert session.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        # Ordinary direct deletion remains rejected after the eraser commits.
        with pytest.raises(IntegrityError):
            session.execute(text("DELETE FROM audit_events"))
        session.rollback()
        assert enqueue_erasure(session, task_id, 2) is None
    with sqlite3.connect(tmp_path / "workbench.db") as connection:
        assert task_id not in "\n".join(connection.iterdump())
    db.close()


def test_file_failure_preserves_job_tombstone_and_triggers_for_retry(tmp_path, monkeypatch):
    db, settings, task_id, _, _ = seeded(tmp_path)
    job_id = prepare(db, task_id)
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if "sha256" in path.parts:
            raise OSError("fixture credential-canary must not leak")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        result = worker(db, settings)
        assert result.state == "failed"
        assert "canary" not in result.error_message
    with db.session() as session:
        assert session.get(ContributionTask, task_id)
        with pytest.raises(IntegrityError):
            session.execute(text("DELETE FROM contribution_tasks"))
        session.rollback()
        assert enqueue_erasure(session, task_id, 2).id == job_id
    assert worker(db, settings).state == "succeeded"
    db.close()


def test_erasure_rejects_symlink_outside_storage(tmp_path):
    db, settings, task_id, _, context = seeded(tmp_path)
    artifact_id = context.payload["artifact_id"]
    artifact = Path(settings.artifact_root) / "sha256" / artifact_id[:2] / artifact_id
    victim = tmp_path / "keep.txt"
    victim.write_text("must survive")
    artifact.unlink()
    artifact.symlink_to(victim)
    prepare(db, task_id)
    assert worker(db, settings).state == "failed"
    assert victim.read_text() == "must survive"
    db.close()


def test_erases_completed_execution_review_and_publication_graph(tmp_path):
    from tests.test_execution_pipeline import _complete_verified_execution
    from fastapi.testclient import TestClient
    app, execution_id, task_id, _ = _complete_verified_execution(tmp_path)
    with TestClient(app) as client:
        review = client.post(f"/api/v1/executions/{execution_id}/reviews",
            json={"actor_id": "user-1", "reviewer": "fake"}, headers={"Idempotency-Key": "erasure-review"})
        assert review.status_code in {200, 201}, review.text
        intent = client.post(f"/api/v1/reviews/{review.json()['id']}/publish-intents",
            json={"actor_id": "user-1", "title": "Fixture", "body": "Offline erasure fixture"},
            headers={"Idempotency-Key": "erasure-publication"})
        assert intent.status_code == 201, intent.text
        published = client.post(f"/api/v1/publish-intents/{intent.json()['id']}/confirm",
            json={"actor_id": "user-1", "confirmation_nonce": intent.json()["confirmation_nonce"]})
        assert published.status_code == 200, published.text
    db = app.state.database
    with db.session() as session:
        TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=0)
        job = enqueue_erasure(session, task_id, 1)
    outcome = worker(db, app.state.settings)
    assert outcome.state == "succeeded", outcome.error_message
    with db.session() as session:
        for table in ("contribution_tasks", "plan_versions", "execution_attempts", "review_runs", "execution_stage_runs", "plan_approvals", "publish_intents", "draft_pull_requests", "publish_confirmations"):
            assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0
        assert not session.execute(text("PRAGMA foreign_key_check")).all()
