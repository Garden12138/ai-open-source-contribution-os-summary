"""Prepare a frozen environment and run baseline before requesting code changes."""
from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any
from sqlalchemy.orm import Session
from app.config import Settings
from app.database import Database
from app.archives import RepositoryArchiveStore
from app.changesets import plan_sandbox_commands
from app.executions import ExecutionAttemptService
from app.execution_environment import task_environment
from app.execution_outcome import classify_verification
from app.jobs import TERMINAL_STATES
from app.models import Job, ExecutionAttempt, WorkbenchEvent
from app.plans import PlanVersionService
from app.provenance import content_hash
from app.sandbox_worker.dependencies import (
    DependencyManifest, DependencyPreparationPlan, DependencyPlanSigner, DependencyPreparationError,
    DependencyProxyPolicy, WORKSPACE_DEPENDENCY_PLAN_VERSION,
    DYNAMIC_WORKSPACE_DEPENDENCY_PLAN_VERSION,
    SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION,
)
from app.sandbox_worker.specs import SandboxPolicy
from app.sandbox_worker.implementation import ImplementRuntime
from app.sandbox_worker.verification import VerifyRuntime
from app.workbench import Workbench, WorkbenchError
from app.workbench_worker import WorkbenchJobWorker

PREPARE = "execution_prepare"


def preparation_receipt(wb: Workbench, attempt_id: str) -> WorkbenchEvent | None:
    attempt = ExecutionAttemptService(wb.session).get_verified(attempt_id)
    return next((e for e in reversed(wb.history(attempt.task_id))
                 if e.kind == "execution_baseline_ready" and e.payload["attempt_id"] == attempt_id), None)


def preparation_needs_restore(wb: Workbench, receipt: WorkbenchEvent) -> bool:
    """A disposed environment cannot be reused just because its baseline is durable."""
    baseline = wb.read(receipt.payload["result_hash"])
    dependency_hash = baseline.get("dependency_result_hash")
    if dependency_hash is None:
        return False
    events = wb.history(receipt.task_id)
    ready = next((e for e in reversed(events) if e.kind == "dependencies_ready"
                  and e.job_id == receipt.job_id and e.payload["result_hash"] == dependency_hash), None)
    allocation = next((e for e in reversed(events) if e.kind == "dependency_workspace"
                       and e.job_id == receipt.job_id and ready
                       and {k: v for k, v in e.payload["workspace"].items() if k != "inventory_hash"}
                       == {k: v for k, v in ready.payload["workspace"].items() if k != "inventory_hash"}), None)
    if allocation is None:
        raise WorkbenchError("冻结依赖分配记录缺失，不能恢复执行", code="environment_stale")
    return any(e.kind == "dependency_workspace_disposed"
               and e.payload["allocation_hash"] == allocation.record_hash for e in events)


def enqueue_preparation(wb: Workbench, attempt_id: str, *, replaces: WorkbenchEvent | None = None) -> Job:
    attempts = ExecutionAttemptService(wb.session)
    attempt = attempts.get_verified(attempt_id)
    stage = attempts.current(attempt_id)
    if stage.stage != "explore" or stage.status != "succeeded":
        raise WorkbenchError("环境准备需要完成隔离代码探索", code="environment_stale")
    payload = {
        "version": "workspace-preparation-job-v1", "execution_attempt_id": attempt_id,
        "explore_result_hash": stage.result_hash,
    }
    key = "prepare-workspace:" + attempt_id
    if replaces is not None:
        if replaces.payload["attempt_id"] != attempt_id or not preparation_needs_restore(wb, replaces):
            raise WorkbenchError("旧基线无需恢复或不属于当前执行", code="environment_stale")
        payload["replaces_baseline_hash"] = replaces.record_hash
        key += ":restore:" + replaces.id
    return wb.enqueue(attempt.task_id, PREPARE, payload, key=key, timeout_seconds=1800)


class WorkspacePreparationWorker(WorkbenchJobWorker):
    kinds = (PREPARE,)

    def __init__(self, database: Database, settings: Settings, materializer: ImplementRuntime,
                 verifier: VerifyRuntime, dependencies: Any, proxy: Any, *, worker_id: str) -> None:
        super().__init__(database, worker_id=worker_id)
        self.settings = settings
        self.materializer, self.verifier = materializer, verifier
        self.dependencies, self.proxy = dependencies, proxy
        self.signer = DependencyPlanSigner(key_id=settings.sandbox_job_spec_key_id,
                                          signing_key=settings.sandbox_job_spec_signing_key)

    async def cleanup(self) -> None:
        from sqlalchemy import select
        from app.models import WorkbenchEvent
        from app.sandbox_worker.implementation import DisposableWorkspace
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            for event in session.scalars(select(WorkbenchEvent).where(WorkbenchEvent.kind == "preparation_workspace")):
                if any(e.idempotency_key == "preparation-disposed:" + event.id for e in wb.history(event.task_id)):
                    continue
                job = session.get(Job, event.job_id)
                if job is None or job.state not in TERMINAL_STATES:
                    continue
                await self.materializer.destroy(DisposableWorkspace(**event.payload["workspace"]))
                wb.append(event.task_id, "preparation_workspace_disposed", {"allocation_hash": event.record_hash},
                          key="preparation-disposed:" + event.id)

    def current(self, session: Session, job: Job) -> tuple[Workbench, ExecutionAttempt, dict[str, Any]]:
        self.check(session, job)
        wb = Workbench(session, self.settings.artifact_root)
        attempts = ExecutionAttemptService(session)
        attempt = attempts.get_verified(job.payload["execution_attempt_id"])
        stage = attempts.current(attempt.id)
        recovery = job.payload.get("recovery_stage_hash")
        if recovery:
            from app.execution_recovery import require_recovery_authorization
            authorization = require_recovery_authorization(wb, attempt, self.settings)
            previous = next((e for e in wb.history(attempt.task_id)
                if e.record_hash == job.payload.get("replaces_baseline_hash")
                and e.kind == "execution_baseline_ready" and e.payload["attempt_id"] == attempt.id), None)
            if (stage.stage != "verify" or stage.status != "failed" or stage.record_hash != recovery
                    or authorization.record_hash != job.payload.get("authorization_hash")
                    or previous is None or not preparation_needs_restore(wb, previous)
                    or wb.read(previous.payload["result_hash"])["explore_result_hash"] != job.payload["explore_result_hash"]):
                raise WorkbenchError("验证环境恢复输入已过期。", code="environment_stale")
        elif stage.stage != "explore" or stage.status != "succeeded" or stage.result_hash != job.payload["explore_result_hash"]:
            raise WorkbenchError("环境准备输入已过期", code="environment_stale")
        if wb.latest(attempt.task_id, "stop_requested"):
            stop = wb.latest(attempt.task_id, "stop_requested")
            authorization = wb.latest(attempt.task_id, "execution_authorized")
            if authorization is None or stop.sequence > authorization.sequence:
                raise WorkbenchError("执行已停止", code="environment_stale")
        authorization=wb.latest(attempt.task_id,"execution_authorized")
        if authorization and authorization.payload.get("preflight_hash"):
            from app.execution_preflight import task_preflight
            preflight=task_preflight(wb,attempt.task_id)
            if (authorization.plan_version_id!=attempt.plan_version_id or
                    authorization.payload['context']['archive_hash']!=attempt.repository_archive_hash or
                    authorization.payload['context']['image']!=attempt.runner_image_digest or
                    preflight['status']!='ready' or preflight['report_hash']!=authorization.payload['preflight_hash']):
                raise WorkbenchError('环境能力或冻结依据已变化，需要重新确认方案。',code='environment_stale')
        return wb, attempt, wb.read(job.payload["explore_result_hash"])

    async def execute(self, job: Job) -> dict:
        with self.database.session() as session:
            wb, attempt, explore = self.current(session, job)
            selected = task_environment(wb, attempt.task_id)
            from app.execution_preflight import environment_inputs_hash
            environment_hash = environment_inputs_hash(wb.latest(attempt.task_id,"context_ready"),selected)
            if selected["status"] not in {"supported", "builtin"}:
                raise WorkbenchError(selected["reason"], code="environment_unsupported")
            approved = PlanVersionService(session).get_verified(attempt.plan_version_id)
            commands = plan_sandbox_commands(approved)
            context = wb.latest(attempt.task_id, "context_ready")
            frozen_inventory = set(wb.read(context.payload["artifact_id"])["inventory"])
            from app.verification_commands import baseline_commands
            commands = baseline_commands(commands, frozen_inventory, set(approved.files_likely_to_change))
            if not commands:
                raise WorkbenchError("缺少可在修改前运行的验证命令，请修订方案。", code="baseline_commands_missing")
            archive = RepositoryArchiveStore(self.settings.artifact_root).lookup(
                attempt.repository_archive_hash, repository_full_name=attempt.repository_full_name,
                base_commit_sha=attempt.base_commit_sha)
            policy = SandboxPolicy()
            from app.workspace_scope import attempt_workspace_paths
            scope = attempt_workspace_paths(session, attempt)
        materializer = self.materializer.for_workspace_scope(scope) if scope else self.materializer
        workspace = await materializer.create_workspace(image_digest=attempt.runner_image_digest, policy=policy)
        dependency = None
        environment_spec = None
        retained = False
        try:
            with self.database.session() as session:
                wb, _, _ = self.current(session, job)
                wb.append(attempt.task_id, "preparation_workspace", {"attempt_id": attempt.id, "workspace": asdict(workspace)},
                          key="preparation-workspace:" + job.id, job_id=job.id)
            await materializer.materialize(workspace=workspace, archive=archive, policy=policy)
            workspace = replace(workspace, inventory_hash=explore["inventory_hash"])
            if selected["status"] == "supported":
                # Manifest hashes come from the credential-free archive reader;
                # this reader never evaluates repository build/install code.
                from app.sandbox_worker.planning_context import DockerPlanningContextRuntime
                inspector = getattr(self, "inspector", None) or DockerPlanningContextRuntime(
                    docker_environment=self.materializer.docker_environment)
                evidence = await inspector.inspect(archive=archive, image_digest=attempt.runner_image_digest,
                    policy=policy, query="", paths=tuple(selected["manifests"]))
                entries = {entry["path"]: entry for entry in evidence["entries"] if entry.get("content") is not None}
                local_projects = []
                if selected.get("mode") == "workspace-v2" and selected["ecosystem"] == "python":
                    import re, tomllib
                    inventory = frozen_inventory
                    pending = [selected["project_directory"] + "/pyproject.toml"]
                    if selected["project_directory"] == ".": pending = ["pyproject.toml"]
                    visited = set()
                    while pending:
                        manifest_path = pending.pop()
                        if manifest_path in visited: continue
                        visited.add(manifest_path)
                        if manifest_path not in entries:
                            extra = await inspector.inspect(archive=archive, image_digest=attempt.runner_image_digest,
                                policy=policy, query="", paths=(manifest_path,))
                            entries.update({entry["path"]: entry for entry in extra["entries"] if entry.get("content") is not None})
                        if manifest_path not in entries:
                            raise WorkbenchError("本地依赖清单不可读取", code="environment_manifest_unavailable")
                        metadata = tomllib.loads(entries[manifest_path]["content"])
                        from app.execution_preflight import python_development_requirements
                        declared = list(metadata.get("project", {}).get("dependencies", []))
                        if selected.get("preparation_version") == "python-workspace-preparation-v2":
                            declared += python_development_requirements(metadata)
                        for requirement in declared:
                            match = re.match(r"^([A-Za-z0-9_.-]+)", requirement)
                            if not match: continue
                            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
                            candidates = [p for p in inventory if p.endswith("/pyproject.toml")
                                and re.sub(r"[-_.]+", "-", p.split("/")[-2]).lower() == name]
                            if len(candidates) > 1:
                                raise WorkbenchError("本地依赖存在多个候选路径，需缩小范围", code="environment_manifest_unsupported")
                            if candidates and candidates[0] not in selected["manifests"]:
                                if len(local_projects) >= 6:
                                    raise WorkbenchError("本地依赖数量超出预算", code="environment_manifest_unsupported")
                                selected["manifests"].append(candidates[0])
                                local_projects.append(candidates[0].rsplit("/", 1)[0])
                                pending.append(candidates[0])
                if any(path not in entries for path in selected["manifests"]):
                    raise WorkbenchError("依赖清单不可读取，环境未准备", code="environment_manifest_unavailable")
                proxy_policy = DependencyProxyPolicy.for_ecosystem(selected["ecosystem"])
                # V2 source is the real Explore result, never a synthetic Implement.
                plan = DependencyPreparationPlan(plan_id=job.id, execution_attempt_id=attempt.id,
                    implement_result_hash=explore["result_hash"] if "result_hash" in explore else job.payload["explore_result_hash"],
                    workspace_inventory_hash=explore["inventory_hash"], ecosystem=selected["ecosystem"],
                    manifests=tuple(DependencyManifest(path=p, content_hash=entries[p]["prior_hash"]) for p in selected["manifests"]),
                    runner_image_digest=attempt.runner_image_digest, sandbox_policy_hash=policy.policy_hash,
                    proxy_policy_hash=proxy_policy.policy_hash, disk_bytes=policy.disk_bytes,
                    version=SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION if selected.get("distribution_strategy") == "source-wheel-v1" else DYNAMIC_WORKSPACE_DEPENDENCY_PLAN_VERSION if selected.get("metadata_strategy") == "wheel-metadata-v1" else WORKSPACE_DEPENDENCY_PLAN_VERSION,
                    distribution_strategy=selected.get("distribution_strategy","wheel-only-v1"),
                    builder_image_digest=selected.get("builder_image_digest"),
                    preparation_version=selected.get("preparation_version", "python-workspace-preparation-v1"),
                    native_resource=selected.get("native_resource"),
                    metadata_strategy=selected.get("metadata_strategy", "declarative-v1"), source_kind="explore",
                    project_directory=selected.get("project_directory", "."), local_projects=tuple(local_projects))
                environment_spec = plan.to_wire()
                if job.payload.get("recovery_stage_hash"):
                    previous = next(e for e in wb.history(attempt.task_id)
                                    if e.record_hash == job.payload["replaces_baseline_hash"])
                    previous_baseline = wb.read(previous.payload["result_hash"])
                    previous_spec = wb.read(previous_baseline["environment_spec_hash"])
                    if ({k:v for k,v in environment_spec.items() if k != "plan_id"}
                            != {k:v for k,v in previous_spec.items() if k != "plan_id"}):
                        raise WorkbenchError("冻结环境配方已变化，不能恢复旧验证。", code="environment_stale")
                lease = await self.proxy.start(image_digest=attempt.runner_image_digest, policy=proxy_policy)
                def allocated(value):
                    with self.database.session() as session:
                        wb, _, _ = self.current(session, job)
                        wb.append(attempt.task_id, "dependency_workspace", {"attempt_id": attempt.id, "workspace": asdict(value)},
                                  key="dependency-workspace:" + job.id, job_id=job.id)
                try:
                    dependency = await self.dependencies.prepare(signed_plan=self.signer.sign(plan), signer=self.signer,
                        repository_volume=workspace.volume_name, proxy_policy=proxy_policy, proxy_lease=lease,
                        sandbox_policy=policy, on_workspace=allocated,
                        **({"native_resource_path": str(wb.artifacts.root / "native-resources" / selected["native_resource"]["resource_hash"])}
                           if selected.get("native_resource") else {}))
                finally:
                    await self.proxy.stop(lease, ignore_missing=True)
            kwargs = {"workspace": workspace, "commands": commands, "policy": policy}
            if dependency:
                kwargs["dependency_workspace"] = dependency.workspace
            inspection = await self.verifier.run(**kwargs)
            if inspection.before_inventory_hash != explore["inventory_hash"] or inspection.after_inventory_hash != explore["inventory_hash"]:
                raise WorkbenchError("基线验证改变了冻结代码", code="baseline_inventory_mismatch")
            wire = {"before_inventory_hash": inspection.before_inventory_hash,
                    "after_inventory_hash": inspection.after_inventory_hash,
                    "command_evidence": [e.to_wire() for e in inspection.command_evidence]}
            # Use the same environment-vs-code classification as post-change Verify.
            value = {"succeeded": all(e.exit_code == 0 for e in inspection.command_evidence),
                     "test_results": [{"outcome": "passed" if e.exit_code == 0 else "failed", "exit_code": e.exit_code}
                                      for e in inspection.command_evidence],
                     "command_evidence": [{"command_id": e.command_id, "exit_code": e.exit_code,
                                           "status": e.status.value, "stderr_log": e.stderr_log}
                                          for e in inspection.command_evidence]}
            outcome = classify_verification(value)
            with self.database.session() as session:
                wb, _, _ = self.current(session, job)
                dependency_hash = None
                if dependency:
                    dependency_hash = wb.artifact(dependency.result.hash_payload())
                    if dependency_hash != dependency.result.result_hash:
                        raise WorkbenchError("环境产物哈希不匹配")
                    wb.append(attempt.task_id, "dependencies_ready", {"attempt_id": attempt.id,
                        "result_hash": dependency_hash, "workspace": asdict(dependency.workspace)},
                        key="dependencies-ready:" + job.id, job_id=job.id, commit=False)
                payload = {"version": "execution-baseline-v1", "attempt_id": attempt.id,
                    "explore_result_hash": job.payload["explore_result_hash"], "plan_hash": approved.record_hash,
                    "commands_hash": content_hash([c.to_wire() for c in commands]), "dependency_result_hash": dependency_hash,
                    "command_selection_policy": "existing-target-commands-v1",
                    "inspection": wire, "outcome": outcome}
                if job.payload.get("replaces_baseline_hash"):
                    payload["replaces_baseline_hash"] = job.payload["replaces_baseline_hash"]
                if job.payload.get("recovery_stage_hash"):
                    payload["recovery_stage_hash"] = job.payload["recovery_stage_hash"]
                payload["environment_spec_hash"] = wb.artifact(environment_spec) if environment_spec else None
                baseline_hash = wb.artifact(payload)
                wb.append(attempt.task_id, "execution_baseline_ready", {"attempt_id": attempt.id,
                    "result_hash": baseline_hash, "outcome": outcome}, key="baseline-ready:" + job.id,
                    job_id=job.id, commit=False)
                result = self.finish(session, job, {"result_hash": baseline_hash, "outcome": outcome})
                retained = True
                return result
        except DependencyPreparationError as exc:
            with self.database.session() as session:
                wb, _, _ = self.current(session, job)
                spec_hash = wb.artifact(environment_spec) if environment_spec else None
                failure = {"version":"environment-preparation-failure-v1", "attempt_id":attempt.id,
                    "job_id":job.id, "plan_hash":approved.record_hash,
                    "explore_result_hash":job.payload["explore_result_hash"],
                    "environment_spec_hash":spec_hash, "error_code":exc.code,
                    "environment_inputs_hash":environment_hash,"diagnostic":exc.diagnostic}
                failure_hash = wb.artifact(failure)
                wb.append(attempt.task_id,"execution_preparation_failed",{
                    "attempt_id":attempt.id,"result_hash":failure_hash,"error_code":exc.code,
                    "environment_inputs_hash":environment_hash,
                    "diagnostic":exc.diagnostic},key="preparation-failed:"+job.id,job_id=job.id)
            raise
        finally:
            await self.materializer.destroy(workspace)
            if dependency and not retained:
                await self.dependencies.destroy(dependency.workspace, ignore_missing=True)
