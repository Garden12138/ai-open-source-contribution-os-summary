from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.database import Database
from app.github import GitHubAPIError, GitHubClient
from app.models import DailyPick, Opportunity, ScanRun
from app.schemas import (
    DailyLeaderboardResponse,
    DailyPickItem,
    HealthResponse,
    MetaResponse,
    OpportunityDetail,
    ScanRequest,
    ScanResponse,
)
from app.service import DiscoveryService


STATIC_DIR = Path(__file__).parent / "static"


def get_session(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.database
    with database.session() as session:
        yield session


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
    app.state.scan_lock = asyncio.Lock()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
        latest_scan = session.scalar(
            select(ScanRun)
            .where(ScanRun.status == "completed")
            .order_by(desc(ScanRun.completed_at))
            .limit(1)
        )
        return DailyLeaderboardResponse(
            selection_date=selection_date,
            generated_at=latest_scan.completed_at if latest_scan else None,
            total_candidates=latest_scan.candidate_count if latest_scan else 0,
            total_eligible=latest_scan.eligible_count if latest_scan else 0,
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
        response_model=ScanResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["discovery"],
    )
    async def start_scan(
        payload: ScanRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> ScanResponse:
        lock: asyncio.Lock = request.app.state.scan_lock
        if lock.locked():
            raise HTTPException(
                status_code=409, detail="A discovery scan is already running"
            )
        current: Settings = request.app.state.settings
        client_factory = request.app.state.github_client_factory
        try:
            async with lock:
                async with client_factory() as github:
                    run = await DiscoveryService(session, github, current).scan(
                        payload.queries,
                        top_n=payload.top_n,
                        now=datetime.now(timezone.utc),
                    )
        except GitHubAPIError as exc:
            response_status = 429 if exc.status_code in {403, 429} else 502
            raise HTTPException(status_code=response_status, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ScanResponse.model_validate(run)

    return app


app = create_app()
