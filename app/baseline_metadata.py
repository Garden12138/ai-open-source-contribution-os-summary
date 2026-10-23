"""Read-only metadata continuation for unavailable baseline failure source."""
from __future__ import annotations
import re
from pathlib import PurePosixPath
from typing import Any

from app.archives import RepositoryArchiveStore
from app.executions import ExecutionAttemptService
from app.plans import PlanVersionService
from app.sandbox_worker.import_metadata import VERSION, validate_metadata
from app.sandbox_worker.specs import SandboxPolicy
from app.workbench import WorkbenchError

KIND = "planning_import_metadata"


def metadata_paths(wb: Any, recovery_hash: str, evidence: dict) -> list[str]:
    packet = wb.read(recovery_hash)
    if packet.get("import_metadata") is not None:
        return []
    baseline = wb.read(packet["baseline_result_hash"])
    attempt = ExecutionAttemptService(wb.session).get_verified(baseline["attempt_id"])
    plan = PlanVersionService(wb.session).get_verified(attempt.plan_version_id)
    directories = {c["command_id"]: c["working_directory"] for c in plan.commands_to_run}
    unavailable = {e["path"] for e in evidence["entries"] if e.get("content") is None}
    paths = set()
    for item in baseline["inspection"]["command_evidence"]:
        output = item.get("stdout_log", "") + "\n" + item.get("stderr_log", "")
        for name in re.findall(r'''^([^:\n]+\.py):\d+:\s*error:\s*Call to untyped function ["']entry_points["']''', output, re.M):
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            path = str(PurePosixPath(directories[item["command_id"]]) / relative)
            if path in unavailable and path in evidence["inventory"]:
                paths.add(path)
    return sorted(paths)[:4]


def enqueue_metadata(wb: Any, job: Any, evidence: dict) -> Any | None:
    data = job.payload
    if not data.get("baseline_recovery_hash"):
        return None
    paths = metadata_paths(wb, data["baseline_recovery_hash"], evidence)
    if not paths:
        return None
    context = wb.latest(data["task_id"], "context_ready")
    return wb.enqueue(data["task_id"], KIND, {**data, "metadata_paths": paths,
        "archive_hash":context.payload["archive_hash"], "base_sha":context.payload["base_sha"],
        "image":context.payload["image"], "metadata_version":VERSION},
        key="import-metadata:" + job.id, commit=False)


async def complete_metadata(worker: Any, job: Any) -> dict:
    from app.planner import PlanningService, TURN
    data = job.payload
    def current(wb):
        context = wb.latest(data["task_id"], "context_ready")
        plan = wb.latest_plan(data["task_id"])
        evidence = wb.read(context.payload["artifact_id"]) if context else {}
        if (wb.require_planning(data["task_id"]) != data["state_hash"] or context is None
                or context.id != data["context_id"] or plan is None or plan.id != data["parent_id"]
                or data["archive_hash"] != context.payload["archive_hash"] or data["base_sha"] != context.payload["base_sha"]
                or data["image"] != context.payload["image"] or data["image"] != worker.settings.workbench_runner_image
                or context.payload["policy_hash"] != SandboxPolicy().policy_hash
                or data.get("metadata_version") != VERSION
                or data["metadata_paths"] != metadata_paths(wb, data["baseline_recovery_hash"], evidence)):
            raise WorkbenchError("导入元数据读取依据已变化。", code="baseline_feedback_stale")
    with worker.database.session() as session:
        worker.check(session, job)
        current(PlanningService(session, worker.settings.artifact_root))
    archive = RepositoryArchiveStore(worker.settings.artifact_root).lookup(data["archive_hash"],
        repository_full_name=data["inputs"]["repository"], base_commit_sha=data["base_sha"])
    if archive is None:
        raise WorkbenchError("导入元数据的冻结归档不可用。", code="baseline_feedback_stale")
    result = await worker.runtime.inspect_import_metadata(archive=archive, image_digest=data["image"],
        policy=SandboxPolicy(), paths=tuple(data["metadata_paths"]))
    with worker.database.session() as session:
        worker.check(session, job)
        wb = PlanningService(session, worker.settings.artifact_root)
        current(wb)
        if (result.get("archive_hash") != data["archive_hash"] or result.get("base_sha") != data["base_sha"]
                or result.get("image") != data["image"] or result.get("policy_hash") != SandboxPolicy().policy_hash
                or result.get("full_source_available") is not False):
            raise WorkbenchError("导入元数据的出处不匹配。", code="baseline_feedback_stale")
        if set(result) != {"version", "entries", "archive_hash", "base_sha", "image", "policy_hash", "full_source_available"}:
            raise WorkbenchError("导入元数据格式无效。", code="baseline_feedback_stale")
        validate_metadata({"version":result.get("version"), "entries":result.get("entries")}, tuple(data["metadata_paths"]))
        metadata_hash = wb.artifact(result)
        packet = wb.read(data["baseline_recovery_hash"])
        enriched = wb.artifact({**packet, "import_metadata":result, "import_metadata_hash":metadata_hash})
        wb.append(data["task_id"], "baseline_import_metadata_ready", {"version":"python-entrypoints-imports-v1",
            "metadata_hash":metadata_hash, "recovery_hash":enriched, "full_source_available":False,
            "baseline_result_hash":packet["baseline_result_hash"]},
            key="import-metadata-ready:" + job.id, job_id=job.id, actor="sandbox-worker", commit=False)
        child = wb.enqueue(data["task_id"], TURN, {**data, "baseline_recovery_hash":enriched},
                           key="import-metadata-next:" + job.id, commit=False)
        return worker.finish(session, job, {"next_job_id":child.id, "metadata_hash":metadata_hash})
