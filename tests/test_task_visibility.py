from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.api import create_app
from app.audit import AuditService
from app.contribution_workflow import advance_workflows
from app.database import Database
from app.jobs import JobConflictError, JobService, JobTransitionError
from app.models import ContributionTask, TaskVisibilityVersion, WorkbenchEvent
from app.planning import ContributionTaskConflictError, ContributionTaskNotFoundError, ContributionTaskService
from app.task_visibility import TaskVisibilityService
from app.workbench import Workbench
from tests.test_workbench import seeded


def change(client, task_id, action, sequence):
    return client.request("DELETE" if action == "delete" else "POST",
        f"/api/v1/tasks/{task_id}" + ("" if action == "delete" else f"/{action}"),
        json={"expected_sequence": sequence})


def test_archive_restore_then_permanent_delete_rejects_stale_requests(tmp_path):
    db, settings, task_id, _, _ = seeded(tmp_path)
    with db.session() as session:
        root_hash = session.get(ContributionTask, task_id).record_hash
        history = Workbench(session, settings.artifact_root).history(task_id)
    with TestClient(create_app(settings)) as client:
        assert change(client, task_id, "delete", 0).status_code == 409
        assert change(client, "missing", "archive", 0).status_code == 404
        assert client.post(f"/api/v1/tasks/{task_id}/archive", json={}).status_code == 422
        archived = change(client, task_id, "archive", 0)
        assert archived.status_code == 200, archived.text
        assert archived.json()["state"] == "archived"
        assert change(client, task_id, "archive", 0).json() == archived.json()
        assert client.get("/api/v1/tasks").json() == []
        row = client.get("/api/v1/tasks?archived=true").json()[0]
        assert row["id"] == task_id and row["visibility_sequence"] == 1
        assert client.get(f"/api/v1/tasks/{task_id}").status_code == 409
        assert change(client, task_id, "restore", 1).status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}").status_code == 200
        assert change(client, task_id, "archive", 0).status_code == 409
        assert change(client, task_id, "delete", 1).status_code == 409
        assert change(client, task_id, "archive", 2).status_code == 200
        deleted = change(client, task_id, "delete", 3)
        assert deleted.status_code == 202
        assert change(client, task_id, "delete", 3).json() == deleted.json()
        assert change(client, task_id, "restore", 4).status_code == 404
        assert client.get("/api/v1/tasks?archived=true").json()[0]["erasure_state"] == "queued"
        import asyncio
        from app.task_erasure import TaskErasureWorker
        completed = asyncio.run(TaskErasureWorker(db, settings.artifact_root, worker_id="eraser").run_once())
        assert completed.state == "succeeded"
        assert client.get("/api/v1/tasks?archived=true").json() == []
        assert client.get("/api/v1/tasks").json() == []
        assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
        assert client.get(f"/api/v1/tasks/{task_id}/workbench").status_code == 404
        dashboard = client.get("/api/v1/contributions/dashboard").json()
        assert dashboard["metrics"]["task_count"] == 0
        assert sum(dashboard["current_states"].values()) == 0
    db.close()
    reopened = Database(settings.database_url)
    reopened.create_schema()
    with reopened.session() as session:
        assert session.get(ContributionTask, task_id) is None
        assert list(session.scalars(select(WorkbenchEvent.record_hash))) == []
        assert session.scalar(select(func.count(TaskVisibilityVersion.id))) == 0
        assert AuditService(session).verify().valid
        with pytest.raises(ContributionTaskNotFoundError):
            ContributionTaskService(session).get_verified(task_id)
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert session.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    reopened.close()


def test_archive_rejects_active_jobs_and_inactive_task_cannot_retry_or_enqueue(tmp_path):
    db, settings, task_id, _, _ = seeded(tmp_path)
    with db.session() as session:
        jobs = JobService(session)
        # Covers an untracked job too: payload ownership must be sufficient.
        job, _ = jobs.enqueue(kind="planning_turn", idempotency_key="busy",
                              payload={"task_id": task_id})
        job_id = job.id
        with pytest.raises(ContributionTaskConflictError, match="正在运行"):
            TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=0)
        session.rollback()
        jobs.request_cancel(job.id)
        TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=0)
        with pytest.raises(JobTransitionError, match="归档或删除"):
            jobs.retry(job.id)
        with pytest.raises(JobConflictError):
            jobs.enqueue(kind="planning_turn", idempotency_key="late", payload={"task_id": task_id})
        session.rollback()
        # A new DB connection sees the archive and rejects direct state changes.
    with db.session() as session:
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE jobs SET state='queued', completed_at=NULL, cancel_requested_at=NULL WHERE id=:id"), {"id": job_id})
        session.rollback()
        TaskVisibilityService(session).change(task_id, target="active", expected_sequence=1)
        assert JobService(session).retry(job_id).state == "queued"
    db.close()


def test_visibility_is_immutable_and_audit_failure_rolls_back(tmp_path, monkeypatch):
    db, _, task_id, _, _ = seeded(tmp_path)
    with db.session() as session:
        def fail(*args, **kwargs):
            raise RuntimeError("injected audit failure")
        with monkeypatch.context() as patch:
            patch.setattr(AuditService, "prepare", fail)
            with pytest.raises(RuntimeError, match="injected"):
                TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=0)
        session.rollback()
        assert session.scalar(select(func.count(TaskVisibilityVersion.id))) == 0
        TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=0)
        for statement in ("DELETE FROM task_visibility_versions", "UPDATE task_visibility_versions SET state='active'"):
            with pytest.raises(IntegrityError):
                session.execute(text(statement))
            session.rollback()
    db.close()


@pytest.mark.parametrize("action", ["archive", "restore", "delete"])
def test_visibility_requires_auth_origin_csrf_and_does_not_leak_canary(tmp_path, caplog, action):
    db, settings, task_id, _, _ = seeded(tmp_path)
    canary = "contribos-task-visibility-credential-canary-8291"
    app = create_app(replace(settings, local_access_token=canary))
    method = "DELETE" if action == "delete" else "POST"
    url = f"/api/v1/tasks/{task_id}" + ("" if action == "delete" else f"/{action}")
    responses = []
    with TestClient(app) as client:
        for headers, status in [({}, 401),
                ({"X-ContribOS-Token": canary, "Origin": "https://evil.test"}, 403),
                ({"X-ContribOS-Token": canary, "Origin": "http://testserver"}, 403)]:
            response = client.request(method, url, json={"expected_sequence": 0}, headers=headers)
            assert response.status_code == status
            responses.append(response.text)
        response = client.request(method, url, json={"expected_sequence": 0}, headers={
            "X-ContribOS-Token": canary, "Origin": "http://testserver", "X-CSRF-Token": app.state.csrf_token})
        assert response.status_code == (200 if action == "archive" else 409)
        responses.append(response.text)
    with sqlite3.connect(tmp_path / "workbench.db") as connection:
        assert canary not in "\n".join(connection.iterdump())
    assert canary not in "".join(responses) + caplog.text
    for artifact in Path(settings.artifact_root).rglob("*"):
        if artifact.is_file():
            assert canary.encode() not in artifact.read_bytes()
    db.close()


def test_archived_workflow_is_skipped_and_unknown_publication_blocks_archive(tmp_path):
    db, settings, task_id, _, _ = seeded(tmp_path)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        wb.append(task_id, "stop_requested", {"job_ids": []}, key="stop:fixture")
        TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=0)
        count = len(session.scalars(select(WorkbenchEvent)).all())
    advance_workflows(db, settings)
    with db.session() as session:
        assert len(session.scalars(select(WorkbenchEvent)).all()) == count
        TaskVisibilityService(session).change(task_id, target="active", expected_sequence=1)
        wb = Workbench(session, settings.artifact_root)
        wb.append(task_id, "publication_confirmed", {}, key="publication:fixture")
        with pytest.raises(ContributionTaskConflictError, match="发布结果"):
            TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=2)
    db.close()


def test_upgrade_from_0030_preserves_tasks_and_backup_recovery(tmp_path, monkeypatch):
    from app.migrations import versions
    with monkeypatch.context() as patch:
        patch.setattr(versions, "MIGRATIONS", versions.MIGRATIONS[:-2])
        # Create a populated pre-upgrade DB without calling new visibility code.
        from tests.test_planning import _seed_analysis
        import app.task_visibility as visibility
        patch.setattr(visibility, "require_active_task", lambda *_: None)
        analysis_id, _, _ = _seed_analysis(tmp_path / "old.db")
        db = Database(f"sqlite:///{tmp_path / 'old.db'}")
        with db.session() as session:
            task = ContributionTaskService(session).create(analysis_version_id=analysis_id, idempotency_key="old-task")
    with sqlite3.connect(tmp_path / "old.db") as source, sqlite3.connect(tmp_path / "backup.db") as backup:
        source.backup(backup)
    report = db.create_schema()
    assert report.applied == ("0031_task_visibility", "0032_minimax_reviews")
    with db.session() as session:
        assert ContributionTaskService(session).get_verified(task.id).record_hash == task.record_hash
        TaskVisibilityService(session).change(task.id, target="archived", expected_sequence=0)
    restored = Database(f"sqlite:///{tmp_path / 'backup.db'}")
    assert restored.current_revision() == "0030_model_settings"
    restored.create_schema()
    with restored.session() as session:
        assert ContributionTaskService(session).get_verified(task.id).record_hash == task.record_hash
        assert session.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    db.close()
    restored.close()


def test_archive_and_job_creation_race_cannot_both_succeed(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db, _, task_id, _, _ = seeded(tmp_path)
    barrier = Barrier(2)

    def archive():
        with db.session() as session:
            barrier.wait(timeout=5)
            try:
                TaskVisibilityService(session).change(task_id, target="archived", expected_sequence=0)
                return True
            except ContributionTaskConflictError:
                return False

    def enqueue():
        with db.session() as session:
            barrier.wait(timeout=5)
            try:
                JobService(session).enqueue(kind="planning_turn", idempotency_key="race", payload={"task_id": task_id})
                return True
            except (JobConflictError, IntegrityError):
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        archive_result = pool.submit(archive)
        job_result = pool.submit(enqueue)
        assert archive_result.result() != job_result.result()
    db.close()
