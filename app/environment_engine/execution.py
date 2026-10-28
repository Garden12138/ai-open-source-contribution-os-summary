"""Bounded full-log facts derived before UI excerpts are truncated."""
from __future__ import annotations

import hashlib
import re
from app.security import redact_text


def execution_summary(stdout: bytes, stderr: bytes) -> dict:
    text = redact_text((stdout + b"\n" + stderr).decode("utf-8", "replace"))
    count = re.search(r"(?im)\bRan ([1-9]\d*) tests?\b|\b([1-9]\d*) passed\b|^# tests ([1-9]\d*)\b|Tests\s+([1-9]\d*) passed", text)
    environment = []
    if re.search(r"Read-only file system|EROFS", text):
        environment.append("immutable_cache_write_blocked")
    if re.search(r"ModuleNotFoundError:|ERR_DLOPEN_FAILED|Cannot find module|No such file or directory", text):
        environment.append("entrypoint_or_dependency_unavailable")
    if re.search(r"EAI_AGAIN|ENETUNREACH|ENOTFOUND", text):
        environment.append("network_required_in_verify")
    return {"version": "verification-log-summary-v1", "stdout_hash": hashlib.sha256(stdout).hexdigest(),
        "stderr_hash": hashlib.sha256(stderr).hexdigest(),
        "test_count": next((int(g) for g in count.groups() if g), None) if count else None,
        "zero_tests": bool(re.search(r"(?im)\bRan 0 tests\b|\bno tests (?:ran|found|collected)\b|^# tests 0\b", text)),
        "help_only": any(bool(re.search(r"(?im)^\s*usage:[^\n]*\n(?:\s*\n)?(?:options|commands|flags):", s.decode("utf-8", "replace"))) for s in (stdout, stderr)),
        "assertion_failure": bool(re.search(r"AssertionError|FAILED\s*\(|\bFAIL(?:ED)?\b|error\[|error TS\d+|Found \d+ errors?", text)),
        "environment_reason_codes": environment}
