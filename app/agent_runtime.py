"""Task-local runtime facts, dialogue focus and action barriers.

The immutable domain services remain the authority for every effect. This layer
owns orchestration metadata; neither a focus nor a model decision is a capability.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.jobs import TERMINAL_STATES
from app.provenance import content_hash
from app.workbench import Workbench, WorkbenchError

VERSION = "agent-runtime-v1"
REASONING_VERSION = "conversation-reasoning-v4"
FOREGROUND_KINDS = frozenset({"conversation_intent", "execution_question"})
DISPLAY_KINDS = frozenset({"conversation_result", "assistant_message", "execution_answer"})


class ConversationFocus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    message_hash: str
    references: list[dict[str, Any]]


class TaskObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["agent-runtime-v1"] = VERSION
    task_id: str
    revision: str
    user_state: dict[str, Any]
    focus: ConversationFocus | None
    foreground: list[dict[str, str]]
    background: list[dict[str, str]]
    blocking_message_ids: list[str]
    maintenance_count: int = 0
    autonomous_eligible: bool = True


def enabled(wb: Workbench, task_id: str) -> bool:
    event = wb.latest(task_id, "agent_runtime_enabled")
    if event is None:
        return False
    if event.actor_id != "orchestrator" or event.payload.get("version") != VERSION:
        raise WorkbenchError("任务运行协议不匹配。", code="agent_runtime_version")
    return True


def activate(wb: Workbench, task_id: str) -> bool:
    """Switch only at a safe idle boundary; leave queued legacy work untouched."""
    if enabled(wb, task_id):
        return True
    from app.conversation import pending_messages
    if pending_messages(wb, task_id) or any(j.state not in TERMINAL_STATES for j in wb.jobs(task_id)):
        return False
    rows = wb.history(task_id)
    wb.append(task_id, "agent_runtime_enabled", {
        "version": VERSION, "predecessor_hash": rows[-1].record_hash if rows else None,
    }, key="agent-runtime:v1:" + task_id, actor="orchestrator", commit=False)
    return True


def display_source(wb: Workbench, task_id: str, source_id: str, source_hash: str) -> Any:
    source = next((e for e in wb.history(task_id) if e.id == source_id), None)
    if source is None or source.kind not in DISPLAY_KINDS or source.record_hash != source_hash:
        raise WorkbenchError("引用的对话不属于本任务或已不匹配。", code="conversation_reference_invalid")
    return source


def focus(wb: Workbench, task_id: str, *, through_sequence: int | None = None) -> dict | None:
    rows = wb.history(task_id)
    latest = next((e for e in reversed(rows) if e.kind == "agent_focus_changed"
                   and (through_sequence is None or e.sequence <= through_sequence)), None)
    if latest is None:
        return None
    if latest.actor_id != "orchestrator" or latest.payload.get("version") != VERSION:
        raise WorkbenchError("对话焦点记录无效。", code="conversation_reference_invalid")
    value = ConversationFocus.model_validate(latest.payload["focus"]).model_dump()
    display_source(wb, task_id, value["message_id"], value["message_hash"])
    return value


def record_focus(wb: Workbench, task_id: str, result: Any, references: list[dict] | None) -> None:
    if not enabled(wb, task_id):
        return
    meaningful = [r for r in references or [] if r.get("name") not in {"task", "progress"}]
    # A progress interlude does not replace the artifact being discussed.
    if not meaningful:
        return
    value = ConversationFocus(message_id=result.id, message_hash=result.record_hash,
                              references=meaningful).model_dump()
    wb.append(task_id, "agent_focus_changed", {"version": VERSION, "focus": value},
              key="agent-focus:" + result.id, actor="orchestrator", commit=False)


def classify_turn(wb: Workbench, task_id: str, message_id: str, effect: str, *, job_id: str) -> None:
    if effect not in {"observe", "command"}:
        return
    previous = next((e for e in wb.history(task_id) if e.kind == "agent_turn_classified"
                     and e.payload.get("message_id") == message_id), None)
    if previous:
        if previous.payload["effect"] == "command" and effect == "observe":
            return
        if previous.payload["effect"] != effect:
            raise WorkbenchError("只读对话不能转成任务操作，请另发明确要求。", code="agent_effect_conflict")
        return
    wb.append(task_id, "agent_turn_classified", {
        "version": VERSION, "message_id": message_id, "effect": effect,
    }, key="agent-classify:" + message_id, actor="orchestrator", job_id=job_id, commit=False)


def blocking_messages(wb: Workbench, task_id: str) -> list[Any]:
    from app.conversation import pending_messages
    pending = pending_messages(wb, task_id)
    if not enabled(wb, task_id):
        return pending
    rows = wb.history(task_id)
    readonly = {e.payload["message_id"] for e in rows if e.kind == "agent_turn_classified"
                and e.actor_id == "orchestrator" and e.payload.get("version") == VERSION
                and e.payload.get("effect") == "observe"}
    return [e for e in pending if e.id not in readonly]


def turn_effect(wb: Workbench, task_id: str, message_id: str) -> str | None:
    event=next((e for e in wb.history(task_id) if e.kind=="agent_turn_classified"
                and e.payload.get("message_id")==message_id and e.actor_id=="orchestrator"
                and e.payload.get("version")==VERSION),None)
    return event.payload["effect"] if event else None


def failed_turn(wb: Workbench, task_id: str) -> tuple[Any, Any] | None:
    if not enabled(wb, task_id):
        return None
    rows = wb.history(task_id)
    finished = next((e for e in reversed(rows) if e.kind == "conversation_finished"
                     and e.payload.get("status") == "failed"), None)
    if finished is None:
        return None
    source = next((e for e in rows if e.id == finished.payload["message_id"] and e.kind == "conversation_received"), None)
    classification = next((e for e in reversed(rows) if e.kind == "conversation_classifying"
                            and e.payload.get("message_id") == finished.payload["message_id"]), None)
    if source is None or classification is None:
        return None
    from app.conversation import pending_messages, progress_only
    readonly = {e.payload["message_id"] for e in rows if e.kind == "agent_turn_classified"
                and e.actor_id == "orchestrator" and e.payload.get("effect") == "observe"}
    pending_ids = {e.id for e in pending_messages(wb, task_id)}
    # Completed observations do not consume or replace a failed request. A later
    # command does; continuing must never resurrect a superseded instruction.
    if any(e.kind == "conversation_received" and e.sequence > source.sequence
           and e.actor_id == "local-user" and e.id not in readonly
           and not progress_only(e.payload["text"])
           and e.id not in pending_ids
           for e in rows):
        return None
    from app.models import Job
    job = wb.session.get(Job, classification.job_id)
    if job is None or job.state not in {"failed", "timed_out"} or job.error_code not in {
        "conversation_response_budget", "conversation_input_budget", "conversation_context_budget",
        "conversation_decision_invalid", "conversation_basis_changed", "planning_budget_exceeded",
        "conversation_call_budget", "planning_provider_failed", "workbench_failed"}:
        return None
    return source, job


def resume_turn(wb: Workbench, task_id: str, message: Any) -> Any | None:
    previous = failed_turn(wb, task_id)
    if previous is None:
        return None
    source, failed = previous
    from app.conversation import progress_only
    rows = wb.history(task_id)
    readonly = {e.payload["message_id"] for e in rows if e.kind == "agent_turn_classified"
                and e.actor_id == "orchestrator" and e.payload.get("effect") == "observe"}
    if any(e.kind == "conversation_received" and source.sequence < e.sequence < message.sequence
           and e.actor_id == "local-user" and e.id not in readonly
           and not progress_only(e.payload["text"]) for e in rows):
        return None
    if message.payload.get("reply_to"):
        anchor = message.payload["reply_to"]
        target = display_source(wb, task_id, anchor["id"], anchor["record_hash"])
        if target.payload.get("message_id") != source.id:
            return None
    from app.planning_budget import require_planning_budget
    require_planning_budget(wb, task_id)
    request_text = failed.payload.get("request_text", source.payload["text"])
    job = wb.enqueue(task_id, "conversation_intent", {
        "message_id": message.id, "text": message.payload["text"],
        "request_text": request_text, "reasoning_version": REASONING_VERSION,
        "resumed_from_job_id": failed.id,
    }, key="agent-turn-resume:" + failed.id, timeout_seconds=180, max_attempts=2, commit=False)
    inherited=turn_effect(wb,task_id,source.id)
    if inherited=="observe":
        classify_turn(wb,task_id,message.id,"observe",job_id=job.id)
    wb.append(task_id, "agent_turn_resumed", {"version": VERSION,
        "message_id": message.id, "source_id": source.id, "source_hash": source.record_hash,
        "failed_job_id": failed.id, "request_hash": content_hash(request_text),"inherited_effect":inherited},
        key="agent-turn-resumed:" + failed.id, job_id=job.id, actor="orchestrator", commit=False)
    return job


def request_text(wb: Workbench, task_id: str, message: Any) -> str:
    receipt = next((e for e in reversed(wb.history(task_id)) if e.kind == "agent_turn_resumed"
                    and e.payload.get("message_id") == message.id), None)
    if receipt is None:
        return message.payload["text"]
    from app.models import Job
    job = wb.session.get(Job, receipt.job_id)
    source = next((e for e in wb.history(task_id) if e.id == receipt.payload["source_id"]), None)
    if (receipt.actor_id != "orchestrator" or receipt.payload.get("version") != VERSION or job is None
            or source is None or source.record_hash != receipt.payload["source_hash"]
            or content_hash(job.payload.get("request_text")) != receipt.payload["request_hash"]):
        raise WorkbenchError("恢复请求依据无效。", code="agent_resume_stale")
    return job.payload["request_text"]


def observe(wb: Workbench, task_id: str) -> dict:
    from app.conversation import user_status
    state = user_status(wb, task_id)
    active = [j for j in wb.jobs(task_id) if j.state not in TERMINAL_STATES]
    def item(j: Any) -> dict[str, str]:
        return {"id": j.id, "kind": j.kind, "state": j.state}
    foreground = [item(j) for j in active if j.kind in FOREGROUND_KINDS]
    background = [item(j) for j in active if j.kind not in FOREGROUND_KINDS]
    # Focus and read receipts deliberately do not contribute to domain revision.
    from app.context_compaction import source_binding
    revision = content_hash(source_binding(wb, task_id))
    # Older operator receipts remain distinguishable without changing their
    # payloads or fabricating a new provenance chain for historical work.
    maintenance_sources = {e.payload["source_id"] for e in wb.history(task_id)
                           if e.kind == "agent_maintenance_recorded"}
    maintenance_sources.update(e.id for e in wb.history(task_id) if e.kind in {
        "proposal_corrected", "runtime_rebaseline_requested", "preparation_runtime_retry_requested"})
    return TaskObservation(task_id=task_id, revision=revision, user_state=state,
        focus=focus(wb, task_id), foreground=foreground, background=background,
        blocking_message_ids=[e.id for e in blocking_messages(wb, task_id)],
        maintenance_count=len(maintenance_sources),
        autonomous_eligible=not maintenance_sources).model_dump()


def mark_maintenance(wb: Workbench, task_id: str, receipt: Any, *, reason: str) -> None:
    """Operator-created evidence must not count as autonomous completion."""
    wb.append(task_id, "agent_maintenance_recorded", {"version": VERSION,
        "source_id": receipt.id, "source_hash": receipt.record_hash, "reason_code": reason,
        "counts_as_autonomous": False}, key="agent-maintenance:" + receipt.id,
        actor="orchestrator", job_id=receipt.job_id, commit=False)


def refresh_decision(wb: Workbench, task_id: str, message: Any, decision: Any) -> bool:
    """Reobserve a stale command within the original turn's four-call budget."""
    if not enabled(wb, task_id):
        return False
    from app.models import Job
    old = wb.session.get(Job, decision.job_id)
    if old is None or old.payload.get("reasoning_version") != REASONING_VERSION:
        return False
    rows = wb.history(task_id)
    consumed = old.payload.get("consumed_calls", 0) + sum(
        e.kind == "model_call" and e.job_id == old.id for e in rows)
    if consumed >= 4:
        return False
    reads = old.payload.get("consumed_reads", 0) + sum(
        e.kind == "conversation_tool_result" and e.job_id == old.id for e in rows)
    job = wb.enqueue(task_id, "conversation_intent", {**old.payload,
        "consumed_calls": consumed, "consumed_reads": reads},
        key="agent-reobserve:" + decision.id, timeout_seconds=180, max_attempts=2, commit=False)
    wb.append(task_id, "agent_decision_superseded", {"version": VERSION,
        "decision_hash": decision.record_hash, "message_id": message.id,
        "reason_code": "conversation_basis_changed"}, key="agent-superseded:" + decision.id,
        job_id=job.id, actor="orchestrator", commit=False)
    wb.append(task_id, "conversation_classifying", {"version": "conversation-v1",
        "message_id": message.id}, key="agent-reclassifying:" + decision.id,
        job_id=job.id, actor="orchestrator", commit=False)
    return True


def recovery(code: str, *, remaining: bool = False) -> dict:
    """Classify effects, not exception text. Unknown writes are never retried."""
    if code.endswith("outcome_unknown") or code in {"publisher_unknown", "publication_outcome_unknown"}:
        category, action = "external_unknown", "reconcile"
    elif "budget" in code or "context_limit" in code:
        category, action = "budget", "resume_with_budget"
    elif code.endswith("_completion_truncated"):
        category, action = "budget", "revise_output_budget"
    elif code in {"lease_expired", "worker_lease_expired"}:
        category, action = "interrupted", "reconcile_execution"
    elif code in {"edit_anchor_missing", "edit_anchor_ambiguous", "proposal_structure_invalid",
                  "conversation_decision_invalid", "planning_output_invalid"}:
        category, action = "protocol", "correct" if remaining else "inspect_diagnostic"
    elif code.startswith("environment_") or code in {"dependency_volume_missing", "environment_blocked"}:
        category, action = "environment", "restore_environment"
    elif code in {"baseline_failed", "verification_failed", "review_blocked"}:
        category, action = "code", "repair_within_scope" if remaining else "revise_plan"
    elif code in {"provider_timeout", "model_gateway_upstream_failure", "planning_provider_failed",
                  "model_connection_failed", "model_call_failed", "model_gateway_rate_limited"}:
        category, action = "transport", "retry" if remaining else "inspect_diagnostic"
    elif code == "worker_runtime_incomplete":
        category, action = "platform", "rebuild_runtime"
    else:
        category, action = "platform", "inspect_diagnostic"
    return {"version": VERSION, "category": category, "action": action,
            "reason_code": code, "uses_code_repair": category == "code"}


def advance_tasks(database: Any, settings: Any) -> None:
    """One persistent coordination entry; services retain short CAS transactions."""
    from app.conversation import advance_conversations
    from app.contribution_workflow import advance_workflows
    # Classify/consume messages before any new effect is dispatched.
    advance_conversations(database, settings)
    advance_workflows(database, settings)
