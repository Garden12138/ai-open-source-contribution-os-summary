"""Typed, durable continuations for human-confirmed workflow steps.

These commands prepare local evidence/previews. They cannot confirm a remote
write, infer acceptance from chat, or alter the exhausted reasoning Job.
"""
from __future__ import annotations

from typing import Any

from app.models import Job, WorkbenchEvent
from app.workbench import Workbench, WorkbenchError

CONTINUATION_VERSION = "accepted-publication-v1"


def _accepted(wb: Workbench, task_id: str, receipt_hash: str) -> WorkbenchEvent:
    from app.execution_acceptance import acceptance_view
    from app.task_visibility import require_active_task
    require_active_task(wb.session, task_id)
    view = acceptance_view(wb, task_id)
    receipt = next((e for e in wb.history(task_id) if e.kind == "execution_accepted"
                    and e.record_hash == receipt_hash and e.actor_id == "local-user"), None)
    if (receipt is None or not view.get("accepted")
            or view["accepted"]["record_hash"] != receipt_hash
            or wb.latest(task_id, "publication_confirmed") or wb.latest(task_id, "publication_completed")):
        raise WorkbenchError("验收结果已变化，不能继续旧的发布准备。", code="continuation_stale")
    return receipt


def request_after_acceptance(wb: Workbench, task_id: str, *, receipt_hash: str,
                             settings: Any, source: WorkbenchEvent | None = None,
                             request_key: str | None = None,
                             commit: bool = True) -> dict:
    """Persist the exact next step in the same transaction as acceptance."""
    from app.cla import _lock
    from app.conversation import require_settled_messages, message_receipt
    _lock(wb)
    receipt = _accepted(wb, task_id, receipt_hash)
    from app.provenance import content_hash
    suffix = ":" + content_hash(request_key)[:24] if request_key else ""
    command_key = "continue-accepted:" + receipt.id + suffix
    old = next((e for e in wb.history(task_id) if e.idempotency_key == command_key), None)
    if old:
        return message_receipt(wb, task_id, old.payload["message_id"])
    if source and (source.kind != "conversation_received" or source.actor_id != "local-user"
                   or source.task_id != task_id or source.payload.get("text") != "准备发布"
                   or source.idempotency_key != "conversation:accepted-" + receipt.id):
        raise WorkbenchError("原继续请求不匹配。", code="continuation_stale")
    require_settled_messages(wb, task_id)
    wb.assert_idle(task_id)
    message = wb.append(task_id, "conversation_received", {"version":"conversation-v1", "text":"准备发布"},
        key="conversation:" + ("accepted-recovery-" if source else "accepted-") + receipt.id + suffix,
        actor="orchestrator", commit=False)
    result = message_receipt(wb, task_id, message.id)
    wb.append(task_id, "conversation_continuation_requested", {
        "version":"conversation-v1", "continuation_version":CONTINUATION_VERSION, "action":"prepare_publication",
        "message_id":result["message_id"], "acceptance_hash":receipt.record_hash,
        "binding_hash":receipt.payload["binding_hash"],
        **({"recovered_message_id":source.id, "recovered_message_hash":source.record_hash} if source else {}),
    }, key=command_key, plan_id=receipt.plan_version_id, commit=False)
    if commit:
        wb.session.commit()
    return result


def continuation_intent(wb: Workbench, task_id: str, message: WorkbenchEvent) -> str | None:
    command = next((e for e in wb.history(task_id) if e.kind == "conversation_continuation_requested"
                    and e.payload.get("message_id") == message.id), None)
    if command is None:
        return None
    if (command.actor_id != "system" or command.payload.get("continuation_version") != CONTINUATION_VERSION
            or command.payload.get("action") != "prepare_publication"):
        raise WorkbenchError("继续请求的协议无效。", code="continuation_invalid")
    receipt = _accepted(wb, task_id, command.payload["acceptance_hash"])
    if command.payload["binding_hash"] != receipt.payload["binding_hash"]:
        raise WorkbenchError("继续请求的验收依据不匹配。", code="continuation_stale")
    return "publication"


def recover_interrupted_acceptance(wb: Workbench, task_id: str, settings: Any) -> bool:
    """Upgrade only the old button-generated request that died in reasoning."""
    from app.conversation import pending_messages
    events = wb.history(task_id)
    receipt = wb.latest(task_id, "execution_accepted")
    if (not receipt or pending_messages(wb, task_id)
            or any(e.idempotency_key == "continue-accepted:" + receipt.id for e in events)):
        return False
    source = next((e for e in events if e.kind == "conversation_received" and e.actor_id == "local-user"
                   and e.idempotency_key == "conversation:accepted-" + receipt.id
                   and e.payload.get("text") == "准备发布"), None)
    if source is None or any(e.sequence > source.sequence and e.kind in {
            "conversation_received", "stop_requested", "execution_change_requested", "execution_authorized"} for e in events):
        return False
    finished = next((e for e in events if e.kind == "conversation_finished"
                     and e.payload.get("message_id") == source.id), None)
    classified = next((e for e in events if e.kind == "conversation_classifying"
                       and e.payload.get("message_id") == source.id), None)
    job = wb.session.get(Job, classified.job_id) if classified else None
    if (finished is None or finished.payload["status"] != "failed" or job is None
            or job.state != "failed" or job.error_code != "conversation_decision_invalid"):
        return False
    try:
        _accepted(wb, task_id, receipt.record_hash)
    except WorkbenchError:
        return False
    result = request_after_acceptance(wb, task_id, receipt_hash=receipt.record_hash,
                                      settings=settings, source=source, commit=False)
    wb.append(task_id, "conversation_continuation_recovered", {
        "version":"conversation-v1", "continuation_version":CONTINUATION_VERSION, "message_id":result["message_id"],
        "source_id":source.id, "source_hash":source.record_hash,
        "acceptance_hash":receipt.record_hash, "failed_job_id":job.id,
        "original_job_attempts":job.attempt_count, "model_calls_added":0,
    }, key="recover-accepted:" + receipt.id, job_id=job.id, commit=False)
    return True
