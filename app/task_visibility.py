"""Archive and remove local tasks without erasing their provenance."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.audit import AuditService
from app.jobs import TERMINAL_STATES
from app.models import (
    ContributionTask, ExecutionAttempt, ExecutionStageRun, Job, PublishIntent,
    TaskVisibilityVersion, WorkbenchEvent,
)
from app.planning import ContributionTaskConflictError, ContributionTaskNotFoundError
from app.provenance import content_hash

Visibility = Literal["active", "archived", "deleted"]


def visibility_expression(task_id: object = ContributionTask.id) -> ColumnElement[str]:
    return func.coalesce(
        select(TaskVisibilityVersion.state)
        .where(TaskVisibilityVersion.task_id == task_id)
        .order_by(TaskVisibilityVersion.sequence.desc()).limit(1)
        .correlate_except(TaskVisibilityVersion).scalar_subquery(),
        "active",
    )


def current_visibility(session: Session, task_id: str) -> TaskVisibilityVersion | None:
    version = session.scalar(select(TaskVisibilityVersion)
        .where(TaskVisibilityVersion.task_id == task_id)
        .order_by(TaskVisibilityVersion.sequence.desc()).limit(1))
    if version:
        created_at = version.created_at.replace(tzinfo=timezone.utc) if version.created_at.tzinfo is None else version.created_at
        payload = {"task_id": version.task_id, "task_record_hash": version.task_record_hash,
                   "sequence": version.sequence, "state": version.state,
                   "previous_hash": version.previous_hash, "created_at": created_at}
        if content_hash(payload) != version.record_hash:
            raise ContributionTaskConflictError("任务归档记录校验失败")
    return version


def require_active_task(session: Session, task_id: str) -> None:
    current = current_visibility(session, task_id)
    if current and current.state == "deleted":
        raise ContributionTaskNotFoundError("任务已删除")
    if current and current.state == "archived":
        raise ContributionTaskConflictError("任务已归档，请先在归档列表恢复")


def task_job_condition(task_id: str) -> ColumnElement[bool]:
    attempts = select(ExecutionAttempt.id).where(ExecutionAttempt.task_id == task_id)
    return or_(
        Job.payload["task_id"].as_string() == task_id,
        Job.payload["execution_attempt_id"].as_string().in_(attempts),
        Job.payload["publish_intent_id"].as_string().in_(
            select(PublishIntent.id).where(PublishIntent.task_id == task_id)),
        Job.id.in_(select(WorkbenchEvent.job_id).where(WorkbenchEvent.task_id == task_id)),
        Job.id.in_(select(ExecutionStageRun.job_id).where(
            ExecutionStageRun.execution_attempt_id.in_(attempts))),
    )


def require_active_job_task(session: Session, job: Job) -> None:
    inactive_ids = session.scalars(select(ContributionTask.id)
        .where(visibility_expression() != "active"))
    for task_id in inactive_ids:
        if session.scalar(select(Job.id).where(Job.id == job.id, task_job_condition(task_id))):
            require_active_task(session, task_id)


def require_active_payload_task(session: Session, payload: dict[str, Any]) -> None:
    task_id = payload.get("task_id")
    if isinstance(task_id, str):
        require_active_task(session, task_id)
    for key, model in (("execution_attempt_id", ExecutionAttempt), ("publish_intent_id", PublishIntent)):
        identifier = payload.get(key)
        if isinstance(identifier, str):
            owner = session.get(model, identifier)
            if owner:
                require_active_task(session, owner.task_id)


class TaskVisibilityService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def change(self, task_id: str, *, target: Visibility, expected_sequence: int, commit: bool = True) -> TaskVisibilityVersion:
        # Serialize with job creation/coordinator transactions, including legacy
        # SQLite transaction mode where a SELECT alone does not begin a transaction.
        connection = self.session.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        task = self.session.get(ContributionTask, task_id)
        if task is None:
            raise ContributionTaskNotFoundError("贡献任务不存在")
        previous = current_visibility(self.session, task_id)
        sequence = previous.sequence if previous else 0
        state = previous.state if previous else "active"
        # A lost response can be retried against the same predecessor, but a
        # request from before a restore must never archive/delete newer state.
        if previous and state == target and sequence == expected_sequence + 1:
            return previous
        if state == "deleted":
            raise ContributionTaskNotFoundError("任务已删除")
        if sequence != expected_sequence:
            raise ContributionTaskConflictError("任务状态已变化，请刷新后重试")
        if (state, target) not in {("active", "archived"), ("archived", "active"), ("archived", "deleted")}:
            raise ContributionTaskConflictError("请先归档任务，再从归档列表删除")
        if target != "active":
            from app.task_states import ContributionTaskStateService

            task_state = ContributionTaskStateService(self.session).current(task_id)
            if task_state.to_state in {"plan_approved", "executing", "reviewing"}:
                raise ContributionTaskConflictError("任务仍在执行流程中，请先停止并返回方案讨论后归档")
            active = self.session.scalar(select(Job.id).where(
                task_job_condition(task_id), Job.state.not_in(TERMINAL_STATES)).limit(1))
            if active:
                raise ContributionTaskConflictError("任务正在运行，请先停止并等待结束后归档")
            events = list(self.session.scalars(select(WorkbenchEvent).where(
                WorkbenchEvent.task_id == task_id).order_by(WorkbenchEvent.sequence)))
            confirmed = next((e for e in reversed(events) if e.kind == "publication_confirmed"), None)
            if confirmed and not any(e.kind == "publication_completed" and e.sequence > confirmed.sequence for e in events):
                raise ContributionTaskConflictError("发布结果尚未核对完成，请先核对后归档")
        created_at = datetime.now(timezone.utc)
        payload = {"task_id": task_id, "task_record_hash": task.record_hash,
                   "sequence": sequence + 1, "state": target,
                   "previous_hash": previous.record_hash if previous else None,
                   "created_at": created_at}
        version = TaskVisibilityVersion(id=str(uuid4()), **payload, record_hash=content_hash(payload))
        self.session.add(version)
        AuditService(self.session).prepare(
            event_type={"active": "task_restored", "archived": "task_archived", "deleted": "task_deleted"}[target],
            actor_type="user", actor_id="local-user", correlation_id=task_id,
            payload={**payload, "created_at": created_at.isoformat(), "record_hash": version.record_hash},
        )
        try:
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContributionTaskConflictError("任务状态或运行状态已变化，请刷新后重试") from exc
        return version
