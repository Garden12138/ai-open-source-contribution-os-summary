"""Compatibility bridge: actual sandbox snapshot → existing Verify/Review chain."""
from __future__ import annotations

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import schedule, validate_finish
from app.agent_v2.orchestrator import TaskOrchestrator
from app.changesets import ChangeSetService, ChangeSetStore
from app.providers.runtime import resolve_job_spec_signer
from app.sandbox_worker.implementation import ChangeOperation, ChangeOperationKind, ImplementationChangeSet
from app.workbench import WorkbenchError
from app.workbench import Workbench
from app.config import Settings


def advance_coding(wb: Workbench, attempt_id: str, settings: Settings) -> None:
    service = TaskOrchestrator(wb, settings)
    journal = service.start(attempt_id)
    service.require_current(journal)
    cp = journal.checkpoint()
    if cp.state == "finished":
        validate_finish(journal)
        spec = journal.spec()
        snapshot = wb.read(cp.snapshot_artifact)
        operations = tuple(ChangeOperation(path=path, kind=ChangeOperationKind.WRITE,
            expected_prior_hash=value["original_hash"], content=value["content"], executable=value["executable"])
            for path, value in sorted(snapshot["files"].items())
            if value["original_content"] != value["content"])
        if not operations:
            raise WorkbenchError("本次没有可交付的新修改。", code="agent_no_change")
        changes = ImplementationChangeSet(change_set_id="agent-v2-" + journal.session_id,
            plan_version_id=spec.binding.plan_id, plan_content_hash=spec.binding.plan_content_hash,
            plan_record_hash=spec.binding.plan_hash, operations=operations)
        receipt = journal.append("exported", {"snapshot_artifact": cp.snapshot_artifact,
            "checkpoint_hash": cp.revision, "change_set_hash": changes.change_set_hash}, key="export:" + journal.session_id)
        wb.append(journal.task_id, "automatic_change_accepted", {
            "attempt_id": attempt_id, "authorization_hash": spec.binding.authorization_hash,
            "change_set_hash": changes.change_set_hash, "runtime_receipt_hash": receipt.record_hash},
            key="auto-v2-accept:" + journal.session_id, actor="orchestrator", commit=False)
        ChangeSetService(wb.session, ChangeSetStore(settings.artifact_root), resolve_job_spec_signer(settings)).accept_existing(
            attempt_id, change_set=changes, idempotency_key="apply-v2:" + journal.session_id)
    elif cp.state == "blocked":
        blocked = next((e for e in reversed(journal.events()) if e.payload.get("failure")), None)
        code = blocked.payload["failure"]["safe_reason_code"] if blocked else "agent_session_blocked"
        raise WorkbenchError("工具执行已停止，请查看真实证据和允许的下一步。", code=code)
    else:
        schedule(journal)
        wb.session.commit()
