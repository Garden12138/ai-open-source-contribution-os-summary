from __future__ import annotations
from typing import Any, Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.jobs import TERMINAL_STATES, JobConflictError
from app.planner import PlanningService
from app.plans import PlanVersionError
from app.planning import ContributionTaskError
from app.schemas import JobResponse, PlanVersionResponse, PlanVersionCreateRequest
from app.task_states import ContributionTaskStateService
from app.workbench import Workbench, WorkbenchError
from app.security import SensitiveDataError
from app.planning_budget import PlanningBudgetRequest
from app.contribution_workflow import authorize_execution, stop_workflow, _advance
from app.agent_runtime import TaskObservation


class PlanningMessageRequest(BaseModel):
    mode: Literal["planning", "question", "pr_update", "pr_ready", "pr_followup"] = "planning"
    model_config = ConfigDict(extra="forbid")
    text: str = Field(
        default="请阅读相关代码，为这个 Issue 制定具体改造方案。",
        min_length=1,
        max_length=8000,
    )
    parent_id: str | None = Field(default=None, max_length=128)
    expected_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    refresh: bool = False
    model_profile_id: str | None = None


class WorkbenchPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: PlanVersionCreateRequest


class ConversationMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)
    question_source_id: str | None = Field(default=None, min_length=1, max_length=128)
    question_source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reply_to_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    reply_to_message_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_question_binding(self) -> ConversationMessageRequest:
        if (self.question_source_id is None) != (self.question_source_hash is None):
            raise ValueError("Question source ID and hash must be supplied together")
        if (self.reply_to_message_id is None) != (self.reply_to_message_hash is None):
            raise ValueError("Reply source ID and hash must be supplied together")
        return self


class ConversationMessageReceipt(BaseModel):
    message_id: str
    status: str
    text: str


class AgentRuntimeControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "continue", "cancel"]
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=1, max_length=128)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve_plan: bool
    start_execution: bool
    max_repairs: int = Field(default=2, ge=2, le=2)
    model_binding_hash: str | None = None
    conversation: bool = False


class TaskModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_hash: str | None = None
    profiles: dict[str, str | None]


class TaskModelsResponse(BaseModel):
    record_hash: str
    changed: bool


class ContextManagementResponse(BaseModel):
    status: Literal["not_built", "stale", "compacted", "within_budget", "pinned_context_large"]
    original_bytes: int = 0
    prompt_bytes: int = 0
    saved_bytes: int = 0
    target_bytes: int = 0
    target_met: bool = True
    reference_count: int = 0
    instruction_count: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)


class PlanningBudgetResponse(BaseModel):
    version: str
    revision: str
    record_hash: str | None
    limits: dict[str, int]
    usage: dict[str, int]
    remaining: dict[str, int]
    bounds: dict[str, dict[str, int]]
    configurable: bool


class ConversationResponse(BaseModel):
    version: str
    user_state: dict[str, Any]
    pending_action: dict[str, Any] | None
    paused: bool
    pending_count: int
    blocking_count: int = 0
    messages: list[dict[str, Any]]
    results: list[dict[str, Any]]
    observation: TaskObservation | None = None


class WorkbenchResponse(BaseModel):
    task_id: str
    state: str
    events: list[dict[str, Any]]
    jobs: list[JobResponse]
    plans: list[PlanVersionResponse]
    context: dict[str, Any] | None
    busy: bool
    planner_available: bool
    planner_unavailable_reason: str | None = None
    publisher_mode: str
    model_profiles: dict[str, str | None] = Field(default_factory=dict)
    model_labels: dict[str, str] = Field(default_factory=dict)
    model_binding_hash: str | None = None
    execution: dict[str, Any] = Field(default_factory=dict, json_schema_extra={
        "properties":{"context_management":ContextManagementResponse.model_json_schema(),
                      "planning_budget":PlanningBudgetResponse.model_json_schema()}})
    cla: dict[str, Any] = Field(default_factory=dict)
    pr_update: dict[str, Any] = Field(default_factory=dict)
    pr_update_available: bool = False
    pr_lifecycle: dict[str, Any] = Field(default_factory=dict)
    conversation: ConversationResponse
    native_resources: dict[str, Any] = Field(default_factory=dict)


class ChangeGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)
    expected_revision: str


class ExecutionAcceptanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=8000)
    prepare_pr: bool = False


class ExecutionAcceptanceResponse(BaseModel):
    record_hash: str
    binding_hash: str
    accepted: bool


class PublicationContinuationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=7600)
    document_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    template_path: str | None = Field(default=None, max_length=500)


class PublicationConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationRenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CLAConfirmRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    nonce: str = Field(pattern=r'^[0-9a-f]{64}$')
    agreement_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    acknowledgement: str = Field(min_length=1, max_length=2000)
    read_and_agree: Literal[True]


class PRUpdateConfirmRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    nonce: str = Field(pattern=r'^[0-9a-f]{64}$')
    text: str = Field(min_length=1, max_length=2000)
    approved: Literal[True]


class NativeResourceConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: Literal[True]


class NativeResourcePrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NativeResourcePreviewResponse(BaseModel):
    version: Literal["native-resource-policy-v1"]
    binding: dict[str, Any]
    resource: dict[str, Any]
    nonce: str
    expires_at: str
    record_hash: str


def register_workbench_routes(app, get_session, require_mutation_access) -> None:
    router = APIRouter(prefix="/api/v1/tasks/{task_id}/workbench", tags=["workbench"])

    @router.get("/runtime")
    def runtime_status(task_id: str, request: Request, session: Session = Depends(get_session)):
        from app.agent_v2.orchestrator import runtime_view
        return runtime_view(Workbench(session, request.app.state.settings.artifact_root), task_id)

    @router.post("/runtime/control", dependencies=[Depends(require_mutation_access)])
    def runtime_control(task_id: str, payload: AgentRuntimeControlRequest, request: Request,
                        session: Session = Depends(get_session)):
        from app.agent_v2.journal import AgentJournal
        from app.agent_v2.orchestrator import TaskOrchestrator
        from app.agent_v2.loop import schedule
        wb = Workbench(session, request.app.state.settings.artifact_root)
        root = wb.latest(task_id, "agent_v2_session")
        if root is None:
            raise WorkbenchError("本任务没有 v2 运行会话。", code="agent_session_invalid")
        journal = AgentJournal(wb, task_id, root.payload["session_id"])
        if payload.action == "continue":
            TaskOrchestrator(wb, request.app.state.settings).require_current(journal)
        cp = journal.control(payload.action, payload.expected_revision)
        if payload.action == "cancel":
            from app.jobs import JobService
            for job in wb.jobs(task_id):
                if job.payload.get("session_id") == journal.session_id and job.state not in TERMINAL_STATES:
                    JobService(session).request_cancel(job.id, commit=False)
        if payload.action == "continue":
            schedule(journal)
        session.commit()
        return cp.model_dump()

    @router.get("/runtime/artifacts/{artifact_id}")
    def runtime_artifact(task_id: str, artifact_id: str, request: Request,
                         offset: int = Query(default=0, ge=0, le=8000000),
                         max_bytes: int = Query(default=4096, ge=128, le=16000), session: Session = Depends(get_session)):
        from app.agent_v2.contracts import Page
        page = Page(offset=offset, max_bytes=max_bytes)
        wb = Workbench(session, request.app.state.settings.artifact_root)
        refs = {e.payload["artifact_id"] for e in wb.history(task_id)
                if e.kind == "agent_v2_tool_result"}
        if artifact_id not in refs:
            raise WorkbenchError("证据不属于本任务。", code="agent_artifact_scope_denied")
        from app.agent_v2.tools import artifact_page
        return {"artifact_id": artifact_id, **artifact_page(wb.artifacts.read_bytes(artifact_id),page.offset,page.max_bytes)}

    @router.post("/budget", response_model=PlanningBudgetResponse,
                 dependencies=[Depends(require_mutation_access)])
    def configure_budget(task_id: str, payload: PlanningBudgetRequest, request: Request,
                         key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
                         session: Session = Depends(get_session)):
        from app.planning_budget import configure_planning_budget
        try:
            return configure_planning_budget(Workbench(session, request.app.state.settings.artifact_root),
                                             task_id, payload, key=key)
        except IntegrityError:
            session.rollback()
            raise WorkbenchError("任务预算已变化，请刷新后重新选择。", code="planning_budget_conflict") from None

    @router.post("/native-resources/prepare", response_model=NativeResourcePreviewResponse, dependencies=[Depends(require_mutation_access)])
    def native_resource_prepare(task_id: str, payload: NativeResourcePrepareRequest, request: Request,
                                key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
                                session: Session = Depends(get_session)):
        from app.native_resources import prepare
        try:
            return prepare(Workbench(session, request.app.state.settings.artifact_root), task_id, key=key)
        except IntegrityError:
            session.rollback()
            raise WorkbenchError("资源请求状态已变化，请刷新后重试", code="native_resource_conflict") from None

    @router.post("/native-resources/confirm", response_model=JobResponse, status_code=202,
                 dependencies=[Depends(require_mutation_access)])
    def native_resource_confirm(task_id: str, payload: NativeResourceConfirmRequest, request: Request,
                                session: Session = Depends(get_session)):
        from app.native_resources import confirm
        try:
            return JobResponse.model_validate(confirm(Workbench(session, request.app.state.settings.artifact_root), task_id, **payload.model_dump()))
        except IntegrityError:
            session.rollback()
            raise WorkbenchError("资源授权状态已变化，请刷新核对", code="native_resource_conflict") from None

    @router.post("/conversation/messages", response_model=ConversationMessageReceipt,
                 status_code=202, dependencies=[Depends(require_mutation_access)])
    def conversation_message(task_id: str, payload: ConversationMessageRequest, request: Request,
                             key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
                             session: Session = Depends(get_session)):
        from app.conversation import receive_message
        try:
            wb = Workbench(session, request.app.state.settings.artifact_root)
            receipt = receive_message(wb, task_id,
                                   text=payload.text, key=key, settings=request.app.state.settings,
                                   question_source_id=payload.question_source_id,
                                   question_source_hash=payload.question_source_hash,
                                   reply_to_message_id=payload.reply_to_message_id,
                                   reply_to_message_hash=payload.reply_to_message_hash,
                                   enable_runtime=True)
            if request.app.state.settings.agent_runtime_v2_enabled and wb.latest(task_id, "native_journey_started") is None:
                source = next(e for e in wb.history(task_id) if e.id == receipt["message_id"])
                wb.append(task_id, "native_journey_started", {"runtime_version": "agent-runtime-v2",
                    "message_id": source.id, "message_hash": source.record_hash},
                    key="native-journey:" + task_id, actor="api")
            return receipt
        except IntegrityError:
            session.rollback()
            raise WorkbenchError("另一条消息正在保存，请重新发送。", code="conversation_conflict") from None
        except SensitiveDataError:
            session.rollback()
            raise HTTPException(422, "消息中包含凭证形式的内容，请移除后重试") from None

    @router.post('/pr-ready/prepare', response_model=JobResponse, status_code=202, dependencies=[Depends(require_mutation_access)])
    @router.post('/pr-followup', response_model=JobResponse, status_code=202, dependencies=[Depends(require_mutation_access)])
    def pr_lifecycle_read(task_id: str, request: Request,
                          key: str = Header(alias='Idempotency-Key', min_length=1, max_length=80),
                          session: Session = Depends(get_session)):
        from app.pr_lifecycle import request_lifecycle, PREPARE, FOLLOWUP
        from app.publisher_accounts import require_account
        if not request.app.state.settings.publisher_management_key:
            raise HTTPException(409, '请在设置中配置独立 Publisher 发布账号')
        account = require_account(session)
        try:
            return JobResponse.model_validate(request_lifecycle(Workbench(session,request.app.state.settings.artifact_root),task_id,
                kind=PREPARE if request.url.path.endswith('/prepare') else FOLLOWUP,account_hash=account.event_hash,key=key))
        except IntegrityError:
            session.rollback()
            raise HTTPException(409,'PR 跟进请求状态已变化，请刷新后重试') from None

    @router.post('/pr-ready/confirm', response_model=JobResponse, status_code=202, dependencies=[Depends(require_mutation_access)])
    def pr_ready_confirm(task_id: str, payload: PRUpdateConfirmRequest, request: Request,
                         session: Session = Depends(get_session)):
        from app.pr_lifecycle import confirm_ready
        if not request.app.state.settings.publisher_management_key:
            raise HTTPException(409, '请在设置中配置独立 Publisher 发布账号')
        try:
            return JobResponse.model_validate(confirm_ready(Workbench(session,request.app.state.settings.artifact_root),task_id,**payload.model_dump()))
        except IntegrityError:
            session.rollback()
            raise HTTPException(409,'审核状态授权已使用或状态已变化，请跟进结果') from None

    @router.post('/pr-update/confirm', response_model=JobResponse, status_code=202, dependencies=[Depends(require_mutation_access)])
    def pr_update_confirm(task_id: str, payload: PRUpdateConfirmRequest, request: Request,
                          session: Session = Depends(get_session)):
        from app.pr_updates import confirm_update
        if not request.app.state.settings.publisher_management_key:
            raise HTTPException(409, '请在设置中配置独立 Publisher 发布账号')
        try:
            return JobResponse.model_validate(confirm_update(
                Workbench(session, request.app.state.settings.artifact_root), task_id, **payload.model_dump()))
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, '更新授权已使用或状态已变化，请核对结果') from None

    @router.post('/pr-update/check', response_model=JobResponse, status_code=202, dependencies=[Depends(require_mutation_access)])
    def pr_update_check(task_id: str, request: Request,
                        key: str = Header(alias='Idempotency-Key', min_length=1, max_length=80),
                        session: Session = Depends(get_session)):
        from app.pr_updates import request_update
        from app.publisher_accounts import require_account
        if not request.app.state.settings.publisher_management_key:
            raise HTTPException(409, '请在设置中配置独立 Publisher 发布账号')
        account = require_account(session)
        try:
            return JobResponse.model_validate(request_update(Workbench(session, request.app.state.settings.artifact_root),
                task_id, text='', account_hash=account.event_hash, key=key, check=True))
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, '核对任务状态已变化，请刷新后重试') from None

    @router.post('/cla/prepare', response_model=JobResponse, status_code=202, dependencies=[Depends(require_mutation_access)])
    @router.post('/cla/check', response_model=JobResponse, status_code=202, dependencies=[Depends(require_mutation_access)])
    def cla_read(task_id: str, request: Request,
        key: str = Header(alias='Idempotency-Key', min_length=1, max_length=80),
        session: Session = Depends(get_session)):
        from app.cla import request_cla
        from app.publisher_accounts import require_account
        if not request.app.state.settings.publisher_management_key:
            raise HTTPException(409, '请在设置中配置独立 Publisher 发布账号')
        account = require_account(session)
        try:
            return JobResponse.model_validate(request_cla(
                Workbench(session, request.app.state.settings.artifact_root), task_id,
                account_hash=account.event_hash, key=key, check=request.url.path.endswith('/check')))
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, 'CLA 请求状态已变化，请刷新后重试') from None

    @router.post('/cla/confirm', response_model=JobResponse, status_code=202, dependencies=[Depends(require_mutation_access)])
    def cla_confirm(task_id: str, payload: CLAConfirmRequest, request: Request,
        session: Session = Depends(get_session)):
        from app.cla import confirm_cla
        if not request.app.state.settings.publisher_management_key:
            raise HTTPException(409, '请在设置中配置独立 Publisher 发布账号')
        try:
            return JobResponse.model_validate(confirm_cla(
                Workbench(session, request.app.state.settings.artifact_root), task_id, **payload.model_dump()))
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, 'CLA 授权已使用或状态已变化，请核对结果') from None

    @router.post("/models", response_model=TaskModelsResponse, dependencies=[Depends(require_mutation_access)])
    def task_models(task_id: str, payload: TaskModelsRequest, request: Request,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
        session: Session = Depends(get_session)):
        from app.model_settings import ModelSettingsService, STAGES
        models = ModelSettingsService(session)
        wb = Workbench(session, request.app.state.settings.artifact_root)
        wb.require_planning(task_id)
        wb.assert_idle(task_id)
        if set(payload.profiles) != set(STAGES):
            raise HTTPException(422, "请提交四个阶段的模型选择")
        for identifier in payload.profiles.values():
            if identifier:
                models.get(identifier, "profile")
        prior = next((event for event in wb.history(task_id)
            if event.idempotency_key == "model:" + key), None)
        if prior:
            if prior.payload.get("profiles") != payload.profiles:
                raise HTTPException(409, "重复请求的模型选择不同")
            return {"record_hash": prior.payload["binding_hash"], "changed": prior.kind == "model_changed"}
        previous = models.latest("task:" + task_id)
        if (previous.record_hash if previous else None) != payload.expected_hash:
            raise HTTPException(409, "设置已在其他页面更新，请重新载入")
        if previous and previous.payload == payload.profiles:
            # A receipt makes replay stable even after a later genuine switch.
            # It is not a model change and creates no configuration version.
            wb.append(task_id, "model_selection_applied", {
                "profiles": payload.profiles, "binding_hash": previous.record_hash,
            }, key="model:" + key, actor="local-user")
            return {"record_hash": previous.record_hash, "changed": False}
        row = models.append("task:" + task_id, payload.profiles, payload.expected_hash, commit=False)
        wb.append(task_id, "model_changed", {"profiles": payload.profiles, "binding_hash": row.record_hash},
            key="model:" + key, actor="local-user", commit=False)
        session.commit()
        return {"record_hash": row.record_hash, "changed": True}

    @app.exception_handler(WorkbenchError)
    async def workbench_error(request, exc):
        from fastapi.responses import JSONResponse
        from app.security import redact_text

        return JSONResponse(status_code=409, content={"detail": redact_text(str(exc)), "reason_code": exc.code})

    @app.exception_handler(ContributionTaskError)
    async def task_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=404, content={"detail": "贡献任务不存在或记录无效"}
        )

    @router.get("", response_model=WorkbenchResponse)
    def detail(task_id: str, request: Request, session: Session = Depends(get_session)):
        from sqlalchemy import select
        from app.models import PlanVersion
        from app.plans import PlanVersionService

        wb = Workbench(session, request.app.state.settings.artifact_root)
        try:
            events = wb.history(task_id)
        except ContributionTaskError:
            raise HTTPException(404, "贡献任务不存在")
        context = wb.latest(task_id, "context_ready")
        jobs = wb.jobs(task_id)
        settings = request.app.state.settings
        from app.model_settings import ModelSettingsService
        models = ModelSettingsService(session)
        profiles = models.task_profiles(task_id)
        model_binding = models.latest("task:" + task_id)
        reason = None
        if not profiles.get("planning") and settings.implementation_provider != "nvidia_nim":
            reason = "尚未配置规划模型服务，请配置实现模型。"
        elif not settings.workbench_runner_image:
            reason = (
                "尚未配置规划使用的固定 Runner 镜像，请设置 WORKBENCH_RUNNER_IMAGE。"
            )
        elif (
            settings.sandbox_stage_runtime != "docker"
            or not settings.sandbox_job_spec_signing_key
        ):
            reason = "尚未配置隔离执行服务，请配置并启动独立 Sandbox Worker。"
        from app.workbench_view import workbench_view
        from app.publisher_accounts import account_status
        projection = workbench_view(wb, task_id)
        from app.cla import cla_view
        from app.pr_updates import update_view
        from app.conversation import conversation_view
        from app.native_resources import view as native_resource_view
        return WorkbenchResponse(
            native_resources=native_resource_view(wb, task_id),
            conversation=conversation_view(wb, task_id),
            execution=projection,
            pr_lifecycle=projection["pr_lifecycle"],
            cla=cla_view(wb, task_id),
            pr_update=update_view(wb, task_id),
            pr_update_available=bool(profiles.get("planning") or settings.implementation_provider == "nvidia_nim"),
            task_id=task_id,
            state=ContributionTaskStateService(session).current(task_id).to_state,
            events=[
                {
                    "id": e.id,
                    "sequence": e.sequence,
                    "kind": e.kind,
                    "actor_id": e.actor_id,
                    "job_id": e.job_id,
                    "plan_id": e.plan_version_id,
                    "payload": e.payload,
                    "record_hash": e.record_hash,
                }
                for e in events
                if e.kind
                in {
                    "execution_question",
                    "execution_answer",
                    "execution_change_requested",
                    "environment_blocked",
                    "user_message",
                    "assistant_message",
                    "automation_started",
                    "automation_blocked",
                    "automation_resumed",
                    "awaiting_acceptance",
                    "execution_accepted",
                    "stop_requested",
                    "publication_prepared",
                    "publication_confirmed",
                    "publication_completed",
                    "publication_document_ready",
                    "pr_lifecycle_requested", "pr_followup_observed", "pr_ready_prepared", "pr_ready_confirmed", "pr_ready_completed",
                    "pr_update_requested", "pr_update_prepared", "pr_update_confirmed",
                    "pr_update_completed", "pr_update_observed",
                    "cla_confirmed",
                    "cla_prepared",
                    "cla_submitted",
                    "cla_status_observed",
                }
            ],
            jobs=[JobResponse.model_validate(j) for j in jobs],
            plans=[
                PlanVersionResponse.model_validate(
                    PlanVersionService(session).get_verified(p.id)
                )
                for p in session.scalars(
                    select(PlanVersion)
                    .where(PlanVersion.task_id == task_id)
                    .order_by(PlanVersion.version_number)
                )
            ],
            context=(
                None
                if context is None
                else {**context.payload, "record_hash": context.record_hash}
            ),
            busy=projection["busy"],
            planner_available=reason is None,
            planner_unavailable_reason=reason,
            publisher_mode=(("gh" if account_status(session)["connected"] else "none")
                            if request.app.state.settings.publisher_management_key
                            else request.app.state.settings.publisher_mode),
            model_profiles=profiles,
            model_labels={stage: models.profile(identifier)["model"] for stage, identifier in profiles.items() if identifier},
            model_binding_hash=model_binding.record_hash if model_binding else None,
        )

    @router.post(
        "/messages",
        response_model=JobResponse,
        status_code=202,
        dependencies=[Depends(require_mutation_access)],
    )
    def message(
        task_id: str,
        payload: PlanningMessageRequest,
        request: Request,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
        session: Session = Depends(get_session),
    ):
        wb = Workbench(session, request.app.state.settings.artifact_root)
        from app.pr_lifecycle import conversation_action, request_lifecycle, PREPARE, FOLLOWUP
        lifecycle_kind = PREPARE if payload.mode == "pr_ready" else FOLLOWUP if payload.mode == "pr_followup" else conversation_action(payload.text) if payload.mode == "question" and wb.latest(task_id,"publication_completed") else None
        from app.pr_updates import is_update_request
        if lifecycle_kind == FOLLOWUP and payload.mode == "question" and is_update_request(payload.text):
            lifecycle_kind = None
        if lifecycle_kind and payload.mode != "pr_update":
            from app.publisher_accounts import require_account
            if not request.app.state.settings.publisher_management_key:
                raise HTTPException(409, '请在设置中配置独立 Publisher 发布账号')
            account = require_account(session)
            try:
                return JobResponse.model_validate(request_lifecycle(wb,task_id,kind=lifecycle_kind,text=payload.text,
                    account_hash=account.event_hash,key=key))
            except IntegrityError:
                session.rollback()
                raise HTTPException(409,'PR 跟进请求状态已变化，请刷新后重试') from None
        from app.pr_updates import is_update_request, request_update
        if payload.mode == "pr_update" or (payload.mode == "question" and wb.latest(task_id, "publication_completed") and is_update_request(payload.text)):
            from app.publisher_accounts import require_account
            if not request.app.state.settings.publisher_management_key:
                raise HTTPException(409, '请在设置中配置独立 Publisher 发布账号')
            account = require_account(session)
            try:
                return JobResponse.model_validate(request_update(wb, task_id, text=payload.text,
                    account_hash=account.event_hash, key=key, model_profile_id=payload.model_profile_id))
            except IntegrityError:
                session.rollback()
                raise HTTPException(409, '文案更新请求状态已变化，请刷新后重试') from None
        if payload.mode == "question":
            from app.execution_chat import ask_execution
            return JobResponse.model_validate(ask_execution(wb, task_id, text=payload.text, key=key))
        from app.model_settings import ModelSettingsService
        models = ModelSettingsService(session)
        profiles = models.task_profiles(task_id, bind=True)
        target_profile_id = payload.model_profile_id
        if target_profile_id:
            target_profile = models.get(target_profile_id, "profile")
            latest_profile = models.latest(target_profile.scope)
            if latest_profile:
                target_profile_id = latest_profile.id
        if target_profile_id and target_profile_id != profiles.get("planning"):
            models.get(target_profile_id, "profile")
            wb = Workbench(session, request.app.state.settings.artifact_root)
            wb.require_planning(task_id)
            wb.assert_idle(task_id)
            previous = models.latest("task:" + task_id)
            profiles = {**profiles, "planning": target_profile_id}
            models.append("task:" + task_id, profiles, previous.record_hash if previous else None, commit=False)
            wb.append(task_id, "model_changed", {"planning_profile_id": target_profile_id},
                key="model:" + key, actor="local-user", commit=False)
        if not profiles.get("planning") and request.app.state.settings.implementation_provider != "nvidia_nim":
            raise HTTPException(409, "请先配置用于规划的实现模型服务")
        try:
            job = PlanningService(
                session, request.app.state.settings.artifact_root
            ).request(
                task_id,
                text=payload.text,
                parent_id=payload.parent_id,
                expected_hash=payload.expected_hash,
                key=key,
                refresh=payload.refresh,
                model_profile_id=target_profile_id,
                runtime_version="agent-runtime-v2" if request.app.state.settings.agent_runtime_v2_enabled else None,
            )
            return JobResponse.model_validate(job)
        except (IntegrityError, JobConflictError):
            session.rollback()
            raise HTTPException(409, "另一个请求已更新任务，请重新载入")

    @router.post(
        "/plans",
        response_model=PlanVersionResponse,
        status_code=201,
        dependencies=[Depends(require_mutation_access)],
    )
    def save(
        task_id: str,
        payload: WorkbenchPlanRequest,
        request: Request,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
        session: Session = Depends(get_session),
    ):
        try:
            plan = PlanningService(
                session, request.app.state.settings.artifact_root
            ).save(task_id, payload.plan, context_hash=payload.context_hash, key=key)
            session.commit()
            return PlanVersionResponse.model_validate(plan)
        except (PlanVersionError, IntegrityError):
            session.rollback()
            raise HTTPException(409, "方案版本冲突，请重新载入")
        except ValueError:
            session.rollback()
            raise HTTPException(422, "方案路径、命令或内容无效")

    @router.post(
        "/execute",
        response_model=JobResponse,
        status_code=202,
        dependencies=[Depends(require_mutation_access)],
    )
    def execute(
        task_id: str,
        payload: ExecutePlanRequest,
        request: Request,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
        session: Session = Depends(get_session),
    ):
        if not payload.approve_plan or not payload.start_execution:
            raise HTTPException(403, "需要明确批准方案并启动执行")
        settings = request.app.state.settings
        wb = Workbench(session, settings.artifact_root)
        if any(e.idempotency_key == "authorize:" + key for e in wb.history(task_id)):
            return JobResponse.model_validate(authorize_execution(wb, task_id,
                plan_id=payload.plan_id, plan_hash=payload.plan_hash, key=key, settings=settings))
        from app.model_settings import ModelSettingsService
        current_binding = ModelSettingsService(session).latest("task:" + task_id)
        if not current_binding and any(ModelSettingsService(session).defaults().values()):
            raise HTTPException(409, "请先在当前任务中确认模型，或发送消息重新规划")
        if current_binding and payload.model_binding_hash != current_binding.record_hash:
            raise HTTPException(409, "模型选择已变化，请重新确认当前方案和模型")
        if payload.conversation:
            wb.append(task_id, "conversation_started", {"version": "conversation-v1"},
                      key="conversation-start:" + key, actor="local-user", commit=False)
        job = authorize_execution(
            wb,
            task_id,
            plan_id=payload.plan_id,
            plan_hash=payload.plan_hash,
            key=key,
            settings=settings,
        )
        return JobResponse.model_validate(job)

    @router.post("/acceptance", response_model=ExecutionAcceptanceResponse,
                 dependencies=[Depends(require_mutation_access)])
    def acceptance(task_id: str, payload: ExecutionAcceptanceRequest, request: Request,
                   key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
                   session: Session = Depends(get_session)):
        from app.execution_acceptance import accept_execution
        from app.artifacts import ArtifactError
        from app.reviews import ReviewError
        from app.security import SensitiveDataError
        wb = Workbench(session, request.app.state.settings.artifact_root)
        try:
            receipt = accept_execution(wb, task_id, binding_hash=payload.binding_hash,
                                       text=payload.text, key=key, commit=False)
            if payload.prepare_pr:
                from app.conversation_continuations import request_after_acceptance
                request_after_acceptance(wb, task_id, receipt_hash=receipt.record_hash,
                                         settings=request.app.state.settings, commit=False)
            session.commit()
        except IntegrityError:
            session.rollback()
            raise WorkbenchError("任务已被其他请求更新，请刷新后重试")
        except (ArtifactError, ReviewError):
            session.rollback()
            raise WorkbenchError("执行证据校验失败，请核对当前结果后重试")
        except SensitiveDataError:
            session.rollback()
            raise HTTPException(422, "消息中包含凭证形式的内容，请移除后重试")
        return {"record_hash": receipt.record_hash, "binding_hash": receipt.payload["binding_hash"], "accepted": True}

    @router.post("/publication/continue", response_model=ConversationMessageReceipt,
                 dependencies=[Depends(require_mutation_access)])
    def continue_publication(task_id: str, payload: PublicationContinuationRequest, request: Request,
                             key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
                             session: Session = Depends(get_session)):
        from app.execution_acceptance import acceptance_view
        from app.conversation_continuations import request_after_acceptance
        wb = Workbench(session, request.app.state.settings.artifact_root)
        from app.cla import _lock
        _lock(wb)
        accepted = acceptance_view(wb, task_id)
        if not accepted.get("accepted") or accepted["binding_hash"] != payload.binding_hash:
            raise WorkbenchError("验收依据已变化，请刷新后继续。", code="continuation_stale")
        return request_after_acceptance(wb, task_id, receipt_hash=accepted["accepted"]["record_hash"],
            request_key=key, settings=request.app.state.settings)

    @router.get("/timeline")
    def timeline(task_id: str, request: Request, after: int = 0,
                 session: Session = Depends(get_session)):
        wb = Workbench(session, request.app.state.settings.artifact_root)
        from app.workbench_view import workbench_view
        projection = workbench_view(wb, task_id)
        allowed = {"user_message", "assistant_message", "execution_question", "execution_answer",
                   "automation_started", "automation_blocked", "awaiting_acceptance", "stop_requested",
                   "environment_blocked", "execution_change_requested", "execution_accepted",
                   "cla_confirmed", "cla_submitted", "cla_status_observed",
                   "pr_lifecycle_requested", "pr_followup_observed", "pr_ready_prepared", "pr_ready_confirmed", "pr_ready_completed",
                   "pr_update_requested", "pr_update_prepared", "pr_update_confirmed", "pr_update_completed", "pr_update_observed"}
        events = [{"id": e.id, "sequence": e.sequence, "kind": e.kind,
                   "payload": e.payload, "job_id": e.job_id, "plan_id": e.plan_version_id}
                  for e in wb.history(task_id) if e.sequence > after and e.kind in allowed]
        from app.conversation import conversation_view
        return {"events": events, "cursor": projection["event_cursor"], "execution": projection,
                "conversation": conversation_view(wb, task_id)}

    @router.post("/replan", status_code=202, dependencies=[Depends(require_mutation_access)])
    def change_goal(task_id: str, payload: ChangeGoalRequest, request: Request,
                    key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
                    session: Session = Depends(get_session)):
        wb = Workbench(session, request.app.state.settings.artifact_root)
        from app.workbench_view import workbench_view
        old = next((e for e in wb.history(task_id) if e.idempotency_key == "change:" + key), None)
        if old:
            if old.payload["text"] != payload.text:
                raise WorkbenchError("重复请求内容不同")
            return {"status": "stopping"}
        view = workbench_view(wb, task_id)
        if view["revision"] != payload.expected_revision:
            raise WorkbenchError("执行状态已变化，请检查最新进度后确认", code="execution_stale")
        if not view["actions"]["can_replan"] or wb.latest(task_id, "publication_confirmed"):
            raise WorkbenchError("当前状态不能修改执行目标")
        wb.append(task_id, "execution_change_requested", {"text": payload.text, "stop_key": "stop:" + key},
                  key="change:" + key, actor="local-user", commit=False)
        stop_workflow(wb, task_id, key=key)
        _advance(wb, task_id, request.app.state.settings)
        return {"status": "stopping"}

    @router.post(
        "/stop", status_code=202, dependencies=[Depends(require_mutation_access)]
    )
    def stop(
        task_id: str,
        request: Request,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
        session: Session = Depends(get_session),
    ):
        wb = Workbench(session, request.app.state.settings.artifact_root)
        state = ContributionTaskStateService(session).current(task_id)
        if state.to_state in {"draft_pr", "merged", "rewarded"} or wb.latest(
            task_id, "publication_confirmed"
        ):
            raise HTTPException(409, "发布已确认，不能撤回正在进行的远端操作")
        stop_workflow(wb, task_id, key=key)
        _advance(wb, task_id, request.app.state.settings)
        return {"status": "stopping"}

    @router.post("/publication/document", response_model=JobResponse, status_code=202,
        dependencies=[Depends(require_mutation_access)])
    def publication_document(task_id: str, request: Request,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
        session: Session = Depends(get_session)):
        from app.publication import request_publication
        account_hash = None
        settings = request.app.state.settings
        if settings.publisher_management_key:
            from app.publisher_accounts import require_account
            account_hash = require_account(session).event_hash
        elif settings.publisher_mode != "gh":
            raise HTTPException(409, "请先在设置中连接 GitHub 发布账号，以读取仓库规范")
        from app.model_settings import ModelSettingsService
        if not ModelSettingsService(session).task_profiles(task_id).get("planning") and settings.implementation_provider != "nvidia_nim":
            raise HTTPException(409, "请先配置讨论模型，以生成英文发布文案")
        return JobResponse.model_validate(request_publication(
            Workbench(session, settings.artifact_root), task_id, title="", body="", key=key,
            document_only=True, publisher_account_hash=account_hash))

    @router.post(
        "/publication",
        response_model=JobResponse,
        status_code=202,
        dependencies=[Depends(require_mutation_access)],
    )
    def prepare_publication(
        task_id: str,
        payload: PublicationPrepareRequest,
        request: Request,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
        session: Session = Depends(get_session),
    ):
        from app.publication import request_publication

        account_hash = None
        if request.app.state.settings.publisher_management_key:
            from app.publisher_accounts import require_account
            account_hash = require_account(session).event_hash
        elif request.app.state.settings.publisher_mode != "gh":
            raise HTTPException(409, "真实 Publisher 尚未配置")
        wb = Workbench(session, request.app.state.settings.artifact_root)
        return JobResponse.model_validate(
            request_publication(
                wb, task_id, title=payload.title, body=payload.body, key=key,
                publisher_account_hash=account_hash,
                document_hash=payload.document_hash, template_path=payload.template_path,
            )
        )

    @router.post("/publication/renew", response_model=JobResponse, status_code=202,
        dependencies=[Depends(require_mutation_access)])
    def renew_preview(task_id: str, payload: PublicationRenewRequest, request: Request,
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
        session: Session = Depends(get_session)):
        from app.publication import renew_publication_preview
        settings = request.app.state.settings
        account_hash = None
        if settings.publisher_management_key:
            from app.publisher_accounts import require_account
            account_hash = require_account(session).event_hash
        elif settings.publisher_mode != "gh":
            raise HTTPException(409, "真实 Publisher 尚未配置")
        from app.conversation import require_settled_messages
        wb = Workbench(session, settings.artifact_root)
        require_settled_messages(wb, task_id)
        return JobResponse.model_validate(renew_publication_preview(
            wb, task_id, preview_hash=payload.preview_hash,
            key=key, publisher_account_hash=account_hash))

    @router.post(
        "/publication/confirm",
        response_model=JobResponse,
        status_code=202,
        dependencies=[Depends(require_mutation_access)],
    )
    def publish(
        task_id: str,
        payload: PublicationConfirmRequest,
        request: Request,
        session: Session = Depends(get_session),
    ):
        from app.publication import confirm_publication

        if request.app.state.settings.publisher_management_key:
            from app.publisher_accounts import require_account
            account = require_account(session)
            prepared = Workbench(session, request.app.state.settings.artifact_root).latest(task_id, "publication_prepared")
            if not prepared or prepared.payload.get("publisher_account_hash") != account.event_hash:
                raise WorkbenchError("发布账号已变化，请重新准备精确发布预览")
        elif request.app.state.settings.publisher_mode != "gh":
            raise HTTPException(409, "真实 Publisher 尚未配置")
        try:
            wb = Workbench(session, request.app.state.settings.artifact_root)
            return JobResponse.model_validate(
                confirm_publication(
                    wb, task_id, intent_hash=payload.intent_hash, nonce=payload.nonce
                )
            )
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "发布已确认或状态已变化")

    app.include_router(router)
