"""Read-only workbench projection over immutable execution evidence."""
from __future__ import annotations

from typing import Any
from sqlalchemy import select
from app.models import ExecutionAttempt, ReviewRun
from app.executions import ExecutionAttemptService
from app.execution_outcome import attempt_verification
from app.jobs import TERMINAL_STATES
from app.provenance import content_hash
from app.task_states import ContributionTaskStateService
from app.workbench import Workbench


def workbench_view(wb: Workbench, task_id: str) -> dict[str, Any]:
    events = wb.history(task_id)
    authorization = next((e for e in reversed(events) if e.kind == "execution_authorized"), None)
    jobs = wb.jobs(task_id)
    attempts = []
    roots = {}
    for event in events:
        if event.kind == "automation_started":
            root = ExecutionAttemptService(wb.session).get_verified(event.payload["attempt_id"])
            auth = next((e for e in events if e.record_hash == event.payload["authorization_hash"]), None)
            if auth:
                roots[root.plan_approval_id] = auth.id
    for row in wb.session.scalars(select(ExecutionAttempt).where(
        ExecutionAttempt.task_id == task_id).order_by(ExecutionAttempt.attempt_number)):
        attempt = ExecutionAttemptService(wb.session).get_verified(row.id)
        stage = ExecutionAttemptService(wb.session).current(attempt.id)
        review = wb.session.scalar(select(ReviewRun).where(ReviewRun.execution_attempt_id == attempt.id))
        attempts.append({"run_id": roots.get(attempt.plan_approval_id),
                         "provenance": "verified" if attempt.plan_approval_id in roots else "legacy_unverified",
                         "id": attempt.id, "number": attempt.attempt_number,
                         "plan_id": attempt.plan_version_id, "stage": stage.stage,
                         "stage_status": stage.status, "stage_hash": stage.record_hash,
                         "verification": attempt_verification(wb.session, str(wb.artifacts.root), attempt.id),
                         "review": review.verdict if review else "not_run"})
    current_attempts = [a for a in attempts if authorization and a["run_id"] == authorization.id]
    latest = current_attempts[-1] if current_attempts else (attempts[-1] if attempts and not authorization else None)
    state = ContributionTaskStateService(wb.session).current(task_id)
    busy = any(j.state not in TERMINAL_STATES and j.kind not in {"execution_question", "conversation_intent"} for j in jobs)
    blocked = wb.active_blocker(task_id)
    if blocked and authorization and blocked.sequence <= authorization.sequence:
        blocked = None
    plan = wb.latest_plan(task_id)
    from app.execution_environment import task_environment
    environment = task_environment(wb, task_id)
    from app.execution_preflight import task_preflight
    preflight = task_preflight(wb, task_id, environment=environment)
    baseline = next((e for e in reversed(events) if e.kind == "execution_baseline_ready"
                     and (not latest or e.payload["attempt_id"] == latest["id"])), None)
    context = wb.latest(task_id, "context_ready")
    binding = next((e for e in reversed(events) if e.kind == "plan_bound" and plan and e.plan_version_id == plan.id), None)
    plan_stale = bool(plan and state.to_state == "planning" and (
        plan.task_state_record_hash != state.record_hash or context is None or binding is None
        or binding.payload["context_hash"] != context.record_hash))
    blockers = []
    if plan:
        from app.plans import PlanVersionService
        from app.verification_commands import verification_command_error
        context = wb.latest(task_id, "context_ready")
        if context:
            evidence = wb.read(context.payload["artifact_id"])
            content = PlanVersionService._validated_content(plan)
            if verification_command_error(content.commands_to_run, set(evidence["inventory"]), set(content.files_likely_to_change)):
                blockers.append({"code": "verification_commands_invalid", "message": "验证命令包含安装操作或不存在的路径，请修订方案。"})
    if environment["status"] == "pending":
        blockers.append({"code": "environment_unsupported" if environment["status"] == "unsupported" else "context_pending",
                         "message": environment["reason"]})
    blockers.extend(preflight["issues"])
    if plan_stale:
        blockers.append({"code": "plan_stale", "message": "执行依据已变化，请保存新版本并重新确认。"})
    reply = wb.latest(task_id, "assistant_message")
    if (plan and plan.questions_for_maintainer) or (reply and (reply.payload.get("questions") or reply.payload.get("read_paths"))):
        blockers.append({"code": "planning_incomplete", "message": "请先回答方案问题并完成代码阅读。"})
    if wb.plan_rejection_active(task_id, plan.id if plan else None):
        blockers.append({"code": "planning_plan_invalid", "message": "新草稿未通过校验，请修订并保存方案。"})
    actions = {"can_ask": True, "can_stop": busy, "can_replan": state.to_state in {
        "plan_approved", "executing", "reviewing", "ready"},
        "can_execute": bool(plan and state.to_state == "planning" and not busy and
                            not blockers and preflight["status"] == "ready")}
    view = {"environment": environment, "preflight":preflight, "run_id": authorization.id if authorization else None,
            "baseline": baseline.payload if baseline else None,
            "runtime": {"code_reading_verified": wb.latest(task_id, "context_ready") is not None,
                        "environment_prepared": any(e.kind == "dependencies_ready" for e in events),
                        "note": "can_execute 是当前方案操作的准入状态，不是沙箱是否具备执行能力；builtin 不是环境已验证。"},
            "state": state.to_state, "busy": busy, "attempts": attempts,
            "readiness": {"scope": "execution", "blocks_repository_reading": False,
                          "blocks_planning": False, "blockers": blockers},
            "last_attempt_id": attempts[-1]["id"] if attempts else None,
            "latest_attempt_id": latest["id"] if latest else None,
            "verification": latest["verification"] if latest else {"status": "not_run", "passed": 0, "total": 0},
            "blocked": blocked.payload if blocked else None, "actions": actions,
            "plan_stale": plan_stale,
            "question_busy": any(j.kind == "execution_question" and j.state not in TERMINAL_STATES for j in jobs),
            "event_cursor": events[-1].sequence if events else 0}
    from app.execution_acceptance import acceptance_view
    view["acceptance"] = acceptance_view(wb, task_id)
    from app.publisher_accounts import account_record
    publisher = account_record(wb.session)
    view["publisher_account_hash"] = publisher.event_hash if publisher else None
    actions["can_accept"] = bool(view["acceptance"]["available"] and not view["acceptance"]["accepted"]
                                 and not busy and not blocked and latest and latest["verification"]["status"] == "passed")
    from app.pr_lifecycle import lifecycle_view
    view["pr_lifecycle"] = lifecycle_view(wb, task_id)
    view["revision"] = content_hash({**view, "jobs": [(j.id, j.state, bool(j.cancel_requested_at)) for j in jobs]})
    return view


def execution_context(wb: Workbench, task_id: str) -> dict[str, Any]:
    """Bounded, evidence-based status for both planning and progress questions."""
    view = workbench_view(wb, task_id)
    attempts = view.pop("attempts")
    view["attempt_count"] = len(attempts)
    lifecycle = view.pop("pr_lifecycle")
    snapshot = lifecycle.get("snapshot")
    view["published_pr"] = None if not snapshot else {k:snapshot[k] for k in
        ("stage", "ci", "next_actor", "next_action", "observed_at", "warnings")}
    if snapshot:
        view["published_pr"]["failures"] = snapshot.get("failures",[])[:6]
        view["published_pr"]["annotations"] = [{**a,"message":a["message"][:500]} for a in snapshot.get("annotations",[])[:6]]
        view["published_pr"]["reviews"] = [{**r,"body":r["body"][:500]} for r in snapshot["reviews"][-5:]]
        view["published_pr"]["checks"] = [{k:r.get(k) for k in ("name","status","conclusion","url")} for r in snapshot["checks"][-10:]]
    view["next_action"] = (
        snapshot["next_action"] if snapshot else
        "follow_published_pr" if wb.latest(task_id, "publication_completed") else
        ("prepare_publication_preview" if wb.latest(task_id, "publication_document_ready") else "generate_english_publication_document") if view["acceptance"]["accepted"] else
        "request_local_acceptance" if view["actions"]["can_accept"] else
        "prepare_or_repair_plan" if wb.latest_plan(task_id) is None or view["plan_stale"] or wb.plan_rejection_active(task_id, wb.latest_plan(task_id).id) or any(b["code"] == "verification_commands_invalid" for b in view["readiness"]["blockers"]) else
        "wait_for_current_job" if view["busy"] else
        "prepare_supported_environment" if view["environment"]["status"] == "unsupported" else
        "request_exact_execution_confirmation" if view["state"] == "planning" else "follow_execution_evidence"
    )
    view["recent_attempts"] = []
    for attempt in attempts[-3:]:
        verification = {**attempt["verification"]}
        verification["failures"] = [{**failure, "stderr": failure.get("stderr", "")[:1000]}
                                    for failure in verification.get("failures", [])[:6]]
        view["recent_attempts"].append({**attempt, "verification": verification})
    context = wb.latest(task_id, "context_ready")
    evidence = wb.read(context.payload["artifact_id"]) if context else None
    view["repository_snapshot"] = None if context is None else {
        "base_sha": context.payload["base_sha"], "context_hash": context.record_hash,
        "archive_downloaded": True,
        "inventory_count": len(evidence["inventory"]),
        "files": [{"path": e["path"], "status": "read" if e.get("content") is not None else "unavailable"}
                  for e in evidence["entries"]],
        "note": "已下载完整源码归档，并已读取 files 中 status=read 的文件原文；清单中的其余文件可按需补读。缓存的是固定提交，不证明刚刚刷新了上游。"}
    view["workflow"] = {
        "order": ["download_archive", "read_project_and_target_code", "prepare_plan", "confirm_plan",
                  "prepare_isolated_dependencies", "run_pre_change_tests", "implement", "verify", "review"],
        "source_reading_requires_project_dependencies": False,
        "environment_support_scope": "implementation_and_verification_only",
    }
    return view
