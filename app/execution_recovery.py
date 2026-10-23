"""Bounded restoration of disposed verification environments, never code edits."""
from __future__ import annotations

from typing import Any
from sqlalchemy import select
from app.models import ExecutionAttempt, ExecutionStageRun, Job
from app.executions import ExecutionAttemptService
from app.workbench import Workbench, WorkbenchError


def resume_preparation_after_runtime_repair(wb: Workbench, failed_job_id: str, settings: Any, proof: dict) -> Job:
    """Trusted maintenance only: rerun unchanged preparation after a proven runtime fix.

    No API/model route accepts this proof. A successful exact tool probe may
    also establish restored acquisition availability, without claiming that its
    earlier unknown failure was diagnosed. Keep the failed job and allocations
    immutable, and allocate a new bounded job under the existing exact approval.
    """
    import hashlib
    from pathlib import Path
    import re
    from app.cla import _lock
    from app.provenance import content_hash
    _lock(wb)
    failed = wb.session.get(Job, failed_job_id)
    if failed is None or failed.kind != "execution_prepare" or failed.state != "failed" or failed.payload.get("recovery_stage_hash"):
        raise WorkbenchError("不是可恢复的初始环境准备任务。", code="environment_stale")
    attempt = ExecutionAttemptService(wb.session).get_verified(failed.payload["execution_attempt_id"])
    authorization = require_recovery_authorization(wb, attempt, settings)
    sources = {"app/sandbox_worker/dependencies.py", "app/sandbox_worker/verification.py"}
    project = Path(__file__).resolve().parent.parent
    inventory = isinstance(proof, dict) and proof.get("version") == "node-inventory-runtime-repair-v1"
    required = {"version", "failed_job_id", "source_hashes", "regression_hash"}
    required.update({"supervisor_exit_code", "supervisor_log"} if inventory else {"probe"})
    if (not isinstance(proof, dict) or set(proof) != required or proof["failed_job_id"] != failed.id
            or not isinstance(proof["source_hashes"], dict) or set(proof["source_hashes"]) != sources
            or any(proof["source_hashes"][name] != hashlib.sha256((project / name).read_bytes()).hexdigest() for name in sources)
            or not isinstance(proof["regression_hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", proof["regression_hash"])):
        raise WorkbenchError("环境准备修复证据不完整。", code="environment_stale")
    events = wb.history(attempt.task_id)
    if inventory:
        valid = (proof["supervisor_exit_code"] == 89 and isinstance(proof["supervisor_log"], str)
            and len(proof["supervisor_log"]) <= 4096 and failed.id in proof["supervisor_log"]
            and "code=89" in proof["supervisor_log"] and "Verification" in proof["supervisor_log"]
            and failed.error_code in {"environment_preparation_failed", "environment_baseline_integrity_failed"})
    else:
        from app.execution_environment import task_environment
        recipe = task_environment(wb, attempt.task_id).get("node_environment", {})
        diagnosis = next((e for e in reversed(events) if e.kind == "execution_preparation_failed" and e.job_id == failed.id), None)
        probe = proof["probe"]
        valid = (proof["version"] == "node-tools-availability-v1"
            and failed.error_code == "environment_node_preparation_failed" and diagnosis is not None
            and diagnosis.payload.get("diagnostic", {}).get("phase") == "node_tools"
            and isinstance(probe, dict) and set(probe) == {"version", "exit_code", "known_codes", "registry",
                "manager", "manager_version", "npm_version", "scripts_disabled", "output_bytes"}
            and probe["version"] == "node-tools-probe-v1" and probe["exit_code"] == 0
            and probe["known_codes"] == [] and probe["scripts_disabled"] is True
            and probe["registry"] == recipe.get("registry") and probe["manager"] == recipe.get("manager")
            and probe["manager_version"] == recipe.get("manager_version") and probe["npm_version"] == "10.9.4"
            and type(probe["output_bytes"]) is int and 0 <= probe["output_bytes"] <= 65536)
    if not valid:
        raise WorkbenchError("准备失败与已核对的修复或可用性证据不匹配。", code="environment_stale")
    previous = next((e for e in events if e.idempotency_key == "prepare-runtime-repair:" + failed.id), None)
    if previous:
        if previous.payload["proof_hash"] != content_hash(proof):
            raise WorkbenchError("重复恢复的证据不同。", code="environment_stale")
        return wb.session.get(Job, previous.job_id)
    stage = ExecutionAttemptService(wb.session).current(attempt.id)
    repairs = [e for e in events if e.kind == "preparation_runtime_retry_requested" and e.payload["attempt_id"] == attempt.id]
    if (stage.stage != "explore" or stage.status != "succeeded" or stage.result_hash != failed.payload["explore_result_hash"]
            or len(repairs) >= 2 or any(e.kind == "execution_baseline_ready" and e.payload["attempt_id"] == attempt.id for e in events)
            or any(e.kind in {"coding_started", "automatic_change_accepted", "awaiting_acceptance"}
                   and e.payload.get("attempt_id") == attempt.id for e in events)
            or any(j.state not in {"succeeded", "failed", "cancelled", "timed_out"} for j in wb.jobs(attempt.task_id))):
        raise WorkbenchError("已有执行进展或恢复预算耗尽，不能重跑旧准备。", code="environment_stale")
    proof_hash = wb.artifact(proof)
    job = wb.enqueue(attempt.task_id, "execution_prepare", {
        **failed.payload, "replaces_preparation_job_id": failed.id, "runtime_repair_hash": proof_hash},
        key="prepare-runtime-repair:" + failed.id, timeout_seconds=failed.timeout_seconds, commit=False)
    wb.append(attempt.task_id, "preparation_runtime_retry_requested", {
        "attempt_id": attempt.id, "authorization_hash": authorization.record_hash,
        "stage_hash": stage.record_hash, "proof_hash": proof_hash,
        "superseded_preparation_job_ids": [failed.id]}, key="prepare-runtime-repair:" + failed.id,
        job_id=job.id, plan_id=attempt.plan_version_id, actor="orchestrator", commit=False)
    _resume_blocker(wb, attempt, authorization, job, stage)
    wb.session.commit()
    return job


def record_transport_recovery(wb: Workbench, failed_job_id: str, proof: dict) -> Any:
    """Trusted maintenance entry; require a real proxy/TLS probe before retry."""
    import ipaddress
    from app.sandbox_worker.dependencies import DependencyProxyPolicy
    from app.execution_environment import task_environment
    from app.execution_preflight import environment_inputs_hash
    job = wb.session.get(Job, failed_job_id)
    if job is None or job.kind != "execution_prepare" or job.state != "failed" or not job.payload.get("recovery_stage_hash"):
        raise WorkbenchError("不是可核对的验证环境恢复任务。", code="environment_stale")
    attempt = ExecutionAttemptService(wb.session).get_verified(job.payload["execution_attempt_id"])
    diagnosis = next((e for e in reversed(wb.history(attempt.task_id))
                      if e.kind == "execution_preparation_failed" and e.job_id == job.id
                      and e.payload.get("diagnostic", {}).get("reason_code") == "proxy_denied"), None)
    hosts = proof.get("hosts", {})
    if (diagnosis is None or proof.get("version") != "package-dns-transport-v1"
            or proof.get("proxy_policy_hash") != DependencyProxyPolicy.for_ecosystem("python").policy_hash
            or set(hosts) != {"pypi.org", "files.pythonhosted.org"}
            or any(v.get("tls_verified") is not True or v.get("status") not in {200,404}
                   or not v.get("addresses") or len(v["addresses"]) > 32
                   or any(not ipaddress.ip_address(address).is_global for address in v["addresses"])
                   for v in hosts.values())):
        raise WorkbenchError("包代理恢复证据不完整。", code="environment_stale")
    environment_hash = environment_inputs_hash(wb.latest(attempt.task_id, "context_ready"), task_environment(wb, attempt.task_id))
    if diagnosis.payload["environment_inputs_hash"] != environment_hash:
        raise WorkbenchError("包代理恢复的环境依据已变化。", code="environment_stale")
    artifact = wb.artifact(proof)
    return wb.append(attempt.task_id, "execution_environment_diagnostic_resolved", {
        "version":"package-dns-transport-v1", "diagnostic_hash":diagnosis.record_hash,
        "environment_inputs_hash":environment_hash, "proof_hash":artifact,
    }, key="transport-recovered:" + diagnosis.id, job_id=job.id)


def require_recovery_authorization(wb: Workbench, attempt: ExecutionAttempt, settings: Any) -> Any:
    from app.execution_preflight import task_preflight
    from app.model_settings import task_identity
    authorization = wb.latest(attempt.task_id, "execution_authorized")
    start = wb.latest(attempt.task_id, "automation_started")
    stop = wb.latest(attempt.task_id, "stop_requested")
    plan = wb.latest_plan(attempt.task_id)
    latest = wb.session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.task_id == attempt.task_id).order_by(ExecutionAttempt.attempt_number.desc()).limit(1))
    if (authorization is None or start is None or plan is None
            or latest != attempt.id or plan.id != attempt.plan_version_id
            or authorization.plan_version_id != attempt.plan_version_id
            or start.payload["authorization_hash"] != authorization.record_hash
            or (stop and stop.sequence > authorization.sequence)
            or authorization.payload.get("workflow_version") != "sandbox-workflow-v2"
            or authorization.payload["context"]["archive_hash"] != attempt.repository_archive_hash
            or authorization.payload["context"]["image"] != attempt.runner_image_digest
            or any(authorization.payload[role] != task_identity(wb.session, settings, attempt.task_id, role).hash_payload()
                   for role in ("implementation", "review"))):
        raise WorkbenchError("执行依据已变化或任务已停止，不能恢复旧验证。", code="environment_stale")
    preflight = task_preflight(wb, attempt.task_id)
    if preflight["status"] != "ready" or preflight["report_hash"] != authorization.payload.get("preflight_hash"):
        raise WorkbenchError("环境能力或冻结依据已变化，需要重新确认方案。", code="environment_stale")
    return authorization


def _resume_blocker(wb: Workbench, attempt: ExecutionAttempt, authorization: Any, job: Job, stage: Any) -> None:
    blocker = wb.active_blocker(attempt.task_id)
    if blocker:
        start = wb.latest(attempt.task_id, "automation_started")
        wb.append(attempt.task_id, "automation_resumed", {
            "start_hash": start.record_hash, "authorization_hash": authorization.record_hash,
            "blocked_hash": blocker.record_hash, "attempt_id": attempt.id,
            "job_id": job.id, "job_attempt_count": job.attempt_count,
        }, key="restore-resume:" + job.id, job_id=job.id, plan_id=attempt.plan_version_id,
           commit=False)


def advance_environment_recovery(wb: Workbench, attempt: ExecutionAttempt, settings: Any) -> bool:
    """Return true while a proven disposed-environment recovery owns this tick."""
    from app.cla import _lock
    from app.sandbox_worker.workspace_jobs import preparation_receipt, preparation_needs_restore, PREPARE
    from app.jobs import TERMINAL_STATES
    attempts = ExecutionAttemptService(wb.session)
    stage = attempts.current(attempt.id)
    if stage.stage != "verify" or stage.status != "failed" or len(stage.input_hashes) != 2:
        return False
    # Ordinary test failures and unknown sandbox failures are not recovery grants.
    if stage.reason_code not in {"environment_stale", "sandbox_workbencherror"}:
        return False
    baseline = preparation_receipt(wb, attempt.id)
    if baseline is None:
        return False
    old = wb.read(baseline.payload["result_hash"])
    jobs = [j for j in wb.jobs(attempt.task_id) if j.kind == PREPARE
            and j.payload.get("recovery_stage_hash") == stage.record_hash]
    if not jobs and (old.get("dependency_result_hash") != stage.input_hashes[1]
                     or not preparation_needs_restore(wb, baseline)):
        return False
    _lock(wb)
    # Fail closed if another process advanced before we obtained the write lock.
    if attempts.current(attempt.id).record_hash != stage.record_hash:
        wb.session.commit()
        return True
    authorization = require_recovery_authorization(wb, attempt, settings)
    runs = list(wb.session.scalars(select(ExecutionStageRun).where(
        ExecutionStageRun.execution_attempt_id == attempt.id, ExecutionStageRun.stage == "verify")
        .order_by(ExecutionStageRun.stage_run_number)))
    if not runs or len(runs) >= runs[-1].max_stage_runs:
        raise WorkbenchError("验证已达到恢复次数上限，已有改动和证据保留。", code="environment_recovery_exhausted")
    if not jobs:
        payload = {"version": "workspace-preparation-job-v1", "execution_attempt_id": attempt.id,
            "explore_result_hash": old["explore_result_hash"], "recovery_stage_hash": stage.record_hash,
            "replaces_baseline_hash": baseline.record_hash, "authorization_hash": authorization.record_hash}
        job = wb.enqueue(attempt.task_id, PREPARE, payload,
            key="restore-verify:" + stage.id, timeout_seconds=1800)
        _resume_blocker(wb, attempt, authorization, job, stage)
        wb.session.commit()
        return True
    job = jobs[-1]
    previous = next(e for e in wb.history(attempt.task_id)
                    if e.record_hash == job.payload["replaces_baseline_hash"])
    old = wb.read(previous.payload["result_hash"])
    if job.state not in TERMINAL_STATES:
        wb.session.commit()
        return True
    if job.state != "succeeded":
        resolved = next((e for e in reversed(wb.history(attempt.task_id))
            if e.kind == "execution_environment_diagnostic_resolved" and e.job_id == job.id
            and e.payload.get("version") == "package-dns-transport-v1"), None)
        if job.state == "failed" and resolved and len(jobs) < 3:
            # New allocation keys; never reuse a failed job's immutable volume
            # receipts. This does not extend the existing Verify run budget.
            retry = wb.enqueue(attempt.task_id, PREPARE, {
                **job.payload, "replaces_preparation_job_id":job.id,
                "transport_recovery_hash":resolved.record_hash,
            }, key="restore-transport:" + job.id, timeout_seconds=1800)
            _resume_blocker(wb, attempt, authorization, retry, stage)
            wb.session.commit()
            return True
        error = WorkbenchError("隔离环境恢复失败：" + (job.error_message or "请检查环境准备记录。"),
                               code=job.error_code or "environment_recovery_failed")
        error.job_id = job.id
        raise error
    fresh = wb.read(job.result_data["result_hash"])
    if (fresh.get("replaces_baseline_hash") != job.payload["replaces_baseline_hash"]
            or fresh.get("recovery_stage_hash") != stage.record_hash
            or fresh["outcome"]["status"] != "passed"
            or fresh["explore_result_hash"] != old["explore_result_hash"]
            or fresh["plan_hash"] != old["plan_hash"] or fresh["commands_hash"] != old["commands_hash"]):
        raise WorkbenchError("恢复基线未通过或输入不一致，不能开始验证。", code="environment_stale")
    from app.sandbox_worker.dependency_jobs import load_dependency
    dependency = load_dependency(wb, attempt.id)
    if dependency is None or dependency.result.result_hash != fresh["dependency_result_hash"]:
        raise WorkbenchError("恢复依赖收据不一致。", code="environment_stale")
    _resume_blocker(wb, attempt, authorization, job, stage)
    receipt = wb.append(attempt.task_id, "verification_environment_restored", {
        "attempt_id": attempt.id, "failed_stage_hash": stage.record_hash,
        "authorization_hash": authorization.record_hash, "old_dependency_hash": stage.input_hashes[1],
        "dependency_result_hash": dependency.result.result_hash,
        "implement_result_hash": stage.input_hashes[0], "baseline_hash": job.result_data["result_hash"],
        "superseded_preparation_job_ids": [j.id for j in jobs if j.id != job.id],
    }, key="verify-environment:" + stage.id, job_id=job.id, commit=False)
    attempts.retry_verification_environment(attempt.id, expected_record_hash=stage.record_hash,
        recovery_event_hash=receipt.record_hash, idempotency_key="restore-stage:" + stage.id, commit=False)
    from app.execution_environment import schedule_verification
    from app.providers.runtime import resolve_job_spec_signer
    schedule_verification(wb, attempt.id, resolve_job_spec_signer(settings))
    return True
