"""Non-secret account state over the existing append-only audit journal."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import AuditService
from app.jobs import TERMINAL_STATES
from app.models import AuditEvent, Job
from app.workbench import WorkbenchError

ACCOUNT_EVENT = "publisher.account.updated"
PROBE = "publisher_account_probe"


def account_record(session: Session) -> AuditEvent | None:
    if not AuditService(session).verify().valid:
        raise WorkbenchError("发布账号审计链校验失败")
    return session.scalar(select(AuditEvent).where(AuditEvent.event_type == ACCOUNT_EVENT)
                          .order_by(AuditEvent.sequence.desc()).limit(1))


def account_status(session: Session) -> dict[str, Any]:
    row = account_record(session)
    payload = row.payload if row else {}
    job = session.get(Job, payload.get("job_id")) if payload.get("job_id") else None
    state = payload.get("state", "disconnected")
    if state == "pending" and job and job.state in TERMINAL_STATES and job.state != "succeeded":
        state = "verification_failed"
    return {"account_hash": row.event_hash if row else None,
            "state": state, "connected": state == "connected",
            "login": payload.get("login"), "scopes": payload.get("scopes", []),
            "checked_at": payload.get("checked_at"),
            "job_id": job.id if job else None, "job_state": job.state if job else None,
            "error": job.error_message if job and state == "verification_failed" else None}


def require_account(session: Session, expected_hash: str | None = None) -> AuditEvent:
    row = account_record(session)
    if row is None or row.payload["state"] != "connected" or (
        expected_hash is not None and row.event_hash != expected_hash
    ):
        raise WorkbenchError("发布账号尚未连接或已变化，请在设置中连接并重新准备发布预览")
    return row


def assert_account_changeable(session: Session) -> None:
    jobs = list(session.scalars(select(Job).where(Job.kind.in_(
        ("publication_requirements", "publication_prepare", "publication_write", "cla_prepare", "cla_write", "cla_check", "pr_update_read", "pr_update_document", "pr_update_write", "pr_update_check", "pr_ready_prepare", "pr_ready_write", "pr_followup", PROBE)))))
    if any(j.state not in TERMINAL_STATES or (
        j.kind == "publication_write" and j.state != "succeeded") for j in jobs):
        raise WorkbenchError("账号检测或发布任务尚未结束；存在未确认的发布结果时，请先核对原发布任务")
    from app.models import WorkbenchEvent
    started = list(session.scalars(select(WorkbenchEvent).where(WorkbenchEvent.kind == 'cla_write_started')))
    resolved = {e.payload['authorization_hash'] for e in session.scalars(
        select(WorkbenchEvent).where(WorkbenchEvent.kind == 'cla_submitted'))}
    if any(e.payload['authorization_hash'] not in resolved for e in started):
        raise WorkbenchError('CLA 签署结果尚未确认，请先核对原任务再更换账号')

    updates = list(session.scalars(select(WorkbenchEvent).where(WorkbenchEvent.kind.in_(
        ('pr_update_write_started', 'pr_update_completed', 'pr_update_rejected')))))
    resolved_updates = {e.payload['authorization_hash'] for e in updates if e.kind != 'pr_update_write_started'}
    if any(e.kind == 'pr_update_write_started' and e.payload['authorization_hash'] not in resolved_updates for e in updates):
        raise WorkbenchError('PR 文案更新结果尚未明确，请先核对原任务再更换账号')

    readiness = list(session.scalars(select(WorkbenchEvent).where(WorkbenchEvent.kind.in_(
        ('pr_ready_write_started', 'pr_ready_completed', 'pr_ready_rejected')))))
    resolved_ready = {e.payload['authorization_hash'] for e in readiness if e.kind != 'pr_ready_write_started'}
    if any(e.kind == 'pr_ready_write_started' and e.payload['authorization_hash'] not in resolved_ready for e in readiness):
        raise WorkbenchError('Ready for review 结果尚未明确，请先跟进原任务再更换账号')


def append_account(session: Session, payload: dict[str, Any], *, correlation_id: str,
                   actor: str = "local-user") -> AuditEvent:
    row = AuditService(session).prepare(event_type=ACCOUNT_EVENT,
        actor_type="local_user" if actor == "local-user" else "publisher",
        actor_id=actor, correlation_id=correlation_id, payload=payload)
    session.flush()
    return row
