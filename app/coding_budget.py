"""One explicit, attempt-bound extension of an exhausted proposal lineage.

This maintenance service has no HTTP, conversation-tool or model entry point.
The caller must have an explicit user decision for the exact 6 -> 8 extension.
It appends that decision and retries atomically; it never resets usage.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models import Job
from app.provenance import content_hash
from app.workbench import Workbench, WorkbenchError

VERSION = "coding-budget-extension-v1"
EVENT = "coding_budget_extended"
DEFAULT_LIMIT = 6
EXTENDED_LIMIT = 8


class CodingBudgetDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal["coding-budget-extension-v1"] = VERSION
    task_id: str = Field(min_length=1, max_length=80)
    root_job_id: str = Field(min_length=1, max_length=80)
    failed_job_id: str = Field(min_length=1, max_length=80)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_limit: Literal[8]
    instruction_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _root(wb: Workbench, task_id: str, root_job_id: str) -> tuple[Job, Any, dict]:
    from app.executions import ExecutionAttemptService
    root = wb.session.get(Job, root_job_id)
    if (root is None or root.kind != "change_set_proposal"
            or root.payload.get("version") != "change-set-proposal-job-v2"
            or root.payload_hash != content_hash(root.payload)
            or root.payload.get("root_job_id", root.id) != root.id):
        raise WorkbenchError("提案预算的原始任务不匹配。", code="coding_budget_invalid")
    attempt = ExecutionAttemptService(wb.session).get_verified(root.payload["execution_attempt_id"])
    if attempt.task_id != task_id:
        raise WorkbenchError("提案预算不属于当前任务。", code="coding_budget_invalid")
    binding = {"task_id":task_id,"root_job_id":root.id,"root_payload_hash":root.payload_hash,
        "attempt_id":attempt.id,"attempt_hash":attempt.record_hash,
        "plan_id":attempt.plan_version_id,"plan_hash":attempt.plan_record_hash,
        "context_hash":root.payload["context_hash"],"conversation_hash":root.payload["conversation_hash"]}
    return root, attempt, binding


def proposal_budget(wb: Workbench, task_id: str, root_job_id: str) -> dict:
    """Read the immutable root, original usage and an optional exact grant."""
    _, _, binding = _root(wb, task_id, root_job_id)
    history = wb.history(task_id)
    calls = [e for e in history if e.kind == "agent_code_call"
             and e.payload.get("root_job_id") == root_job_id]
    limit, grant_hash = DEFAULT_LIMIT, None
    for event in history:
        if event.kind != EVENT or event.payload.get("root_job_id") != root_job_id:
            continue
        value = event.payload
        try:
            request = CodingBudgetDecision.model_validate(value["request"])
        except (KeyError, ValidationError, TypeError):
            raise WorkbenchError("提案预算授权记录无效。", code="coding_budget_invalid") from None
        prior_revision = content_hash({"version":VERSION,"binding":binding,
            "used":DEFAULT_LIMIT,"limit":DEFAULT_LIMIT,"grant_hash":None})
        if (grant_hash is not None or set(value) != {"version","root_job_id","request","binding",
                "previous_limit","new_limit","used_calls_at_grant","consent_source"}
                or event.actor_id != "maintenance" or event.job_id != request.failed_job_id
                or event.plan_version_id != binding["plan_id"]
                or value["version"] != VERSION or value["binding"] != binding
                or request.task_id != task_id or request.root_job_id != root_job_id
                or request.expected_revision != prior_revision
                or value["consent_source"] != "explicit_user_instruction"
                or type(value["previous_limit"]) is not int or value["previous_limit"] != DEFAULT_LIMIT
                or type(value["new_limit"]) is not int or value["new_limit"] != EXTENDED_LIMIT
                or type(value["used_calls_at_grant"]) is not int or value["used_calls_at_grant"] != DEFAULT_LIMIT
                or sum(e.sequence < event.sequence for e in calls) != DEFAULT_LIMIT):
            raise WorkbenchError("提案预算授权与原始依据不符。", code="coding_budget_invalid")
        limit, grant_hash = EXTENDED_LIMIT, event.record_hash
    return {"version":VERSION,"binding":binding,"limit":limit,"used":len(calls),
        "remaining":max(0,limit-len(calls)),"grant_hash":grant_hash,
        "revision":content_hash({"version":VERSION,"binding":binding,"used":len(calls),
            "limit":limit,"grant_hash":grant_hash})}


def extend_and_resume(wb: Workbench, request: CodingBudgetDecision, settings: Any,
                      *, commit: bool = True) -> Job:
    """Apply an exact user-directed two-call grant, once, with no new approval."""
    from app.cla import _lock
    from app.coding_reads import _current
    from app.coding import CodingConversationService
    from app.execution_recovery import require_recovery_authorization
    from app.conversation import require_settled_messages
    from app.contribution_workflow import retry_execution_job
    from app.agent_runtime import mark_maintenance
    from app.jobs import TERMINAL_STATES
    _lock(wb)
    root, attempt, binding = _root(wb, request.task_id, request.root_job_id)
    key = "coding-budget-extension:" + root.id
    existing = next((e for e in wb.history(request.task_id) if e.idempotency_key == key), None)
    if existing:
        proposal_budget(wb, request.task_id, root.id)
        if existing.payload.get("request") != request.model_dump():
            raise WorkbenchError("本提案链已追加过预算，重复请求不同。", code="coding_budget_conflict")
        return wb.session.get(Job, request.failed_job_id)
    budget = proposal_budget(wb, request.task_id, root.id)
    if (budget["revision"] != request.expected_revision or budget["used"] != DEFAULT_LIMIT
            or budget["limit"] != DEFAULT_LIMIT):
        raise WorkbenchError("提案预算或用量已变化。", code="coding_budget_stale")
    require_recovery_authorization(wb, attempt, settings)
    require_settled_messages(wb, request.task_id)
    failed = wb.session.get(Job, request.failed_job_id)
    if (failed is None or failed.kind != "change_set_proposal" or failed.state != "failed"
            or failed.error_code not in {"proposal_budget_exceeded","proposal_call_budget_exceeded"}
            or failed.cancel_requested_at or failed.attempt_count >= failed.max_attempts
            or failed.payload.get("root_job_id", failed.id) != root.id
            or failed.payload.get("execution_attempt_id") != attempt.id
            or any(j.state not in TERMINAL_STATES for j in wb.jobs(request.task_id))):
        raise WorkbenchError("当前失败任务不能追加调用预算。", code="coding_budget_stale")
    coding, _, _ = _current(wb, failed.payload)
    if CodingConversationService(wb.session, artifact_root=str(wb.artifacts.root)).proposals(coding.id):
        raise WorkbenchError("已经产生修改草案，不能追加此提案预算。", code="coding_budget_stale")
    with wb.session.begin_nested():
        event = wb.append(request.task_id, EVENT, {"version":VERSION,"root_job_id":root.id,
            "request":request.model_dump(),"binding":binding,"previous_limit":DEFAULT_LIMIT,
            "new_limit":EXTENDED_LIMIT,"used_calls_at_grant":budget["used"],
            "consent_source":"explicit_user_instruction"}, key=key, actor="maintenance",
            job_id=failed.id, plan_id=attempt.plan_version_id, commit=False)
        result = retry_execution_job(wb, failed.id, settings, commit=False)
        mark_maintenance(wb, request.task_id, event, reason="explicit_proposal_budget_extension")
    if commit:
        wb.session.commit()
    return result
