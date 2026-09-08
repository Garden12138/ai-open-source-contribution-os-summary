"""User-authorized orchestration of existing isolated execution services."""

from __future__ import annotations
from sqlalchemy import select
from app.approvals import PlanApprovalService, ApprovalInputFingerprint
from app.authorizations import UserAction
from app.coding import CodingContextService, CodingConversationService
from app.config import Settings
from app.execution_control import ExecutionStageControlService
from app.executions import ExecutionAttemptService
from app.jobs import JobService, TERMINAL_STATES
from app.models import Job, ExecutionAttempt, ReviewRun, ExecutionStageRun
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
    planning_state_hash = wb.require_planning(task_id)
    wb.assert_idle(task_id)
    if (
        settings.implementation_provider != "nvidia_nim"
        or settings.review_provider != "nvidia_nim"
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
        raise WorkbenchError("请重新保存方案或让 AI 重新规划，为当前状态生成新版本")
    binding = wb.binding(task_id, plan.id)
    context = wb.latest(task_id, "context_ready")
    if (
        context.payload["policy_hash"] != SandboxPolicy().policy_hash
        or context.payload["image"] != settings.workbench_runner_image
    ):
        raise WorkbenchError("隔离策略或镜像已变化，请重新读取代码")
    reply = wb.latest(task_id, "assistant_message")
    if plan.questions_for_maintainer or (
        reply and (reply.payload.get("questions") or reply.payload.get("read_paths"))
    ):
        raise WorkbenchError("请先回答方案中的问题并完成代码阅读")
    payload = {
        "task_id": task_id,
        "plan_id": plan.id,
        "plan_hash": plan.record_hash,
        "binding_hash": binding.record_hash,
        "context": context.payload,
        "context_hash": context.record_hash,
        "max_repairs": 2,
        "implementation": model_identity(settings.implementation_model).hash_payload(),
        "review": model_identity(settings.review_model).hash_payload(),
        "actions": [
            "approve_plan",
            "start_execution",
            "accept_plan_bound_changes",
            "verify",
            "start_review",
            "repair_twice",
        ],
    }
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


def stop_workflow(wb: Workbench, task_id: str, *, key: str) -> None:
    wb.append(task_id, "stop_requested", {}, key="stop:" + key, actor="local-user")
    for job in wb.jobs(task_id):
        if job.state not in TERMINAL_STATES:
            JobService(wb.session).request_cancel(job.id)


def advance_workflows(database, settings: Settings) -> None:
    """One bounded, idempotent transition per task per coordinator tick."""
    from app.models import WorkbenchEvent

    with database.session() as session:
        cursor = getattr(database, "_workbench_scan_cursor", "")
        ids = list(
            session.scalars(
                select(WorkbenchEvent.task_id)
                .where(
                    WorkbenchEvent.kind.in_(("automation_started", "stop_requested")),
                    WorkbenchEvent.task_id > cursor,
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
                # A durable blocker stops the chain; never fall back to fake execution.
                latest = wb.latest(task_id, "automation_started")
                if latest:
                    wb.append(
                        task_id,
                        "automation_blocked",
                        {
                            "reason": (
                                str(exc)
                                if isinstance(exc, WorkbenchError)
                                else "执行推进失败，需要人工检查"
                            )
                        },
                        key="blocked:" + latest.id,
                    )


def _advance(wb: Workbench, task_id: str, settings: Settings) -> None:
    events = wb.history(task_id)
    start = wb.latest(task_id, "automation_started")
    stop = wb.latest(task_id, "stop_requested")
    latest_auth = wb.latest(task_id, "execution_authorized")
    if stop and latest_auth and latest_auth.sequence > stop.sequence:
        stop = None
    if stop and (not start or stop.sequence > start.sequence):
        jobs = wb.jobs(task_id)
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
        return
    if not start or any(
        e.kind in {"automation_blocked", "awaiting_acceptance"}
        and e.sequence > start.sequence
        for e in events
    ):
        return
    authorization = next(
        e for e in events if e.record_hash == start.payload["authorization_hash"]
    )
    if (
        authorization.payload["implementation"]
        != model_identity(settings.implementation_model).hash_payload()
        or authorization.payload["review"]
        != model_identity(settings.review_model).hash_payload()
    ):
        raise WorkbenchError("模型配置已变化")
    attempts = ExecutionAttemptService(wb.session)
    attempt = wb.session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.task_id == task_id)
        .order_by(ExecutionAttempt.attempt_number.desc())
        .limit(1)
    )
    if attempt.plan_version_id != authorization.plan_version_id:
        raise WorkbenchError("执行方案已变化")
    stage = attempts.current(attempt.id)
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
    if any(j.state in {"failed", "cancelled", "timed_out"} for j in relevant):
        raise WorkbenchError("执行子任务失败")
    if any(j.state not in TERMINAL_STATES for j in relevant):
        return
    if stage.status == "pending":
        schedule(wb, attempt.id, settings)
        return
    if stage.status != "succeeded":
        raise WorkbenchError("执行阶段未成功")
    if stage.stage == "explore":
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
            prompt = "请完整实现上下文中的 approved_plan，严格限制在批准路径和命令内。"
            if prior_reviews:
                prompt += "\n上一轮需修复的问题：" + canonical_json(
                    prior_reviews[-1].findings
                )
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
    if stage.stage == "verify":
        review = wb.session.scalar(
            select(ReviewRun).where(ReviewRun.execution_attempt_id == attempt.id)
        )
        if review is None:
            job, _ = ProviderReviewService(wb.session, artifacts=wb.artifacts).enqueue(
                attempt.id,
                action=UserAction.START_REVIEW,
                actor_id="local-user",
                expected_provider=model_identity(settings.review_model),
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
