"""Source-bound continuation of local plan preparation, never execution consent."""
from __future__ import annotations

import re
from typing import Any

from app.workbench import Workbench
from app.provenance import content_hash

VERSION = "planning-continuation-v1"


def proceed_requested(text: str) -> bool:
    value=text.strip().rstrip("。.!！")
    return bool(re.fullmatch(r"(?:请)?按(?:照)?(?:你(?:的|说的|建议的|给出的)|刚才的|上述)?(?:下一步(?:计划)?|计划|步骤|方案)(?:进行|推进|执行|继续|做)(?:吧)?",value))


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
