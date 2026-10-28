"""Separate readable source, static admission and actual execution evidence."""
from __future__ import annotations

from typing import Literal
from pydantic import Field
from app.agent_v2.contracts import StrictModel
from app.execution_environment import task_environment
from app.execution_outcome import baseline_outcome
from app.workbench import Workbench


class CapabilityVerdict(StrictModel):
    version: Literal["agent-capabilities-v1"] = "agent-capabilities-v1"
    source_readable: bool
    manifest_recognized: bool
    dependencies_preparable: Literal["admitted", "unknown", "unsupported"]
    baseline_executable: Literal["proven", "unknown", "blocked"]
    verification_supported: Literal["proven", "unknown", "unsupported"]
    reason_codes: list[str] = Field(default_factory=list)
    source_event_hash: str | None = None
    baseline_event_hash: str | None = None


def capability_verdict(wb: Workbench, task_id: str) -> CapabilityVerdict:
    context = wb.latest(task_id, "context_ready")
    environment = task_environment(wb, task_id)
    baseline = wb.latest(task_id, "execution_baseline_ready")
    status = baseline_outcome(wb.read(baseline.payload["result_hash"]))["status"] if baseline else "not_run"
    # A prior baseline from another source/base cannot prove current capability.
    if baseline and context:
        from app.executions import ExecutionAttemptService
        attempt = ExecutionAttemptService(wb.session).get_verified(baseline.payload["attempt_id"])
        if (attempt.repository_archive_hash != context.payload["archive_hash"]
                or attempt.base_commit_sha != context.payload["base_sha"]):
            status = "not_run"
    unsupported = environment["status"] == "unsupported"
    return CapabilityVerdict(source_readable=context is not None,
        manifest_recognized=bool(environment.get("manifests")),
        dependencies_preparable="unsupported" if unsupported else "admitted" if environment["status"] == "supported" else "unknown",
        baseline_executable="proven" if status == "passed" else "unknown" if status == "not_run" else "blocked",
        verification_supported="unsupported" if unsupported else "proven" if status == "passed" else "unknown",
        reason_codes=[environment["reason_code"]] if environment.get("reason_code") else [],
        source_event_hash=context.record_hash if context else None,
        baseline_event_hash=baseline.record_hash if baseline else None)
