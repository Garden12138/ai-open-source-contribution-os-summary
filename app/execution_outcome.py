"""Evidence-based outcomes; finishing a Job never means tests passed."""
from __future__ import annotations

import re
from typing import Any

from app.artifacts import ArtifactStore
from app.executions import ExecutionAttemptService
from app.security import redact_text


def require_coding_environment(wb: Any, attempt_id: str) -> None:
    """A recorded startup failure cannot become an implementation request."""
    from app.sandbox_worker.workspace_jobs import preparation_receipt
    from app.workbench import WorkbenchError
    receipt = preparation_receipt(wb, attempt_id)
    if receipt and baseline_outcome(wb.read(receipt.payload["result_hash"]))["status"] == "environment_blocked":
        raise WorkbenchError("修改前测试环境尚未就绪，需先恢复环境并通过基线，再生成代码。", code="environment_blocked")


def reconcile_baseline_blocker(wb: Any, attempt: Any) -> bool:
    """Reclassify legacy evidence append-only, before retrying a later failed job."""
    from app.sandbox_worker.workspace_jobs import preparation_receipt
    stage = ExecutionAttemptService(wb.session).current(attempt.id)
    receipt = preparation_receipt(wb, attempt.id)
    if stage.stage != "explore" or stage.status != "succeeded" or receipt is None:
        return False
    outcome = baseline_outcome(wb.read(receipt.payload["result_hash"]))
    if outcome["status"] != "environment_blocked":
        return False
    # A proven replacement owns this baseline until it records new evidence.
    # Re-adding the old blocker after its resume would strand a passing result.
    from app.models import Job
    replacement = next((e for e in reversed(wb.history(attempt.task_id))
        if e.kind == "preparation_runtime_retry_requested" and e.actor_id == "orchestrator"
        and e.payload.get("attempt_id") == attempt.id), None)
    if replacement:
        job = wb.session.get(Job, replacement.job_id)
        if job and job.payload.get("replaces_baseline_hash") == receipt.record_hash:
            return job.state in {"queued", "leased", "running"}
    key = "baseline-environment-v2:" + receipt.id
    if not any(e.idempotency_key == key for e in wb.history(attempt.task_id)):
        old = wb.active_blocker(attempt.task_id)
        from app.agent_runtime import recovery
        wb.append(attempt.task_id, "automation_blocked", {"reason_code":"environment_blocked",
            "retryable":False,"recovery":recovery("environment_blocked"),
            "reason":"修改前检查未能实际完成，执行环境或命令尚未就绪；需先修订方案或恢复环境并重建基线。",
            "baseline_hash":receipt.record_hash,"missing_dependencies":outcome["missing_dependencies"],
            "superseded_blocker_hash":old.record_hash if old else None}, key=key,
            actor="orchestrator", job_id=receipt.job_id)
    return True


def reconcile_proposal_budget_blocker(wb: Any, attempt: Any) -> bool:
    """Explain exhausted legacy call lineages without retrying or changing usage."""
    stage = ExecutionAttemptService(wb.session).current(attempt.id)
    if stage.stage != "explore" or stage.status != "succeeded":
        return False
    jobs = [j for j in wb.jobs(attempt.task_id) if j.kind == "change_set_proposal"
        and j.state == "failed" and j.payload.get("execution_attempt_id") == attempt.id
        and j.payload.get("version") == "change-set-proposal-job-v2"
        and j.error_code in {"proposal_budget_exceeded", "proposal_call_budget_exceeded"}]
    if not jobs:
        return False
    job = jobs[-1]
    root = job.payload.get("root_job_id", job.id)
    from app.coding_budget import proposal_budget
    budget = proposal_budget(wb, attempt.task_id, root)
    if budget["remaining"]:
        return False
    from app.proposal_validation import MESSAGES
    wb.append(attempt.task_id, "automation_blocked", {
        "reason_code":"proposal_call_budget_exceeded", "reason":MESSAGES["proposal_call_budget_exceeded"],
        "retryable":False, "root_job_id":root,"used_calls":budget["used"],"call_limit":budget["limit"],
        "recovery":{"category":"budget","action":"requires_budget_authorization","uses_code_repair":False}},
        key="proposal-call-budget:"+job.id+":"+str(budget["limit"]), actor="orchestrator", job_id=job.id)
    return True


def classify_verification(value: dict[str, Any]) -> dict[str, Any]:
    tests = value.get("test_results", [])
    evidence = value.get("command_evidence", [])
    passed = sum(t.get("outcome") == "passed" and t.get("exit_code") == 0 for t in tests)
    failures = []
    missing = set()
    for item in evidence:
        stderr_full = redact_text(str(item.get("stderr_log") or ""))
        stdout_full = redact_text(str(item.get("stdout_log") or ""))
        # Keep immutable exit/status evidence intact while correcting derived
        # verdicts for known CLI dispatch failures, including historical runs.
        unexecuted = any(log.lstrip().startswith("Usage: bun run [flags] <file or script>")
                         or re.match(r"(?is)^usage:[^\n]*\n(?:\s*\n)?(?:options:|flags:|commands:)", log.lstrip())
                         or re.search(r"(?m)^Ran 0 tests\b|^# tests 0\s*$|^=+ no tests ran\b", log)
                         for log in (stdout_full, stderr_full))
        if item.get("status") == "passed" and item.get("exit_code") == 0:
            if not unexecuted:
                continue
            passed = max(0, passed - 1)
        if unexecuted:
            missing.add("verification_command_not_executed")
        stderr, stdout = stderr_full[:4000], stdout_full[:8000]
        output = "\n\n".join(part for part in (stdout, stderr) if part)
        # Classify the complete bounded command evidence, not just the UI excerpt.
        diagnostic_output = stdout_full + "\n" + stderr_full
        if "PinnedDownloadError" in diagnostic_output and "ffmpeg" in diagnostic_output.lower():
            missing.update(("ffmpeg", "ffprobe"))
        if (re.search(r"(?:Cannot find module[^\n]*\.node|ERR_DLOPEN_FAILED|Failed to load [^\n]*native addon|Could not locate the bindings file)", diagnostic_output, re.I)
                or re.search(r"(?:ImportError|OSError):[^\n]*(?:cannot open shared object file|undefined symbol|DLL load failed)", diagnostic_output)):
            missing.add("native_addon_unavailable")
        missing.update(re.findall(r"ModuleNotFoundError: No module named ['\"]([a-zA-Z0-9_.-]+)", diagnostic_output))
        if item.get("exit_code") == 127:
            missing.add("command_unavailable")
        if "ENOSPC" in diagnostic_output or "no space left on device" in diagnostic_output.lower():
            missing.add("sandbox_storage_exhausted")
        if (re.search(r"(?:ENOENT|EACCES|EROFS)", diagnostic_output)
                and re.search(r"node_modules[/\\](?:\.vite-temp|\.vite|\.cache)(?:[/\\\s'\"]|$)", diagnostic_output)):
            missing.add("node_tool_cache_unavailable")
        if re.search(r"FileNotFoundError: /usr/(?:local/)?bin/[A-Za-z0-9_.-]+", diagnostic_output):
            missing.add("command_unavailable")
        if re.search(r"ImportError: Start directory is not importable:", diagnostic_output):
            missing.add("test_discovery_directory_unavailable")
        failures.append({"command_id": item.get("command_id"), "exit_code": item.get("exit_code"),
                         "status": "execution_unproven" if unexecuted else item.get("status"), "stderr": stderr,
                         "stdout": stdout, "output": output})
    status = "passed" if tests and passed == len(tests) and value.get("succeeded") is True else "failed"
    if missing:
        status = "environment_blocked"
    return {"status": status, "passed": passed, "total": len(tests),
            "missing_dependencies": sorted(missing), "failures": failures,
            "test_results_hash": value.get("test_results_hash")}


def unchanged_baseline_failures(baseline: dict[str, Any], verification: dict[str, Any]) -> bool:
    """Only exact command/output evidence can identify unchanged failures."""
    def signature(item: dict[str, Any]) -> tuple | None:
        fields = ("command_id", "command_hash", "exit_code", "stdout_hash", "stderr_hash")
        if any(item.get(k) is None for k in fields) or item.get("exit_code") == 0:
            return None
        return tuple(item[k] for k in fields)
    before = {signature(e) for e in baseline.get("inspection", {}).get("command_evidence", [])}
    before.discard(None)
    after = [e for e in verification.get("command_evidence", [])
             if e.get("exit_code") != 0 or e.get("status") != "passed"]
    return bool(after) and all(signature(e) in before for e in after)


def baseline_outcome(value: dict[str, Any]) -> dict[str, Any]:
    """Project complete immutable evidence, including older stdout-less summaries."""
    evidence = value["inspection"]["command_evidence"]
    return classify_verification({
        "succeeded": bool(evidence) and all(e["exit_code"] == 0 for e in evidence),
        "test_results": [{"outcome": "passed" if e["exit_code"] == 0 else "failed",
                          "exit_code": e["exit_code"]} for e in evidence],
        "command_evidence": evidence,
    })


def attempt_verification(session: Any, artifact_root: str, attempt_id: str) -> dict[str, Any]:
    stages = ExecutionAttemptService(session).history(attempt_id)
    completed = next((s for s in reversed(stages) if s.stage == "verify" and s.status == "succeeded"), None)
    if completed is None:
        return {"status": "not_run", "passed": 0, "total": 0, "failures": [], "missing_dependencies": []}
    import json
    value = json.loads(ArtifactStore(session, artifact_root).read_bytes(completed.result_hash))
    result = classify_verification(value)
    result["result_hash"] = completed.result_hash
    return result
