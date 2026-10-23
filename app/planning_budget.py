"""Actual lifetime consumption is independent of prompt compaction."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.jobs import TERMINAL_STATES
from app.models import Job
from app.provenance import content_hash
from app.task_states import ContributionTaskStateService
from app.workbench import Workbench, WorkbenchError

VERSION = "planning-budget-v1"
EVENT = "planning_budget_changed"
BOUNDS = {"calls": {"min": 1, "max": 256},
          "input_tokens": {"min": 1000, "max": 20_000_000},
          "output_tokens": {"min": 1000, "max": 1_000_000}}


class PlanningBudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    calls: int = Field(ge=1, le=256)
    input_tokens: int = Field(ge=1000, le=20_000_000)
    output_tokens: int = Field(ge=1000, le=1_000_000)


class PlanningBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    limits: PlanningBudgetLimits


def _limits(history: list) -> tuple[dict, str | None]:
    # Keep the established defaults; an override belongs to one immutable task
    # journal, never to a process global or provider-supplied instruction.
    from app.planner import MAX_MODEL_CALLS, MAX_TOTAL_INPUT_TOKENS, MAX_TOTAL_OUTPUT_TOKENS
    limits = {"calls": MAX_MODEL_CALLS, "input_tokens": MAX_TOTAL_INPUT_TOKENS,
              "output_tokens": MAX_TOTAL_OUTPUT_TOKENS}
    previous_hash = None
    for event in history:
        if event.kind != EVENT:
            continue
        payload = event.payload
        try:
            candidate = PlanningBudgetLimits.model_validate(payload["limits"]).model_dump()
            prior = PlanningBudgetLimits.model_validate(payload["previous_limits"]).model_dump()
            request = PlanningBudgetRequest.model_validate(payload["request"])
            recorded_usage = payload["usage_at_change"]
        except (KeyError, TypeError, ValidationError):
            raise WorkbenchError("任务预算记录无效，已停止调用。", code="planning_budget_invalid") from None
        if (event.actor_id != "local-user" or event.job_id is not None
                or payload.get("version") != VERSION
                or candidate != request.limits.model_dump()
                or not isinstance(recorded_usage, dict) or set(recorded_usage) != set(candidate)
                or any(type(value) is not int or value < 0 for value in recorded_usage.values())
                or payload.get("previous_budget_hash") != previous_hash
                or (previous_hash is not None and prior != limits)):
            raise WorkbenchError("任务预算记录无效，已停止调用。", code="planning_budget_invalid")
        limits, previous_hash = candidate, event.record_hash
    return limits, previous_hash


def planning_usage(wb: Workbench, task_id: str) -> dict:
    history = wb.history(task_id)
    limits, _ = _limits(history)
    usage = [event.payload.get("usage", {}) for event in history if event.kind in {
        "assistant_message", "conversation_usage", "model_output_rejected", "baseline_diagnosed"}]
    return {"calls": sum(event.kind == "model_call" for event in history),
        "input_tokens": sum(item.get("input_tokens", 0) for item in usage),
        "output_tokens": sum(item.get("output_tokens", 0) for item in usage),
        "limits": limits}


def planning_budget_view(wb: Workbench, task_id: str) -> dict:
    history = wb.history(task_id)
    limits, event_hash = _limits(history)
    usage = planning_usage(wb, task_id)
    state = ContributionTaskStateService(wb.session).current(task_id)
    revision = content_hash({"version": VERSION, "task_id": task_id, "event_hash": event_hash,
                             "limits": limits, "usage": usage, "state_hash": state.record_hash})
    return {"version": VERSION, "revision": revision, "record_hash": event_hash,
            "limits": limits, "usage": {key: usage[key] for key in limits},
            "remaining": {key: max(0, limits[key] - usage[key]) for key in limits},
            "bounds": BOUNDS,
            "configurable": not any(j.state not in TERMINAL_STATES for j in wb.jobs(task_id))}


def configure_planning_budget(wb: Workbench, task_id: str, request: PlanningBudgetRequest,
                              *, key: str, commit: bool = True) -> dict:
    """Authenticated local-user action; no queueing, provider call or approval."""
    from app.cla import _lock
    _lock(wb)
    history = wb.history(task_id)
    idempotency_key = "budget:" + content_hash({"task_id": task_id, "key": key})
    replay = next((event for event in history if event.idempotency_key == idempotency_key), None)
    values = request.model_dump()
    if replay is not None:
        if replay.payload.get("request") != values or replay.actor_id != "local-user":
            raise WorkbenchError("重复预算请求内容不同。", code="planning_budget_conflict")
        return planning_budget_view(wb, task_id)
    current = planning_budget_view(wb, task_id)
    if current["revision"] != request.expected_hash:
        raise WorkbenchError("任务预算或用量已变化，请刷新后重新选择。", code="planning_budget_stale")
    if not current["configurable"]:
        raise WorkbenchError("任务正在处理，请等待完成或停止后再调整预算。", code="planning_budget_busy")
    limits = request.limits.model_dump()
    if any(limits[key] < current["usage"][key] for key in limits):
        raise WorkbenchError("预算上限不能低于已经记录的用量。", code="planning_budget_below_usage")
    wb.append(task_id, EVENT, {"version": VERSION, "request": values, "limits": limits,
        "previous_limits": current["limits"], "previous_budget_hash": current["record_hash"],
        "usage_at_change": current["usage"]}, key=idempotency_key, actor="local-user", commit=False)
    if commit:
        wb.session.commit()
    return planning_budget_view(wb, task_id)


def require_planning_budget(wb: Workbench, task_id: str) -> None:
    usage = planning_usage(wb, task_id)
    names = {"calls": "调用次数", "input_tokens": "输入 token", "output_tokens": "输出 token"}
    reached = [f"{name} {usage[key]:,} / {usage['limits'][key]:,}" for key, name in names.items()
               if usage[key] >= usage["limits"][key]]
    if reached:
        raise WorkbenchError("本任务累计预算已用尽（" + "；".join(reached)
            + "）。这是任务预算限制，不能据此判断模型账户额度；已有记录保留。压缩上下文不会恢复已消耗预算，继续调用需明确追加任务预算。",
            code="planning_budget_exceeded")


def failed_budget_planning_job(wb: Workbench, task_id: str) -> Job | None:
    """Recognize budget-only interruptions, including the pre-code legacy error."""
    state = ContributionTaskStateService(wb.session).current(task_id)
    if state.to_state != "planning":
        return None
    jobs = [job for job in wb.jobs(task_id) if job.kind in {
        "planning_turn", "planning_context", "planning_archive", "planning_import_metadata"}]
    failed = jobs[-1] if jobs else None
    if (failed is None or failed.kind != "planning_turn" or failed.state != "failed"
            or failed.payload.get("baseline_recovery_hash")):
        return None
    known_error = (failed.error_code == "planning_budget_exceeded" or
        (failed.error_code == "planning_turn_failed" and failed.error_message ==
         "规划累计用量已达上限，请缩小范围；现有代码和方案已保留"))
    if not known_error:
        return None
    plan, context = wb.latest_plan(task_id), wb.latest(task_id, "context_ready")
    data = failed.payload
    if (context is None or data.get("context_id") != context.id
            or data.get("parent_id") != (plan.id if plan else None)
            or data.get("parent_hash") != (plan.record_hash if plan else None)
            or data.get("state_hash") != state.record_hash):
        return None
    return failed


def resume_budget_planning(wb: Workbench, task_id: str, *, text: str, key: str) -> Job | None:
    from app.planner import PlanningService
    failed = failed_budget_planning_job(wb, task_id)
    if failed is None:
        return None
    require_planning_budget(wb, task_id)
    data = failed.payload
    return PlanningService(wb.session, str(wb.artifacts.root)).request(task_id,
        text=data["text"] + "\nUser retry message (untrusted data): " + text,
        parent_id=data["parent_id"], expected_hash=data["parent_hash"], key=key,
        model_profile_id=data.get("model_profile_id"), commit=False)
