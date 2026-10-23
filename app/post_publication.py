"""Publication hands off to read-only readiness, never to an external write."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.jobs import JobService, TERMINAL_STATES
from app.models import Job
from app.pr_lifecycle import PREPARE, FOLLOWUP, request_lifecycle, unresolved_ready
from app.pr_updates import unresolved_update
from app.publisher_accounts import require_account
from app.workbench import Workbench, WorkbenchError

VERSION = "post-publication-workflow-v2"
RETRY_DELAY_SECONDS = 5


def _fresh_preview(wb: Workbench, task_id: str, target: dict, account_hash: str) -> Any:
    preview = wb.latest(task_id, "pr_ready_prepared")
    if preview is None:
        return None
    p = preview.payload
    try:
        expired = datetime.now(timezone.utc) >= datetime.fromisoformat(p["expires_at"])
    except (KeyError, TypeError, ValueError):
        raise WorkbenchError("审核预览有效期记录无效，请重新核对。", code="post_publication_preview_invalid") from None
    if (p["target"] != target or p["publisher_account_hash"] != account_hash or expired
            or any(e.kind == "pr_ready_confirmed" and e.payload["preview_hash"] == preview.record_hash
                   for e in wb.history(task_id))):
        return None
    observation = wb.latest(task_id, "pr_followup_observed")
    if observation and observation.sequence > preview.sequence:
        pr = observation.payload["pr"]
        if any(pr[k] != p["before"][k] for k in ("head_sha", "base_sha", "title", "body", "state", "draft")):
            return None
    return preview


def advance_post_publication(wb: Workbench, task_id: str, settings: Any) -> None:
    """Called only by the serialized ordinary conversation coordinator."""
    from app.conversation import paused, pending_messages
    from app.cla import published_target
    from app.task_visibility import require_active_task
    require_active_task(wb.session, task_id)
    publication = wb.latest(task_id, "publication_completed")
    if publication is None or paused(wb, task_id) or pending_messages(wb, task_id):
        return
    events = wb.history(task_id)
    stop = wb.latest(task_id, "stop_requested")
    resumed = wb.latest(task_id, "conversation_resumed")
    if stop and stop.sequence > publication.sequence and (resumed is None or resumed.sequence < stop.sequence):
        return
    source_hash = publication.record_hash
    finished = [e for e in events if e.kind == "post_publication_finished" and e.payload["publication_hash"] == source_hash]
    if finished:
        previous = finished[-1]
        # v1 incorrectly read the coordinator's deliberately disabled publisher
        # mode as the isolated Publisher's capability. Recover only that exact
        # pre-queue failure, once, without rewriting its receipt or any consent.
        if (previous.payload.get("version") != "post-publication-workflow-v1" or previous.job_id is not None
                or previous.payload["status"] != "blocked"
                or previous.payload["message"] != "尚未启用独立 Publisher，请在设置中核对。"):
            return
        wb.append(task_id, "post_publication_recovered", {"version": VERSION, "publication_hash": source_hash,
            "finished_hash": previous.record_hash, "reason_code": "coordinator_publisher_capability_fixed",
            "message": "自动接续的配置判断已修复，正在重新核对 PR 状态。"},
            key="post-publish-recovered:" + previous.id, commit=False)

    def finish(status: str, *, job: Job | None = None, preview: Any = None, message: str = "") -> None:
        wb.append(task_id, "post_publication_finished", {"version": VERSION, "publication_hash": source_hash,
            "status": status, "message": message, "preview_hash": preview.record_hash if preview else None},
            key="post-publish-finished:v2:" + publication.id, job_id=job.id if job else None, commit=False)

    jobs = wb.jobs(task_id)
    step = next((e for e in reversed(events) if e.kind == "post_publication_step"
                 and e.payload["publication_hash"] == source_hash), None)
    # Backfilled conversations may already have progressed beyond Draft. Their
    # recorded remote lifecycle takes precedence over an old publication milestone.
    observed = wb.latest(task_id, "pr_followup_observed")
    if step is None and observed and (not observed.payload["pr"]["draft"]
                                      or observed.payload["pr"]["state"] != "open"):
        return
    from app.pr_freshness import saved_status_requested
    message = wb.latest(task_id, "conversation_received")
    if message and message.sequence > publication.sequence and saved_status_requested(message.payload["text"]):
        return  # An explicit request to use saved evidence overrides new reads.
    job = wb.session.get(Job, step.job_id) if step else None
    if job and job.state not in TERMINAL_STATES:
        return
    if any(j.state not in TERMINAL_STATES for j in jobs):
        return
    try:
        target = published_target(wb, task_id)
        account = require_account(wb.session)
        preview = _fresh_preview(wb, task_id, target, account.event_hash)
        if step:
            if step.payload["account_hash"] != account.event_hash:
                raise WorkbenchError("发布账号已变化，请在原对话重新核对。", code="post_publication_account_stale")
            if job is None:
                raise WorkbenchError("自动跟进任务记录缺失。", code="post_publication_job_missing")
            if job.state != "succeeded":
                permanent = {"pr_ready_target_stale", "pr_lifecycle_target_stale", "pr_ready_outcome_unknown",
                             "pr_lifecycle_remote_invalid", "pr_ready_document_invalid"}
                if (job.state in {"failed", "timed_out"} and not job.cancel_requested_at and job.attempt_count < job.max_attempts
                        and job.error_code not in permanent):
                    with wb.session.begin_nested():
                        JobService(wb.session).retry(job.id, run_after=datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAY_SECONDS), commit=False)
                        wb.append(task_id, "post_publication_retrying", {"version": VERSION, "publication_hash": source_hash,
                            "attempt": job.attempt_count, "message": "只读跟进未完成，正在原额度内重试。"},
                            key=f"post-publish-retry:{job.id}:{job.attempt_count}", job_id=job.id, commit=False)
                    return
                finish("stopped" if job.state == "cancelled" else "blocked", job=job,
                    message=job.error_message or "自动核对未完成；可在原对话查看原因并继续。")
                return
            observation = wb.latest(task_id, "pr_followup_observed")
            if observation is None or observation.job_id != job.id:
                raise WorkbenchError("自动跟进缺少本次远端核对回执。", code="post_publication_evidence_missing")
            snapshot = observation.payload
            pr = snapshot["pr"]
            if (pr["state"] != "open" or pr["merged"]
                    or pr["head_sha"] != target["head_sha"] or pr["head_branch"] != target["head_branch"]):
                finish("not_eligible", job=job, message=snapshot["next_action"])
                return
            if not pr["draft"]:
                finish("already_ready", job=job, message="远端 PR 已提交审核，将按最新状态继续跟进。")
                return
            if preview:
                finish("awaiting_confirmation", job=job, preview=preview,
                    message="已自动核对远端状态，审核预览已就绪。请查看预览并单独确认提交正式审核。")
                return
            if job.kind == PREPARE:
                raise WorkbenchError("未生成可核对的审核预览。", code="post_publication_preview_missing")
        elif unresolved_ready(wb, task_id) or unresolved_update(wb, task_id):
            finish("blocked", message="已有远端操作结果尚未明确，请先在原对话核对该操作。")
            return
        # Verified publication + connected account establish eligibility. The
        # coordinator deliberately has neither Publisher credentials nor its
        # local mode: only the isolated Publisher consumes this read-only job.
        # A valid manually prepared preview stays intact; first refresh its
        # remote facts. Otherwise PREPARE itself reads status and creates preview.
        kind = FOLLOWUP if step is None and preview else PREPARE
        with wb.session.begin_nested():
            queued = request_lifecycle(wb, task_id, kind=kind, account_hash=account.event_hash,
                key="post-publish:" + publication.id + (":observe" if kind == FOLLOWUP else ":ready"),
                origin="workflow", source=publication, commit=False)
            wb.append(task_id, "post_publication_step", {"version": VERSION, "publication_hash": source_hash,
                "account_hash": account.event_hash, "kind": kind}, key="post-publish-step:" + queued.id,
                job_id=queued.id, commit=False)
    except WorkbenchError as exc:
        finish("blocked", job=job, message=str(exc))


def result_origin(wb: Workbench, event: Any, events: list[Any]) -> dict[str, str] | None:
    """Projection only: enrich historical receipts without rewriting their text."""
    if event and event.kind.startswith("post_publication_"):
        return {"kind": "workflow", "label": "系统自动跟进"}
    job = wb.session.get(Job, event.job_id) if event and event.job_id else None
    if job is None or job.kind not in {PREPARE, FOLLOWUP, "publication_write"}:
        return None
    attribution = next((e for e in reversed(events) if e.kind == "pr_lifecycle_origin_attributed"
                        and e.job_id == job.id and e.actor_id == "orchestrator"
                        and e.payload["job_payload_hash"] == job.payload_hash), None)
    kind = attribution.payload["origin"] if attribution else job.payload.get("trigger", {}).get("kind", "request")
    if job.kind == "publication_write": kind = "confirmed_action"
    labels = {"workflow": "系统自动跟进", "maintenance": "维护操作触发", "request": "手动请求触发",
              "confirmed_action": "发布确认后生成"}
    return {"kind": kind, "label": labels[kind]} if kind in labels else None


def attribute_maintenance_origin(wb: Workbench, task_id: str, job_id: str, *, reason: str) -> Any:
    """Trusted corrective attribution; never edits old Jobs, actors or approvals."""
    from app.cla import _lock
    _lock(wb)
    job = wb.session.get(Job, job_id)
    if job is None or job.kind not in {PREPARE, FOLLOWUP} or job.payload.get("task_id") != task_id:
        raise WorkbenchError("来源核对任务无效。")
    return wb.append(task_id, "pr_lifecycle_origin_attributed", {"job_payload_hash": job.payload_hash,
        "origin": "maintenance", "reason": reason}, key="pr-origin-attributed:" + job.id,
        actor="orchestrator", job_id=job.id)
