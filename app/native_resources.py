"""Exact catalog resources, separately consented from implementation and publishing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import platform
import secrets
from typing import Any

from app.provenance import content_hash
from app.workbench import Workbench, WorkbenchError

VERSION = "native-resource-policy-v1"
DOWNLOAD = "native_resource_download"
ARCHIVE = "34b88d208186647e8772f3fdd423f5a2cd1a77ce85df0474add9854d3c72b9b3"
RELEASE = "autobuild-2026-08-31-13-27"
TOOL_VERSION = "n8.1.2-50-g1a748fe2cd"
PINS = {
    "arm64": ("linuxarm64", "ae5da4f51b9052390f414005f8ab26c1eed1268f327cce7cb79aa076b29bd66e"),
    "amd64": ("linux64", "c733b4b2951e5957e15505f788b2c65a7a41b6da4b289e295852cc38079b4d2b"),
}


def catalog(architecture: str) -> dict[str, Any]:
    if architecture not in PINS:
        raise WorkbenchError("当前平台没有固定资源适配", code="native_resource_platform")
    target, digest = PINS[architecture]
    stem = f"ffmpeg-{TOOL_VERSION}-{target}-gpl-8.1"
    return {"policy": VERSION, "name": "FFmpeg / ffprobe", "version": TOOL_VERSION,
            "platform": "linux/" + architecture,
            "url": f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{RELEASE}/{stem}.tar.xz",
            "sha256": digest, "members": {name: f"{stem}/bin/{name}" for name in ("ffmpeg", "ffprobe")},
            "download_bytes": 256 * 1024**2, "expanded_bytes": 512 * 1024**2,
            "timeout_seconds": 600, "purpose": "供项目测试夹具在断网环境使用固定版本的媒体工具",
            "actions": ["download_exact_archive", "verify_sha256", "extract_two_regular_files", "cache_readonly_for_tests"]}


def local_architecture() -> str:
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(platform.machine(), "unsupported")


def binding(wb: Workbench, task_id: str) -> dict:
    context, plan = wb.latest(task_id, "context_ready"), wb.latest_plan(task_id)
    if not context or not plan or context.payload["archive_hash"] != ARCHIVE:
        raise WorkbenchError("当前冻结项目尚无固定资源适配", code="native_resource_unsupported")
    return {"task_id": task_id, "context_hash": context.record_hash, "archive_hash": ARCHIVE,
            "plan_id": plan.id, "plan_hash": plan.record_hash, "image": context.payload["image"],
            "policy_hash": context.payload["policy_hash"]}


def ready_receipt(wb: Workbench, task_id: str) -> Any:
    """A receipt remains bound to its plan; historical approvals cannot migrate."""
    try:
        current = binding(wb, task_id)
    except WorkbenchError:
        return None
    return next((e for e in reversed(wb.history(task_id)) if e.kind == "native_resource_ready"
                 and e.payload["binding"] == current
                 and e.payload["resource"] == catalog(e.payload["resource"]["platform"].split("/")[1])), None)


def view(wb: Workbench, task_id: str) -> dict:
    try:
        current = binding(wb, task_id)
    except WorkbenchError:
        return {"available": False}
    receipt = ready_receipt(wb, task_id)
    preview = wb.latest(task_id, "native_resource_prepared")
    confirmations = [e for e in wb.history(task_id) if e.kind == "native_resource_confirmed"]
    valid = bool(preview and preview.payload["binding"] == current)
    consumed = bool(preview and any(e.payload["preview_hash"] == preview.record_hash for e in confirmations))
    # A new preview has its own lifecycle. Keep prior failures in history,
    # rather than presenting their status as the outcome of a fresh request.
    jobs = [j for j in wb.jobs(task_id) if j.kind == DOWNLOAD and preview
            and j.payload.get("preview_hash") == preview.record_hash]
    job = jobs[-1] if jobs else None
    return {"available": True, "ready": bool(receipt), "receipt_hash": receipt.record_hash if receipt else None,
            "status": "ready" if receipt else job.state if job else "awaiting_consent",
            "error": job.error_message if job and job.state in {"failed", "timed_out", "cancelled"} else None,
            "preview": {**preview.payload, "record_hash": preview.record_hash, "consumed": consumed, "current": valid} if preview else None}


def prepare(wb: Workbench, task_id: str, *, key: str, architecture: str | None = None) -> dict:
    from app.cla import _lock
    from app.conversation import require_settled_messages
    _lock(wb)
    from app.task_states import ContributionTaskStateService
    if ContributionTaskStateService(wb.session).current(task_id).to_state != "planning":
        raise WorkbenchError("资源准备需要当前待确认方案", code="native_resource_stale")
    require_settled_messages(wb, task_id)
    wb.assert_idle(task_id)
    current = binding(wb, task_id)
    resource = catalog(architecture or local_architecture())
    old = next((e for e in wb.history(task_id) if e.idempotency_key == "native-preview:" + key), None)
    if old:
        if old.payload["binding"] != current or old.payload["resource"] != resource:
            raise WorkbenchError("资源预览请求已变化", code="native_resource_stale")
        return {**old.payload, "record_hash": old.record_hash}
    if sum(e.kind == "native_resource_prepared" for e in wb.history(task_id)) >= 32:
        raise WorkbenchError("资源准备请求次数已达上限", code="native_resource_budget")
    payload = {"version": VERSION, "binding": current, "resource": resource,
               "nonce": secrets.token_hex(32), "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}
    event = wb.append(task_id, "native_resource_prepared", payload, key="native-preview:" + key, actor="local-user", plan_id=current["plan_id"])
    return {**payload, "record_hash": event.record_hash}


def confirm(wb: Workbench, task_id: str, *, preview_hash: str, nonce: str, approved: bool) -> Any:
    from app.cla import _lock
    from app.conversation import require_settled_messages
    from app.audit import AuditService
    _lock(wb)
    # Transport retries return the existing job, never consume another consent.
    consent = next((e for e in wb.history(task_id) if e.kind == "native_resource_confirmed" and e.payload["preview_hash"] == preview_hash), None)
    preview = wb.latest(task_id, "native_resource_prepared")
    if not preview or preview.record_hash != preview_hash or not approved or not secrets.compare_digest(preview.payload["nonce"], nonce):
        raise WorkbenchError("请核对最新资源预览并单独授权下载", code="native_resource_consent_required")
    if consent:
        from app.models import Job
        return wb.session.get(Job, consent.job_id)
    from app.task_states import ContributionTaskStateService
    if ContributionTaskStateService(wb.session).current(task_id).to_state != "planning":
        raise WorkbenchError("资源准备需要当前待确认方案", code="native_resource_stale")
    require_settled_messages(wb, task_id)
    wb.assert_idle(task_id)
    p = preview.payload
    if p["binding"] != binding(wb, task_id) or p["resource"] != catalog(p["resource"]["platform"].split("/")[1]):
        raise WorkbenchError("资源或方案依据已变化，请重新预览", code="native_resource_stale")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(p["expires_at"]):
        raise WorkbenchError("资源下载预览已过期，请重新生成", code="native_resource_expired")
    job = wb.enqueue(task_id, DOWNLOAD, {"preview_hash": preview_hash}, key="native-download:" + preview.id,
                     actor="local-user", commit=False, timeout_seconds=1260, max_attempts=1)
    consent = wb.append(task_id, "native_resource_confirmed", {"preview_hash": preview_hash, "action": "download_exact_resource"},
                        key="native-consent:" + preview.id, actor="local-user", job_id=job.id, commit=False)
    AuditService(wb.session).prepare(event_type="native_resource.authorized", actor_type="local_user", actor_id="local-user",
        correlation_id=job.id, payload={"consent_hash": consent.record_hash, "preview_hash": preview_hash})
    wb.session.commit()
    return job


def dependency_binding(wb: Workbench, task_id: str) -> dict | None:
    receipt = ready_receipt(wb, task_id)
    if not receipt:
        return None
    return {"version": VERSION, "receipt_hash": receipt.record_hash, "resource_hash": content_hash(receipt.payload["resource"]),
            "files": receipt.payload["files"]}
