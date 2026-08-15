from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.database import Database
from app.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    ExecutionArtifactManifestView,
)
from app.execution_control import (
    ExecutionControlConflictError,
    ExecutionStageControlService,
    ExecutionStageRunNotFoundError,
)
from app.execution_readiness import (
    ExecutionReadinessError,
    ExecutionReadinessService,
)
from app.executions import (
    ExecutionAttemptConflictError,
    ExecutionAttemptNotFoundError,
    ExecutionAttemptService,
    ExecutionStageTransitionError,
)
from app.approvals import (
    ApprovalInputFingerprint,
    PlanApprovalConflictError,
    PlanApprovalNotFoundError,
    PlanApprovalService,
)
from app.authorizations import UserAction
from app.archives import (
    RepositoryArchiveConflictError,
    RepositoryArchiveService,
)
from app.changesets import (
    ChangeSetConflictError,
    ChangeSetError,
    ChangeSetService,
    ChangeSetStore,
)
from app.dashboard import ContributionDashboardService
from app.lifecycle import LifecycleConflictError, TaskLifecycleService
from app.publish_intents import (
    PublishIntentConflictError,
    PublishIntentForbiddenError,
    PublishIntentNotFoundError,
    PublishIntentService,
    PublishIntentStaleError,
)
from app.publishers import FakeGitHubPublisher
from app.pull_request_events import (
    PullRequestEventConflictError,
    PullRequestEventNotFoundError,
    PullRequestEventService,
)
from app.reviews import (
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewRunService,
)
from app.github import GitHubClient
from app.jobs import (
    JobConflictError,
    JobNotFoundError,
    JobService,
    JobTransitionError,
)
from app.models import (
    DailyPick,
    DraftPullRequest,
    ExecutionAttempt,
    ExecutionStageRun,
    ExecutionStageVersion,
    Job,
    Opportunity,
    OpportunitySnapshot,
    PlanApproval,
    PlanLock,
    PlanVersion,
    PublishIntent,
    ReviewRun,
    ScanRun,
    TaskLifecycleMark,
)
from app.plan_locks import (
    PlanLockConflictError,
    PlanLockNotFoundError,
    PlanLockService,
)
from app.plan_conversations import (
    PlanConversationConflictError,
    PlanConversationNotFoundError,
    PlanConversationService,
)
from app.planning import (
    ContributionTaskConflictError,
    ContributionTaskNotFoundError,
    ContributionTaskService,
)
from app.provenance import content_hash
from app.plans import (
    PlanCommand,
    PlanContent,
    PlanVersionConflictError,
    PlanVersionNotFoundError,
    PlanVersionService,
)
from app.providers import (
    AnalysisHistoryConflictError,
    AnalysisHistoryNotFoundError,
    AnalysisHistoryService,
    JobProgressService,
    ManualAnalysisConflictError,
    ManualAnalysisNotFoundError,
    ManualAnalysisRequest,
    ManualAnalysisService,
    PROVIDER_ANALYSIS_JOB_KIND,
    resolve_analysis_availability,
    resolve_analysis_runtime,
    resolve_job_spec_signer,
)
from app.schemas import (
    ChangeSetCreateRequest,
    ChangeSetResponse,
    ContributionDashboardResponse,
    ContributionTaskSummaryResponse,
    DraftPullRequestResponse,
    PublishConfirmationResponse,
    PublishIntentConfirmRequest,
    PublishIntentCreateRequest,
    PublishIntentResponse,
    PullRequestEventCreateRequest,
    PullRequestEventResponse,
    RepairCreateRequest,
    ReviewCreateRequest,
    ReviewRunResponse,
    TaskLifecycleRequest,
    AnalysisAvailabilityResponse,
    AnalysisCreateRequest,
    AnalysisHistoryResponse,
    AnalysisVersionComparisonResponse,
    AnalysisVersionDetailResponse,
    AnalysisVersionSummaryResponse,
    ContributionTaskCreateRequest,
    ContributionTaskDetailResponse,
    ContributionTaskResponse,
    ContributionTaskStateResponse,
    DailyLeaderboardResponse,
    DailyPickItem,
    ExecutionArtifactEntryResponse,
    ExecutionArtifactListResponse,
    ExecutionArtifactManifestResponse,
    ExecutionAttemptResponse,
    ExecutionCreateRequest,
    ExecutionDetailResponse,
    ExecutionJobStatusResponse,
    ExecutionReadinessRequest,
    ExecutionReadinessResponse,
    ExecutionStageRunResponse,
    ExecutionStageResponse,
    HealthResponse,
    JobResponse,
    JobProgressEventResponse,
    JobProgressFeedResponse,
    MetaResponse,
    OpportunityDetail,
    PlanApprovalRequest,
    PlanApprovalResponse,
    PlanConversationEntryResponse,
    PlanConversationMessageRequest,
    PlanFieldDifferenceResponse,
    PlanLockResponse,
    PlanVersionCreateRequest,
    PlanVersionComparisonResponse,
    PlanVersionResponse,
    RepositoryArchiveCreateRequest,
    ScanRequest,
    ScanResponse,
)
from app.sandbox_worker.specs import SandboxPolicy
from app.security import redact_text
from app.task_states import (
    ContributionTaskStateService,
    TaskStateError,
    TaskStateConflictError,
    TaskStateTransitionError,
)
from app.worker import DISCOVERY_JOB_KIND


STATIC_DIR = Path(__file__).parent / "static"


def _execution_attempt_response(
    attempt: ExecutionAttempt,
    current_stage: ExecutionStageVersion,
) -> ExecutionAttemptResponse:
    return ExecutionAttemptResponse(
        id=attempt.id,
        task_id=attempt.task_id,
        plan_version_id=attempt.plan_version_id,
        plan_approval_id=attempt.plan_approval_id,
        approved_state_version_id=attempt.approved_state_version_id,
        executing_state_version_id=attempt.executing_state_version_id,
        schema_version=attempt.schema_version,
        attempt_number=attempt.attempt_number,
        actor_type=attempt.actor_type,
        actor_id=attempt.actor_id,
        action=attempt.action,
        repository_full_name=attempt.repository_full_name,
        base_commit_sha=attempt.base_commit_sha,
        repository_archive_hash=attempt.repository_archive_hash,
        runner_image_digest=attempt.runner_image_digest,
        sandbox_policy_version=attempt.sandbox_policy_version,
        sandbox_policy_hash=attempt.sandbox_policy_hash,
        task_record_hash=attempt.task_record_hash,
        analysis_version_id=attempt.analysis_version_id,
        analysis_record_hash=attempt.analysis_record_hash,
        analysis_output_hash=attempt.analysis_output_hash,
        snapshot_id=attempt.snapshot_id,
        snapshot_inputs_hash=attempt.snapshot_inputs_hash,
        provider_contract_hash=attempt.provider_contract_hash,
        plan_content_hash=attempt.plan_content_hash,
        plan_record_hash=attempt.plan_record_hash,
        approval_hash=attempt.approval_hash,
        approved_state_record_hash=attempt.approved_state_record_hash,
        executing_state_record_hash=attempt.executing_state_record_hash,
        observed_fingerprint_hash=attempt.observed_fingerprint_hash,
        record_hash=attempt.record_hash,
        created_at=attempt.created_at,
        current_stage=ExecutionStageResponse.model_validate(current_stage),
    )


def _execution_job_status_response(
    job: Job,
    *,
    secrets: tuple[str | None, ...],
) -> ExecutionJobStatusResponse:
    return ExecutionJobStatusResponse(
        state=job.state,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        timeout_seconds=job.timeout_seconds,
        lease_expires_at=job.lease_expires_at,
        heartbeat_at=job.heartbeat_at,
        cancel_requested_at=job.cancel_requested_at,
        progress_current=job.progress_current,
        progress_total=job.progress_total,
        progress_message=(
            redact_text(job.progress_message, secrets=secrets)
            if job.progress_message is not None
            else None
        ),
        error_code=(
            redact_text(job.error_code, secrets=secrets)
            if job.error_code is not None
            else None
        ),
        error_message=(
            redact_text(job.error_message, secrets=secrets)
            if job.error_message is not None
            else None
        ),
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _execution_stage_run_response(
    run: ExecutionStageRun,
    *,
    secrets: tuple[str | None, ...],
) -> ExecutionStageRunResponse:
    return ExecutionStageRunResponse(
        id=run.id,
        job_id=run.job_id,
        pending_stage_version_id=run.pending_stage_version_id,
        schema_version=run.schema_version,
        stage=run.stage,
        stage_run_number=run.stage_run_number,
        max_stage_runs=run.max_stage_runs,
        timeout_seconds=run.timeout_seconds,
        job_spec_hash=run.job_spec_hash,
        input_hashes=list(run.input_hashes),
        attempt_record_hash=run.attempt_record_hash,
        pending_stage_record_hash=run.pending_stage_record_hash,
        record_hash=run.record_hash,
        created_at=run.created_at,
        job=_execution_job_status_response(
            run.job,
            secrets=secrets,
        ),
    )


def _execution_artifact_manifest_response(
    manifest: ExecutionArtifactManifestView,
) -> ExecutionArtifactManifestResponse:
    return ExecutionArtifactManifestResponse(
        id=manifest.id,
        job_id=manifest.job_id,
        execution_stage_run_id=manifest.execution_stage_run_id,
        execution_attempt_id=manifest.execution_attempt_id,
        schema_version=manifest.schema_version,
        stage=manifest.stage,
        job_spec_hash=manifest.job_spec_hash,
        result_hash=manifest.result_hash,
        entry_count=manifest.entry_count,
        manifest_hash=manifest.manifest_hash,
        created_at=manifest.created_at,
        entries=[
            ExecutionArtifactEntryResponse(
                position=entry.position,
                role=entry.role,
                artifact_id=entry.artifact_id,
                algorithm=entry.algorithm,
                size_bytes=entry.size_bytes,
                media_type=entry.media_type,
                created_at=entry.created_at,
                content_url=(
                    f"/api/v1/executions/{manifest.execution_attempt_id}"
                    f"/artifacts/{entry.artifact_id}"
                ),
            )
            for entry in manifest.entries
        ],
    )


def _execution_artifact_store(
    session: Session,
    settings: Settings,
) -> ArtifactStore:
    return ArtifactStore(
        session,
        settings.artifact_root,
        secrets=(settings.github_token, settings.local_access_token),
    )


def _review_response(review, *, stale: bool) -> ReviewRunResponse:
    payload = ReviewRunResponse.model_validate(review).model_dump()
    payload["stale"] = stale
    return ReviewRunResponse.model_validate(payload)


def _publish_intent_response(intent, *, status: str) -> PublishIntentResponse:
    return PublishIntentResponse(
        id=intent.id,
        review_run_id=intent.review_run_id,
        execution_attempt_id=intent.execution_attempt_id,
        task_id=intent.task_id,
        schema_version=intent.schema_version,
        actor_type=intent.actor_type,
        actor_id=intent.actor_id,
        upstream_repository=intent.upstream_repository,
        base_commit_sha=intent.base_commit_sha,
        head_branch=intent.head_branch,
        head_commit_sha=intent.head_commit_sha,
        diff_hash=intent.diff_hash,
        test_results_hash=intent.test_results_hash,
        review_record_hash=intent.review_record_hash,
        title=intent.title,
        body=intent.body,
        allowed_actions=list(intent.allowed_actions),
        confirmation_nonce=intent.confirmation_nonce,
        expires_at=intent.expires_at,
        status=status,  # type: ignore[arg-type]
        record_hash=intent.record_hash,
        created_at=intent.created_at,
    )


def _schedule_execution_if_signed(
    request: Request,
    session: Session,
    attempt_id: str,
):
    executions = ExecutionAttemptService(session)
    signer = request.app.state.job_spec_signer
    if signer is None:
        return executions.current(attempt_id)
    spec = executions.build_current_job_spec(attempt_id)
    ExecutionStageControlService(session).schedule_current(
        attempt_id,
        signed_job_spec=signer.sign(spec),
        signer=signer,
        sandbox_policy=SandboxPolicy(),
        idempotency_key="schedule:" + attempt_id,
    )
    return executions.current(attempt_id)


def get_session(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.database
    with database.session() as session:
        yield session


def require_mutation_access(
    request: Request,
    authorization: str | None = Header(default=None),
    access_token: str | None = Header(
        default=None,
        alias="X-ContribOS-Token",
    ),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    settings: Settings = request.app.state.settings
    origin = request.headers.get("origin")
    if origin is not None:
        expected_origin = str(request.base_url).rstrip("/")
        if origin.rstrip("/") != expected_origin:
            raise HTTPException(status_code=403, detail="Cross-origin request rejected")
        expected_csrf: str = request.app.state.csrf_token
        if csrf_token is None or not compare_digest(csrf_token, expected_csrf):
            raise HTTPException(status_code=403, detail="CSRF validation failed")

    if settings.local_access_token:
        bearer = None
        if authorization and authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip()
        presented = bearer or access_token
        if presented is None or not compare_digest(
            presented,
            settings.local_access_token,
        ):
            raise HTTPException(
                status_code=401,
                detail="Local access token required",
                headers={"WWW-Authenticate": "Bearer"},
            )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.create_schema()
        yield
        database.close()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Discover, filter, and rank open-source contribution opportunities.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.github_client_factory = lambda: GitHubClient(settings)
    provider, budget = resolve_analysis_runtime(settings)
    app.state.analysis_provider = provider
    app.state.analysis_budget = budget
    app.state.job_spec_signer = resolve_job_spec_signer(settings)
    app.state.csrf_token = token_urlsafe(32)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        message = redact_text(
            exc.detail if isinstance(exc.detail, str) else "Request failed",
            secrets=(settings.github_token, settings.local_access_token),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": message,
                }
            },
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _: Request, __: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                }
            },
        )

    @app.exception_handler(TaskStateConflictError)
    async def task_state_conflict(
        _: Request,
        exc: TaskStateConflictError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "task_state_conflict",
                    "message": redact_text(
                        str(exc),
                        secrets=(
                            settings.github_token,
                            settings.local_access_token,
                        ),
                    ),
                }
            },
        )

    @app.exception_handler(TaskStateTransitionError)
    async def illegal_task_state_transition(
        _: Request,
        exc: TaskStateTransitionError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "illegal_task_state_transition",
                    "message": redact_text(
                        str(exc),
                        secrets=(
                            settings.github_token,
                            settings.local_access_token,
                        ),
                    ),
                }
            },
        )

    @app.exception_handler(Exception)
    async def internal_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Internal request failure",
                }
            },
        )

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health(session: Session = Depends(get_session)) -> HealthResponse:
        session.execute(text("SELECT 1"))
        return HealthResponse(status="ok", database="ok")

    @app.get("/api/v1/meta", response_model=MetaResponse, tags=["system"])
    def metadata(request: Request) -> MetaResponse:
        current: Settings = request.app.state.settings
        return MetaResponse(
            app_name=current.app_name,
            token_configured=bool(current.github_token),
            local_access_token_required=bool(current.local_access_token),
            csrf_token=request.app.state.csrf_token,
            preferred_languages=list(current.preferred_languages),
            queries=list(current.github_queries),
            daily_pick_count=current.daily_pick_count,
            timezone=current.timezone,
        )

    @app.get(
        "/api/v1/opportunities/daily",
        response_model=DailyLeaderboardResponse,
        tags=["opportunities"],
    )
    def daily_opportunities(
        request: Request,
        on: date | None = Query(default=None, description="Selection date"),
        limit: int = Query(default=10, ge=1, le=50),
        session: Session = Depends(get_session),
    ) -> DailyLeaderboardResponse:
        current: Settings = request.app.state.settings
        selection_date = on or datetime.now(ZoneInfo(current.timezone)).date()
        picks = list(
            session.scalars(
                select(DailyPick)
                .options(
                    joinedload(DailyPick.opportunity).joinedload(Opportunity.repository)
                )
                .where(DailyPick.selection_date == selection_date)
                .order_by(DailyPick.rank)
                .limit(limit)
            ).unique()
        )
        linked_scan_ids = {
            pick.scan_run_id for pick in picks if pick.scan_run_id is not None
        }
        linked_scan = (
            session.get(ScanRun, next(iter(linked_scan_ids)))
            if len(linked_scan_ids) == 1
            else None
        )
        date_scan = session.scalar(
            select(ScanRun)
            .where(
                ScanRun.status == "completed",
                ScanRun.selection_date == selection_date,
            )
            .order_by(desc(ScanRun.completed_at))
            .limit(1)
        )
        scan = linked_scan or (date_scan if not picks else None)
        provenance_status = (
            scan.provenance_status
            if scan is not None
            else "legacy_unverified"
            if picks
            else "not_generated"
        )
        analysis = resolve_analysis_availability(
            provider=request.app.state.analysis_provider,
            budget=request.app.state.analysis_budget,
        )
        return DailyLeaderboardResponse(
            selection_date=selection_date,
            scan_run_id=scan.id if scan else None,
            provenance_status=provenance_status,
            generated_at=scan.completed_at if scan else None,
            total_candidates=scan.candidate_count if scan else 0,
            total_eligible=scan.eligible_count if scan else 0,
            analysis=AnalysisAvailabilityResponse(
                mode=analysis.mode,
                automatic_model_invocation_enabled=(
                    analysis.automatic_model_invocation_enabled
                ),
                rule_leaderboard_preserved=analysis.rule_leaderboard_preserved,
                fallback_reasons=list(analysis.fallback_reasons),
            ),
            picks=[DailyPickItem.model_validate(pick) for pick in picks],
        )

    @app.get(
        "/api/v1/opportunities/{opportunity_id}",
        response_model=OpportunityDetail,
        tags=["opportunities"],
    )
    def opportunity_detail(
        opportunity_id: int, session: Session = Depends(get_session)
    ) -> OpportunityDetail:
        opportunity = session.scalar(
            select(Opportunity)
            .options(joinedload(Opportunity.repository))
            .where(Opportunity.id == opportunity_id)
        )
        if opportunity is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        return OpportunityDetail.model_validate(opportunity)

    @app.post(
        "/api/v1/opportunities/{opportunity_id}/analyses",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["analysis"],
    )
    def create_analysis(
        opportunity_id: int,
        payload: AnalysisCreateRequest,
        request: Request,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> JobResponse:
        provider = request.app.state.analysis_provider
        budget = request.app.state.analysis_budget
        availability = resolve_analysis_availability(
            provider=provider,
            budget=budget,
        )
        if not availability.automatic_model_invocation_enabled:
            reasons = ", ".join(
                reason.value for reason in availability.fallback_reasons
            )
            raise HTTPException(
                status_code=409,
                detail=f"Analysis is unavailable: {reasons}",
            )
        opportunity = session.get(Opportunity, opportunity_id)
        if opportunity is None:
            raise HTTPException(
                status_code=404,
                detail="Opportunity not found",
            )
        snapshot = session.get(
            OpportunitySnapshot,
            payload.snapshot_id,
        )
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail="OpportunitySnapshot not found",
            )
        if snapshot.opportunity_id != opportunity.id:
            raise HTTPException(
                status_code=409,
                detail="OpportunitySnapshot does not match the Opportunity",
            )
        key = idempotency_key or str(uuid4())
        correlation_id = "analysis-" + content_hash(
            {
                "opportunity_id": opportunity.id,
                "snapshot_id": snapshot.id,
                "idempotency_key": key,
            }
        )[:32]
        try:
            job, _ = ManualAnalysisService(session).request(
                ManualAnalysisRequest(
                    snapshot_id=snapshot.id,
                    correlation_id=correlation_id,
                    idempotency_key=key,
                    budget=budget,
                    expected_provider=provider.identity,
                )
            )
        except ManualAnalysisNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ManualAnalysisConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JobResponse.model_validate(job)

    @app.get(
        "/api/v1/opportunities/{opportunity_id}/analyses",
        response_model=AnalysisHistoryResponse,
        tags=["analysis"],
    )
    def analysis_history(
        opportunity_id: int,
        limit: int = Query(default=50, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> AnalysisHistoryResponse:
        try:
            versions = AnalysisHistoryService(
                session
            ).list_for_opportunity(
                opportunity_id,
                limit=limit,
            )
        except AnalysisHistoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return AnalysisHistoryResponse(
            opportunity_id=opportunity_id,
            versions=[
                AnalysisVersionSummaryResponse.model_validate(item)
                for item in versions
            ],
        )

    @app.post(
        "/api/v1/tasks",
        response_model=ContributionTaskResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["planning"],
    )
    def create_contribution_task(
        payload: ContributionTaskCreateRequest,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> ContributionTaskResponse:
        try:
            task = ContributionTaskService(session).create(
                analysis_version_id=payload.analysis_version_id,
                idempotency_key=idempotency_key or str(uuid4()),
            )
        except ContributionTaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ContributionTaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ContributionTaskResponse.model_validate(task)

    @app.get(
        "/api/v1/tasks",
        response_model=list[ContributionTaskSummaryResponse],
        tags=["planning"],
    )
    def list_contribution_tasks(
        state: str | None = Query(default=None, max_length=40),
        session: Session = Depends(get_session),
    ) -> list[ContributionTaskSummaryResponse]:
        try:
            summaries = ContributionDashboardService(session).task_summaries(
                state=state,
            )
        except TaskStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return [
            ContributionTaskSummaryResponse.model_validate(item)
            for item in summaries
        ]

    @app.get(
        "/api/v1/tasks/{task_id}",
        response_model=ContributionTaskDetailResponse,
        tags=["planning"],
    )
    def contribution_task_detail(
        task_id: str,
        session: Session = Depends(get_session),
    ) -> ContributionTaskDetailResponse:
        try:
            task = ContributionTaskService(session).get_verified(task_id)
            current = ContributionTaskStateService(session).current(task.id)
            plan_rows = list(
                session.scalars(
                    select(PlanVersion)
                    .where(PlanVersion.task_id == task.id)
                    .order_by(PlanVersion.version_number)
                )
            )
            plans = [
                PlanVersionService(session).get_verified(plan.id)
                for plan in plan_rows
            ]
            lock_rows = list(
                session.scalars(
                    select(PlanLock)
                    .where(PlanLock.task_id == task.id)
                    .order_by(PlanLock.created_at, PlanLock.id)
                )
            )
            locks = [
                PlanLockService(session).get_verified(lock.id)
                for lock in lock_rows
            ]
            approval_rows = list(
                session.scalars(
                    select(PlanApproval)
                    .where(PlanApproval.task_id == task.id)
                    .order_by(PlanApproval.created_at, PlanApproval.id)
                )
            )
            approvals = [
                PlanApprovalService(session).get_verified(approval.id)
                for approval in approval_rows
            ]
            execution_rows = list(
                session.scalars(
                    select(ExecutionAttempt)
                    .where(ExecutionAttempt.task_id == task.id)
                    .order_by(
                        ExecutionAttempt.attempt_number,
                        ExecutionAttempt.id,
                    )
                )
            )
            executions = [
                ExecutionAttemptService(session).get_verified(execution.id)
                for execution in execution_rows
            ]
            conversation = PlanConversationService(session).history(task.id)
            review_rows = list(
                session.scalars(
                    select(ReviewRun)
                    .where(ReviewRun.task_id == task.id)
                    .order_by(ReviewRun.review_number, ReviewRun.id)
                )
            )
            reviews = [
                ReviewRunService(session).get_verified(review.id)
                for review in review_rows
            ]
            intent_rows = list(
                session.scalars(
                    select(PublishIntent)
                    .where(PublishIntent.task_id == task.id)
                    .order_by(PublishIntent.created_at, PublishIntent.id)
                )
            )
            intents = [
                PublishIntentService(session).get_verified(intent.id)
                for intent in intent_rows
            ]
            draft_rows = list(
                session.scalars(
                    select(DraftPullRequest)
                    .where(DraftPullRequest.task_id == task.id)
                    .order_by(DraftPullRequest.created_at, DraftPullRequest.id)
                )
            )
        except ContributionTaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ContributionTaskConflictError,
            TaskStateError,
            PlanVersionNotFoundError,
            PlanVersionConflictError,
            PlanLockNotFoundError,
            PlanLockConflictError,
            PlanApprovalNotFoundError,
            PlanApprovalConflictError,
            PlanConversationConflictError,
            ExecutionAttemptConflictError,
            ReviewConflictError,
            PublishIntentConflictError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        active = next(
            (
                approval
                for approval in approvals
                if approval.approved_state_version_id == current.id
                and approval.approved_state_record_hash
                == current.record_hash
            ),
            None,
        )
        if active is None and executions:
            latest_execution = executions[-1]
            active = next(
                (
                    approval
                    for approval in approvals
                    if approval.id == latest_execution.plan_approval_id
                ),
                None,
            )
        approval_status = (
            "approved"
            if active is not None
            else "revoked"
            if approvals
            else "unapproved"
        )
        return ContributionTaskDetailResponse(
            task=ContributionTaskResponse.model_validate(task),
            current_state=ContributionTaskStateResponse.model_validate(
                current
            ),
            plan_versions=[
                PlanVersionResponse.model_validate(plan) for plan in plans
            ],
            plan_locks=[
                PlanLockResponse.model_validate(lock) for lock in locks
            ],
            approvals=[
                PlanApprovalResponse.model_validate(approval)
                for approval in approvals
            ],
            conversation=[
                PlanConversationEntryResponse.model_validate(entry)
                for entry in conversation
            ],
            latest_plan_version_id=plans[-1].id if plans else None,
            active_approval_id=active.id if active is not None else None,
            approval_status=approval_status,
            execution_attempt_ids=[
                execution.id for execution in executions
            ],
            latest_execution_attempt_id=(
                executions[-1].id if executions else None
            ),
            review_run_ids=[review.id for review in reviews],
            latest_review_run_id=reviews[-1].id if reviews else None,
            publish_intent_ids=[intent.id for intent in intents],
            latest_publish_intent_id=intents[-1].id if intents else None,
            draft_pull_request_ids=[draft.id for draft in draft_rows],
            latest_draft_pull_request_id=(
                draft_rows[-1].id if draft_rows else None
            ),
        )

    @app.post(
        "/api/v1/tasks/{task_id}/plan-versions",
        response_model=PlanVersionResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["planning"],
    )
    def create_plan_version(
        task_id: str,
        payload: PlanVersionCreateRequest,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> PlanVersionResponse:
        try:
            content = PlanContent(
                goal=payload.goal,
                acceptance_criteria=tuple(payload.acceptance_criteria),
                files_to_inspect=tuple(payload.files_to_inspect),
                files_likely_to_change=tuple(
                    payload.files_likely_to_change
                ),
                implementation_steps=tuple(payload.implementation_steps),
                tests_to_add_or_run=tuple(payload.tests_to_add_or_run),
                commands_to_run=tuple(
                    PlanCommand(
                        command_id=command.command_id,
                        purpose=command.purpose,
                        argv=tuple(command.argv),
                        working_directory=command.working_directory,
                    )
                    for command in payload.commands_to_run
                ),
                risks=tuple(payload.risks),
                questions_for_maintainer=tuple(
                    payload.questions_for_maintainer
                ),
            )
            plans = PlanVersionService(session)
            if payload.parent_version_id is None:
                plan = plans.create_initial(
                    task_id=task_id,
                    content=content,
                    idempotency_key=idempotency_key or str(uuid4()),
                )
            else:
                parent = plans.get_verified(payload.parent_version_id)
                if parent.task_id != task_id:
                    raise PlanVersionConflictError(
                        "Plan revision parent belongs to another task"
                    )
                plan = plans.create_revision(
                    parent_version_id=parent.id,
                    content=content,
                    idempotency_key=idempotency_key or str(uuid4()),
                )
        except PlanVersionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanVersionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return PlanVersionResponse.model_validate(plan)

    @app.post(
        "/api/v1/plan-versions/{plan_version_id}/approve",
        response_model=PlanApprovalResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["planning"],
    )
    def approve_plan_version(
        plan_version_id: str,
        payload: PlanApprovalRequest,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> PlanApprovalResponse:
        key = idempotency_key or str(uuid4())
        lock_key = "approve-lock-" + content_hash(
            {
                "plan_version_id": plan_version_id,
                "idempotency_key": key,
            }
        )[:40]
        try:
            lock = PlanLockService(session).create(
                plan_version_id=plan_version_id,
                base_commit_sha=payload.base_commit_sha,
                idempotency_key=lock_key,
            )
            approval = PlanApprovalService(session).approve(
                plan_lock_id=lock.id,
                idempotency_key=key,
                actor_type="local_user",
                actor_id=payload.actor_id,
                action=UserAction.APPROVE_PLAN,
            )
        except (PlanLockNotFoundError, PlanApprovalNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            PlanLockConflictError,
            PlanApprovalConflictError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return PlanApprovalResponse.model_validate(approval)

    @app.post(
        "/api/v1/tasks/{task_id}/conversation/messages",
        response_model=PlanConversationEntryResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["planning"],
    )
    def append_plan_conversation_message(
        task_id: str,
        payload: PlanConversationMessageRequest,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> PlanConversationEntryResponse:
        try:
            entry = PlanConversationService(session).append_message(
                task_id=task_id,
                plan_version_id=payload.plan_version_id,
                actor_type="local_user",
                actor_id=payload.actor_id,
                text=payload.text,
                idempotency_key=idempotency_key or str(uuid4()),
            )
        except PlanConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanConversationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return PlanConversationEntryResponse.model_validate(entry)

    @app.get(
        "/api/v1/tasks/{task_id}/plan-versions/compare",
        response_model=PlanVersionComparisonResponse,
        tags=["planning"],
    )
    def compare_plan_versions(
        task_id: str,
        left_version_id: str = Query(min_length=1, max_length=128),
        right_version_id: str = Query(min_length=1, max_length=128),
        session: Session = Depends(get_session),
    ) -> PlanVersionComparisonResponse:
        try:
            comparison = PlanVersionService(session).compare(
                left_version_id,
                right_version_id,
            )
        except PlanVersionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PlanVersionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if comparison.task_id != task_id:
            raise HTTPException(
                status_code=409,
                detail="PlanVersion comparison belongs to another task",
            )
        return PlanVersionComparisonResponse(
            left_version_id=comparison.left_version_id,
            right_version_id=comparison.right_version_id,
            task_id=comparison.task_id,
            semantic_differences=[
                PlanFieldDifferenceResponse.model_validate(item)
                for item in comparison.semantic_differences
            ],
            unified_diff=comparison.unified_diff,
        )

    @app.post(
        "/api/v1/plan-approvals/{approval_id}/execution-readiness",
        response_model=ExecutionReadinessResponse,
        tags=["planning"],
    )
    def check_execution_readiness(
        approval_id: str,
        payload: ExecutionReadinessRequest,
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> ExecutionReadinessResponse:
        try:
            approval = PlanApprovalService(session).get_verified(approval_id)
            lock = PlanLockService(session).get_verified(
                approval.plan_lock_id
            )
            latest = session.scalar(
                select(PlanVersion)
                .where(PlanVersion.task_id == approval.task_id)
                .order_by(
                    PlanVersion.version_number.desc(),
                    PlanVersion.id.desc(),
                )
                .limit(1)
            )
            if latest is None:
                raise ExecutionReadinessError(
                    "Execution requires a current PlanVersion",
                    reason_codes=("plan_missing",),
                )
            latest = PlanVersionService(session).get_verified(latest.id)
            observed = ApprovalInputFingerprint(
                analysis_version_id=lock.analysis_version_id,
                snapshot_id=lock.snapshot_id,
                base_commit_sha=payload.base_commit_sha,
                provider_contract_hash=lock.provider_contract_hash,
                plan_version_id=latest.id,
                plan_content_hash=latest.content_hash,
                plan_record_hash=latest.record_hash,
            )
            readiness = ExecutionReadinessService(session).assert_ready(
                approval_id=approval.id,
                observed=observed,
                action=UserAction.START_EXECUTION,
            )
        except (PlanApprovalNotFoundError, PlanLockNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            PlanApprovalConflictError,
            PlanLockConflictError,
            PlanVersionConflictError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ExecutionReadinessError as exc:
            reasons = ",".join(exc.reason_codes)
            raise HTTPException(
                status_code=409,
                detail=f"{exc} [{reasons}]",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ExecutionReadinessResponse.model_validate(readiness)

    @app.post(
        "/api/v1/plan-versions/{plan_version_id}/archives",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["execution"],
    )
    def create_repository_archive(
        plan_version_id: str,
        payload: RepositoryArchiveCreateRequest,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> JobResponse:
        try:
            job, _ = RepositoryArchiveService(session).enqueue_for_approved_plan(
                plan_version_id=plan_version_id,
                approval_id=payload.approval_id,
                base_commit_sha=payload.base_commit_sha,
                idempotency_key=idempotency_key or str(uuid4()),
            )
        except (
            PlanVersionNotFoundError,
            PlanApprovalNotFoundError,
            PlanLockNotFoundError,
            ContributionTaskNotFoundError,
        ) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            RepositoryArchiveConflictError,
            PlanVersionConflictError,
            PlanApprovalConflictError,
            PlanLockConflictError,
            ContributionTaskConflictError,
            JobConflictError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JobResponse.model_validate(job)

    @app.post(
        "/api/v1/plan-versions/{plan_version_id}/executions",
        response_model=ExecutionAttemptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["execution"],
    )
    def create_execution(
        plan_version_id: str,
        payload: ExecutionCreateRequest,
        request: Request,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> ExecutionAttemptResponse:
        try:
            plan = PlanVersionService(session).get_verified(plan_version_id)
            approval = PlanApprovalService(session).get_verified(
                payload.approval_id
            )
            if approval.plan_version_id != plan.id:
                raise ExecutionAttemptConflictError(
                    "PlanApproval belongs to another PlanVersion"
                )
            lock = PlanLockService(session).get_verified(
                approval.plan_lock_id
            )
            observed = ApprovalInputFingerprint(
                analysis_version_id=lock.analysis_version_id,
                snapshot_id=lock.snapshot_id,
                base_commit_sha=payload.base_commit_sha,
                provider_contract_hash=lock.provider_contract_hash,
                plan_version_id=plan.id,
                plan_content_hash=plan.content_hash,
                plan_record_hash=plan.record_hash,
            )
            executions = ExecutionAttemptService(session)
            attempt = executions.start(
                approval_id=approval.id,
                observed=observed,
                action=UserAction.START_EXECUTION,
                idempotency_key=idempotency_key or str(uuid4()),
                actor_type="local_user",
                actor_id=payload.actor_id,
                repository_archive_hash=payload.repository_archive_hash,
                runner_image_digest=payload.runner_image_digest,
                sandbox_policy=SandboxPolicy(),
            )
            current_stage = executions.current(attempt.id)
            signer = request.app.state.job_spec_signer
            if signer is not None:
                spec = executions.build_current_job_spec(attempt.id)
                ExecutionStageControlService(session).schedule_current(
                    attempt.id,
                    signed_job_spec=signer.sign(spec),
                    signer=signer,
                    sandbox_policy=SandboxPolicy(),
                    idempotency_key="schedule:" + attempt.id,
                )
                current_stage = executions.current(attempt.id)
        except (
            PlanVersionNotFoundError,
            PlanApprovalNotFoundError,
            PlanLockNotFoundError,
        ) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExecutionAttemptConflictError as exc:
            reasons = ",".join(exc.reason_codes)
            detail = f"{exc} [{reasons}]" if reasons else str(exc)
            raise HTTPException(status_code=409, detail=detail) from exc
        except (
            PlanVersionConflictError,
            PlanApprovalConflictError,
            PlanLockConflictError,
            ExecutionControlConflictError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _execution_attempt_response(attempt, current_stage)

    @app.post(
        "/api/v1/executions/{execution_attempt_id}/change-sets",
        response_model=ChangeSetResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["execution"],
    )
    def accept_change_set(
        execution_attempt_id: str,
        payload: ChangeSetCreateRequest,
        request: Request,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> ChangeSetResponse:
        signer = request.app.state.job_spec_signer
        if signer is None:
            raise HTTPException(
                status_code=409,
                detail="ChangeSet acceptance requires a configured JobSpec signer",
            )
        settings: Settings = request.app.state.settings
        try:
            change_set = ChangeSetService(
                session,
                ChangeSetStore(settings.artifact_root),
                signer,
            ).accept(
                execution_attempt_id,
                source=payload.source,
                operations=(
                    None
                    if payload.operations is None
                    else [item.model_dump() for item in payload.operations]
                ),
                idempotency_key=idempotency_key or str(uuid4()),
            )
        except ExecutionAttemptNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ChangeSetConflictError,
            ChangeSetError,
            ExecutionAttemptConflictError,
            ExecutionControlConflictError,
            ExecutionStageTransitionError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ChangeSetResponse(
            change_set_id=change_set.change_set_id,
            change_set_hash=change_set.change_set_hash,
            plan_version_id=change_set.plan_version_id,
            paths=list(change_set.paths),
        )

    @app.get(
        "/api/v1/executions/{execution_attempt_id}",
        response_model=ExecutionDetailResponse,
        tags=["execution"],
    )
    def execution_detail(
        execution_attempt_id: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> ExecutionDetailResponse:
        settings: Settings = request.app.state.settings
        secrets = (settings.github_token, settings.local_access_token)
        try:
            executions = ExecutionAttemptService(session)
            attempt = executions.get_verified(execution_attempt_id)
            stages = executions.history(attempt.id)
            run_rows = tuple(
                session.scalars(
                    select(ExecutionStageRun)
                    .where(
                        ExecutionStageRun.execution_attempt_id == attempt.id
                    )
                    .order_by(
                        ExecutionStageRun.created_at,
                        ExecutionStageRun.id,
                    )
                )
            )
            controls = ExecutionStageControlService(session)
            runs = tuple(
                controls.get_verified(run.id) for run in run_rows
            )
            manifests = _execution_artifact_store(
                session,
                settings,
            ).execution_manifests(attempt.id)
        except ExecutionAttemptNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ExecutionAttemptConflictError,
            ExecutionControlConflictError,
            ArtifactIntegrityError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        base = _execution_attempt_response(
            attempt,
            stages[-1],
        ).model_dump()
        return ExecutionDetailResponse(
            **base,
            stages=[
                ExecutionStageResponse.model_validate(stage)
                for stage in stages
            ],
            stage_runs=[
                _execution_stage_run_response(run, secrets=secrets)
                for run in runs
            ],
            artifact_manifests=[
                _execution_artifact_manifest_response(manifest)
                for manifest in manifests
            ],
            reviews=[
                _review_response(
                    review,
                    stale=ReviewRunService(session).is_stale(review.id),
                )
                for review in ReviewRunService(
                    session,
                    _execution_artifact_store(session, settings),
                ).list_for_attempt(attempt.id)
            ],
        )

    @app.get(
        "/api/v1/executions/{execution_attempt_id}/artifacts",
        response_model=ExecutionArtifactListResponse,
        tags=["execution"],
    )
    def execution_artifacts(
        execution_attempt_id: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> ExecutionArtifactListResponse:
        settings: Settings = request.app.state.settings
        try:
            attempt = ExecutionAttemptService(session).get_verified(
                execution_attempt_id
            )
            manifests = _execution_artifact_store(
                session,
                settings,
            ).execution_manifests(attempt.id)
        except ExecutionAttemptNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ExecutionAttemptConflictError,
            ArtifactIntegrityError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ExecutionArtifactListResponse(
            execution_attempt_id=attempt.id,
            manifests=[
                _execution_artifact_manifest_response(manifest)
                for manifest in manifests
            ],
        )

    @app.get(
        "/api/v1/executions/{execution_attempt_id}/artifacts/{artifact_id}",
        tags=["execution"],
        response_class=Response,
    )
    def execution_artifact_content(
        execution_attempt_id: str,
        artifact_id: str,
        request: Request,
        if_none_match: str | None = Header(
            default=None,
            alias="If-None-Match",
            max_length=160,
        ),
        session: Session = Depends(get_session),
    ) -> Response:
        settings: Settings = request.app.state.settings
        try:
            attempt = ExecutionAttemptService(session).get_verified(
                execution_attempt_id
            )
            blob = _execution_artifact_store(
                session,
                settings,
            ).read_execution_artifact(
                execution_attempt_id=attempt.id,
                artifact_id=artifact_id,
            )
        except (
            ExecutionAttemptNotFoundError,
            ArtifactNotFoundError,
        ) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ExecutionAttemptConflictError,
            ArtifactIntegrityError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        etag = f'"sha256:{blob.artifact_id}"'
        extension = "diff" if blob.role == "unified-diff" else "json"
        headers = {
            "ETag": etag,
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Disposition": (
                'attachment; filename="'
                f"{blob.role}-{blob.artifact_id[:12]}.{extension}"
                '"'
            ),
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
            "X-ContribOS-Artifact-Role": blob.role,
            "X-ContribOS-Artifact-Stage": blob.stage,
        }
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            content=blob.data,
            media_type=blob.media_type,
            headers=headers,
        )

    @app.get(
        "/api/v1/opportunities/{opportunity_id}/analyses/compare",
        response_model=AnalysisVersionComparisonResponse,
        tags=["analysis"],
    )
    def compare_analysis_versions(
        opportunity_id: int,
        left_version_id: str = Query(min_length=1, max_length=128),
        right_version_id: str = Query(min_length=1, max_length=128),
        session: Session = Depends(get_session),
    ) -> AnalysisVersionComparisonResponse:
        try:
            comparison = AnalysisHistoryService(session).compare(
                left_version_id,
                right_version_id,
            )
        except AnalysisHistoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AnalysisHistoryConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if comparison.opportunity_id != opportunity_id:
            raise HTTPException(
                status_code=409,
                detail="AnalysisVersion does not match the Opportunity",
            )
        return AnalysisVersionComparisonResponse(
            left_version_id=comparison.left_version_id,
            right_version_id=comparison.right_version_id,
            opportunity_id=comparison.opportunity_id,
            same_snapshot=comparison.same_snapshot,
            same_rule_score=comparison.same_rule_score,
            same_frozen_input=comparison.same_frozen_input,
            left=dict(comparison.left),
            right=dict(comparison.right),
            differences=[
                {
                    "path": item.path,
                    "left_present": item.left_present,
                    "right_present": item.right_present,
                    "left": item.left,
                    "right": item.right,
                }
                for item in comparison.differences
            ],
        )

    @app.get(
        "/api/v1/opportunities/{opportunity_id}/analyses/{version_id}",
        response_model=AnalysisVersionDetailResponse,
        tags=["analysis"],
    )
    def analysis_version_detail(
        opportunity_id: int,
        version_id: str,
        session: Session = Depends(get_session),
    ) -> AnalysisVersionDetailResponse:
        try:
            detail = AnalysisHistoryService(session).get_detail(version_id)
        except AnalysisHistoryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AnalysisHistoryConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if detail.summary.opportunity_id != opportunity_id:
            raise HTTPException(
                status_code=409,
                detail="AnalysisVersion does not match the Opportunity",
            )
        return AnalysisVersionDetailResponse(
            **{
                field: getattr(detail.summary, field)
                for field in (
                    "id",
                    "job_id",
                    "opportunity_id",
                    "snapshot_id",
                    "score_version_id",
                    "provider_name",
                    "model_name",
                    "model_version",
                    "analyze_output_schema_version",
                    "record_hash",
                    "created_at",
                )
            },
            content=dict(detail.content),
        )

    @app.get("/api/v1/scans", response_model=list[ScanResponse], tags=["discovery"])
    def scan_history(
        limit: int = Query(default=20, ge=1, le=100),
        session: Session = Depends(get_session),
    ) -> list[ScanResponse]:
        runs = session.scalars(
            select(ScanRun).order_by(desc(ScanRun.started_at)).limit(limit)
        )
        return [ScanResponse.model_validate(run) for run in runs]

    @app.post(
        "/api/v1/scans",
        response_model=JobResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["discovery"],
    )
    def start_scan(
        payload: ScanRequest,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> JobResponse:
        try:
            job, _ = JobService(session).enqueue(
                kind=DISCOVERY_JOB_KIND,
                idempotency_key=idempotency_key or str(uuid4()),
                payload={
                    "queries": payload.queries,
                    "top_n": payload.top_n,
                },
            )
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JobResponse.model_validate(job)

    @app.get(
        "/api/v1/jobs/{job_id}",
        response_model=JobResponse,
        tags=["jobs"],
    )
    def job_detail(
        job_id: str,
        session: Session = Depends(get_session),
    ) -> JobResponse:
        try:
            job = JobService(session).get(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JobResponse.model_validate(job)

    @app.get(
        "/api/v1/jobs/{job_id}/events",
        response_model=JobProgressFeedResponse,
        tags=["jobs"],
    )
    def job_progress_events(
        job_id: str,
        after_revision: str | None = Query(
            default=None,
            min_length=64,
            max_length=64,
            pattern=r"^[0-9a-f]{64}$",
        ),
        session: Session = Depends(get_session),
    ) -> JobProgressFeedResponse:
        try:
            feed = JobProgressService(session).get(
                job_id,
                after_revision=after_revision,
            )
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JobProgressFeedResponse(
            job_id=feed.job_id,
            state=feed.state,
            revision=feed.revision,
            unchanged=feed.unchanged,
            events=[
                JobProgressEventResponse(
                    sequence=event.sequence,
                    event_id=event.event_id,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    data=dict(event.data),
                )
                for event in feed.events
            ],
        )

    @app.post(
        "/api/v1/jobs/{job_id}/cancel",
        response_model=JobResponse,
        tags=["jobs"],
    )
    def cancel_job(
        job_id: str,
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> JobResponse:
        try:
            job = JobService(session).request_cancel(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JobResponse.model_validate(job)

    @app.post(
        "/api/v1/jobs/{job_id}/retry",
        response_model=JobResponse,
        tags=["jobs"],
    )
    def retry_job(
        job_id: str,
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> JobResponse:
        try:
            current = JobService(session).get(job_id)
            job = (
                ManualAnalysisService(session).retry(job_id)
                if current.kind == PROVIDER_ANALYSIS_JOB_KIND
                else JobService(session).retry(job_id)
            )
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ManualAnalysisNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ManualAnalysisConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except JobTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JobResponse.model_validate(job)

    @app.post(
        "/api/v1/executions/{execution_attempt_id}/reviews",
        response_model=ReviewRunResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["review"],
    )
    def create_review(
        execution_attempt_id: str,
        payload: ReviewCreateRequest,
        request: Request,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> ReviewRunResponse:
        settings: Settings = request.app.state.settings
        try:
            review = ReviewRunService(
                session,
                _execution_artifact_store(session, settings),
            ).create(
                execution_attempt_id=execution_attempt_id,
                action=UserAction.START_REVIEW,
                idempotency_key=idempotency_key or str(uuid4()),
                actor_type="local_user",
                actor_id=payload.actor_id,
                reviewer_kind=payload.reviewer,
            )
        except ReviewNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ReviewConflictError, TaskStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _review_response(
            review,
            stale=ReviewRunService(session).is_stale(review.id),
        )

    @app.get(
        "/api/v1/reviews/{review_id}",
        response_model=ReviewRunResponse,
        tags=["review"],
    )
    def review_detail(
        review_id: str,
        session: Session = Depends(get_session),
    ) -> ReviewRunResponse:
        try:
            reviews = ReviewRunService(session)
            review = reviews.get_verified(review_id)
            stale = reviews.is_stale(review.id)
        except ReviewNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ReviewConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _review_response(review, stale=stale)

    @app.post(
        "/api/v1/reviews/{review_id}/repair",
        response_model=ExecutionAttemptResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["review"],
    )
    def repair_review(
        review_id: str,
        payload: RepairCreateRequest,
        request: Request,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> ExecutionAttemptResponse:
        try:
            review = ReviewRunService(session).get_verified(review_id)
            attempt = ExecutionAttemptService(session).start_repair(
                previous_attempt_id=review.execution_attempt_id,
                action=UserAction.START_REPAIR,
                idempotency_key=idempotency_key or str(uuid4()),
                actor_type="local_user",
                actor_id=payload.actor_id,
            )
            current_stage = _schedule_execution_if_signed(
                request,
                session,
                attempt.id,
            )
        except (ReviewNotFoundError, ExecutionAttemptNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ReviewConflictError,
            ExecutionAttemptConflictError,
            ExecutionControlConflictError,
            TaskStateError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _execution_attempt_response(attempt, current_stage)

    @app.post(
        "/api/v1/reviews/{review_id}/publish-intents",
        response_model=PublishIntentResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["publish"],
    )
    def create_publish_intent(
        review_id: str,
        payload: PublishIntentCreateRequest,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> PublishIntentResponse:
        try:
            service = PublishIntentService(session, FakeGitHubPublisher())
            intent = service.create(
                review_run_id=review_id,
                action=UserAction.CREATE_PUBLISH_INTENT,
                idempotency_key=idempotency_key or str(uuid4()),
                actor_type="local_user",
                actor_id=payload.actor_id,
                title=payload.title,
                body=payload.body,
            )
            status_value = service.status(intent.id)
        except ReviewNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishIntentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishIntentStaleError as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except (PublishIntentConflictError, ReviewConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _publish_intent_response(intent, status=status_value)

    @app.get(
        "/api/v1/publish-intents/{intent_id}",
        response_model=PublishIntentResponse,
        tags=["publish"],
    )
    def publish_intent_detail(
        intent_id: str,
        session: Session = Depends(get_session),
    ) -> PublishIntentResponse:
        try:
            service = PublishIntentService(session)
            intent = service.get_verified(intent_id)
            status_value = service.status(intent.id)
        except PublishIntentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishIntentConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _publish_intent_response(intent, status=status_value)

    @app.post(
        "/api/v1/publish-intents/{intent_id}/confirm",
        response_model=PublishConfirmationResponse,
        tags=["publish"],
    )
    def confirm_publish_intent(
        intent_id: str,
        payload: PublishIntentConfirmRequest,
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> PublishConfirmationResponse:
        try:
            service = PublishIntentService(session, FakeGitHubPublisher())
            intent, draft, _confirmation = service.confirm(
                intent_id=intent_id,
                action=UserAction.PUBLISH_DRAFT_PR,
                actor_id=payload.actor_id,
                confirmation_nonce=payload.confirmation_nonce,
            )
        except PublishIntentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PublishIntentForbiddenError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except PublishIntentStaleError as exc:
            raise HTTPException(status_code=412, detail=str(exc)) from exc
        except PublishIntentConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return PublishConfirmationResponse(
            intent=_publish_intent_response(intent, status="confirmed"),
            draft_pull_request=DraftPullRequestResponse.model_validate(draft),
        )

    @app.post(
        "/api/v1/draft-pull-requests/{draft_id}/events",
        response_model=PullRequestEventResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["publish"],
    )
    def ingest_pull_request_event(
        draft_id: str,
        payload: PullRequestEventCreateRequest,
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> PullRequestEventResponse:
        try:
            event = PullRequestEventService(session).ingest(
                draft_pull_request_id=draft_id,
                action=UserAction.INGEST_PR_EVENT,
                remote_event_id=payload.remote_event_id,
                event_type=payload.event_type,
                payload=payload.payload,
                actor_type="local_user",
                actor_id=payload.actor_id,
            )
        except PullRequestEventNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PullRequestEventConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return PullRequestEventResponse.model_validate(event)

    @app.get(
        "/api/v1/draft-pull-requests/{draft_id}/events",
        response_model=list[PullRequestEventResponse],
        tags=["publish"],
    )
    def list_pull_request_events(
        draft_id: str,
        session: Session = Depends(get_session),
    ) -> list[PullRequestEventResponse]:
        try:
            events = PullRequestEventService(session).history(draft_id)
        except PullRequestEventConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return [
            PullRequestEventResponse.model_validate(event) for event in events
        ]

    @app.post(
        "/api/v1/tasks/{task_id}/lifecycle",
        response_model=ContributionTaskStateResponse,
        tags=["planning"],
    )
    def apply_task_lifecycle(
        task_id: str,
        payload: TaskLifecycleRequest,
        session: Session = Depends(get_session),
        _: None = Depends(require_mutation_access),
    ) -> ContributionTaskStateResponse:
        try:
            version = TaskLifecycleService(session).apply(
                task_id=task_id,
                action=payload.action,
                actor_id=payload.actor_id,
                reason_code=payload.reason_code,
            )
        except ContributionTaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LifecycleConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(version, TaskLifecycleMark):
            return ContributionTaskStateResponse(
                id=version.id,
                task_id=version.task_id,
                schema_version=version.schema_version,
                sequence=1,
                from_state=version.from_state,
                to_state=version.mark,
                reason_code=version.reason_code,
                task_record_hash=version.state_record_hash,
                previous_state_hash=version.state_record_hash,
                record_hash=version.record_hash,
                created_at=version.created_at,
            )
        return ContributionTaskStateResponse.model_validate(version)

    @app.get(
        "/api/v1/contributions/dashboard",
        response_model=ContributionDashboardResponse,
        tags=["dashboard"],
    )
    def contribution_dashboard(
        session: Session = Depends(get_session),
    ) -> ContributionDashboardResponse:
        return ContributionDashboardResponse.model_validate(
            ContributionDashboardService(session).snapshot()
        )

    return app


app = create_app()
