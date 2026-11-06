"""Bind v2 execution to existing exact authorization and immutable domain state."""
from __future__ import annotations
from datetime import datetime, timezone

from app.agent_v2.contracts import AgentCheckpoint, Binding, Limits, TaskSpec
from app.agent_v2.journal import AgentJournal
from app.executions import ExecutionAttemptService
from app.plans import PlanVersionService
from app.provenance import content_hash
from app.workbench import Workbench, WorkbenchError
from app.config import Settings


def freeze_spec(wb: Workbench, attempt_id: str, settings: Settings, *, limits: Limits | None = None) -> TaskSpec:
    from app.execution_recovery import require_recovery_authorization
    from app.execution_outcome import baseline_outcome
    from app.sandbox_worker.workspace_jobs import preparation_receipt, preparation_needs_restore
    from app.changesets import plan_sandbox_commands
    attempt = ExecutionAttemptService(wb.session).get_verified(attempt_id)
    auth = require_recovery_authorization(wb, attempt, settings)
    if auth.payload.get("agent_runtime_version") != "agent-runtime-v2":
        raise WorkbenchError("旧作业保留其原有协议，不能在执行中切换运行时。", code="agent_runtime_version")
    plan = PlanVersionService(wb.session).get_verified(attempt.plan_version_id)
    stage = ExecutionAttemptService(wb.session).current(attempt_id)
    baseline = preparation_receipt(wb, attempt_id)
    if stage.stage != "explore" or stage.status != "succeeded" or baseline is None:
        raise WorkbenchError("需要真实的修改前测试证据。", code="agent_baseline_missing")
    if preparation_needs_restore(wb, baseline):
        raise WorkbenchError("冻结环境需要先恢复。", code="environment_stale")
    baseline_value = wb.read(baseline.payload["result_hash"])
    environment_evidence = None
    if auth.payload.get("environment", {}).get("verification_contract_hash"):
        from app.environment_engine.admission import require_environment_evidence
        environment_evidence = require_environment_evidence(wb, attempt, auth, baseline_value)
    outcome = baseline_outcome(baseline_value)
    if outcome["status"] != "passed":
        raise WorkbenchError("修改前检查未通过，不能当作代码回归修复。", code="agent_baseline_blocked")
    transport = auth.payload.get("agent_model_transport")
    if transport != wb.binding(attempt.task_id, plan.id).payload.get("agent_model_transport"):
        raise WorkbenchError("方案与授权的模型工具协议不一致。", code="agent_model_transport_stale")
    if transport is not None:
        from app.agent_v2.native_tools import require_native_transport
        try:
            transport = require_native_transport(transport)
            if auth.payload["implementation"]["provider"] != "deepseek":
                raise ValueError()
        except ValueError:
            raise WorkbenchError("冻结的模型工具协议不可用，需要新方案及精确确认。", code="agent_model_transport_stale") from None
    return TaskSpec(binding=Binding(
        task_id=attempt.task_id, snapshot_id=attempt.snapshot_id,
        snapshot_hash=attempt.snapshot_inputs_hash, attempt_id=attempt.id, attempt_hash=attempt.record_hash,
        plan_id=plan.id, plan_hash=plan.record_hash, plan_content_hash=plan.content_hash,
        authorization_hash=auth.record_hash, base_sha=attempt.base_commit_sha,
        archive_hash=attempt.repository_archive_hash, image=attempt.runner_image_digest,
        policy_hash=attempt.sandbox_policy_hash, baseline_hash=baseline.record_hash,
        model_binding_hash=content_hash({"implementation": auth.payload["implementation"], "review": auth.payload["review"]})),
        goal=plan.goal, write_paths=list(plan.files_likely_to_change),
        commands=[c.to_wire() for c in plan_sandbox_commands(plan)], limits=limits or Limits(),
        environment_evidence=environment_evidence, model_transport=transport)


class TaskOrchestrator:
    def __init__(self, wb: Workbench, settings: Settings) -> None:
        self.wb, self.settings = wb, settings

    def start(self, attempt_id: str, *, limits: Limits | None = None) -> AgentJournal:
        from app.cla import _lock
        _lock(self.wb)
        spec = freeze_spec(self.wb, attempt_id, self.settings, limits=limits)
        session_id = "session-" + content_hash(spec.binding.model_dump())[:32]
        journal = AgentJournal(self.wb, spec.binding.task_id, session_id)
        old = next((e for e in journal.events() if e.kind == "agent_v2_session"), None)
        if old:
            if journal.spec() != spec:
                raise WorkbenchError("会话的冻结输入或预算不同。", code="agent_session_conflict")
            return journal
        artifact = self.wb.artifact(spec.model_dump())
        previous = None
        budget_started_at = datetime.now(timezone.utc).isoformat()
        for root in reversed(self.wb.history(spec.binding.task_id)):
            if root.kind == "agent_v2_session":
                candidate = AgentJournal(self.wb, spec.binding.task_id, root.payload["session_id"])
                if candidate.spec().binding.authorization_hash == spec.binding.authorization_hash:
                    previous = candidate.checkpoint()
                    budget_started_at = candidate.root().payload.get("budget_started_at", candidate.root().created_at.replace(tzinfo=timezone.utc).isoformat())
                    break
        from app.agent_v2.progress import POLICY, initialize
        journal.append("session", {"spec_artifact": artifact, "attempt_id": attempt_id,
            "budget_started_at": budget_started_at,
            **({"harness_policy": POLICY} if spec.model_transport is not None else {})}, key="session:" + session_id)
        usage = dict(previous.usage) if previous else {}
        if previous:
            usage["code_repairs"] = usage.get("code_repairs", 0) + 1
            if usage["code_repairs"] > spec.limits.code_repairs:
                raise WorkbenchError("代码修复额度已耗尽。", code="agent_budget_exceeded")
        cp = AgentCheckpoint(session_id=session_id, revision=content_hash(None), state="ready",
            source_hash=previous.source_hash if previous else spec.binding.archive_hash,
            snapshot_artifact=previous.snapshot_artifact if previous else None, usage=usage)
        journal.append("checkpoint", {"checkpoint": cp.model_dump()}, key="initial:" + session_id)
        initialize(journal, journal.checkpoint())
        return journal

    def require_current(self, journal: AgentJournal) -> TaskSpec:
        old = journal.spec()
        current = freeze_spec(self.wb, old.binding.attempt_id, self.settings, limits=old.limits)
        if old != current:
            raise WorkbenchError("已批准输入已过期，请重新核对方案。", code="agent_authorization_stale")
        return current


def runtime_view(wb: Workbench, task_id: str) -> dict | None:
    root = wb.latest(task_id, "agent_v2_session")
    if root is None:
        return None
    journal = AgentJournal(wb, task_id, root.payload["session_id"])
    cp = journal.checkpoint()
    timeline = [{"id": e.id, "kind": e.kind, "record_hash": e.record_hash,
                 "created_at": e.created_at.isoformat(), "job_id": e.job_id,
                 "payload": {k: v for k, v in e.payload.items() if k != "checkpoint"}}
                for e in journal.events() if e.kind != "agent_v2_checkpoint"]
    # The API projects references, never worker-private volume identities.
    from app.agent_v2.metrics import journey_metrics
    from app.agent_v2.progress import current_progress
    progress = current_progress(journal)
    progress_view = progress.model_dump() if progress else None
    if progress_view is not None:
        from app.agent_v2.native_tools import native_model_transport
        if journal.spec().model_transport == native_model_transport():
            from app.agent_v2.coding_context import observe
            progress_view["coding"] = observe(journal)[0]
    metrics = journey_metrics(wb, task_id)
    return {"version": "agent-runtime-v2", "session_id": journal.session_id,
            "checkpoint": cp.model_dump(), "remaining_budget": journal.remaining(), "timeline": timeline,
            "implementation_progress": progress_view,
            "metrics": metrics, "native_journey_completed": bool(metrics["native_journey_completed"])}
