"""Tests for context eviction and budget protection in PlanningContextWorker."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
from app.models import Job
from app.planner import PlanningContextWorker, PlanningService
from app.provenance import canonical_json
from app.sandbox_worker.specs import SandboxPolicy


def test_context_eviction_removes_unreferenced_speculative_entries(tmp_path):
    class StubRuntime:
        async def inspect(self, **kwargs):
            return {
                "inventory": ["file_a.py", "file_b.py", "new_file.py"],
                "entries": [
                    {
                        "path": "new_file.py",
                        "prior_hash": "hash_new",
                        "content": "def new_feature(): pass\n" * 100,
                        "executable": False,
                    }
                ],
                "truncated": False,
            }

    # Setup database and mock state
    db = MagicMock()
    session = MagicMock()
    db.session.return_value.__enter__.return_value = session

    wb_mock = MagicMock()
    wb_mock.require_planning.return_value = "state_hash_1"

    # Simulate history with an assistant message referencing specific paths
    event1 = MagicMock()
    event1.id = "ctx_old"
    event1.kind = "context_ready"
    event1.payload = {"artifact_id": "art_1"}

    hist_event = MagicMock()
    hist_event.kind = "assistant_message"
    hist_event.payload = {"read_paths": ["protected_req.py"]}

    wb_mock.history.return_value = [event1, hist_event]
    wb_mock.latest_plan.return_value = None

    # Old context has a speculative file (not referenced) and a protected file
    large_unreferenced_content = "X = 1\n" * 250_000 # ~1.5MB
    wb_mock.read.return_value = {
        "inventory": ["speculative.py", "protected_req.py", "new_file.py"],
        "entries": [
            {
                "path": "speculative.py",
                "prior_hash": "hash_spec",
                "content": large_unreferenced_content,
                "executable": False,
            },
            {
                "path": "protected_req.py",
                "prior_hash": "hash_prot",
                "content": "def keep_me(): pass\n",
                "executable": False,
            },
        ],
        "truncated": False,
    }

    # We will test the eviction logic directly
    job = Job(
        id="job_ctx_1",
        kind="planning_context",
        payload={
            "task_id": "task_1",
            "state_hash": "state_hash_1",
            "context_id": "ctx_old",
            "archive_hash": "a" * 64,
            "base_sha": "b" * 40,
            "branch": "main",
            "image": "img",
            "inputs": {"repository": "owner/repo", "issue": {"title": "Test"}},
            "text": "test",
            "paths": ["new_file.py"],
        },
    )

    settings = MagicMock()
    settings.artifact_root = tmp_path

    # Stub archive store
    worker = PlanningContextWorker(db, settings, runtime=StubRuntime(), worker_id="test")
    worker.check = MagicMock()
    worker.finish = MagicMock()

    import app.planner
    orig_planning_service = app.planner.PlanningService
    orig_archive_store = app.planner.RepositoryArchiveStore
    try:
        app.planner.PlanningService = MagicMock(return_value=wb_mock)
        archive_mock = MagicMock()
        archive_store_mock = MagicMock()
        archive_store_mock.lookup.return_value = archive_mock
        app.planner.RepositoryArchiveStore = MagicMock(return_value=archive_store_mock)

        # Run execute
        asyncio.run(worker.execute(job))

        # Verify wb_mock.artifact was called with context
        assert wb_mock.artifact.called
        saved_context = wb_mock.artifact.call_args[0][0]
        saved_paths = {e["path"] for e in saved_context["entries"]}

        # speculative.py should have been evicted because it was unreferenced and large
        assert "speculative.py" not in saved_paths
        # protected_req.py and new_file.py must be retained
        assert "protected_req.py" in saved_paths
        assert "new_file.py" in saved_paths
        assert len(canonical_json(saved_context).encode()) <= 1_100_000
    finally:
        app.planner.PlanningService = orig_planning_service
        app.planner.RepositoryArchiveStore = orig_archive_store
