"""Exact local correction of a rejected proposal without another provider call."""
from __future__ import annotations

from typing import Any
from app.workbench import Workbench, WorkbenchError


def _inputs(wb: Workbench, job_id: str, settings: Any) -> tuple:
    from app.jobs import JobService
    from app.coding import CodingConversationService, _load_coding_context, coding_conversation_hash
    from app.executions import ExecutionAttemptService
    from app.execution_recovery import require_recovery_authorization
    from app.coding_repair import repair_change_set, repair_context
    from app.conversation import require_settled_messages
    job = JobService(wb.session).get(job_id)
    if (job.kind != "change_set_proposal" or job.state != "failed"
            or job.error_code not in {"edit_anchor_missing", "edit_anchor_ambiguous", "proposal_structure_invalid"}
            or job.cancel_requested_at):
        raise WorkbenchError("当前草案失败不支持本地校正。", code="proposal_correction_stale")
    attempt = ExecutionAttemptService(wb.session).get_verified(job.payload["execution_attempt_id"])
    authorization = require_recovery_authorization(wb, attempt, settings)
    require_settled_messages(wb, attempt.task_id)
    conversation = CodingConversationService(wb.session, artifact_root=str(wb.artifacts.root))
    coding = conversation.require_session(attempt.id)
    conversation._require_session_current(coding)
    turns = conversation.turns(coding.id)
    if (coding.context_hash != job.payload["context_hash"] or not turns
            or turns[-1].record_hash != job.payload["latest_turn_hash"]
            or coding_conversation_hash(turns) != job.payload["conversation_hash"]):
        raise WorkbenchError("草案上下文或讨论已变化。", code="proposal_correction_stale")
    if any(j.id != job.id and j.state in {"queued", "leased", "running"} for j in wb.jobs(attempt.task_id)):
        raise WorkbenchError("请等待当前任务完成后再校正草案。", code="proposal_correction_busy")
    base = _load_coding_context(wb.session, artifact_root=str(wb.artifacts.root), coding_session=coding)
    plan = wb.latest_plan(attempt.task_id)
    previous = repair_change_set(wb.session, str(wb.artifacts.root), base)
    view = repair_context(base, previous, allowed_paths=plan.files_likely_to_change)
    rejection = next((e for e in reversed(wb.history(attempt.task_id)) if e.kind == "proposal_rejected"
                      and e.job_id == job.id and e.payload.get("job_attempt") == job.attempt_count
                      and e.payload.get("editing_context_hash") == view.context_hash
                      and e.payload.get("conversation_hash") == job.payload["conversation_hash"]), None)
    if rejection is None:
        raise WorkbenchError("缺少原草案的精确失败依据。", code="proposal_correction_stale")
    return job, attempt, authorization, base, view, previous, plan, rejection


def record_corrected_proposal(wb: Workbench, job_id: str, *, raw: str,
                              expected_rejection_hash: str, settings: Any) -> Any:
    """Trusted local editing entry; never rewrites a job or fabricates model usage."""
    from app.cla import _lock
    from app.coding import _parse_change_set_proposal, _validate_proposed_operations
    from app.coding_repair import cumulative_operations
    from app.proposal_validation import check_proposal_structure
    from app.provenance import content_hash
    from app.sandbox_worker.implementation import ImplementationChangeSet
    _lock(wb)
    from app.jobs import JobService
    from app.executions import ExecutionAttemptService
    job = JobService(wb.session).get(job_id)
    attempt = ExecutionAttemptService(wb.session).get_verified(job.payload["execution_attempt_id"])
    old = next((e for e in wb.history(attempt.task_id) if e.kind == "proposal_corrected"
                and e.job_id == job_id), None)
    if old:
        if (old.payload.get("rejection_hash") != expected_rejection_hash
                or old.payload.get("proposal_hash") != content_hash(raw)):
            raise WorkbenchError("重复校正请求的内容已变化。", code="proposal_correction_stale")
        wb.read(old.payload["artifact_id"])
        return old
    job, attempt, authorization, base, view, previous, plan, rejection = _inputs(wb, job_id, settings)
    if rejection.record_hash != expected_rejection_hash:
        raise WorkbenchError("草案失败依据已变化。", code="proposal_correction_stale")
    checked = check_proposal_structure(raw, view, plan.files_likely_to_change)
    _, operations = _parse_change_set_proposal(checked, context=view, strict_prior_hash=True)
    operations = cumulative_operations(base, view, previous, operations, allowed_paths=plan.files_likely_to_change)
    change_set = _validate_proposed_operations(base, ImplementationChangeSet(
        change_set_id="changeset-local-" + job.id, plan_version_id=plan.id,
        plan_content_hash=plan.content_hash, plan_record_hash=plan.record_hash, operations=operations),
        allowed_paths=tuple(plan.files_likely_to_change))
    artifact = wb.artifact(change_set.to_wire())
    receipt = wb.append(attempt.task_id, "proposal_corrected", {
        "version":"local-proposal-correction-v1", "source":"local_correction",
        "attempt_id":attempt.id, "authorization_hash":authorization.record_hash,
        "rejection_hash":rejection.record_hash, "rejection_artifact_hash":rejection.payload["result_hash"],
        "context_hash":base.context_hash, "editing_context_hash":view.context_hash,
        "conversation_hash":job.payload["conversation_hash"], "proposal_hash":content_hash(raw),
        "proposal_artifact_id":wb.artifact({"raw":raw}),
        "change_set_hash":change_set.change_set_hash, "artifact_id":artifact,
        "model_calls_added":0,
    }, key="corrected-proposal:" + job.id, job_id=job.id, plan_id=plan.id, commit=False)
    blocker = wb.active_blocker(attempt.task_id)
    if blocker:
        if blocker.job_id != job.id:
            raise WorkbenchError("存在其他执行阻塞，不能恢复草案。", code="proposal_correction_stale")
        start = wb.latest(attempt.task_id, "automation_started")
        wb.append(attempt.task_id, "automation_resumed", {
            "start_hash":start.record_hash, "authorization_hash":authorization.record_hash,
            "blocked_hash":blocker.record_hash, "attempt_id":attempt.id,
            "job_id":job.id, "job_attempt_count":job.attempt_count,
            "correction_hash":receipt.record_hash,
        }, key="resume-corrected:" + job.id, job_id=job.id, plan_id=plan.id, commit=False)
    wb.session.commit()
    return receipt


def corrected_proposal(wb: Workbench, attempt_id: str, authorization_hash: str) -> tuple[Any, Any] | None:
    """Recheck the immutable correction before excluding the rejected model Job."""
    from app.coding import CodingConversationService, CodingContextService, coding_conversation_hash, verify_coding_session
    from app.sandbox_worker.implementation import ImplementationChangeSet
    from app.models import Job
    from app.executions import ExecutionAttemptService
    attempt = ExecutionAttemptService(wb.session).get_verified(attempt_id)
    receipt = next((e for e in reversed(wb.history(attempt.task_id)) if e.kind == "proposal_corrected"
        and e.payload.get("attempt_id") == attempt_id and e.payload.get("authorization_hash") == authorization_hash), None)
    if receipt is None:
        return None
    coding = CodingConversationService(wb.session, artifact_root=str(wb.artifacts.root))
    context = CodingContextService(wb.session).get_session(attempt_id)
    if context is None:
        raise WorkbenchError("本地草案上下文缺失。", code="proposal_correction_stale")
    verify_coding_session(context)
    job = wb.session.get(Job, receipt.job_id)
    if (receipt.actor_id != "system" or receipt.payload.get("version") != "local-proposal-correction-v1"
            or job is None or job.state != "failed" or job.kind != "change_set_proposal"
            or context.context_hash != receipt.payload["context_hash"]
            or coding_conversation_hash(coding.turns(context.id)) != receipt.payload["conversation_hash"]):
        raise WorkbenchError("本地草案校正依据已过期。", code="proposal_correction_stale")
    change_set = ImplementationChangeSet.from_wire(wb.read(receipt.payload["artifact_id"]))
    if change_set.change_set_hash != receipt.payload["change_set_hash"]:
        raise WorkbenchError("本地草案校正哈希不匹配。", code="proposal_correction_stale")
    return receipt, change_set
