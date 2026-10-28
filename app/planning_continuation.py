"""Source-bound continuation of local plan preparation, never execution consent."""
from __future__ import annotations

import re
from typing import Any

from app.workbench import Workbench
from app.provenance import content_hash

VERSION = "planning-continuation-v1"


def narration_only_planning(job: Any) -> bool:
    """A legacy v2 draft job returned a reply without any planning outcome."""
    result = job.result_data or {}
    return bool(job.kind == "planning_turn" and job.state == "succeeded"
        and job.payload.get("agent_runtime_version") == "agent-runtime-v2"
        and result.get("action") == "message" and result.get("plan_status") == "not_saved"
        and not result.get("awaiting_answers"))


def resume_failed_model_planning(wb: Workbench, task_id: str, source: Any) -> Any | None:
    """An explicit continue resumes the frozen read-only step before chat routing."""
    from app.agent_runtime import enabled
    from app.conversation import fast_intent, paused
    from app.jobs import TERMINAL_STATES
    from app.task_states import ContributionTaskStateService
    from app.planner import frozen_inputs
    from app.planning_budget import require_planning_budget, planning_usage
    if (not enabled(wb, task_id) or paused(wb, task_id)
            or fast_intent(source.payload.get("text", "")) != "continue"
            or any(j.state not in TERMINAL_STATES for j in wb.jobs(task_id))):
        return None
    history = wb.history(task_id)
    if not any(e.id == source.id and e.record_hash == source.record_hash
               and e.kind == "conversation_received" and e.actor_id == "local-user" for e in history):
        return None
    state = ContributionTaskStateService(wb.session).current(task_id)
    jobs = [j for j in wb.jobs(task_id) if j.kind in {
        "planning_archive", "planning_context", "planning_turn", "planning_import_metadata"}]
    failed = jobs[-1] if jobs else None
    transport_failure = bool(failed and failed.state in {"failed", "timed_out"} and failed.error_code in {
                "model_gateway_upstream_failure", "model_gateway_network_failure",
                "model_gateway_rate_limited", "planning_provider_failed"})
    narration = failed is not None and narration_only_planning(failed)
    if (state.to_state != "planning" or failed is None or failed.kind != "planning_turn"
            or not (transport_failure or narration)):
        return None
    data = failed.payload
    context, plan = wb.latest(task_id, "context_ready"), wb.latest_plan(task_id)
    if (context is None or data.get("context_id") != context.id
            or data.get("state_hash") != state.record_hash
            or data.get("parent_id") != (plan.id if plan else None)
            or data.get("parent_hash") != (plan.record_hash if plan else None)
            or content_hash(data.get("inputs")) != content_hash(frozen_inputs(wb.session, task_id))
            or wb.models_changed_after(task_id, data.get("history_cursor", 0))):
        return None
    require_planning_budget(wb, task_id)
    if narration:
        from app.planner import MAX_PLAN_REPAIRS
        if data.get("repair_round", 0) >= MAX_PLAN_REPAIRS:
            return None
        data = {**data, "repair_round": data.get("repair_round", 0) + 1,
            "plan_repair": {"reason_code": "planning_output_invalid",
                "constraint": "plan_required_after_repair_or_read",
                "remediation": "The prior reply did not read or save anything. Return actual reads, a complete plan or material structured questions; read-only investigation needs no additional permission."}}
    job = wb.enqueue(task_id, failed.kind, data, key="planning-model-retry:" + failed.id,
        max_attempts=1, timeout_seconds=failed.timeout_seconds, commit=False)
    wb.append(task_id, "planning_model_retry_requested", {
        "version": "planning-model-retry-v1", "failed_job_id": failed.id,
        "source_id": source.id, "source_hash": source.record_hash,
        "request_hash": content_hash(data["text"]), "context_hash": context.record_hash,
        "usage_at_retry": planning_usage(wb, task_id), "execution_authorized": False,
        "retry_reason": "missing_planning_progress" if narration else failed.error_code,
    }, key="planning-model-retry-receipt:" + failed.id, actor="orchestrator",
        job_id=job.id, commit=False)
    return job


def proceed_requested(text: str) -> bool:
    value=text.strip().rstrip("。.!！")
    return bool(re.fullmatch(r"(?:请)?按(?:照)?(?:你(?:的|说的|建议的|给出的)|刚才的|上述)?(?:下一步(?:计划)?|计划|步骤|方案)(?:进行|推进|执行|继续|做)(?:吧)?",value))


def recover_missed_planning_continue(wb: Workbench, task_id: str) -> bool:
    """Recover one old continue that only narrated, never invent a new request."""
    from app.conversation import paused, pending_messages, progress_only, fast_intent
    from app.agent_runtime import mark_maintenance
    if paused(wb, task_id) or pending_messages(wb, task_id):
        return False
    history = wb.history(task_id)
    source = next((e for e in reversed(history) if e.kind == "conversation_received"
        and e.actor_id == "local-user" and not progress_only(e.payload["text"])), None)
    if source is None or fast_intent(source.payload["text"]) != "continue":
        return False
    work = [j for j in wb.jobs(task_id) if j.kind in {
        "planning_archive", "planning_context", "planning_turn", "planning_import_metadata"}]
    latest = work[-1] if work else None
    suffix = ":" + latest.id if latest and narration_only_planning(latest) else ""
    key = "planning-continue-recovered:" + source.id + suffix
    if any(e.idempotency_key == key or (e.kind == "conversation_dispatched"
           and e.payload.get("message_id") == source.id) for e in history):
        return False
    decision = next((e for e in history if e.kind == "conversation_decision"
                     and e.payload.get("message_id") == source.id), None)
    if not decision or decision.payload.get("action") != "respond":
        return False
    if any(e.sequence > source.sequence and e.kind in {
        "stop_requested", "context_ready", "plan_bound", "execution_authorized",
        "execution_change_requested", "model_changed"} for e in history):
        return False
    try:
        job = resume_failed_model_planning(wb, task_id, source)
    except WorkbenchError as error:
        if error.code == "planning_budget_exceeded":
            return False
        raise
    if job is None:
        return False
    receipt = wb.append(task_id, "planning_continue_recovered", {
        "version": "planning-model-retry-v1", "source_id": source.id,
        "source_hash": source.record_hash, "decision_hash": decision.record_hash,
        "execution_authorized": False}, key=key, actor="orchestrator", job_id=job.id, commit=False)
    mark_maintenance(wb, task_id, receipt, reason="recover_missed_planning_continue")
    wb.append(task_id, "conversation_result", {"version": "conversation-v1",
        "source_kind": "planning_retry_recovery", "source_id": receipt.id,
        "text": "已按你此前的继续请求恢复只读调研。原失败记录和已消耗预算保留，尚未授权实施。"},
        key="planning-continue-recovery-result:" + source.id + suffix, actor="orchestrator",
        job_id=job.id, commit=False)
    return True


def planning_next_step(wb: Workbench, task_id: str) -> dict | None:
    from app.agent_runtime import enabled
    from app.task_states import ContributionTaskStateService
    from app.execution_environment import task_environment
    from app.plans import PlanVersionService
    from app.verification_commands import verification_command_error
    if not enabled(wb,task_id) or ContributionTaskStateService(wb.session).current(task_id).to_state!="planning":
        return None
    plan,context=wb.latest_plan(task_id),wb.latest(task_id,"context_ready")
    if plan is None or context is None or plan.questions_for_maintainer or wb.latest(task_id,"publication_confirmed") or wb.latest(task_id,"publication_completed"):
        return None
    question=wb.latest(task_id,"assistant_message")
    if question and question.payload.get("questions"):
        return None
    evidence=wb.read(context.payload["artifact_id"])
    content=PlanVersionService._validated_content(plan)
    error=verification_command_error(content.commands_to_run,set(evidence["inventory"]),set(content.files_likely_to_change),evidence)
    environment=task_environment(wb,task_id)
    if not error and environment["status"]!="pending_read":
        return None
    value={"version":VERSION,"action":"prepare_plan","plan_id":plan.id,"plan_hash":plan.record_hash,
        "context_id":context.id,"context_hash":context.record_hash,
        "reason_codes":[error["constraint"]] if error else [],
        "read_paths":environment.get("required_paths",[]),"execution_authorized":False}
    value["binding_hash"]=content_hash(value)
    return value


def recover_failed_forward(wb: Workbench, task_id: str, settings: Any) -> bool:
    """Recover only the user's still-current explicit plan-preparation command."""
    from app.conversation import paused,pending_messages
    from app.jobs import TERMINAL_STATES
    from app.models import Job
    from app.planner import PlanningService
    if paused(wb,task_id) or pending_messages(wb,task_id):
        return False
    step=planning_next_step(wb,task_id)
    if step is None or any(j.state not in TERMINAL_STATES for j in wb.jobs(task_id)):
        return False
    history=wb.history(task_id)
    source=next((e for e in reversed(history) if e.kind=="conversation_received" and e.actor_id=="local-user"),None)
    if source is None or not proceed_requested(source.payload["text"]):
        return False
    key="recover-forward:"+source.id
    if any(e.idempotency_key==key for e in history):
        return False
    if wb.models_changed_after(task_id,source.sequence) or any(e.sequence>source.sequence and e.kind in {
        "stop_requested","execution_authorized","execution_change_requested","plan_bound","context_ready"} for e in history):
        return False
    finished=next((e for e in history if e.kind=="conversation_finished" and e.payload.get("message_id")==source.id),None)
    classified=next((e for e in reversed(history) if e.kind=="conversation_classifying" and e.payload.get("message_id")==source.id),None)
    failed=wb.session.get(Job,classified.job_id) if classified else None
    if not finished or finished.payload["status"]!="failed" or failed is None or failed.state!="failed" or failed.error_code not in {
        "conversation_tool_budget","conversation_call_budget","conversation_input_budget","conversation_decision_invalid"}:
        return False
    job=PlanningService(wb.session,str(wb.artifacts.root)).request(task_id,text=source.payload["text"],
        parent_id=step["plan_id"],expected_hash=step["plan_hash"],key=key,continuation_source=source,commit=False)
    receipt=wb.append(task_id,"agent_planning_continued",{"version":VERSION,"step":step,
        "source_id":source.id,"source_hash":source.record_hash,"failed_job_id":failed.id,
        "original_request":source.payload["text"],"execution_authorized":False},key=key,
        actor="orchestrator",job_id=job.id,plan_id=step["plan_id"],commit=False)
    from app.agent_runtime import mark_maintenance
    mark_maintenance(wb,task_id,receipt,reason="recover_failed_planning_command")
    wb.append(task_id,"conversation_result",{"version":"conversation-v1","source_kind":"planning_continuation",
        "text":"已接续你之前要求的下一步，正在读取项目配置并修订方案。新方案保存后会请你核对确认，尚未启动实施。"},
        key="forward-recovered-result:"+source.id,actor="orchestrator",job_id=job.id,commit=False)
    return True
