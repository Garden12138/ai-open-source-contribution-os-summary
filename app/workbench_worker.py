"""Shared bounded lease lifecycle for workbench domain workers."""

from __future__ import annotations
import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from pydantic import ValidationError
from app.database import Database
from app.jobs import JobService, TERMINAL_STATES
from app.models import Job
from app.provenance import content_hash
from app.providers.contracts import ProviderRunError
from app.workbench import WorkbenchError
from app.planning_diagnostics import planning_validation_details, MESSAGES

logger = logging.getLogger(__name__)


class WorkbenchJobWorker:
    kinds: tuple[str, ...] = ()

    def __init__(self, database: Database, *, worker_id: str) -> None:
        self.database = database
        self.worker_id = worker_id

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        with self.database.session() as session:
            jobs = JobService(session)
            job = jobs.lease_next(worker_id=self.worker_id, kinds=self.kinds, now=now)
            if job is None:
                return None
            job = jobs.start(job.id, worker_id=self.worker_id, now=now)
        work = asyncio.create_task(self.execute(job))
        heartbeat = asyncio.create_task(self._heartbeat(job.id, work))
        try:
            async with asyncio.timeout(job.timeout_seconds):
                result = await work
            with self.database.session() as session:
                current = JobService(session).get(job.id)
                if current.state in TERMINAL_STATES:
                    return current
                return JobService(session).succeed(
                    job.id, worker_id=self.worker_id, result_data=result
                )
        except (Exception, asyncio.CancelledError) as exc:
            with self.database.session() as session:
                jobs = JobService(session)
                current = jobs.get(job.id)
                if current.state in TERMINAL_STATES:
                    return current
                if current.cancel_requested_at:
                    return jobs.cancel(job.id, worker_id=self.worker_id)
                if isinstance(exc, TimeoutError):
                    return jobs.time_out(job.id, worker_id=self.worker_id)
                if job.kind == "model_connection_test":
                    return jobs.fail(job.id, worker_id=self.worker_id,
                        error_code=exc.code if isinstance(exc, ProviderRunError) else "model_connection_failed",
                        error_message=exc.safe_message if isinstance(exc, ProviderRunError) else "连接测试失败，请检查配置或手动填写模型 ID")
                error_code, error_message = {
                    "planning_archive": (
                        "planning_archive_failed",
                        "下载仓库归档失败，请检查只读 GitHub 访问后重新读取仓库。",
                    ),
                    "planning_context": (
                        "planning_context_failed",
                        "读取仓库代码失败，归档格式、路径或隔离服务校验未通过，请重新读取仓库后重试。",
                    ),
                    "planning_turn": (
                        "planning_turn_failed",
                        "规划模型调用或回复校验失败，请重试；持续失败时检查模型服务。",
                    ),
                }.get(job.kind, (
                    "workbench_failed",
                    "任务执行或证据校验失败，请检查配置后重试",
                ))
                if isinstance(exc, ProviderRunError):
                    error_code = "planning_provider_failed" if job.kind == "planning_turn" else error_code
                    error_message = "模型服务未能完成请求，现有上下文已保留，可直接重试。"
                elif isinstance(exc, ValidationError) and job.kind == "planning_turn":
                    error_code = planning_validation_details(exc)["reason_code"]
                    error_message = MESSAGES[error_code]
                # Never log exception text, model output, validation inputs or
                # arbitrary field names. Error types suffice to diagnose schemas.
                validation_types = (
                    planning_validation_details(exc)["validation_types"]
                    if isinstance(exc, ValidationError) else []
                )
                logger.warning(
                    "Workbench job failed: kind=%s job=%s code=%s exception=%s validation_types=%s",
                    job.kind, job.id, error_code, type(exc).__name__, validation_types,
                )
                return jobs.fail(
                    job.id,
                    worker_id=self.worker_id,
                    error_code=error_code,
                    error_message=(
                        str(exc)
                        if isinstance(exc, WorkbenchError)
                        else error_message
                    ),
                )
        finally:
            work.cancel()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, job_id: str, work: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(10)
            try:
                with self.database.session() as session:
                    jobs = JobService(session)
                    job = jobs.get(job_id)
                    if job.cancel_requested_at or job.state in TERMINAL_STATES:
                        work.cancel()
                        return
                    jobs.heartbeat(job_id, worker_id=self.worker_id)
            except Exception:
                work.cancel()
                return

    def check(self, session, job: Job) -> None:
        current = JobService(session).get(job.id)
        if (
            current.cancel_requested_at
            or current.state != "running"
            or current.lease_owner != self.worker_id
            or current.payload_hash != content_hash(current.payload)
        ):
            raise WorkbenchError("任务已停止或输入已变化")

    def finish(self, session, job: Job, result: dict) -> dict:
        JobService(session).succeed(
            job.id, worker_id=self.worker_id, result_data=result, commit=False
        )
        session.commit()
        return result

    async def execute(self, job: Job) -> dict:
        raise NotImplementedError
