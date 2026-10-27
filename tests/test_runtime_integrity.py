"""A partial runtime must fail its build and never become a model failure."""
from __future__ import annotations

import asyncio
import builtins
from pathlib import Path

from app.runtime_integrity import MissingRuntimeModule, missing_runtime_modules


def test_lazy_internal_import_is_checked_without_executing_source(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "coding.py").write_text('raise RuntimeError("must not execute")\n'
        'def proposal():\n    from app.coding_budget import proposal_budget\n')
    assert missing_runtime_modules(root) == (MissingRuntimeModule("coding.py", "app.coding_budget"),)
    (root / "coding_budget.py").write_text("def proposal_budget(): return 6\n")
    assert not missing_runtime_modules(root)


def test_internal_package_imports_do_not_require_a_module_file(tmp_path):
    root = tmp_path / "app"
    package = root / "providers"
    package.mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (package / "__init__.py").write_text("class Provider: pass\n")
    (root / "worker.py").write_text("from app.providers import Provider\nimport app.providers\nimport third_party\n")
    assert not missing_runtime_modules(root)


def test_actual_deployment_builds_validate_complete_runtime():
    root = Path(__file__).resolve().parents[1]
    assert not missing_runtime_modules(root / "app")
    for path in ("Dockerfile", "Publisher.Dockerfile"):
        text = (root / "docs/deployment" / path).read_text()
        assert text.index("python -m app.runtime_integrity") < text.index("python -m pip install")


def test_missing_runtime_does_not_consume_model_budget_and_can_resume_after_repair(tmp_path, monkeypatch, caplog):
    from app.contribution_workflow import advance_workflows, retry_execution_job
    from app.coding_budget import proposal_budget
    from app.workbench import Workbench
    from tests.test_coding_source_reads import direct_fixture
    from tests import test_workbench as fixtures

    db, settings, task, workers, code, review = direct_fixture(tmp_path, monkeypatch)
    fixtures.populate_round(code, review)
    code.contents.pop(0)  # Direct proposals use the complete draft.
    for _ in range(8):
        for index, worker in enumerate(workers):
            if index != 2:
                job = asyncio.run(worker.run_once())
                if job: assert job.state == "succeeded", job.error_message
        advance_workflows(db, settings)
        with db.session() as session:
            w = Workbench(session, settings.artifact_root)
            root = next((j for j in w.jobs(task) if j.kind == "change_set_proposal" and j.state == "queued"), None)
            if root:
                root_id = root.id
                original = {e.id:e.record_hash for e in w.history(task)}
                auth_hash = w.latest(task, "execution_authorized").record_hash
                break
    else:
        raise AssertionError("Direct proposal was not queued")
    real_import = builtins.__import__
    def incomplete_import(name, *args, **kwargs):
        if name == "app.coding_budget":
            raise ModuleNotFoundError("sk-canary-runtime-import-must-not-leak", name=name)
        return real_import(name, *args, **kwargs)
    with monkeypatch.context() as missing:
        missing.setattr(builtins, "__import__", incomplete_import)
        failed = asyncio.run(workers[2].run_once())
    assert failed.state == "failed" and failed.error_code == "worker_runtime_incomplete"
    assert "重建" in failed.error_message
    assert "sk-canary" not in failed.error_message + caplog.text
    assert not code.invocations
    advance_workflows(db, settings)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        blocker = w.active_blocker(task)
        assert blocker.payload["retryable"] is False
        assert blocker.payload["recovery"]["action"] == "rebuild_runtime"
        assert blocker.payload["recovery"]["uses_code_repair"] is False
        assert proposal_budget(w, task, root_id)["used"] == 0
        assert retry_execution_job(w, failed.id, settings).id == root_id
    fixtures.drive_round(db, settings, task, workers)
    with db.session() as session:
        w = Workbench(session, settings.artifact_root)
        assert w.latest(task, "awaiting_acceptance")
        assert w.latest(task, "execution_authorized").record_hash == auth_hash
        assert all({e.id:e.record_hash for e in w.history(task)}.get(k)==v for k,v in original.items())
        assert proposal_budget(w, task, root_id)["used"] == 1
        assert len(code.invocations) == 1
        assert not w.latest(task, "publication_confirmed")
    db.close()
