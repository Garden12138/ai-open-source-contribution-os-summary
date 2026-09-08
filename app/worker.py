from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.archives import RepositoryArchiveJobWorker, RepositoryArchiveStore
from app.changesets import ChangeSetStore
from app.coding import CodingContextJobWorker, NvidiaCodingJobWorker
from app.config import Settings
from app.database import Database
from app.jobs import JobService, JobState, JobTransitionError
from app.models import Job, ScanRun
from app.product_experience import ProductExperienceService
from app.review_provider import NvidiaReviewJobWorker
from app.execution_worker import ExecutionStageWorker
from app.providers import (
    MINIMAX_M3_PARAMETERS,
    NVIDIA_NIM_ADAPTER_VERSION,
    NVIDIA_NIM_MODEL_VERSION,
    NVIDIA_NIM_PROVIDER,
    NvidiaNimGatewayRunner,
    NvidiaNimParameters,
    ProviderAnalysisJobWorker,
    ProviderIdentity,
    resolve_analysis_runtime,
    resolve_job_spec_signer,
    resolve_model_gateway_broker,
)
from app.sandbox_worker.runtime import resolve_stage_runtimes
from app.service import DiscoveryService, GitHubReader


GitHubClientFactory = Callable[[], AbstractAsyncContextManager[GitHubReader]]
DISCOVERY_JOB_KIND = "discovery_scan"


class DiscoveryJobWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        github_client_factory: GitHubClientFactory,
        *,
        worker_id: str,
        heartbeat_interval_seconds: float = 15,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        self.database = database
        self.settings = settings
        self.github_client_factory = github_client_factory
        self.worker_id = worker_id
        self.heartbeat_interval_seconds = max(0.1, heartbeat_interval_seconds)

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        started_at = self._aware(now)
        with self.database.session() as session:
            service = JobService(session)
            leased = service.lease_next(
                worker_id=self.worker_id,
                kinds=(DISCOVERY_JOB_KIND,),
                lease_seconds=60,
                now=started_at,
            )
            if leased is None:
                return None
            running = service.start(
                leased.id,
                worker_id=self.worker_id,
                now=started_at,
            )
            running = service.heartbeat(
                running.id,
                worker_id=self.worker_id,
                lease_seconds=running.timeout_seconds + 30,
                now=started_at,
            )
            job_id = running.id
            timeout_seconds = running.timeout_seconds
            payload = dict(running.payload)

        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                job_id,
                heartbeat_stop,
                lease_seconds=timeout_seconds + 30,
            )
        )
        try:
            queries, top_n = self._scan_parameters(payload)
            async with asyncio.timeout(timeout_seconds):
                with self.database.session() as session:
                    async with self.github_client_factory() as github:
                        scan_run = await DiscoveryService(
                            session,
                            github,
                            self.settings,
                        ).scan(
                            queries,
                            top_n=top_n,
                            now=started_at,
                        )
            with self.database.session() as session:
                service = JobService(session)
                current = service.get(job_id)
                if current.cancel_requested_at is not None:
                    return service.cancel(
                        job_id,
                        worker_id=self.worker_id,
                        now=self._aware(None),
                    )
                return service.succeed(
                    job_id,
                    worker_id=self.worker_id,
                    scan_run_id=scan_run.id,
                    result_data={
                        "scan_run_id": scan_run.id,
                        "candidate_count": scan_run.candidate_count,
                        "eligible_count": scan_run.eligible_count,
                        "selected_count": scan_run.selected_count,
                        "repository_count": scan_run.repository_count,
                    },
                    now=self._aware(None),
                )
        except TimeoutError:
            with self.database.session() as session:
                return JobService(session).time_out(
                    job_id,
                    worker_id=self.worker_id,
                    now=self._aware(None),
                )
        except Exception as exc:
            with self.database.session() as session:
                service = JobService(session)
                current = service.get(job_id)
                if current.cancel_requested_at is not None:
                    return service.cancel(
                        job_id,
                        worker_id=self.worker_id,
                        now=self._aware(None),
                    )
                return service.fail(
                    job_id,
                    worker_id=self.worker_id,
                    error_code=self._error_code(exc),
                    error_message="Discovery job failed; inspect the associated scan",
                    now=self._aware(None),
                )
        finally:
            heartbeat_stop.set()
            with suppress(JobTransitionError):
                await heartbeat_task

    async def run_forever(self, *, poll_interval_seconds: float = 2) -> None:
        interval = max(0.1, poll_interval_seconds)
        while True:
            job = await self.run_once()
            if job is None:
                await asyncio.sleep(interval)

    async def _heartbeat_loop(
        self,
        job_id: str,
        stop: asyncio.Event,
        *,
        lease_seconds: int,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.heartbeat_interval_seconds,
                )
                return
            except TimeoutError:
                with self.database.session() as session:
                    JobService(session).heartbeat(
                        job_id,
                        worker_id=self.worker_id,
                        lease_seconds=lease_seconds,
                    )

    @staticmethod
    def _scan_parameters(
        payload: dict[str, Any],
    ) -> tuple[list[str] | None, int | None]:
        raw_queries = payload.get("queries")
        if raw_queries is None:
            queries = None
        elif isinstance(raw_queries, list) and all(
            isinstance(query, str) for query in raw_queries
        ):
            queries = raw_queries
        else:
            raise ValueError("Discovery job queries are invalid")
        raw_top_n = payload.get("top_n")
        if raw_top_n is not None and not isinstance(raw_top_n, int):
            raise ValueError("Discovery job top_n is invalid")
        return queries, raw_top_n

    @staticmethod
    def _error_code(error: Exception) -> str:
        name = type(error).__name__.lower()
        return f"discovery_{name}"[:80]

    @staticmethod
    def _aware(value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


class ContribOSWorker:
    """Lease discovery, then analysis, then sandbox-stage jobs in one process."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        github_client_factory: GitHubClientFactory,
        *,
        worker_id: str,
        heartbeat_interval_seconds: float = 15,
    ) -> None:
        self.database = database
        self.settings = settings
        self.discovery = DiscoveryJobWorker(
            database,
            settings,
            github_client_factory,
            worker_id=worker_id,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        from app.planner import PlanningArchiveWorker
        from app.contribution_workflow import ContributionStartWorker
        self.planning_archive = PlanningArchiveWorker(database, settings, github_client_factory, worker_id=worker_id)
        self.contribution_start = ContributionStartWorker(database, settings, github_client_factory, worker_id=worker_id)
        self.archives = RepositoryArchiveJobWorker(
            database,
            settings,
            github_client_factory,
            worker_id=worker_id,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        provider, _budget = resolve_analysis_runtime(
            settings,
            execution_enabled=settings.analysis_provider == "fake",
        )
        self.analysis = (
            ProviderAnalysisJobWorker(
                database,
                provider,
                worker_id=worker_id,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
            )
            if provider is not None and settings.analysis_provider != "nvidia_nim"
            else None
        )
        signer = resolve_job_spec_signer(settings)
        archive_store = RepositoryArchiveStore(settings.artifact_root)
        stage_runtimes = (
            resolve_stage_runtimes(settings)
            if settings.sandbox_stage_runtime != "docker"
            else None
        )
        self.execution = (
            ExecutionStageWorker(
                database,
                signer,
                worker_id=worker_id,
                archive_store=archive_store,
                change_set_store=ChangeSetStore(settings.artifact_root),
                artifact_root=settings.artifact_root,
                secrets=(settings.github_token, settings.local_access_token),
                explore_runtime=(
                    None if stage_runtimes is None else stage_runtimes.explore
                ),
                implement_runtime=(
                    None if stage_runtimes is None else stage_runtimes.implement
                ),
                verify_runtime=(
                    None if stage_runtimes is None else stage_runtimes.verify
                ),
            )
            if signer is not None and settings.sandbox_stage_runtime != "docker"
            else None
        )

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        self._sync_product_experience(now=now)
        from app.contribution_workflow import advance_workflows
        advance_workflows(self.database, self.settings)
        for worker in (self.planning_archive, self.contribution_start):
            completed = await worker.run_once(now=now)
            if completed is not None:
                return completed
        completed = await self.discovery.run_once(now=now)
        if completed is not None:
            return completed
        completed = await self.archives.run_once(now=now)
        if completed is not None:
            return completed
        if self.analysis is not None:
            completed = await self.analysis.run_once(now=now)
            if completed is not None:
                return completed
        if self.execution is not None:
            completed = await self.execution.run_once(now=now)
            if completed is not None:
                return completed
        return None

    def _sync_product_experience(self, *, now: datetime | None) -> None:
        current = now or datetime.now(timezone.utc)
        aware = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
        with self.database.session() as session:
            product = ProductExperienceService(
                session,
                secrets=(
                    self.settings.github_token,
                    self.settings.local_access_token,
                ),
            )
            product.sync_due_reminders(now=aware)
            preference = product.current_preference()
            if preference is None or not preference.auto_scan_enabled:
                return
            local = aware.astimezone(ZoneInfo(self.settings.timezone))
            hour, minute = (
                int(item) for item in preference.auto_scan_local_time.split(":", 1)
            )
            if (local.hour, local.minute) < (hour, minute):
                return
            completed_today = session.scalar(
                select(ScanRun.id)
                .where(
                    ScanRun.status == "completed",
                    ScanRun.selection_date == local.date(),
                )
                .limit(1)
            )
            if completed_today is not None:
                return
            JobService(session).enqueue(
                kind=DISCOVERY_JOB_KIND,
                idempotency_key=f"scheduled-scan:{local.date().isoformat()}",
                payload={"queries": None, "top_n": None},
                now=aware,
            )

    async def run_forever(self, *, poll_interval_seconds: float = 2) -> None:
        interval = max(0.1, poll_interval_seconds)
        while True:
            job = await self.run_once()
            if job is None:
                await asyncio.sleep(interval)


class ContribOSProviderWorker:
    """Lease model-provider Jobs without GitHub or Docker capabilities."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        worker_id: str,
        heartbeat_interval_seconds: float = 15,
    ) -> None:
        self.analysis = None
        if settings.analysis_provider == "nvidia_nim":
            provider, _budget = resolve_analysis_runtime(
                settings,
                execution_enabled=True,
            )
            self.analysis = (
                None
                if provider is None
                else ProviderAnalysisJobWorker(
                    database,
                    provider,
                    worker_id=worker_id,
                    heartbeat_interval_seconds=heartbeat_interval_seconds,
                )
            )
        self.planning = None
        self.coding = None
        if settings.implementation_provider == "nvidia_nim":
            broker = resolve_model_gateway_broker(settings)
            identity = ProviderIdentity(
                provider=NVIDIA_NIM_PROVIDER,
                adapter_version=NVIDIA_NIM_ADAPTER_VERSION,
                model=settings.implementation_model,
                model_version=NVIDIA_NIM_MODEL_VERSION,
            )
            from app.planner import PlanningTurnWorker
            self.planning = PlanningTurnWorker(database, settings,
                NvidiaNimGatewayRunner(broker=broker, parameters=NvidiaNimParameters(
                    temperature=0.2, reasoning_effort="high", max_tokens=16_384)),
                identity, worker_id=worker_id)
            self.coding = NvidiaCodingJobWorker(
                database,
                artifact_root=settings.artifact_root,
                runner=NvidiaNimGatewayRunner(
                    broker=broker,
                    parameters=NvidiaNimParameters(
                        temperature=0.2,
                        reasoning_effort="high",
                        max_tokens=16_384,
                    ),
                ),
                identity=identity,
                worker_id=worker_id,
            )
        self.review = None
        if settings.review_provider == "nvidia_nim":
            review_identity = ProviderIdentity(
                provider=NVIDIA_NIM_PROVIDER,
                adapter_version=NVIDIA_NIM_ADAPTER_VERSION,
                model=settings.review_model,
                model_version=NVIDIA_NIM_MODEL_VERSION,
            )
            self.review = NvidiaReviewJobWorker(
                database,
                artifact_root=settings.artifact_root,
                runner=NvidiaNimGatewayRunner(
                    broker=resolve_model_gateway_broker(settings),
                    parameters=MINIMAX_M3_PARAMETERS,
                ),
                identity=review_identity,
                worker_id=worker_id,
            )
        if self.analysis is None and self.coding is None and self.review is None:
            raise ValueError("A real NVIDIA provider is not configured")

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        if self.planning is not None:
            completed = await self.planning.run_once(now=now)
            if completed is not None:
                return completed
        if self.analysis is not None:
            completed = await self.analysis.run_once(now=now)
            if completed is not None:
                return completed
        if self.coding is not None:
            completed = await self.coding.run_once(now=now)
            if completed is not None:
                return completed
        if self.review is not None:
            return await self.review.run_once(now=now)
        return None

    async def run_forever(self, *, poll_interval_seconds: float = 2) -> None:
        interval = max(0.1, poll_interval_seconds)
        while True:
            job = await self.run_once()
            if job is None:
                await asyncio.sleep(interval)


class ContribOSSandboxWorker:
    """Lease Docker-only Jobs without GitHub or model credentials."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        worker_id: str,
    ) -> None:
        if settings.sandbox_stage_runtime != "docker":
            raise ValueError("sandbox-worker requires SANDBOX_STAGE_RUNTIME=docker")
        signer = resolve_job_spec_signer(settings)
        if signer is None:
            raise ValueError("sandbox-worker requires a JobSpec signing key")
        runtimes = resolve_stage_runtimes(settings)
        if runtimes is None:
            raise ValueError("Docker stage runtimes are unavailable")
        from app.sandbox_worker.coding_context import DockerCodingContextRuntime

        docker_environment = dict(runtimes.explore.docker_environment)
        from app.planner import PlanningContextWorker
        from app.sandbox_worker.planning_context import DockerPlanningContextRuntime
        self.planning_context = PlanningContextWorker(database, settings,
            DockerPlanningContextRuntime(docker_environment=docker_environment), worker_id=worker_id)
        self.context = CodingContextJobWorker(
            database,
            artifact_root=settings.artifact_root,
            runtime=DockerCodingContextRuntime(
                docker_environment=docker_environment
            ),
            worker_id=worker_id,
        )
        self.execution = ExecutionStageWorker(
            database,
            signer,
            worker_id=worker_id,
            archive_store=RepositoryArchiveStore(settings.artifact_root),
            change_set_store=ChangeSetStore(settings.artifact_root),
            artifact_root=settings.artifact_root,
            secrets=(),
            explore_runtime=runtimes.explore,
            implement_runtime=runtimes.implement,
            verify_runtime=runtimes.verify,
        )

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        completed = await self.planning_context.run_once(now=now)
        if completed is not None:
            return completed
        completed = await self.context.run_once(now=now)
        if completed is not None:
            return completed
        return await self.execution.run_once(now=now)

    async def run_forever(self, *, poll_interval_seconds: float = 2) -> None:
        interval = max(0.1, poll_interval_seconds)
        while True:
            job = await self.run_once()
            if job is None:
                await asyncio.sleep(interval)
