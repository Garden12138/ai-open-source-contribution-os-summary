"""Evidence-based outcomes; finishing a Job never means tests passed."""
from __future__ import annotations

import re
from typing import Any

from app.artifacts import ArtifactStore
from app.executions import ExecutionAttemptService
from app.security import redact_text


def classify_verification(value: dict[str, Any]) -> dict[str, Any]:
    tests = value.get("test_results", [])
    evidence = value.get("command_evidence", [])
    passed = sum(t.get("outcome") == "passed" and t.get("exit_code") == 0 for t in tests)
    failures = []
    missing = set()
    for item in evidence:
        if item.get("status") == "passed" and item.get("exit_code") == 0:
            continue
        stderr = redact_text(str(item.get("stderr_log") or ""))[:4000]
        stdout = redact_text(str(item.get("stdout_log") or ""))[:8000]
        output = "\n\n".join(part for part in (stdout, stderr) if part)
        if "PinnedDownloadError" in output and "ffmpeg" in output.lower():
            missing.update(("ffmpeg", "ffprobe"))
        missing.update(re.findall(r"ModuleNotFoundError: No module named ['\"]([a-zA-Z0-9_.-]+)", output))
        if item.get("exit_code") == 127:
            missing.add("command_unavailable")
        if re.search(r"FileNotFoundError: /usr/(?:local/)?bin/[A-Za-z0-9_.-]+", output):
            missing.add("command_unavailable")
        failures.append({"command_id": item.get("command_id"), "exit_code": item.get("exit_code"),
                         "status": item.get("status"), "stderr": stderr,
                         "stdout": stdout, "output": output})
    status = "passed" if tests and passed == len(tests) and value.get("succeeded") is True else "failed"
    if missing:
        status = "environment_blocked"
    return {"status": status, "passed": passed, "total": len(tests),
            "missing_dependencies": sorted(missing), "failures": failures,
            "test_results_hash": value.get("test_results_hash")}


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
