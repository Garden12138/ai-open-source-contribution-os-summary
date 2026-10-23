"""Persistent preparation Jobs owned solely by the Sandbox Worker."""
from __future__ import annotations
from dataclasses import asdict
import json
from app.workbench_worker import WorkbenchJobWorker
from app.workbench import Workbench, WorkbenchError
from app.models import Job
from app.executions import ExecutionAttemptService
from app.sandbox_worker.dependencies import (
    DependencyManifest, DependencyPreparationPlan, DependencyPlanSigner,
    DependencyProxyPolicy, DependencyExecution, DependencyPreparationResult, DependencyWorkspace,
)
from app.sandbox_worker.specs import SandboxPolicy
from app.sandbox_worker.implementation import ImplementResult
from app.execution_environment import environment_plan


def load_dependency(wb: Workbench, attempt_id: str) -> DependencyExecution | None:
    event = next((e for e in reversed(wb.history(ExecutionAttemptService(wb.session).get_verified(attempt_id).task_id))
                  if e.kind == "dependencies_ready" and e.payload["attempt_id"] == attempt_id), None)
    if not event:
        return None
    events = wb.history(event.task_id)
    allocation = next((e for e in reversed(events) if e.kind == "dependency_workspace"
                       and e.job_id == event.job_id
                       and {k: v for k, v in e.payload["workspace"].items() if k != "inventory_hash"}
                       == {k: v for k, v in event.payload["workspace"].items() if k != "inventory_hash"}), None)
    if allocation and any(e.kind in {"dependency_workspace_disposed", "dependency_workspace_invalidated"}
                          and e.payload["allocation_hash"] == allocation.record_hash for e in events):
        raise WorkbenchError("冻结依赖工作区已回收，不能使用旧环境验证", code="environment_stale")
    value = wb.read(event.payload["result_hash"])
    result = DependencyPreparationResult.from_payload({**value, "result_hash": event.payload["result_hash"]})
    return DependencyExecution(result=result, workspace=DependencyWorkspace(**event.payload["workspace"]))


class DependencyJobWorker(WorkbenchJobWorker):
    kinds = ("execution_dependencies",)

    def __init__(self, database, settings, runtime, proxy, *, worker_id: str):
        super().__init__(database, worker_id=worker_id)
        self.settings, self.runtime, self.proxy = settings, runtime, proxy
        self.signer = DependencyPlanSigner(key_id=settings.sandbox_job_spec_key_id,
                                          signing_key=settings.sandbox_job_spec_signing_key)

    async def cleanup(self) -> None:
        from sqlalchemy import select
        from app.models import WorkbenchEvent
        from app.jobs import TERMINAL_STATES
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            allocations = list(session.scalars(select(WorkbenchEvent).where(WorkbenchEvent.kind == "dependency_workspace")))
            for allocated in allocations:
                if any(e.idempotency_key == "dependency-disposed:" + allocated.id for e in wb.history(allocated.task_id)):
                    continue
                job = session.get(Job, allocated.job_id)
                # A coordinator blocker does not cancel already scheduled work.
                # Keep the volume until every consumer has released its lease.
                consumers = session.scalars(select(Job).where(Job.state.not_in(TERMINAL_STATES)))
                if any(j.payload.get("execution_attempt_id") == allocated.payload["attempt_id"]
                       and j.id != job.id for j in consumers):
                    continue
                current = ExecutionAttemptService(session).current(allocated.payload["attempt_id"])
                stop = wb.latest(allocated.task_id, "stop_requested")
                stopped = (stop and allocated.sequence < stop.sequence) or any(
                    e.kind == "baseline_replan_requested" and e.sequence > allocated.sequence
                    and e.payload.get("attempt_id") == allocated.payload["attempt_id"]
                    for e in wb.history(allocated.task_id))
                if not stopped and current.stage == "verify" and current.status in {"pending", "running"}:
                    continue
                finished = current.stage == "verify" and current.status in {"succeeded", "failed", "cancelled", "timed_out"}
                blocker = wb.active_blocker(allocated.task_id)
                blocked = bool(blocker and blocker.sequence > allocated.sequence)
                if (not stopped and current.stage == "implement" and current.status == "succeeded"
                        and (not blocked or blocker.payload.get("reason_code") == "workflow_failed")):
                    continue
                if (job.payload.get("recovery_stage_hash") == current.record_hash
                        and job.state == "succeeded" and not stopped):
                    # A completed restore is awaiting the coordinator's new
                    # immutable Verify input version, not disposable failure.
                    continue
                if job.state not in TERMINAL_STATES or not (finished or stopped or blocked or job.state != "succeeded"):
                    continue
                await self.runtime.destroy(DependencyWorkspace(**allocated.payload["workspace"]), ignore_missing=True)
                wb.append(allocated.task_id, "dependency_workspace_disposed", {"allocation_hash": allocated.record_hash},
                          key="dependency-disposed:" + allocated.id)

    async def execute(self, job: Job) -> dict:
        with self.database.session() as session:
            self.check(session, job)
            wb = Workbench(session, self.settings.artifact_root)
            attempt = ExecutionAttemptService(session).get_verified(job.payload["execution_attempt_id"])
            current = ExecutionAttemptService(session).current(attempt.id)
            if current.stage != "implement" or current.status != "succeeded" or current.result_hash != job.payload["implement_result_hash"]:
                raise WorkbenchError("依赖准备输入已过期", code="environment_stale")
            implement = ImplementResult.from_payload(wb.read(current.result_hash))
            inventory = {f.path: f for f in implement.result_inventory}
            selected = environment_plan(list(inventory))
            if selected["status"] != "supported":
                raise WorkbenchError(selected["reason"], code="environment_unsupported")
            policy = SandboxPolicy()
            proxy_policy = DependencyProxyPolicy.for_ecosystem(selected["ecosystem"])
            plan = DependencyPreparationPlan(plan_id=job.id, execution_attempt_id=attempt.id,
                implement_result_hash=implement.result_hash, workspace_inventory_hash=implement.result_inventory_hash,
                ecosystem=selected["ecosystem"], manifests=tuple(DependencyManifest(path=p, content_hash=inventory[p].sha256) for p in selected["manifests"]),
                runner_image_digest=attempt.runner_image_digest, sandbox_policy_hash=policy.policy_hash,
                proxy_policy_hash=proxy_policy.policy_hash, disk_bytes=policy.disk_bytes)
            repository_volume = current.workspace_ref
            wb.append(attempt.task_id, "dependencies_started", {"attempt_id": attempt.id, "plan_hash": plan.plan_hash},
                      key="dependencies-start:" + job.id, job_id=job.id)
        def record_workspace(workspace):
            with self.database.session() as session:
                self.check(session, job)
                Workbench(session, self.settings.artifact_root).append(attempt.task_id, "dependency_workspace",
                    {"attempt_id": attempt.id, "workspace": asdict(workspace)},
                    key="dependency-workspace:" + job.id, job_id=job.id)

        execution = None
        retained = False
        lease = await self.proxy.start(image_digest=plan.runner_image_digest, policy=proxy_policy)
        try:
            execution = await self.runtime.prepare(signed_plan=self.signer.sign(plan), signer=self.signer,
                repository_volume=repository_volume, proxy_policy=proxy_policy, proxy_lease=lease, sandbox_policy=policy,
                on_workspace=record_workspace)
            with self.database.session() as session:
                self.check(session, job)
                wb = Workbench(session, self.settings.artifact_root)
                result_hash = wb.artifact(execution.result.hash_payload())
                if result_hash != execution.result.result_hash:
                    raise WorkbenchError("依赖产物哈希不匹配")
                wb.append(attempt.task_id, "dependencies_ready", {"attempt_id": attempt.id,
                    "result_hash": result_hash, "workspace": asdict(execution.workspace)},
                    key="dependencies-ready:" + job.id, job_id=job.id, commit=False)
                result = self.finish(session, job, {"result_hash": result_hash})
                retained = True
                return result
        finally:
            try:
                await self.proxy.stop(lease, ignore_missing=True)
            finally:
                if execution and not retained:
                    await self.runtime.destroy(execution.workspace, ignore_missing=True)
