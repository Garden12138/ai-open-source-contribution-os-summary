"""Regression at planning, package selection and actual sandbox boundaries."""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import os
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.execution_environment import environment_plan
from app.executions import ExecutionAttemptService
from app.jobs import JobService
from app.models import CodingSession, ExecutionAttempt
from app.planner import PlanningService
from app.provenance import content_hash
from app.sandbox_worker.dependencies import (
    DependencyManifest, DependencyPreparationPlan, DependencyPreparationResult,
    DependencyPlanSigner, DependencyProxyPolicy, DockerDependencyRuntime,
    DockerDependencyProxyRuntime, WORKSPACE_DEPENDENCY_PLAN_VERSION,
)
from app.sandbox_worker.specs import SandboxPolicy, SandboxCommand
from app.sandbox_worker.workspace_jobs import WorkspacePreparationWorker
from tests.test_workbench import seeded, result, run_turn, automatic_fixture, populate_round, drive_round


def test_verification_policy_separates_preparation_and_new_file_baseline():
    from app.plans import PlanCommand
    from app.verification_commands import verification_command_error, baseline_commands
    inventory = {"pkg/pyproject.toml", "pkg/tests/test_existing.py"}
    new_paths = {"pkg/tests/test_regression.py"}
    existing = PlanCommand("existing", "baseline", ("python", "-m", "pytest", "tests/test_existing.py"), "pkg")
    regression = replace(existing, command_id="regression", argv=("python", "-m", "pytest", "tests/test_regression.py::test_fix"))
    assert verification_command_error([existing, regression], inventory, new_paths) is None
    assert baseline_commands([existing, regression], inventory, new_paths) == (existing,)
    missing = replace(existing, argv=("python", "-m", "pytest", "tests/test_invented.py"))
    assert verification_command_error([missing], inventory, new_paths)["constraint"] == "test_path_missing"
    install = replace(existing, argv=("python", "-m", "pip", "install", "pytest"))
    assert verification_command_error([install], inventory, new_paths)["constraint"] == "dependency_preparation_separate"
    assert verification_command_error([replace(existing, working_directory="missing")], inventory, new_paths)["constraint"] == "command_directory_missing"


def test_installation_draft_is_repaired_without_claiming_saved_or_started(tmp_path):
    db, settings, tid, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(tid, text="实现并验证", parent_id=None, expected_hash=None, key="install-draft")
    broken = json.loads(result(content))
    broken["plan"]["commands_to_run"][0]["argv"] = ["python", "-m", "pip", "install", "pytest"]
    assert run_turn(db, settings, json.dumps(broken))[0].result_data["repair_scheduled"]
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(tid, "assistant_message").payload["plan_error"]["constraint"] == "dependency_preparation_separate"
        assert wb.latest_plan(tid) is None
    run_turn(db, settings, result(content))
    with db.session() as session:
        answer = PlanningService(session, settings.artifact_root).latest(tid, "assistant_message").payload
        assert answer["plan_receipt"]["status"] == "saved"
        assert "尚未批准或执行" in answer["reply"]
    db.close()


def test_legacy_installation_plan_cannot_be_approved_or_resaved(tmp_path):
    from app.plans import PlanVersionService
    from app.schemas import PlanVersionCreateRequest
    from app.contribution_workflow import authorize_execution
    from app.workbench import WorkbenchError
    from app.workbench_view import workbench_view, execution_context
    db, settings, tid, content, context = seeded(tmp_path)
    invalid = replace(content, commands_to_run=(replace(content.commands_to_run[0], argv=("python", "-m", "pip", "install", "pytest")),))
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        old = PlanVersionService(session).create_initial(task_id=tid, content=invalid, idempotency_key="legacy-install")
        wb.append(tid, "plan_bound", {"context_hash":context.record_hash, "context_id":context.id, "plan_hash":old.record_hash}, key="legacy-binding", plan_id=old.id)
        assert not workbench_view(wb, tid)["actions"]["can_execute"]
        assert execution_context(wb, tid)["next_action"] == "prepare_or_repair_plan"
        with pytest.raises(WorkbenchError) as rejected:
            authorize_execution(wb, tid, plan_id=old.id, plan_hash=old.record_hash, key="legacy-approve", settings=settings)
        assert rejected.value.code == "verification_commands_invalid"
        request = PlanVersionCreateRequest(**{k:v for k,v in invalid.hash_payload().items() if k != "schema_version"}, parent_version_id=old.id)
        with pytest.raises(WorkbenchError) as rejected:
            wb.save(tid, request, context_hash=context.record_hash, key="legacy-resave")
        assert rejected.value.code == "verification_commands_invalid"
        assert wb.latest(tid, "execution_authorized") is None
    db.close()


def test_context_refresh_is_persistent_model_free_and_invalidates_old_plan(tmp_path):
    from app.planner import PlanningArchiveWorker, PlanningContextWorker
    from tests.test_workbench import ReadOnlyGitHub
    from app.workbench_view import workbench_view
    from app.contribution_workflow import authorize_execution
    from app.workbench import WorkbenchError
    db, settings, tid, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        wb.request(tid, text="实施", parent_id=None, expected_hash=None, key="old-plan")
    run_turn(db, settings, result(content))
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root); plan = wb.latest_plan(tid)
        evidence = wb.read(context.payload["artifact_id"])
        first = wb.refresh_context(tid, parent_id=plan.id, expected_hash=plan.record_hash, key="refresh")
        assert wb.refresh_context(tid, parent_id=plan.id, expected_hash=plan.record_hash, key="refresh").id == first.id
    class RefreshGitHub(ReadOnlyGitHub):
        async def resolve_repository_head(self, repository, branch=None): return "main", "a" * 40
        async def download_repository_archive(self, repository, sha, *, destination, max_bytes):
            from tests.test_execution_pipeline import ARCHIVE_BYTES
            destination.write_bytes(ARCHIVE_BYTES)
    class ContextRuntime:
        async def inspect(self, **kwargs): return evidence
    assert asyncio.run(PlanningArchiveWorker(db, settings, RefreshGitHub, worker_id="refresh-archive").run_once()).state == "succeeded"
    job = asyncio.run(PlanningContextWorker(db, settings, ContextRuntime(), worker_id="refresh-context").run_once())
    assert job.state == "succeeded" and job.result_data["model_scheduled"] is False
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert workbench_view(wb, tid)["plan_stale"]
        assert not any(j.state == "queued" for j in wb.jobs(tid))
        with pytest.raises(WorkbenchError) as error:
            authorize_execution(wb, tid, plan_id=plan.id, plan_hash=plan.record_hash, key="stale", settings=settings)
        assert error.value.code == "plan_stale"
        assert wb.latest(tid, "execution_authorized") is None
    db.close()


def test_nested_package_selection_ignores_other_ecosystems():
    paths = ["js/package.json", "python/dev-requirements.txt", "python/pkg/pyproject.toml",
             "python/pkg/test-requirements.txt", "python/pkg/src/pkg/model.py", "python/pkg/tests/test_model.py"]
    assert environment_plan(paths)["status"] == "pending_scope"
    selected = environment_plan(paths, change_paths=[paths[-2], paths[-1]])
    assert selected["status"] == "supported"
    assert selected["mode"] == "workspace-v2" and selected["project_directory"] == "python/pkg"
    assert selected["manifests"] == ["python/pkg/pyproject.toml", "python/pkg/test-requirements.txt", "python/dev-requirements.txt"]
    assert environment_plan(paths + ["js/index.ts"], change_paths=[paths[-2], "js/index.ts"])["status"] == "unsupported"


def test_signed_workspace_scope_is_frozen_and_legacy_wire_is_unchanged():
    from tests.test_sandbox_specs import _spec
    from app.sandbox_worker.specs import JobSpec, JobSpecSigner, SandboxStage, SandboxSpecError
    from app.workspace_scope import selected_workspace_paths
    legacy = _spec(SandboxPolicy())
    assert "workspace_paths" not in legacy.to_wire()
    scoped = replace(legacy, workspace_paths=("python", "CHANGELOG.md"))
    assert scoped.version == "sandbox-job-spec-v2"
    assert JobSpec.from_wire(scoped.to_wire()) == scoped
    assert scoped.spec_hash != legacy.spec_hash
    signer = JobSpecSigner(key_id="scope", signing_key=b"s" * 32)
    assert signer.verify(signer.sign(scoped), expected_policy=SandboxPolicy()) == scoped
    with pytest.raises(SandboxSpecError):
        replace(scoped, workspace_paths=("../host",))
    with pytest.raises(SandboxSpecError):
        replace(_spec(SandboxPolicy(), stage=SandboxStage.IMPLEMENT), workspace_paths=("python",))
    scope = selected_workspace_paths({"status":"supported", "project_directory":"python/pkg"},
        {"README.md", "CHANGELOG.md", "python/pkg/pyproject.toml", "js/.npmrc"}, {"python/pkg/src/pkg.py", "CHANGELOG.md"})
    assert scope == ("CHANGELOG.md", "README.md", "python")
    assert "js" not in scope


def test_rejected_claim_is_replaced_and_targeted_repair_saves_plan(tmp_path):
    db, settings, tid, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(tid, text="实现并验证", parent_id=None, expected_hash=None, key="start")
    broken = json.loads(result(content))
    broken["reply"] = "已经保存并执行，请你在本地运行测试"
    broken["plan"]["commands_to_run"][0]["working_directory"] = "../escape"
    first, _ = run_turn(db, settings, json.dumps(broken))
    assert first.result_data["repair_scheduled"]
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        answer = wb.latest(tid, "assistant_message").payload
        assert "请你在本地" not in answer["reply"]
        assert answer["plan_error"]["fields"] == ["commands_to_run.working_directory"]
    second, provider = run_turn(db, settings, result(content))
    assert second.state == "succeeded"
    prompt = json.loads(provider.invocations[0].prompt)
    assert prompt["plan_repair"]["constraint"] == "unsafe_path"
    assert prompt["execution_status"]["next_action"] == "prepare_or_repair_plan"
    assert prompt["execution_status"]["runtime"]["code_reading_verified"]
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(tid)
        assert wb.latest(tid, "assistant_message").payload["plan_receipt"]["status"] == "saved"
        assert not wb.latest(tid, "execution_authorized")
    db.close()


def test_plan_repair_is_bounded_and_plain_message_does_not_hide_failure(tmp_path):
    db, settings, tid, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(tid, text="制定执行方案", parent_id=None, expected_hash=None, key="start")
    broken = json.loads(result(content))
    broken["plan"]["commands_to_run"] *= 2
    assert run_turn(db, settings, json.dumps(broken))[0].state == "succeeded"
    for _ in range(2):
        assert run_turn(db, settings, '{"reply":"方案已落盘","plan":null}')[0].state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert len([e for e in wb.history(tid) if e.kind == "model_call"]) == 3
        assert not wb.latest_plan(tid)
        assert not any(j.state == "queued" for j in wb.jobs(tid))
        assert wb.latest(tid, "assistant_message").payload["plan_receipt"]["status"] == "rejected"
    db.close()


def test_unavailable_existing_test_has_specific_repair_diagnostic(tmp_path):
    db, settings, tid, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        evidence = wb.read(context.payload["artifact_id"])
        blocked = content.files_to_inspect[0]
        for entry in evidence["entries"]:
            if entry["path"] == blocked:
                entry.update(content=None, prior_hash=None, executable=None)
        wb.append(tid, "context_ready", {**context.payload, "artifact_id":wb.artifact(evidence)}, key="blocked-test-context")
        wb.request(tid, text="实现并验证", parent_id=None, expected_hash=None, key="blocked-test")
    assert run_turn(db, settings, result(content))[0].state == "succeeded"
    with db.session() as session:
        error = PlanningService(session, settings.artifact_root).latest(tid, "assistant_message").payload["plan_error"]
        assert error["constraint"] == "path_unavailable"
        assert error["paths"] == [blocked]
        assert "separate new test file" in error["remediation"]
    db.close()


def test_unrelated_translations_stay_discoverable_without_polluting_prompt():
    from app.code_compressor import compress_evidence_for_prompt
    path = "docs/i18n/README.zh.md"
    evidence = {"inventory":[path, "src/module.py"], "entries":[{"path":path, "content":"重复说明" * 20000}, {"path":"src/module.py", "content":"pass"}]}
    compact = compress_evidence_for_prompt(evidence)
    assert path in compact["inventory"]
    assert [e["path"] for e in compact["entries"]] == ["src/module.py"]
    focused = compress_evidence_for_prompt(evidence, focus_paths={path})
    assert any(e["path"] == path for e in focused["entries"])


def test_baseline_precedes_coding_and_post_change_verification(tmp_path):
    db, settings, tid, workers, code, review = automatic_fixture(tmp_path)
    populate_round(code, review)
    drive_round(db, settings, tid, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        baseline = wb.latest(tid, "execution_baseline_ready")
        assert baseline and baseline.payload["outcome"]["status"] == "passed"
        coding_job = next(j for j in wb.jobs(tid) if j.kind == "coding_context")
        assert baseline.created_at <= coding_job.created_at
        artifact = wb.read(baseline.payload["result_hash"])
        assert artifact["version"] == "execution-baseline-v1"
        assert artifact["commands_hash"] and artifact["explore_result_hash"]
        assert wb.latest(tid, "awaiting_acceptance")
    db.close()


def test_baseline_environment_error_stops_before_coding(tmp_path):
    from app.contribution_workflow import advance_workflows
    from app.sandbox_worker.fake import FakeVerifyRuntime
    from app.sandbox_worker.verification import VerifyCommandStatus
    db, settings, tid, workers, code, review = automatic_fixture(tmp_path)
    class MissingDependency(FakeVerifyRuntime):
        async def run(self, **kwargs):
            value = await super().run(**kwargs)
            message = "ModuleNotFoundError: No module named 'missing_test_dependency'"
            return replace(value, command_evidence=tuple(replace(e, status=VerifyCommandStatus.FAILED,
                exit_code=1, stderr_log=message, stderr_bytes=len(message),
                stderr_hash=hashlib.sha256(message.encode()).hexdigest()) for e in value.command_evidence))
    workers[-1].verifier = MissingDependency()
    for _ in range(4):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", job.error_message
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(tid, "automation_blocked").payload["reason_code"] == "environment_blocked"
        assert session.scalar(select(CodingSession)) is None
        attempt = session.scalar(select(ExecutionAttempt))
        assert ExecutionAttemptService(session).current(attempt.id).stage == "explore"
    assert not code.invocations
    db.close()


def test_v2_dependency_provenance_and_v1_hashes_are_distinct():
    from tests.test_sandbox_dependencies import _plan
    policy = SandboxPolicy()
    proxy = DependencyProxyPolicy.for_ecosystem("python")
    old = _plan(policy, proxy)
    assert "source_kind" not in old.to_wire() and "implement_result_hash" in old.to_wire()
    new = replace(old, version=WORKSPACE_DEPENDENCY_PLAN_VERSION, source_kind="explore", project_directory="python/pkg",
                  manifests=(DependencyManifest("python/pkg/pyproject.toml", "3" * 64),))
    assert "implement_result_hash" not in new.to_wire()
    assert new.to_wire()["source_result_hash"] == old.implement_result_hash
    value = DependencyPreparationResult.create(plan=new, inventory_hash="4" * 64, file_count=3, total_bytes=100)
    assert DependencyPreparationResult.from_payload(value.to_wire()) == value
    with pytest.raises(ValueError):
        replace(new, source_kind="implement")
    with pytest.raises(ValueError):
        replace(new, project_directory="../escape")


@pytest.mark.parametrize("fail_preparation",[False,True])
def test_workspace_worker_freezes_local_dependency_graph_before_coding(tmp_path, monkeypatch, fail_preparation):
    from tests import test_workbench as fixtures
    from tests.test_sandbox_dependencies import _FakeDependencyRuntime, _lease
    from app.contribution_workflow import advance_workflows
    base = fixtures._plan_content()
    target = "python/pkg/pyproject.toml"
    common = "python/workspace-common/pyproject.toml"
    monkeypatch.setattr(fixtures, "_plan_content", lambda: replace(base,
        files_to_inspect=("python/pkg/src/pkg.py", target), files_likely_to_change=("python/pkg/src/pkg.py",),
        commands_to_run=(replace(base.commands_to_run[0], argv=("python", "-m", "pytest", "-q")),)))
    entries = {}
    def context(db, settings, tid):
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            old = wb.latest(tid, "context_ready")
            evidence = wb.read(old.payload["artifact_id"])
            contents = {target:'[project]\nname="pkg"\ndependencies=["workspace-common>=0.1.0"]\n',
                        common:'[project]\nname="workspace-common"\ndependencies=[]\n'}
            for path, text in contents.items():
                entries[path] = {"path":path, "content":text, "prior_hash":hashlib.sha256(text.encode()).hexdigest(), "executable":False}
            evidence["entries"] = [entries.get(e["path"], e) for e in evidence["entries"]]
            evidence["inventory"].append(common)
            wb.append(tid, "context_ready", {**old.payload, "artifact_id":wb.artifact(evidence)}, key="workspace-context")
    db, settings, tid, workers, *_ = automatic_fixture(tmp_path, configure_models=context)
    class Inspector:
        async def inspect(self, **kwargs):
            return {"entries":[entries[p] for p in kwargs["paths"]]}
    class Proxy:
        async def start(self, *, image_digest, policy): return _lease(policy)
        async def stop(self, lease, **kwargs): pass
    class Runtime(_FakeDependencyRuntime):
        async def prepare(self, **kwargs):
            self.plan = kwargs["signed_plan"].plan
            if fail_preparation:
                from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure, dependency_diagnostic_message
                from app.sandbox_worker.dependencies import DependencyPreparationError
                diagnostic = summarize_dependency_failure("runtime_resolve",1,"No matching distribution found for timelib>=0.3.0")
                raise DependencyPreparationError("fixture",code="environment_dependency_resolution_failed",
                    safe_message=dependency_diagnostic_message(diagnostic),diagnostic=diagnostic)
            return await super().prepare(**kwargs)
    runtime = Runtime()
    workers[-1].dependencies, workers[-1].proxy, workers[-1].inspector = runtime, Proxy(), Inspector()
    assert asyncio.run(workers[0].run_once()).state == "succeeded"
    advance_workflows(db, settings)
    completed = asyncio.run(workers[-1].run_once())
    if fail_preparation:
        assert completed.state == "failed" and "timelib" in completed.error_message
        with db.session() as session:
            wb = PlanningService(session,settings.artifact_root)
            event = wb.latest(tid,"execution_preparation_failed")
            assert event.job_id == completed.id
            receipt = wb.read(event.payload["result_hash"])
            assert receipt["diagnostic"]["packages"] == ["timelib"]
            assert receipt["explore_result_hash"] == completed.payload["explore_result_hash"]
            assert wb.read(receipt["environment_spec_hash"]) == runtime.plan.to_wire()
            assert session.scalar(select(CodingSession)) is None
            assert not wb.latest(tid,"execution_baseline_ready")
        db.close()
        return
    assert completed.state == "succeeded", completed.error_message
    assert runtime.plan.local_projects == ("python/workspace-common",)
    assert {m.path:m.content_hash for m in runtime.plan.manifests}[common] == entries[common]["prior_hash"]
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(tid, "execution_baseline_ready")
        attempt = session.scalar(select(ExecutionAttempt))
        from app.workspace_scope import attempt_workspace_paths
        assert attempt_workspace_paths(session, attempt) == ("python",)
        assert session.scalar(select(CodingSession)) is None
        artifact = wb.read(wb.latest(tid, "dependencies_ready").payload["result_hash"])
        assert artifact["source_result_hash"] == runtime.plan.implement_result_hash
    db.close()


def test_baseline_migration_preserves_old_history_and_rejects_invalid_reference(tmp_path, monkeypatch):
    import app.migrations.versions as versions
    from app.database import Database
    all_migrations = versions.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(versions, "MIGRATIONS", all_migrations[:-2])
        db, settings, tid, *_ = seeded(tmp_path)
        with db.session() as session:
            before = [e.record_hash for e in PlanningService(session, settings.artifact_root).history(tid)]
    assert db.create_schema().applied == ("0034_execution_baselines", "0035_workspace_scopes")
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert [e.record_hash for e in wb.history(tid)] == before
        with pytest.raises(IntegrityError):
            wb.append(tid, "execution_baseline_ready", {"attempt_id":"missing", "result_hash":"a" * 64}, key="forged-baseline")
    db.close()


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="real isolated package proxy acceptance")
@pytest.mark.parametrize("dynamic,source_build", [(False,False),(True,False),(True,True)])
def test_real_python_workspace_baseline_and_modified_source_use_same_frozen_environment(tmp_path, dynamic, source_build):
    from app.sandbox_worker.implementation import DockerImplementRuntime, ImplementationChangeSet, ChangeOperation, ChangeOperationKind
    from app.sandbox_worker.verification import DockerVerifyRuntime
    from app.sandbox_worker.explore import RepositoryArchive
    image = os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"]
    builder_image=os.getenv('CONTRIBOS_DEPENDENCY_BUILDER_IMAGE') if source_build else None
    if source_build and not builder_image: pytest.skip('source compiler image is opt-in')
    environment = {name: os.environ[name] for name in ("DOCKER_CERT_PATH", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "HOME", "PATH", "TMPDIR") if name in os.environ}
    policy = SandboxPolicy()
    files = {
        "js/.npmrc": "registry=https://registry.npmjs.org/\n",
        "python/pkg/pyproject.toml": '[build-system]\nrequires=["setuptools>=69"]\nbuild-backend="setuptools.build_meta"\n[project]\nname="contribos-workspace-acceptance"\nversion="0.1.0"\ndependencies=["workspace-common>=0.1.0"]\n[project.optional-dependencies]\ntest=["ruff","mypy"]\n[tool.setuptools]\npackage-dir={""="src"}\npy-modules=["workspace_demo"]\n',
        "python/pkg/src/workspace_demo.py": "from workspace_common import identity\n\n\ndef add(a, b):\n    return identity(a - b)\n",
        "python/pkg/tests/test_demo.py": "from workspace_demo import add\ndef test_add():\n    assert add(2, 3) == 5\n",
        "python/workspace-common/pyproject.toml": '[build-system]\nrequires=["setuptools>=69"]\nbuild-backend="setuptools.build_meta"\n[project]\nname="workspace-common"\nversion="0.1.0"\n[tool.setuptools]\npackage-dir={""="src"}\npy-modules=["workspace_common"]\n',
        "python/workspace-common/src/workspace_common.py": "def identity(value):\n    return value\n",
    }
    if dynamic:
        files["python/pkg/pyproject.toml"] = files["python/pkg/pyproject.toml"].replace(
            'dependencies=["workspace-common>=0.1.0"]', 'dynamic=["dependencies"]'
        ) + '\n[tool.setuptools.dynamic]\ndependencies={file=["requirements-dynamic.txt"]}\n'
        files["python/pkg/requirements-dynamic.txt"] = "workspace-common>=0.1.0\npackaging>=24; python_version >= '3.8'\n"
        if source_build:
            files["python/pkg/requirements-dynamic.txt"] += 'timelib>=0.3.0\n'
    archive_path = tmp_path / "repository.tar.gz"
    with tarfile.open(archive_path, "w:gz") as bundle:
        for path, text in files.items():
            data = text.encode(); member = tarfile.TarInfo("fixture-" + "a" * 40 + "/" + path)
            member.size, member.mode = len(data), 0o644
            bundle.addfile(member, io.BytesIO(data))
    archive = RepositoryArchive(repository_full_name="fixture/fixture", base_commit_sha="a" * 40,
        archive_hash=hashlib.sha256(archive_path.read_bytes()).hexdigest(), path=archive_path, size_bytes=archive_path.stat().st_size)
    inventory = [{"path": p, "type": "file", "size": len(c.encode()), "sha256": hashlib.sha256(c.encode()).hexdigest(), "executable": False} for p,c in sorted(files.items()) if p.startswith("python/")]
    baseline_hash = content_hash(inventory)
    materializer = DockerImplementRuntime(docker_environment=environment).for_workspace_scope(("python",))
    dependencies = DockerDependencyRuntime(docker_environment=environment)
    proxy = DockerDependencyProxyRuntime(docker_environment=environment)
    verifier = DockerVerifyRuntime(docker_environment=environment)
    signer = DependencyPlanSigner(key_id="workspace-acceptance", signing_key=b"w" * 32)
    proxy_policy = DependencyProxyPolicy.for_ecosystem("python")
    plan = DependencyPreparationPlan(plan_id="workspace-acceptance", execution_attempt_id="fixture-attempt",
        implement_result_hash=content_hash({"archive": archive.archive_hash}), workspace_inventory_hash=baseline_hash,
        ecosystem="python", manifests=tuple(DependencyManifest(p, hashlib.sha256(files[p].encode()).hexdigest()) for p in ("python/pkg/pyproject.toml", "python/workspace-common/pyproject.toml")),
        runner_image_digest=image, sandbox_policy_hash=policy.policy_hash, proxy_policy_hash=proxy_policy.policy_hash,
        disk_bytes=policy.disk_bytes, version="workspace-dependency-plan-v4" if source_build else "workspace-dependency-plan-v3" if dynamic else WORKSPACE_DEPENDENCY_PLAN_VERSION,
        distribution_strategy='source-wheel-v1' if source_build else 'wheel-only-v1',builder_image_digest=builder_image,
        metadata_strategy="wheel-metadata-v1" if dynamic else "declarative-v1",
        source_kind="explore", project_directory="python/pkg", local_projects=("python/workspace-common",))
    async def run():
        # The legacy whole-repository guard still rejects this archive.
        from app.sandbox_worker.implementation import ImplementationError
        unscoped = DockerImplementRuntime(docker_environment=environment)
        rejected = await unscoped.create_workspace(image_digest=image, policy=policy)
        try:
            with pytest.raises(ImplementationError):
                await unscoped.materialize(workspace=rejected, archive=archive, policy=policy)
        finally:
            await unscoped.destroy(rejected)
        workspace = await materializer.create_workspace(image_digest=image, policy=policy)
        lease = None; dependency = None
        try:
            await materializer.materialize(workspace=workspace, archive=archive, policy=policy)
            scope_probe = await verifier.run(workspace=replace(workspace, inventory_hash=baseline_hash), commands=(SandboxCommand(command_id="scope", argv=("python", "-c", "import os; assert not os.path.exists('/workspace/js')")),), policy=policy)
            assert scope_probe.command_evidence[0].exit_code == 0
            lease = await proxy.start(image_digest=image, policy=proxy_policy)
            dependency = await dependencies.prepare(signed_plan=signer.sign(plan), signer=signer, repository_volume=workspace.volume_name,
                proxy_policy=proxy_policy, proxy_lease=lease, sandbox_policy=policy)
            await proxy.stop(lease, ignore_missing=True); lease = None
            command = SandboxCommand(command_id="regression", argv=("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"), working_directory="python/pkg")
            before = await verifier.run(workspace=replace(workspace, inventory_hash=baseline_hash), commands=(command,), policy=policy, dependency_workspace=dependency.workspace)
            assert before.command_evidence[0].exit_code == 1 and "assert" in before.command_evidence[0].stdout_log
            tools = await verifier.run(workspace=replace(workspace, inventory_hash=baseline_hash), commands=(
                SandboxCommand(command_id="venv", argv=("python", "-c", "import sys; assert sys.prefix == '/dependencies/output/venv'")),
                SandboxCommand(command_id="ruff", argv=("python", "-m", "ruff", "check", "src"), working_directory="python/pkg"),
                SandboxCommand(command_id="mypy", argv=("python", "-m", "mypy", "--explicit-package-bases", "src"), working_directory="python/pkg"),
            ), policy=policy, dependency_workspace=dependency.workspace)
            assert all(e.exit_code == 0 for e in tools.command_evidence), [(e.command_id,e.stdout_log,e.stderr_log) for e in tools.command_evidence]
            if dynamic:
                receipt = await verifier.run(workspace=replace(workspace, inventory_hash=baseline_hash), commands=(
                    SandboxCommand(command_id="metadata", argv=("python", "-c",
                        "import json, packaging; from pathlib import Path; r=json.loads(Path('/dependencies/output/project-metadata.json').read_text()); assert any(any(d.startswith('packaging') for d in p['requires_dist']) for p in r); assert all(len(p['metadata_hash'])==64 for p in r)")),
                ), policy=policy, dependency_workspace=dependency.workspace)
                assert receipt.command_evidence[0].exit_code == 0
            if source_build:
                native=await verifier.run(workspace=replace(workspace,inventory_hash=baseline_hash),commands=(
                    SandboxCommand(command_id='compiled',argv=('python','-c',
                        "import json,timelib; from pathlib import Path; assert timelib.strtodatetime('2020-01-01').year==2020; r=json.loads(Path('/dependencies/output/source-build-output/timelib/receipt.json').read_text()); assert r['request']['source_hash'] and len(r['wheel_hash'])==64")),
                ),policy=policy,dependency_workspace=dependency.workspace)
                assert native.command_evidence[0].exit_code==0
            path = "python/pkg/src/workspace_demo.py"
            change = ImplementationChangeSet(change_set_id="acceptance-change", plan_version_id="acceptance-plan",
                plan_content_hash="1" * 64, plan_record_hash="2" * 64,
                operations=(ChangeOperation(path=path, kind=ChangeOperationKind.WRITE,
                expected_prior_hash=hashlib.sha256(files[path].encode()).hexdigest(), content="from workspace_common import identity\n\n\ndef add(a, b):\n    return identity(a + b)\n", executable=False),))
            changed = await materializer.apply(workspace=workspace, change_set=change, allowed_change_paths=(path,), policy=policy)
            after = await verifier.run(workspace=replace(workspace, inventory_hash=changed.result_inventory_hash), commands=(command,), policy=policy, dependency_workspace=dependency.workspace)
            assert after.command_evidence[0].exit_code == 0
            assert after.before_inventory_hash == after.after_inventory_hash
            assert dependency.result.source_kind == "explore"
        finally:
            if lease: await proxy.stop(lease, ignore_missing=True)
            if dependency: await dependencies.destroy(dependency.workspace, ignore_missing=True)
            await materializer.destroy(workspace)
    asyncio.run(run())
