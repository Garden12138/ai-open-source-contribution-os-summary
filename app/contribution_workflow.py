"""User-authorized orchestration of existing isolated execution services."""

from __future__ import annotations
import logging
import traceback
from datetime import timezone
from sqlalchemy import select
from app.approvals import PlanApprovalService, ApprovalInputFingerprint
from app.authorizations import UserAction
from app.coding import CodingContextService, CodingConversationService
from app.config import Settings
from app.execution_control import ExecutionStageControlService
from app.executions import ExecutionAttemptService
from app.jobs import JobService, TERMINAL_STATES
from app.models import Job, ExecutionAttempt, ReviewRun, ExecutionStageRun, WorkbenchEvent
from app.plan_locks import PlanLockService
from app.provenance import content_hash, canonical_json
from app.providers.contracts import ProviderIdentity
from app.providers.nvidia_nim import (
    NVIDIA_NIM_ADAPTER_VERSION,
    NVIDIA_NIM_MODEL_VERSION,
)
from app.providers.runtime import resolve_job_spec_signer
from app.review_provider import ProviderReviewService
from app.reviews import ReviewRunService
from app.sandbox_worker.specs import SandboxPolicy
from app.task_states import ContributionTaskStateService
from app.workbench import Workbench, WorkbenchError
from app.workbench_worker import WorkbenchJobWorker

START = "contribution_start"


def model_identity(model: str) -> ProviderIdentity:
    return ProviderIdentity(
        provider="nvidia_nim",
        adapter_version=NVIDIA_NIM_ADAPTER_VERSION,
        model=model,
        model_version=NVIDIA_NIM_MODEL_VERSION,
    )


def authorize_execution(
    wb: Workbench,
    task_id: str,
    *,
    plan_id: str,
    plan_hash: str,
    key: str,
    settings: Settings,
) -> Job:
    rows = wb.history(task_id)
    old = next((r for r in rows if r.idempotency_key == "authorize:" + key), None)
    if old:
        if old.plan_version_id != plan_id or old.payload["plan_hash"] != plan_hash:
            raise WorkbenchError("执行确认的内容已变化")
        return wb.session.get(Job, old.job_id)
    from app.cla import _lock
    from app.conversation import require_settled_messages
    _lock(wb)
    require_settled_messages(wb, task_id)
    planning_state_hash = wb.require_planning(task_id)
    wb.assert_idle(task_id)
    from app.model_settings import ModelSettingsService, task_identity
    model_service = ModelSettingsService(wb.session)
    profiles = model_service.task_profiles(task_id, bind=True)
    if (
        (not profiles.get("implementation") and settings.implementation_provider != "nvidia_nim")
        or (not profiles.get("review") and settings.review_provider != "nvidia_nim")
    ):
        raise WorkbenchError("自动执行需要配置实现和独立审查模型")
    if (
        settings.sandbox_stage_runtime != "docker"
        or resolve_job_spec_signer(settings) is None
    ):
        raise WorkbenchError("自动执行需要已配置的隔离 Sandbox Worker")
    plan = wb.latest_plan(task_id)
    if plan is None or plan.id != plan_id or plan.record_hash != plan_hash:
        raise WorkbenchError("方案已更新，请重新确认")
    if plan.task_state_record_hash != planning_state_hash:
        raise WorkbenchError("方案执行依据已变化，请保存新版本并重新确认", code="plan_stale")
    binding = wb.binding(task_id, plan.id)
    if wb.models_changed_after(task_id, binding.sequence):
        raise WorkbenchError("模型已切换，请重新保存方案或让 AI 生成新版本后再批准")
    context = wb.latest(task_id, "context_ready")
    if context is None or binding.payload["context_hash"] != context.record_hash:
        raise WorkbenchError("代码依据已变化，请保存新版本并重新确认方案。", code="plan_stale")
    if (
        context.payload["policy_hash"] != SandboxPolicy().policy_hash
        or context.payload["image"] != settings.workbench_runner_image
    ):
        raise WorkbenchError("隔离策略或镜像已变化，请重新读取代码")
    reply = wb.latest(task_id, "assistant_message")
    if plan.questions_for_maintainer or (
        reply and (reply.payload.get("questions") or reply.payload.get("read_paths") or reply.payload.get("read_context"))
    ):
        raise WorkbenchError("请先回答方案中的问题并完成代码阅读")
    if wb.plan_rejection_active(task_id, plan.id):
        raise WorkbenchError("新草稿未通过校验，请修订并保存方案", code="planning_plan_invalid")
    from app.execution_environment import task_environment
    from app.verification_commands import verification_command_error
    evidence = wb.read(context.payload["artifact_id"])
    from app.plans import PlanVersionService
    content = PlanVersionService._validated_content(plan)
    if verification_command_error(content.commands_to_run, set(evidence["inventory"]), set(content.files_likely_to_change)):
        raise WorkbenchError("方案中的验证命令需要修订：依赖准备必须独立，路径必须存在。", code="verification_commands_invalid")
    environment = task_environment(wb, task_id)
    if environment["status"] == "unsupported":
        raise WorkbenchError(environment["reason"], code="environment_unsupported")
    from app.execution_preflight import task_preflight
    preflight = task_preflight(wb, task_id, environment=environment)
    if preflight["status"] != "ready":
        issue = preflight["issues"][0] if preflight["issues"] else {"code":"execution_preflight_failed", "message":"执行条件尚未校验完成。"}
        raise WorkbenchError(issue["message"], code=issue["code"])
    from app.workspace_scope import selected_workspace_paths
    workspace_paths = selected_workspace_paths(environment, set(evidence["inventory"]), set(content.files_likely_to_change))
    if len(workspace_paths) > 64:
        raise WorkbenchError("工作区范围超过预算，请缩小方案。", code="workspace_scope_unsupported")
    payload = {
        "workflow_version": "sandbox-workflow-v2",
        "workspace_scope_version": "source-tree-scope-v1",
        "workspace_paths": list(workspace_paths),
        "workspace_scope_hash": content_hash(list(workspace_paths)),
        "environment": environment,
        "preflight_hash": preflight["report_hash"],
        "capability_version": preflight["version"],
        "task_id": task_id,
        "plan_id": plan.id,
        "plan_hash": plan.record_hash,
        "binding_hash": binding.record_hash,
        "context": context.payload,
        "context_hash": context.record_hash,
        "max_repairs": 2,
        "implementation": task_identity(wb.session, settings, task_id, "implementation").hash_payload(),
        "review": task_identity(wb.session, settings, task_id, "review").hash_payload(),
        "actions": [
            "approve_plan",
            "start_execution",
            "accept_plan_bound_changes",
            "prepare_locked_dependencies",
            "run_pre_change_baseline",
            "verify",
            "start_review",
            "repair_twice",
        ],
    }
    if preflight.get("baseline_failure_policy"):
        payload["baseline_failure_policy"] = preflight["baseline_failure_policy"]
    job, _ = JobService(wb.session).enqueue(
        kind=START,
        idempotency_key="execute:" + key,
        payload=payload,
        max_attempts=3,
        timeout_seconds=180,
        commit=False,
    )
    wb.append(
        task_id,
        "execution_authorized",
        payload,
        key="authorize:" + key,
        actor="local-user",
        job_id=job.id,
        plan_id=plan.id,
        commit=False,
    )
    wb.session.commit()
    return job


class ContributionStartWorker(WorkbenchJobWorker):
    kinds = (START,)

    def __init__(self, database, settings: Settings, github_factory, *, worker_id: str):
        super().__init__(database, worker_id=worker_id)
        self.settings, self.github_factory = settings, github_factory

    async def execute(self, job: Job) -> dict:
        data = job.payload
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            authorization = next(
                (
                    e
                    for e in wb.history(data["task_id"])
                    if e.kind == "execution_authorized" and e.job_id == job.id
                ),
                None,
            )
            if authorization is None or authorization.payload_hash != job.payload_hash:
                raise WorkbenchError("缺少当前执行授权")
            plan = wb.latest_plan(data["task_id"])
            if plan.id != data["plan_id"] or plan.record_hash != data["plan_hash"]:
                raise WorkbenchError("批准的方案已变化")
            if data.get("preflight_hash"):
                from app.execution_preflight import task_preflight
                current_preflight = task_preflight(wb, data["task_id"])
                if current_preflight["status"] != "ready" or current_preflight["report_hash"] != data["preflight_hash"]:
                    raise WorkbenchError("执行能力或冻结依据已变化，需要重新确认方案。", code="execution_preflight_stale")
            from app.planner import frozen_inputs

            inputs = frozen_inputs(session, data["task_id"])
            if content_hash(inputs) != data["context"]["inputs_hash"]:
                raise WorkbenchError("机会或分析依据已变化")
        async with self.github_factory() as github:
            _, sha = await github.resolve_repository_head(
                inputs["repository"], data["context"]["branch"]
            )
        if sha != data["context"]["base_sha"]:
            raise WorkbenchError("上游提交已变化，请重新读取代码并确认方案")
        with self.database.session() as session:
            self.check(session, job)
            wb = Workbench(session, self.settings.artifact_root)
            if (
                wb.latest(data["task_id"], "stop_requested")
                and wb.latest(data["task_id"], "stop_requested").sequence
                > authorization.sequence
            ):
                raise WorkbenchError("执行授权已撤回")
            binding = wb.binding(data["task_id"], plan.id)
            if binding.record_hash != data["binding_hash"]:
                raise WorkbenchError("方案代码依据已变化")
            lock = PlanLockService(session).create(
                plan_version_id=plan.id,
                base_commit_sha=sha,
                idempotency_key="auto-lock:" + job.id,
            )
            approval = PlanApprovalService(session).approve(
                plan_lock_id=lock.id,
                action=UserAction.APPROVE_PLAN,
                actor_type="local_user",
                actor_id="local-user",
                idempotency_key="auto-approve:" + job.id,
            )
            observed = ApprovalInputFingerprint(
                analysis_version_id=lock.analysis_version_id,
                snapshot_id=lock.snapshot_id,
                base_commit_sha=sha,
                provider_contract_hash=lock.provider_contract_hash,
                plan_version_id=plan.id,
                plan_content_hash=plan.content_hash,
                plan_record_hash=plan.record_hash,
            )
            attempt = ExecutionAttemptService(session).start(
                approval_id=approval.id,
                observed=observed,
                action=UserAction.START_EXECUTION,
                actor_type="local_user",
                actor_id="local-user",
                idempotency_key="auto-start:" + job.id,
                repository_archive_hash=data["context"]["archive_hash"],
                runner_image_digest=data["context"]["image"],
                sandbox_policy=SandboxPolicy(),
            )
            wb.append(
                data["task_id"],
                "automation_started",
                {
                    "attempt_id": attempt.id,
                    "authorization_hash": authorization.record_hash,
                },
                key="automation:" + job.id,
                plan_id=plan.id,
            )
            schedule(wb, attempt.id, self.settings)
        return {"execution_attempt_id": attempt.id}


def schedule(wb: Workbench, attempt_id: str, settings: Settings) -> None:
    signer = resolve_job_spec_signer(settings)
    if signer is None:
        raise WorkbenchError("执行签名配置不可用")
    attempts = ExecutionAttemptService(wb.session)
    if attempts.current(attempt_id).stage == "verify":
        from app.execution_environment import schedule_verification
        schedule_verification(wb, attempt_id, signer)
        return
    spec = attempts.build_current_job_spec(attempt_id)
    run = ExecutionStageControlService(wb.session).schedule_current(
        attempt_id,
        signed_job_spec=signer.sign(spec),
        signer=signer,
        sandbox_policy=SandboxPolicy(),
        idempotency_key="auto-stage:" + spec.spec_hash[:48],
    )
    track(
        wb, attempts.get_verified(attempt_id).task_id, wb.session.get(Job, run.job_id)
    )


def track(wb: Workbench, task_id: str, job: Job) -> None:
    wb.append(
        task_id, "job_queued", {"kind": job.kind}, key="event:" + job.id, job_id=job.id
    )


def stop_workflow(wb: Workbench, task_id: str, *, key: str, commit: bool = True) -> None:
    connection = wb.session.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        # Serialize the scope snapshot with child-job creation. Otherwise a worker
        # could complete the parent and enqueue a child between the read and stop.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    previous = next((event for event in wb.history(task_id)
                     if event.idempotency_key == "stop:" + key), None)
    if previous:
        if previous.kind != "stop_requested" or previous.actor_id != "local-user":
            raise WorkbenchError("重复停止请求的内容不同")
        return
    jobs = [job for job in wb.jobs(task_id) if job.state not in TERMINAL_STATES]
    if any(job.kind in {'cla_write', 'pr_update_write', 'pr_ready_write', 'publication_write'} for job in jobs):
        raise WorkbenchError('远端操作已确认，只能完成或只读核对结果，不能中途取消', code='cla_write_in_progress')
    # Freeze the cancellation scope with the journal event in the same transaction.
    # A late/replayed coordinator tick must never cancel a later planning request.
    wb.append(task_id, "stop_requested", {"job_ids": [job.id for job in jobs]},
              key="stop:" + key, actor="local-user", commit=False)
    for job in jobs:
        JobService(wb.session).request_cancel(job.id, commit=False)
    if commit:
        wb.session.commit()


def _jobs_for_stop(wb: Workbench, task_id: str, stop: WorkbenchEvent) -> list[Job]:
    jobs = wb.jobs(task_id)
    if "job_ids" in stop.payload:
        identifiers = stop.payload["job_ids"]
        if not isinstance(identifiers, list) or any(not isinstance(value, str) for value in identifiers):
            raise WorkbenchError("停止任务范围无效")
        return [job for job in jobs if job.id in identifiers]
    # Legacy stop events had no scope. Respect their original UTC boundary without
    # rewriting history or treating the event as a permanent task-wide prohibition.
    boundary = stop.created_at.replace(tzinfo=timezone.utc)
    return [job for job in jobs if job.created_at.replace(tzinfo=timezone.utc) <= boundary]


def advance_workflows(database, settings: Settings) -> None:
    """One bounded, idempotent transition per task per coordinator tick."""
    from app.models import WorkbenchEvent
    from app.task_visibility import visibility_expression

    with database.session() as session:
        cursor = getattr(database, "_workbench_scan_cursor", "")
        ids = list(
            session.scalars(
                select(WorkbenchEvent.task_id)
                .where(
                    WorkbenchEvent.kind.in_(("automation_started", "stop_requested")),
                    WorkbenchEvent.task_id > cursor,
                    visibility_expression(WorkbenchEvent.task_id) == "active",
                )
                .distinct()
                .order_by(WorkbenchEvent.task_id)
                .limit(100)
            )
        )
        # Ephemeral scan position only; all business transitions remain durable.
        # Completed tasks must not permanently starve tasks beyond the first page.
        database._workbench_scan_cursor = ids[-1] if len(ids) == 100 else ""
    for task_id in ids:
        with database.session() as session:
            wb = Workbench(session, settings.artifact_root)
            try:
                _advance(wb, task_id, settings)
            except Exception as exc:
                session.rollback()
                logging.getLogger(__name__).warning("Workflow advancement failed: exception=%s", type(exc).__name__)
                # Archiving may win the race after this tick selected the task.
                if session.scalar(select(visibility_expression(task_id))) != "active":
                    continue
                # A durable blocker stops the chain; never fall back to fake execution.
                latest = wb.latest(task_id, "automation_started")
                if latest:
                    # Preserve useful locations without exception text, SQL,
                    # parameters, model output, credentials or local variables.
                    diagnostic = {
                        "version": "workflow-diagnostic-v1",
                        "start_hash": latest.record_hash,
                        "exception_type": type(exc).__name__,
                        "frames": [{"module": frame.filename.rsplit("/", 1)[-1],
                                    "function": frame.name, "line": frame.lineno}
                                   for frame in traceback.extract_tb(exc.__traceback__)[-12:]],
                    }
                    diagnostic_hash = wb.artifact(diagnostic)
                    resumed = wb.latest(task_id, "automation_resumed")
                    blocker_key = "blocked:" + latest.id + ":" + (resumed.id if resumed else "initial")
                    if any(e.idempotency_key == blocker_key for e in wb.history(task_id)):
                        session.rollback()
                        continue
                    wb.append(task_id, "workflow_diagnostic", {"artifact_id": diagnostic_hash},
                              key="diagnostic:" + latest.id + ":" + (resumed.id if resumed else "initial"))
                    failed_job = session.get(Job, exc.job_id) if getattr(exc, "job_id", None) else None
                    wb.append(
                        task_id,
                        "automation_blocked",
                        {
                            "reason_code": getattr(exc, "code", "workflow_failed"),
                            "retryable": bool(failed_job and failed_job.kind in {"coding_turn", "change_set_proposal", "provider_review", "baseline_diagnosis"}
                                              and failed_job.attempt_count < failed_job.max_attempts),
                            "reason": (
                                str(exc)
                                if isinstance(exc, WorkbenchError)
                                else "执行推进失败，需要人工检查"
                            )
                        },
                        key="blocked:" + latest.id + ":" + (
                            wb.latest(task_id, "automation_resumed").id
                            if wb.latest(task_id, "automation_resumed") else "initial"),
                        job_id=getattr(exc, "job_id", None),
                    )


def retry_execution_job(wb: Workbench, job_id: str, settings: Settings, *, commit: bool = True) -> Job:
    """Resume a retryable model subtask without granting a new execution budget."""
    from app.coding import CodingConversationService, coding_conversation_hash
    from app.model_settings import task_identity
    from app.jobs import JobTransitionError

    job = JobService(wb.session).get(job_id)
    if job.kind not in {"coding_turn", "change_set_proposal", "provider_review", "baseline_diagnosis"}:
        raise JobTransitionError("Only model subtasks support workflow resume")
    attempts = ExecutionAttemptService(wb.session)
    attempt = attempts.get_verified(job.payload["execution_attempt_id"])
    task_id = attempt.task_id
    events = wb.history(task_id)
    start = wb.latest(task_id, "automation_started")
    auth = wb.latest(task_id, "execution_authorized")
    blocked = wb.active_blocker(task_id)
    if start is None or auth is None:
        return JobService(wb.session).retry(job_id, commit=commit)
    stop = wb.latest(task_id, "stop_requested")
    latest_attempt = wb.session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.task_id == task_id).order_by(ExecutionAttempt.attempt_number.desc()).limit(1))
    plan = wb.latest_plan(task_id)
    if (start.payload["authorization_hash"] != auth.record_hash
            or latest_attempt != attempt.id or plan is None or plan.id != attempt.plan_version_id
            or auth.plan_version_id != attempt.plan_version_id
            or (stop and stop.sequence > start.sequence)
            or any(e.kind == "awaiting_acceptance" and e.sequence > start.sequence for e in events)
            or auth.payload["implementation"] != task_identity(wb.session, settings, task_id, "implementation").hash_payload()
            or auth.payload["review"] != task_identity(wb.session, settings, task_id, "review").hash_payload()
            or attempts.current(attempt.id).stage != ("verify" if job.kind == "provider_review" else "explore")
            or attempts.current(attempt.id).status != "succeeded"):
        raise JobTransitionError("执行依据已变化或任务已停止，不能恢复旧子任务")
    if job.kind == "baseline_diagnosis":
        from app.baseline_feedback import require_inputs
        require_inputs(wb, attempt.id, settings, baseline_hash=job.payload["baseline_hash"])
    elif job.kind == "provider_review":
        from app.review_provider import freeze_review_inputs, _require_job_matches
        _require_job_matches(job.payload, freeze_review_inputs(wb.session,
            execution_attempt_id=attempt.id, artifacts=wb.artifacts),
            task_identity(wb.session, settings, task_id, "review"))
    else:
        conversation = CodingConversationService(wb.session, artifact_root=str(wb.artifacts.root))
        coding = conversation.require_session(attempt.id)
        turns = conversation.turns(coding.id)
        if job.payload.get("context_hash") != coding.context_hash or not turns:
            raise JobTransitionError("Coding Job context is stale")
        if job.kind == "change_set_proposal" and (
                job.payload.get("conversation_hash") != coding_conversation_hash(turns)
                or job.payload.get("latest_turn_hash") != turns[-1].record_hash):
            raise JobTransitionError("Proposal Job conversation is stale")
        if job.kind == "coding_turn" and job.payload.get("user_turn_id") != turns[-1].id:
            raise JobTransitionError("Coding turn Job conversation is stale")
    # A repeated request must not enqueue another attempt or consume another budget.
    if job.state in {"queued", "leased", "running", "succeeded"}:
        resumed = wb.latest(task_id, "automation_resumed")
        if resumed and resumed.job_id == job.id and not blocked:
            return job
        raise JobTransitionError("Coding Job is not retryable")
    if blocked is None:
        return JobService(wb.session).retry(job_id, commit=commit)
    failures = [j for j in wb.jobs(task_id) if j.payload.get("execution_attempt_id") == attempt.id
                and j.state in {"failed", "cancelled", "timed_out"}]
    if {j.id for j in failures} != {job.id} or job.state != "failed":
        raise JobTransitionError("存在其他失败或取消的子任务，不能自动恢复")
    # The event and retry share one transaction. Unique event sequence/key prevents
    # two concurrent resume requests from independently extending the run.
    wb.append(task_id, "automation_resumed", {
        "start_hash": start.record_hash, "authorization_hash": auth.record_hash,
        "blocked_hash": blocked.record_hash, "attempt_id": attempt.id,
        "job_id": job.id, "job_attempt_count": job.attempt_count,
    }, key=f"resume:{job.id}:{job.attempt_count}", job_id=job.id,
       plan_id=attempt.plan_version_id, commit=False)
    result = JobService(wb.session).retry(job.id, commit=False)
    if commit:
        wb.session.commit()
    return result


def _advance(wb: Workbench, task_id: str, settings: Settings) -> None:
    events = wb.history(task_id)
    start = wb.latest(task_id, "automation_started")
    stop = wb.latest(task_id, "stop_requested")
    latest_auth = wb.latest(task_id, "execution_authorized")
    if stop and latest_auth and latest_auth.sequence > stop.sequence:
        stop = None
    if stop and (not start or stop.sequence > start.sequence):
        jobs = _jobs_for_stop(wb, task_id, stop)
        for job in jobs:
            if job.state not in TERMINAL_STATES:
                JobService(wb.session).request_cancel(job.id)
        if any(j.state not in TERMINAL_STATES for j in jobs):
            return
        states = ContributionTaskStateService(wb.session)
        current = states.current(task_id)
        if current.to_state in {"plan_approved", "executing", "reviewing", "ready"}:
            states.transition(
                task_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                to_state="planning",
                reason_code="user_replan",
            )
        change = wb.latest(task_id, "execution_change_requested")
        if change and change.payload.get("stop_key") == stop.idempotency_key:
            if any(e.idempotency_key == "request:change-plan:" + change.id for e in wb.history(task_id)):
                return
            from app.planner import PlanningService
            planner = PlanningService(wb.session, settings.artifact_root)
            latest = wb.latest_plan(task_id)
            planner.request(task_id, text=change.payload["text"], parent_id=latest.id if latest else None,
                expected_hash=latest.record_hash if latest else None, key="change-plan:" + change.id)
        return
    if latest_auth and start and latest_auth.sequence > start.sequence:
        return
    if not start or any(
        e.kind == "awaiting_acceptance" and e.sequence > start.sequence
        for e in events
    ):
        return
    if any(e.kind == "baseline_replan_requested" and e.payload.get("start_hash") == start.record_hash
           for e in events):
        return
    authorization = next(
        e for e in events if e.record_hash == start.payload["authorization_hash"]
    )
    from app.model_settings import task_identity
    if (
        authorization.payload["implementation"]
        != task_identity(wb.session, settings, task_id, "implementation").hash_payload()
        or authorization.payload["review"]
        != task_identity(wb.session, settings, task_id, "review").hash_payload()
    ):
        raise WorkbenchError("模型配置已变化", code="model_stale")
    attempts = ExecutionAttemptService(wb.session)
    attempt = wb.session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.task_id == task_id)
        .order_by(ExecutionAttempt.attempt_number.desc())
        .limit(1)
    )
    if attempt.plan_version_id != authorization.plan_version_id:
        raise WorkbenchError("执行方案已变化")
    from app.execution_recovery import advance_environment_recovery
    if advance_environment_recovery(wb, attempt, settings):
        return
    from app.baseline_feedback import advance_feedback
    if advance_feedback(wb, attempt, settings):
        return
    blocked = wb.active_blocker(task_id)
    if blocked:
        return
    stage = attempts.current(attempt.id)
    from app.proposal_recovery import corrected_proposal
    corrected = corrected_proposal(wb, attempt.id, authorization.record_hash)
    relevant = [
        j
        for j in wb.jobs(task_id)
        if j.payload.get("execution_attempt_id") == attempt.id
    ]
    # Stage jobs are also tracked after automatic Implement -> Verify transitions.
    for run in wb.session.scalars(
        select(ExecutionStageRun).where(
            ExecutionStageRun.execution_attempt_id == attempt.id
        )
    ):
        job = wb.session.get(Job, run.job_id)
        track(wb, task_id, job)
        if job.id not in {j.id for j in relevant}:
            relevant.append(job)
    stage_runs = list(wb.session.scalars(select(ExecutionStageRun).where(
        ExecutionStageRun.execution_attempt_id == attempt.id).order_by(ExecutionStageRun.created_at)))
    latest_runs = {}
    for run in stage_runs:
        latest_runs[run.stage] = run
    superseded = {r.job_id for r in stage_runs if latest_runs[r.stage].id != r.id}
    for receipt in wb.history(task_id):
        if receipt.kind in {"verification_environment_restored", "preparation_runtime_retry_requested"} and receipt.payload.get("attempt_id") == attempt.id:
            superseded.update(receipt.payload.get("superseded_preparation_job_ids", []))
    if corrected:
        superseded.add(corrected[0].job_id)
    relevant = [j for j in relevant if j.id not in superseded]
    failed_jobs = [
        j for j in relevant if j.state in {"failed", "cancelled", "timed_out"}
    ]
    if failed_jobs:
        first = failed_jobs[0]
        err_msg = first.error_message or first.error_code or "执行失败"
        error = WorkbenchError(f"子任务 {first.kind} 失败: {err_msg}", code=first.error_code or "workflow_failed")
        error.job_id = first.id
        raise error
    if any(j.state not in TERMINAL_STATES for j in relevant):
        return
    if stage.status == "pending":
        schedule(wb, attempt.id, settings)
        return
    if stage.status == "running":
        return
    if stage.status != "succeeded":
        raise WorkbenchError(
            f"执行阶段未成功: {stage.stage} ({stage.reason_code or stage.status})"
        )
    if stage.stage == "explore":
        if authorization.payload.get("workflow_version") == "sandbox-workflow-v2":
            from app.sandbox_worker.workspace_jobs import preparation_receipt, enqueue_preparation, preparation_needs_restore
            receipt = preparation_receipt(wb, attempt.id)
            if receipt is None:
                track(wb, task_id, enqueue_preparation(wb, attempt.id))
                return
            if preparation_needs_restore(wb, receipt):
                track(wb, task_id, enqueue_preparation(wb, attempt.id, replaces=receipt))
                return
            from app.execution_outcome import baseline_outcome
            baseline_value = wb.read(receipt.payload["result_hash"])
            outcome = baseline_outcome(baseline_value)
            if outcome["status"] == "environment_blocked":
                raise WorkbenchError("修改前基线无法启动，测试环境未就绪；未进入代码修改。", code="environment_blocked")
        if corrected:
            from app.changesets import ChangeSetService, ChangeSetStore
            receipt, change_set = corrected
            wb.append(task_id, "automatic_change_accepted", {
                "authorization_hash":authorization.record_hash,
                "change_set_hash":change_set.change_set_hash,"attempt_id":attempt.id,
                "correction_hash":receipt.record_hash,
            }, key="accept-corrected:" + receipt.id, plan_id=attempt.plan_version_id, commit=False)
            changes = ChangeSetService(wb.session, ChangeSetStore(settings.artifact_root), resolve_job_spec_signer(settings))
            changes.accept_existing(attempt.id, change_set=change_set, idempotency_key="apply-corrected:" + receipt.id)
            return
        context = CodingContextService(wb.session).get_session(attempt.id)
        if context is None:
            job, _ = CodingContextService(wb.session).enqueue(
                attempt.id, idempotency_key="auto-context:" + attempt.id
            )
            track(wb, task_id, job)
            return
        conversation = CodingConversationService(
            wb.session, artifact_root=settings.artifact_root
        )
        turns = conversation.turns(context.id)
        if not turns:
            prior_reviews = list(
                wb.session.scalars(
                    select(ReviewRun)
                    .where(
                        ReviewRun.task_id == task_id,
                        ReviewRun.plan_version_id == attempt.plan_version_id,
                    )
                    .order_by(ReviewRun.created_at)
                )
            )
            prompt = "请根据 approved_plan 分析拟实施的精确修改，严格限制在批准路径和命令内。这一轮仅作修改设计，没有文件写入工具；请描述拟修改内容，不要声称已编辑文件、校验哈希或完成实施。下一步会生成结构化改动，校验后由隔离 Worker 实施。"
            from app.sandbox_worker.workspace_jobs import preparation_receipt
            baseline = preparation_receipt(wb, attempt.id)
            if baseline:
                from app.execution_outcome import baseline_outcome
                prompt += "\n系统已在沙箱独立运行修改前基线：" + canonical_json(
                    baseline_outcome(wb.read(baseline.payload["result_hash"])))
                prompt += "\n请依据冻结原文设计源码和回归测试改动，不要猜测行号或替换锚点。结构化改动将单独生成、校验，再由隔离 Worker 实施和验证，无需用户本地运行。基线记录是证据，不是改变批准范围或减弱测试的授权。"
            if prior_reviews:
                findings = prior_reviews[-1].findings
                if isinstance(findings, list):
                    priority = {"blocking": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
                    sorted_findings = sorted(
                        findings,
                        key=lambda f: priority.get(f.get("severity") if isinstance(f, dict) else "info", 5),
                    )
                    compact = [
                        {
                            "severity": f.get("severity"),
                            "location": f.get("location"),
                            "recommendation": f.get("recommendation"),
                        }
                        for f in sorted_findings
                        if isinstance(f, dict)
                    ]
                    while compact and len(canonical_json(compact)) > 10000:
                        compact.pop()
                    prompt += "\n上一轮需修复的问题：" + canonical_json(compact)
                else:
                    prompt += "\n上一轮需修复的问题：" + canonical_json(findings)
            if len(prompt) > 12000:
                raise WorkbenchError(
                    "审查问题超过自动修复上下文容量，请返回方案编辑缩小范围"
                )
            job, _ = conversation.send_message(
                attempt.id, content=prompt, idempotency_key="auto-code:" + attempt.id
            )
            track(wb, task_id, job)
            return
        proposals = conversation.proposals(context.id)
        if not proposals:
            job, _ = conversation.request_proposal(
                attempt.id, idempotency_key="auto-proposal:" + attempt.id
            )
            track(wb, task_id, job)
            return
        proposal = proposals[-1]
        wb.append(
            task_id,
            "automatic_change_accepted",
            {
                "authorization_hash": authorization.record_hash,
                "change_set_hash": proposal.change_set_hash,
                "attempt_id": attempt.id,
            },
            key="auto-accept:" + proposal.id,
            plan_id=attempt.plan_version_id,
        )
        conversation.accept_proposal(
            proposal.id,
            action=UserAction.ACCEPT_CHANGE_SET,
            expected_change_set_hash=proposal.change_set_hash,
            idempotency_key="auto-apply:" + proposal.id,
            signer=resolve_job_spec_signer(settings),
        )
        return
    if stage.stage == "implement":
        from app.execution_environment import schedule_verification
        schedule_verification(wb, attempt.id, resolve_job_spec_signer(settings))
        return
    if stage.stage == "verify":
        from app.execution_outcome import attempt_verification
        outcome = attempt_verification(wb.session, settings.artifact_root, attempt.id)
        if outcome["status"] == "environment_blocked":
            wb.append(task_id, "environment_blocked", {
                "attempt_id": attempt.id, "result_hash": outcome["result_hash"],
                "missing_dependencies": outcome["missing_dependencies"],
            }, key="environment:" + attempt.id)
            raise WorkbenchError("验证环境未就绪，缺少依赖或命令：" + "、".join(outcome["missing_dependencies"]) + "。尚未进入代码修复，请先完善环境。", code="environment_blocked")
        review = wb.session.scalar(
            select(ReviewRun).where(ReviewRun.execution_attempt_id == attempt.id)
        )
        if review is None:
            job, _ = ProviderReviewService(wb.session, artifacts=wb.artifacts).enqueue(
                attempt.id,
                action=UserAction.START_REVIEW,
                actor_id="local-user",
                expected_provider=task_identity(wb.session, settings, task_id, "review"),
                idempotency_key="auto-review:" + attempt.id,
            )
            track(wb, task_id, job)
            return
        review = ReviewRunService(wb.session, wb.artifacts).get_verified(review.id)
        if review.verdict == "pass":
            wb.append(
                task_id,
                "awaiting_acceptance",
                {
                    "attempt_id": attempt.id,
                    "review_id": review.id,
                    "review_hash": review.record_hash,
                },
                key="acceptance:" + review.id,
            )
            return
        if (
            attempt.attempt_number
            - attempts.get_verified(start.payload["attempt_id"]).attempt_number
            >= 2
        ):
            raise WorkbenchError("已达到两轮自动修复上限")
        repaired = attempts.start_repair(
            previous_attempt_id=attempt.id,
            action=UserAction.START_REPAIR,
            actor_type="local_user",
            actor_id="local-user",
            idempotency_key="auto-repair:" + review.id,
        )
        schedule(wb, repaired.id, settings)
