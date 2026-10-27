"""Shared bounded lease lifecycle for workbench domain workers."""

from __future__ import annotations
import asyncio
import errno
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
                    "coding_source_read": ("coding_read_invalid", "实施辅助源码读取未完成，冻结来源和已批准修改范围保留。"),
                    "execution_prepare": ("environment_preparation_failed", "沙箱环境准备或修改前基线未完成；代码尚未修改，请查看环境诊断。"),
                    "execution_dependencies": (
                        "environment_preparation_failed",
                        "依赖准备未通过：请检查锁定版本与哈希、wheel 可用性及包代理；未进入代码修复。",
                    ),
                    "execution_question": ("execution_question_failed", "执行问答未完成，当前执行不受影响，可以重新提问。"),
                    "baseline_diagnosis": ("baseline_diagnosis_failed", "修改前自动诊断未完成，原始失败证据保留，可在预算内重试。"),
                    "planning_import_metadata": ("planning_import_metadata_failed", "安全导入元数据读取未完成；完整源码仍不可用，不能猜测其内容。"),
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
                from app.sandbox_worker.dependencies import DependencyPreparationError
                if isinstance(exc, DependencyPreparationError):
                    error_code, error_message = exc.code, exc.safe_message
                elif isinstance(exc, ProviderRunError):
                    error_code = "planning_provider_failed" if job.kind == "planning_turn" else error_code
                    if "truncated" in exc.code:
                        error_message = "模型回复长度达到输出上限（Token 超限），请在模型配置中调大 max_tokens 或缩小方案细节。"
                    elif exc.safe_message:
                        error_message = f"模型服务请求失败：{exc.safe_message}，现有上下文已保留，可重试。"
                    else:
                        error_message = "模型服务未能完成请求，现有上下文已保留，可直接重试。"
                elif isinstance(exc, ValidationError) and job.kind == "planning_turn":
                    error_code = planning_validation_details(exc)["reason_code"]
                    error_message = MESSAGES[error_code]
                elif isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
                    if job.kind == "planning_archive":
                        error_message = "下载仓库归档失败，存储空间不足，请清理后重试。"
                elif isinstance(exc, WorkbenchError) and (exc.code == "planning_budget_exceeded" or job.kind in {"coding_source_read", "native_resource_download", "conversation_intent", "publication_requirements", "publication_document", "publication_prepare", "publication_write", "cla_prepare", "cla_write", "cla_check", "pr_update_read", "pr_update_document", "pr_update_write", "pr_update_check", "pr_ready_prepare", "pr_ready_write", "pr_followup"}):
                    error_code = exc.code
                # Never log exception text, model output, validation inputs or
                # arbitrary field names. Error types suffice to diagnose schemas.
                validation_types = (
                    planning_validation_details(exc)["validation_types"]
                    if isinstance(exc, ValidationError) else []
                )
                logger.warning(
                    "Workbench job failed: kind=%s job=%s code=%s exc_code=%s exception=%s validation_types=%s",
                    job.kind, job.id, error_code, getattr(exc, "code", None), type(exc).__name__, validation_types,
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
