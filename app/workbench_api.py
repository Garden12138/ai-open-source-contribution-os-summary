from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from app.jobs import TERMINAL_STATES, JobConflictError
from app.planner import PlanningService
from app.plans import PlanVersionError
from app.planning import ContributionTaskError
from app.schemas import JobResponse, PlanVersionResponse, PlanVersionCreateRequest
from app.task_states import ContributionTaskStateService
from app.workbench import Workbench, WorkbenchError
from app.contribution_workflow import authorize_execution, stop_workflow


class PlanningMessageRequest(BaseModel):
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


class ExecutePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=1, max_length=128)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve_plan: bool
    start_execution: bool
    max_repairs: int = Field(default=2, ge=2, le=2)
    model_binding_hash: str | None = None


class TaskModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_hash: str | None = None
    profiles: dict[str, str | None]


class TaskModelsResponse(BaseModel):
    record_hash: str
    changed: bool


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


class PublicationPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=7600)


class PublicationConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")


def register_workbench_routes(app, get_session, require_mutation_access) -> None:
    router = APIRouter(prefix="/api/v1/tasks/{task_id}/workbench", tags=["workbench"])

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

        return JSONResponse(status_code=409, content={"detail": redact_text(str(exc))})

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
        return WorkbenchResponse(
            task_id=task_id,
            state=ContributionTaskStateService(session).current(task_id).to_state,
            events=[
                {
                    "id": e.id,
                    "sequence": e.sequence,
                    "kind": e.kind,
                    "actor_id": e.actor_id,
                    "plan_id": e.plan_version_id,
                    "payload": e.payload,
                    "record_hash": e.record_hash,
                }
                for e in events
                if e.kind
                in {
                    "user_message",
                    "assistant_message",
                    "automation_started",
                    "automation_blocked",
                    "awaiting_acceptance",
                    "stop_requested",
                    "publication_prepared",
                    "publication_confirmed",
                    "publication_completed",
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
            busy=any(j.state not in TERMINAL_STATES for j in jobs),
            planner_available=reason is None,
            planner_unavailable_reason=reason,
            publisher_mode=request.app.state.settings.publisher_mode,
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
        from app.model_settings import ModelSettingsService
        models = ModelSettingsService(session)
        profiles = models.task_profiles(task_id, bind=True)
        if payload.model_profile_id and payload.model_profile_id != profiles.get("planning"):
            models.get(payload.model_profile_id, "profile")
            wb = Workbench(session, request.app.state.settings.artifact_root)
            wb.require_planning(task_id)
            wb.assert_idle(task_id)
            previous = models.latest("task:" + task_id)
            profiles = {**profiles, "planning": payload.model_profile_id}
            models.append("task:" + task_id, profiles, previous.record_hash if previous else None, commit=False)
            wb.append(task_id, "model_changed", {"planning_profile_id": payload.model_profile_id},
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
                model_profile_id=payload.model_profile_id,
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
        from app.model_settings import ModelSettingsService
        current_binding = ModelSettingsService(session).latest("task:" + task_id)
        if not current_binding and any(ModelSettingsService(session).defaults().values()):
            raise HTTPException(409, "请先在当前任务中确认模型，或发送消息重新规划")
        if current_binding and payload.model_binding_hash != current_binding.record_hash:
            raise HTTPException(409, "模型选择已变化，请重新确认当前方案和模型")
        job = authorize_execution(
            Workbench(session, settings.artifact_root),
            task_id,
            plan_id=payload.plan_id,
            plan_hash=payload.plan_hash,
            key=key,
            settings=settings,
        )
        return JobResponse.model_validate(job)

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
        return {"status": "stopping"}

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

        if request.app.state.settings.publisher_mode != "gh":
            raise HTTPException(409, "真实 Publisher 尚未配置")
        wb = Workbench(session, request.app.state.settings.artifact_root)
        return JobResponse.model_validate(
            request_publication(
                wb, task_id, title=payload.title, body=payload.body, key=key
            )
        )

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

        if request.app.state.settings.publisher_mode != "gh":
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
