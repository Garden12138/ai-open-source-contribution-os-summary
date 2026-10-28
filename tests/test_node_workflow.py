"""Node environment evidence continues through review, acceptance and preview."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import io
import json
import os
import tarfile

import pytest
from sqlalchemy import select

from app.models import ExecutionAttempt, ReviewRun
from app.planner import PlanningService
from app.plans import PlanCommand
from app.provenance import content_hash
from app.sandbox_worker.specs import SandboxPolicy
from tests.test_node_environments import node_files, selected, FakeNodeDependencies


def node_workflow(tmp_path, monkeypatch, manager, version, *, real=False,
                  files_override=None, source_override=None, commands_override=None,
                  goal_override=None, write_paths_override=None, runtime_v2=False):
    from tests import test_workbench as fixtures
    from tests import test_execution_pipeline as archives
    from app.sandbox_worker.coding_context import CodingContextEntry
    from app.sandbox_worker.fake import FakeExploreRuntime, FakeImplementRuntime, FakeVerifyRuntime
    from app.sandbox_worker.explore import ExploreInspection
    from app.sandbox_worker.implementation import FileInventoryEntry
    from tests.test_sandbox_dependencies import _FakeDependencyRuntime, _lease
    files = node_files(manager, version) if files_override is None else files_override
    source = source_override or "packages/lib/index.js"
    def selection(values):
        if files_override is None:
            return selected(values)
        from app.execution_environment import environment_plan
        from app.execution_preflight import inspect_environment
        return inspect_environment(environment_plan(list(values), change_paths=[source]),
            {"inventory":sorted(values), "entries":[{"path":p,"content":text,
                "prior_hash":hashlib.sha256(text.encode()).hexdigest()} for p,text in values.items()]})
    original_content = fixtures._plan_content()
    content = replace(original_content, goal=goal_override or "Update the workspace fixture while preserving isolated verification",
        files_to_inspect=tuple(selection(files)["manifests"] + [source] + [p for p in files if p == "test.mjs" or "/tests/" in p]),
        files_likely_to_change=tuple(write_paths_override) if write_paths_override else (source,), questions_for_maintainer=(),
        commands_to_run=commands_override or (PlanCommand("workspace-test", "Verify current workspace source", (manager, "run", "test")),))
    monkeypatch.setattr(fixtures, "_plan_content", lambda: content)
    original_seeded = fixtures.seeded
    def seeded(path):
        db, settings, task, plan, context = original_seeded(path)
        if real:
            settings = replace(settings, workbench_runner_image=os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"])
        return db, settings, task, plan, context
    monkeypatch.setattr(fixtures, "seeded", seeded)
    def configure(db, settings, task):
        from app.planner import frozen_inputs
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            context = wb.latest(task, "context_ready")
            evidence = {"inventory": sorted(files), "truncated": False, "entries": [
                {"path": p, "content": value, "prior_hash": hashlib.sha256(value.encode()).hexdigest(),
                 "executable": False} for p, value in files.items()]}
            wb.append(task, "context_ready", {**context.payload, "image": settings.workbench_runner_image,
                "artifact_id": wb.artifact(evidence)}, key="node-context")
            repository = frozen_inputs(session, task)["repository"]
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
                for path, text in files.items():
                    entry = tarfile.TarInfo(repository.split("/")[-1] + "-" + "a" * 40 + "/" + path)
                    entry.size, entry.mode = len(text.encode()), 0o644
                    archive.addfile(entry, io.BytesIO(text.encode()))
            monkeypatch.setattr(archives, "ARCHIVE_BYTES", bundle.getvalue())
    initial = seeded(tmp_path)
    configure(*initial[:3])
    monkeypatch.setattr(fixtures, "seeded", lambda path: initial)
    db, settings, task, workers, code, review = fixtures.automatic_fixture(tmp_path, runtime_v2=runtime_v2)
    class Inspector:
        async def inspect(self, **kwargs):
            return {"entries": [{"path": p, "content": files[p], "prior_hash": hashlib.sha256(files[p].encode()).hexdigest()}
                                for p in kwargs["paths"]]}
    class Context:
        async def capture(self, *, paths, **kwargs):
            return tuple(CodingContextEntry(path=p, prior_hash=hashlib.sha256(files[p].encode()).hexdigest(),
                content=files[p], executable=False) for p in paths)
    class Explore(FakeExploreRuntime):
        async def inspect(self, *, snapshot, **kwargs):
            inventory = [{"path": p, "type": "file", "size": len(value.encode()),
                "sha256": hashlib.sha256(value.encode()).hexdigest(), "executable": False} for p, value in sorted(files.items())]
            return ExploreInspection.from_wire({"inventory_hash": content_hash(inventory), "file_count": snapshot.file_count,
                "total_bytes": snapshot.total_bytes, "sample_paths": sorted(files), "manifest_paths": selection(files)["manifests"]})
    class Implement(FakeImplementRuntime):
        async def apply(self, **kwargs):
            inspection = await super().apply(**kwargs)
            baseline = tuple(FileInventoryEntry(path=p, kind="file", size=len(value.encode()),
                sha256=hashlib.sha256(value.encode()).hexdigest(), executable=False) for p, value in sorted(files.items()))
            result = tuple(next((e for e in inspection.result_inventory if e.path == entry.path), entry) for entry in baseline)
            return replace(inspection, baseline_inventory=baseline, result_inventory=result,
                baseline_inventory_hash=content_hash([e.to_wire() for e in baseline]),
                result_inventory_hash=content_hash([e.to_wire() for e in result]),
                baseline_file_count=len(baseline), result_file_count=len(result),
                baseline_total_bytes=sum(e.size for e in baseline), result_total_bytes=sum(e.size for e in result))
    class Proxy:
        async def start(self, *, policy, **kwargs): return _lease(policy)
        async def stop(self, *args, **kwargs): pass
    if real:
        from app.sandbox_worker.implementation import DockerImplementRuntime
        from app.sandbox_worker.explore import DockerExploreRuntime
        from app.sandbox_worker.verification import DockerVerifyRuntime
        from app.sandbox_worker.coding_context import DockerCodingContextRuntime
        from app.sandbox_worker.dependencies import DockerDependencyRuntime, DockerDependencyProxyRuntime
        from app.sandbox_worker.planning_context import DockerPlanningContextRuntime
        environment = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CERT_PATH", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY") if k in os.environ}
        materializer = DockerImplementRuntime(docker_environment=environment)
        verifier = DockerVerifyRuntime(docker_environment=environment)
        workers[0].explore_runtime = DockerExploreRuntime(docker_environment=environment)
        workers[1].runtime = DockerCodingContextRuntime(docker_environment=environment)
        workers[-1].inspector = DockerPlanningContextRuntime(docker_environment=environment)
        workers[-1].dependencies = DockerDependencyRuntime(docker_environment=environment)
        workers[-1].proxy = DockerDependencyProxyRuntime(docker_environment=environment)
    else:
        materializer, verifier = Implement(), FakeVerifyRuntime()
        workers[0].explore_runtime = Explore()
        workers[1].runtime = Context()
        workers[-1].inspector, workers[-1].dependencies, workers[-1].proxy = Inspector(), FakeNodeDependencies(), Proxy()
    workers[0].implement_runtime, workers[0].verify_runtime = materializer, verifier
    workers[-1].materializer, workers[-1].verifier = materializer, verifier
    code.contents.extend([json.dumps({"reply": "按当前方案修改。"}), json.dumps({"summary": "Update current workspace source",
        "operations": [{"path": source, "kind": "write", "expected_prior_hash": hashlib.sha256(files[source].encode()).hexdigest(),
                        "content": files[source].replace("before", "after"), "executable": False}]})])
    from tests.test_publication_workbench import REVIEW
    review.contents.append(REVIEW)
    return db, settings, task, workers, code, review


@pytest.mark.parametrize("manager,version", [("npm", "10.9.4"), ("yarn", "1.22.22"), ("yarn", "4.9.2"), ("pnpm", "10.11.0")])
def test_node_environment_reaches_review_acceptance_and_readonly_preview(tmp_path, monkeypatch, manager, version):
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, manager, version)
    verify_to_preview(db, settings, task, workers, code, review)


def failed_preparation(tmp_path, monkeypatch, *, tools=False):
    from app.contribution_workflow import advance_workflows
    from app.sandbox_worker.verification import VerificationDependencyIntegrityError
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, "yarn", "1.22.22")
    original = workers[-1].verifier
    class IntegrityFailure:
        async def run(self, **kwargs):
            raise VerificationDependencyIntegrityError("Test supervisor inventory mismatch")
    original_dependencies = workers[-1].dependencies
    if tools:
        from app.sandbox_worker.dependencies import DependencyPreparationError
        class ToolsFailure(FakeNodeDependencies):
            async def prepare(self, **kwargs):
                raise DependencyPreparationError("Test tool acquisition failure", code="environment_node_preparation_failed",
                    diagnostic={"version":"dependency-failure-v1","phase":"node_tools","reason_code":"subprocess_failed","exit_code":1,"packages":[]})
        workers[-1].dependencies = ToolsFailure()
    else:
        workers[-1].verifier = IntegrityFailure()
    failed = None
    for _ in range(4):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job and job.state == "failed": failed = job
        advance_workflows(db, settings)
        if failed: break
    assert failed and failed.error_code == ("environment_node_preparation_failed" if tools else "environment_baseline_integrity_failed")
    if not tools: assert "冻结依赖清单" in failed.error_message
    assert not code.invocations and not review.invocations
    workers[-1].verifier = original
    workers[-1].dependencies = original_dependencies
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        diagnostic = wb.latest(task, "execution_preparation_failed")
        assert diagnostic.payload["diagnostic"]["reason_code"] == ("subprocess_failed" if tools else "dependency_integrity_changed")
        assert wb.active_blocker(task)
    return db, settings, task, workers, code, review, failed.id


def runtime_repair_proof(failed_id, *, tools=False):
    from pathlib import Path
    proof = {"version": "node-inventory-runtime-repair-v1", "failed_job_id": failed_id,
        "supervisor_exit_code": 89,
        "supervisor_log": "Verification container failed: code=89; execution_prepare job=" + failed_id,
        "source_hashes": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in
            ("app/sandbox_worker/dependencies.py", "app/sandbox_worker/verification.py")},
        "regression_hash": "a" * 64}
    if tools:
        proof.pop("supervisor_exit_code"); proof.pop("supervisor_log")
        proof.update(version="node-tools-availability-v1", probe={"version":"node-tools-probe-v1", "exit_code":0,
            "known_codes":[], "registry":"https://registry.npmjs.org", "manager":"yarn", "manager_version":"1.22.22",
            "npm_version":"10.9.4", "scripts_disabled":True,"output_bytes":24})
    return proof


@pytest.mark.parametrize("tools", [False, True])
def test_proven_runtime_repair_resumes_new_preparation_without_new_approval(tmp_path, monkeypatch, tools):
    from app.execution_recovery import resume_preparation_after_runtime_repair
    from tests.test_workbench import drive_round
    db, settings, task, workers, code, review, failed_id = failed_preparation(tmp_path, monkeypatch, tools=tools)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        approval = wb.latest(task, "execution_authorized").record_hash
        replacement = resume_preparation_after_runtime_repair(wb, failed_id, settings, runtime_repair_proof(failed_id, tools=tools))
        assert replacement.id != failed_id and replacement.state == "queued"
        assert resume_preparation_after_runtime_repair(wb, failed_id, settings, runtime_repair_proof(failed_id, tools=tools)).id == replacement.id
    drive_round(db, settings, task, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task, "awaiting_acceptance") and not wb.active_blocker(task)
        assert wb.latest(task, "execution_authorized").record_hash == approval
        assert next(j for j in wb.jobs(task) if j.id == failed_id).state == "failed"
        assert len(list(session.scalars(select(ExecutionAttempt)))) == 1
        assert len(code.invocations) == 2 and len(review.invocations) == 1
        assert len([e for e in wb.history(task) if e.kind == "preparation_runtime_retry_requested"]) == 1
    asyncio.run(cleanup_node_workflow(db, settings, workers))
    db.close()


@pytest.mark.parametrize("invalid", ["source", "exit", "stop"])
def test_runtime_repair_rejects_unproven_or_stopped_preparation(tmp_path, monkeypatch, invalid):
    from app.execution_recovery import resume_preparation_after_runtime_repair
    from app.contribution_workflow import stop_workflow
    from app.workbench import WorkbenchError
    db, settings, task, workers, code, review, failed_id = failed_preparation(tmp_path, monkeypatch)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        proof = runtime_repair_proof(failed_id)
        if invalid == "source": proof["source_hashes"]["app/sandbox_worker/verification.py"] = "0" * 64
        if invalid == "exit": proof["supervisor_exit_code"] = 1
        if invalid == "stop": stop_workflow(wb, task, key="stop-before-runtime-repair")
        with pytest.raises(WorkbenchError):
            resume_preparation_after_runtime_repair(wb, failed_id, settings, proof)
        assert not wb.latest(task, "preparation_runtime_retry_requested")
    asyncio.run(cleanup_node_workflow(db, settings, workers))
    db.close()


def test_tool_availability_probe_must_match_the_exact_frozen_manager(tmp_path, monkeypatch):
    from app.execution_recovery import resume_preparation_after_runtime_repair
    from app.workbench import WorkbenchError
    db, settings, task, workers, code, review, failed_id = failed_preparation(tmp_path, monkeypatch, tools=True)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        proof = runtime_repair_proof(failed_id, tools=True)
        proof["probe"]["manager_version"] = "4.9.2"
        with pytest.raises(WorkbenchError):
            resume_preparation_after_runtime_repair(wb, failed_id, settings, proof)
        assert not wb.latest(task, "preparation_runtime_retry_requested")
    asyncio.run(cleanup_node_workflow(db, settings, workers))
    db.close()


def test_preparation_recovery_cannot_extend_its_two_replacement_budget(tmp_path, monkeypatch):
    from app.execution_recovery import resume_preparation_after_runtime_repair
    from app.contribution_workflow import advance_workflows
    from app.sandbox_worker.verification import VerificationDependencyIntegrityError
    from app.workbench import WorkbenchError
    db, settings, task, workers, code, review, failed_id = failed_preparation(tmp_path, monkeypatch)
    class StillFailing:
        async def run(self, **kwargs): raise VerificationDependencyIntegrityError("Test repeated mismatch")
    workers[-1].verifier = StillFailing()
    for _ in range(2):
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            replacement = resume_preparation_after_runtime_repair(wb, failed_id, settings, runtime_repair_proof(failed_id))
        failed = asyncio.run(workers[-1].run_once())
        assert failed.id == replacement.id and failed.state == "failed"
        failed_id = failed.id
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        with pytest.raises(WorkbenchError):
            resume_preparation_after_runtime_repair(wb, failed_id, settings, runtime_repair_proof(failed_id))
        assert len([e for e in wb.history(task) if e.kind == "preparation_runtime_retry_requested"]) == 2
        assert not code.invocations and not review.invocations
    asyncio.run(cleanup_node_workflow(db, settings, workers))
    db.close()


def verify_to_preview(db, settings, task, workers, code, review, *, expected_runtime=None):
    from tests.test_workbench import drive_round
    from app.execution_acceptance import acceptance_view, accept_execution
    from app.publication import request_publication, PublicationWorker
    from tests.test_publication_workbench import FakeTransport, FakeCheckout
    try:
        drive_round(db, settings, task, workers)
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            assert wb.latest(task, "awaiting_acceptance")
            attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id == task))
            baseline = wb.read(wb.latest(task, "execution_baseline_ready").payload["result_hash"])
            environment = wb.read(baseline["environment_spec_hash"])
            assert environment["version"] == "workspace-dependency-plan-v5"
            assert baseline["outcome"]["status"] == "passed"
            frozen_dependency = baseline["dependency_result_hash"]
            audited_review = session.scalar(select(ReviewRun).where(ReviewRun.execution_attempt_id == attempt.id))
            verify = wb.read(audited_review.verify_result_hash)
            assert verify["dependency_result_hash"] == frozen_dependency
            assert audited_review.verdict == "pass"
            assert all(j.state == "succeeded" for j in wb.jobs(task))
            acceptance = acceptance_view(wb, task)
            assert acceptance["available"] and not acceptance["accepted"]
            accept_execution(wb, task, binding_hash=acceptance["binding_hash"], text="确认测试结果", key="node-human")
            request_publication(wb, task, title="fix: update workspace fixture", body="## Summary\nUpdate fixture.\n\n## Testing\nWorkspace verification passed.\n", key="node-preview")
            transport = FakeTransport(attempt.repository_full_name)
        publisher = PublicationWorker(db, replace(settings, publisher_mode="gh"), worker_id="node-preview",
            transport=transport, checkout=FakeCheckout())
        job = asyncio.run(publisher.run_once())
        assert job.state == "succeeded", job.error_message
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            assert wb.latest(task, "publication_prepared")
            assert not wb.latest(task, "publication_confirmed")
            assert all(call[0] == "GET" for call in transport.calls)
            assert len(review.invocations) == 1
            assert len(code.invocations) == 2
            envelope = json.loads(review.invocations[0].prompt.split("=", 1)[1])
            assert envelope["frozen_environment"]["dependency_result_hash"] == frozen_dependency
            assert envelope["frozen_environment"]["preparation"]["built_offline"] is True
            if expected_runtime:
                assert envelope["frozen_environment"]["preparation"]["runtime_version"] == expected_runtime
    finally:
        asyncio.run(cleanup_node_workflow(db, settings, workers))
        db.close()


async def cleanup_node_workflow(db, settings, workers):
    from app.models import ExecutionStageVersion
    from app.sandbox_worker.dependency_jobs import DependencyJobWorker
    from app.sandbox_worker.implementation import DisposableWorkspace
    await DependencyJobWorker(db, settings, workers[-1].dependencies, workers[-1].proxy, worker_id="cleanup").cleanup()
    await workers[-1].cleanup()
    with db.session() as session:
        seen = set()
        for stage in session.scalars(select(ExecutionStageVersion)):
            if not stage.workspace_ref or stage.workspace_ref in seen:
                continue
            attempt = session.get(ExecutionAttempt, stage.execution_attempt_id)
            await workers[0].implement_runtime.destroy(DisposableWorkspace(stage.workspace_id, stage.workspace_ref,
                attempt.runner_image_digest, attempt.sandbox_policy_hash))
            seen.add(stage.workspace_ref)


def cache_failed_baseline(tmp_path, monkeypatch):
    from app.contribution_workflow import advance_workflows
    from app.sandbox_worker.fake import FakeVerifyRuntime
    from app.sandbox_worker.verification import VerifyCommandStatus
    from app.sandbox_worker.workspace_jobs import preparation_receipt
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, "yarn", "1.22.22")
    original = workers[-1].verifier
    class CacheFailure(FakeVerifyRuntime):
        async def run(self, **kwargs):
            value = await super().run(**kwargs)
            text = "Error: ENOSPC: no space left on device"
            return replace(value, command_evidence=tuple(replace(e, status=VerifyCommandStatus.FAILED,
                exit_code=1, stderr_log=text, stderr_bytes=len(text), stderr_hash=hashlib.sha256(text.encode()).hexdigest())
                for e in value.command_evidence))
    workers[-1].verifier = CacheFailure()
    for _ in range(4):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job: assert job.state == "succeeded"
        advance_workflows(db, settings)
    workers[-1].verifier = original
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        attempt = session.scalar(select(ExecutionAttempt))
        receipt = preparation_receipt(wb, attempt.id)
        baseline = wb.read(receipt.payload["result_hash"])
        assert baseline["outcome"]["status"] == "environment_blocked"
        from pathlib import Path
        proof = wb.artifact({"version":"node-runtime-readiness-proof-v1", "attempt_hash":attempt.record_hash,
            "dependency_hash":baseline["dependency_result_hash"],
            "source_hashes":{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in
                ("app/sandbox_worker/node_execution.py","app/execution_outcome.py")},
            "probe_hash":wb.artifact({"diagnostic_only":True,"checks":[{"command":"workspace-test","status":"passed","exit":0}]})})
        session.commit()
    return db, settings, task, workers, code, review, attempt.id, receipt.record_hash, proof


def test_cached_rebaseline_revalidates_bundle_without_acquisition_and_reaches_review(tmp_path, monkeypatch):
    from app.sandbox_worker.workspace_jobs import enqueue_cached_rebaseline, preparation_receipt
    from tests.test_workbench import drive_round
    db, settings, task, workers, code, review, attempt_id, previous, proof = cache_failed_baseline(tmp_path, monkeypatch)
    class NoAcquisition(FakeNodeDependencies):
        async def prepare(self, **kwargs): raise AssertionError("Frozen dependencies must be reused")
    workers[-1].dependencies = NoAcquisition()
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        job = enqueue_cached_rebaseline(wb, attempt_id, settings, proof)
        assert enqueue_cached_rebaseline(wb, attempt_id, settings, proof).id == job.id
    drive_round(db, settings, task, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        baseline = wb.read(preparation_receipt(wb, attempt_id).payload["result_hash"])
        assert baseline["replaces_baseline_hash"] == previous
        assert baseline["runtime_rebaseline_proof_hash"] == proof
        assert baseline["outcome"]["status"] == "passed"
        assert wb.latest(task, "awaiting_acceptance")
    asyncio.run(cleanup_node_workflow(db, settings, workers)); db.close()


@pytest.mark.parametrize("invalid", ["probe", "disposed"])
def test_cached_rebaseline_rejects_unproven_or_disposed_bundle(tmp_path, monkeypatch, invalid):
    from app.sandbox_worker.workspace_jobs import enqueue_cached_rebaseline
    from app.workbench import WorkbenchError
    db, settings, task, workers, code, review, attempt_id, previous, proof = cache_failed_baseline(tmp_path, monkeypatch)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        if invalid == "disposed":
            allocation = wb.latest(task, "dependency_workspace")
            wb.append(task,"dependency_workspace_disposed",{"allocation_hash":allocation.record_hash},key="dispose-before-cache-reuse")
        else:
            data = wb.read(proof)
            data["probe_hash"] = wb.artifact({"diagnostic_only":True,"checks":[{"command":"workspace-test","status":"failed","exit":1}]})
            proof = wb.artifact(data)
        with pytest.raises(WorkbenchError): enqueue_cached_rebaseline(wb, attempt_id, settings, proof)
        assert not wb.latest(task,"runtime_rebaseline_requested")
    asyncio.run(cleanup_node_workflow(db, settings, workers)); db.close()


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="real containers are opt-in")
@pytest.mark.parametrize("manager,version", [("npm", "10.9.4"), ("yarn", "1.22.22"), ("yarn", "4.9.2"), ("pnpm", "10.11.0")])
def test_real_node_workflow_through_independent_review_and_publication_preview(tmp_path, monkeypatch, manager, version):
    # Model completions and GitHub reads are deterministic fakes. Repository
    # preparation, builds, baseline, changes and Verify use real isolated OCI.
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, manager, version, real=True)
    verify_to_preview(db, settings, task, workers, code, review)


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1" or not os.getenv("DEPENDENCY_BUILDER_IMAGE"),
                    reason="real signed compiler image is opt-in")
def test_real_native_workspace_build_uses_signed_image_through_verify_and_review(tmp_path, monkeypatch):
    original_files = node_files
    def native_files(manager, version):
        files = original_files(manager, version)
        files["native.c"] = "int main(void) { return 0; }\n"
        files["prepare.cjs"] += "require('child_process').execFileSync('cc',['native.c','-o','generated-tool']);\n"
        # Native dependencies execute from their frozen read-only volume. The
        # writable per-run source tmpfs intentionally retains noexec.
        files["test.mjs"] += "import {execFileSync} from 'node:child_process'; execFileSync('/dependencies/output/project/generated-tool');\n"
        return files
    monkeypatch.setitem(globals(), "node_files", native_files)
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, "yarn", "1.22.22", real=True)
    verify_to_preview(db, settings, task, workers, code, review)


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="real runtime provisioning is opt-in")
def test_real_project_runtime_pin_is_provisioned_and_preserved_through_review(tmp_path, monkeypatch):
    original_files = node_files
    def pinned_files(manager, version):
        files = original_files(manager, version)
        files[".node-version"] = "20.19.0\n"
        return files
    monkeypatch.setitem(globals(), "node_files", pinned_files)
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, "yarn", "1.22.22", real=True)
    verify_to_preview(db, settings, task, workers, code, review, expected_runtime="20.19.0")


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="real link inventory regression is opt-in")
def test_real_unscoped_workspace_link_preserves_frozen_dependency_inventory(tmp_path, monkeypatch):
    original_files = node_files
    def unscoped_files(manager, version):
        return {p: text.replace("@fixture/lib", "fixture-lib") for p, text in original_files(manager, version).items()}
    monkeypatch.setitem(globals(), "node_files", unscoped_files)
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, "yarn", "1.22.22", real=True)
    verify_to_preview(db, settings, task, workers, code, review)


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="real temporary storage regression is opt-in")
def test_real_tool_temporary_data_uses_execution_tmpfs(tmp_path, monkeypatch):
    original_files = node_files
    def temporary_files(manager, version):
        files = original_files(manager, version)
        files["test.mjs"] += "import {tmpdir} from 'node:os'; const p=tmpdir()+'/temporary-data'; assert.ok(tmpdir().startsWith('/execution/')); fs.writeFileSync(p,Buffer.alloc(32*1024*1024)); fs.unlinkSync(p);\n"
        return files
    monkeypatch.setitem(globals(), "node_files", temporary_files)
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, "yarn", "1.22.22", real=True)
    verify_to_preview(db, settings, task, workers, code, review)


@pytest.mark.parametrize("manager,version", [("yarn", "1.22.22"), ("pnpm", "10.11.0")])
@pytest.mark.parametrize("physical", [False, True])
def test_reclaimed_node_environment_rebuilds_baseline_and_resumes_original_implementation(tmp_path, monkeypatch, manager, version, physical):
    reclaimed_workflow(tmp_path, monkeypatch, manager, version, physical=physical)


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="real physical volume loss is opt-in")
def test_real_missing_dependency_volume_rebuilds_baseline_and_finishes_review(tmp_path, monkeypatch):
    reclaimed_workflow(tmp_path, monkeypatch, "yarn", "1.22.22", real=True, physical=True)


def reclaimed_workflow(tmp_path, monkeypatch, manager, version, *, real=False, physical=False):
    from app.contribution_workflow import _advance, advance_workflows
    from app.execution_environment import schedule_verification
    from app.executions import ExecutionAttemptService
    from app.providers.runtime import resolve_job_spec_signer
    db, settings, task, workers, code, review = node_workflow(tmp_path, monkeypatch, manager, version, real=real)
    monkeypatch.setattr(workers[0], "_schedule_verify", lambda *args: None)
    for _ in range(10):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job: assert job.state == "succeeded", job.error_message
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            attempt = session.scalar(select(ExecutionAttempt))
            stage = ExecutionAttemptService(session).current(attempt.id)
            if stage.stage == "implement" and stage.status == "succeeded":
                schedule_verification(wb, attempt.id, resolve_job_spec_signer(settings))
                allocation = wb.latest(task, "dependency_workspace")
                if physical:
                    if real:
                        from app.sandbox_worker.dependency_jobs import load_dependency
                        asyncio.run(workers[-1].dependencies.destroy(load_dependency(wb, attempt.id).workspace))
                    else:
                        from app.sandbox_worker.verification import VerificationEnvironmentUnavailable
                        class Missing:
                            async def run(self, **kwargs):
                                raise VerificationEnvironmentUnavailable("Test trusted inventory probe")
                        original_verifier = workers[0].verify_runtime
                        monkeypatch.setattr(workers[0], "verify_runtime", Missing())
                else:
                    wb.append(task, "dependency_workspace_disposed", {"allocation_hash": allocation.record_hash}, key="test-disposal")
                original_approval = wb.latest(task, "execution_authorized").record_hash
                original_baseline = wb.latest(task, "execution_baseline_ready").payload["result_hash"]
                break
            _advance(wb, task, settings)
    failed = asyncio.run(workers[0].run_once())
    assert failed.state == "failed" and failed.error_code == "environment_stale"
    if physical and not real:
        monkeypatch.setattr(workers[0], "verify_runtime", original_verifier)
    advance_workflows(db, settings)
    restored = asyncio.run(workers[-1].run_once())
    assert restored.state == "succeeded", restored.error_message
    advance_workflows(db, settings)
    for _ in range(4):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job: assert job.state == "succeeded", job.error_message
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task, "awaiting_acceptance")
        assert wb.latest(task, "execution_authorized").record_hash == original_approval
        new_baseline = wb.read(wb.latest(task, "execution_baseline_ready").payload["result_hash"])
        assert new_baseline["replaces_baseline_hash"]
        assert wb.latest(task, "execution_baseline_ready").payload["result_hash"] != original_baseline
        assert len(list(session.scalars(select(ExecutionAttempt)))) == 1
        assert len(code.invocations) == 2 and len(review.invocations) == 1
        assert not wb.latest(task, "publication_confirmed")
        if physical:
            assert wb.latest(task, "dependency_workspace_invalidated").payload["allocation_hash"] == allocation.record_hash
    asyncio.run(cleanup_node_workflow(db, settings, workers))
    db.close()
