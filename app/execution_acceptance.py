"""Local human acceptance, bound to immutable execution evidence."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.executions import ExecutionAttemptService
from app.models import ExecutionAttempt, WorkbenchEvent
from app.provenance import content_hash
from app.reviews import ReviewRunService
from app.task_states import ContributionTaskStateService
from app.workbench import Workbench, WorkbenchError


def acceptance_view(wb: Workbench, task_id: str) -> dict[str, Any]:
    """Acceptance of an older attempt never applies to a new plan or review."""
    state = ContributionTaskStateService(wb.session).current(task_id)
    empty = {"available": False, "binding_hash": None, "accepted": None}
    if state.to_state != "ready":
        return empty
    attempt = wb.session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.task_id == task_id).order_by(ExecutionAttempt.attempt_number.desc()).limit(1))
    plan = wb.latest_plan(task_id)
    if attempt is None or plan is None or attempt.plan_version_id != plan.id:
        return empty
    attempt = ExecutionAttemptService(wb.session).get_verified(attempt.id)
    reviews = ReviewRunService(wb.session, wb.artifacts)
    rows = reviews.list_for_attempt(attempt.id)
    review = rows[-1] if rows else None
    if review is None or review.verdict != "pass" or reviews.is_stale(review.id):
        return empty
    if any(f.get("severity") in {"high", "blocking"} or f.get("verdict") == "block"
           for f in review.findings):
        return empty
    binding = {"attempt_id": attempt.id, "attempt_hash": attempt.record_hash,
               "plan_id": plan.id, "plan_hash": plan.record_hash,
               "state_hash": state.record_hash, "review_hash": review.record_hash,
               "diff_hash": review.diff_hash, "verify_result_hash": review.verify_result_hash,
               "test_results_hash": review.test_results_hash}
    digest = content_hash(binding)
    receipt = next((e for e in reversed(wb.history(task_id))
                    if e.kind == "execution_accepted" and e.payload.get("binding_hash") == digest), None)
    return {"available": True, "binding_hash": digest, "binding": binding,
            "accepted": {"record_hash": receipt.record_hash, "text": receipt.payload["text"]}
            if receipt else None}


def accept_execution(wb: Workbench, task_id: str, *, binding_hash: str,
                     text: str, key: str, commit: bool = True) -> WorkbenchEvent:
    """A receipt grants no external write capability and requires no model."""
    from app.execution_outcome import attempt_verification
    from app.security import ensure_no_sensitive_data

    text = text.strip()
    if not text or len(text) > 8000:
        raise WorkbenchError("请填写验收确认消息")
    ensure_no_sensitive_data({"text": text}, context="execution acceptance")
    events = wb.history(task_id)
    prior = next((e for e in events if e.idempotency_key == "accept:" + key), None)
    if prior:
        if prior.payload.get("binding_hash") != binding_hash or prior.payload.get("text") != text:
            raise WorkbenchError("重复验收请求内容不同")
        return prior
    from app.cla import _lock
    from app.conversation import require_settled_messages
    _lock(wb)
    require_settled_messages(wb, task_id)
    wb.assert_idle(task_id)
    view = acceptance_view(wb, task_id)
    if not view["available"] or view["binding_hash"] != binding_hash or wb.active_blocker(task_id):
        raise WorkbenchError("执行结果已变化或尚未通过，请刷新后核对再验收", code="acceptance_stale")
    if wb.latest(task_id, "publication_confirmed"):
        raise WorkbenchError("发布已经确认，请查看发布进度")
    verification = attempt_verification(wb.session, str(wb.artifacts.root), view["binding"]["attempt_id"])
    wb.artifacts.read_bytes(view["binding"]["diff_hash"])
    wb.artifacts.read_bytes(view["binding"]["test_results_hash"])
    if verification["status"] != "passed" or verification.get("result_hash") != view["binding"]["verify_result_hash"]:
        raise WorkbenchError("测试尚未全部通过，不能确认验收")
    if view["accepted"]:
        return next(e for e in events if e.record_hash == view["accepted"]["record_hash"])
    return wb.append(task_id, "execution_accepted", {
        "binding_hash": binding_hash, **view["binding"], "text": text,
        "reply": "已记录你对当前代码、测试和审查结果的验收。下一步检查 PR 草稿并准备发布预览；发布需要单独确认。",
    }, key="accept:" + key, actor="local-user", plan_id=view["binding"]["plan_id"], commit=commit)
