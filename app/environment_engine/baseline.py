"""Conservative evidence classification, separate from a successful Job."""
from __future__ import annotations

import re
import shlex
from app.environment_engine.contracts import BaselineVerdict
from app.provenance import content_hash


def classify_baseline(contract: dict, preparation_receipt_hash: str, evidence: list[dict], baseline_hash: str) -> BaselineVerdict:
    commands = contract["commands"]
    results, reasons = [], set()
    environment = False
    assertion = False
    reproduced = set()
    for command in commands:
        matches = [e for e in evidence if e.get("command_id") == command["command_id"]]
        exact = {k: command[k] for k in ("command_id", "argv", "working_directory")}
        if len(matches) != 1 or matches[0].get("command_hash") != content_hash(exact):
            reasons.add("baseline_evidence_insufficient")
            continue
        item = matches[0]
        summary = item.get("execution_summary") or {}
        summary_valid = (summary.get("version") == "verification-log-summary-v1"
            and summary.get("stdout_hash") == item.get("stdout_hash")
            and summary.get("stderr_hash") == item.get("stderr_hash"))
        text = str(item.get("stdout_log", "")) + "\n" + str(item.get("stderr_log", ""))
        codes = []
        actual_assertion = False
        # Manager argv can hide a help-only leaf in package.json. Resolve that
        # frozen static chain too; exit zero from the manager proves no checks.
        effective = [command["argv"]]
        typed_leaves = command.get("leaf_commands")
        leaves = [leaf["argv"] for leaf in typed_leaves] if typed_leaves is not None else []
        if typed_leaves is None:
            for edge in command.get("chain", []):
                directory, separator, text_edge = edge.partition(":")
                if separator and "#" not in directory:
                    try:
                        leaf = shlex.split(text_edge)
                    except ValueError:
                        leaf = []
                    if leaf:
                        leaves.append(leaf)
        effective.extend(leaves)
        started = item.get("started_at") is not None and item.get("status") not in {"not_run", "cancelled"}
        if not started or item.get("raw_logs_omitted") or item.get("logs_truncated") and not summary_valid:
            codes.append("baseline_evidence_insufficient")
        if (item.get("status") not in {"passed", "failed"}
                or (item.get("status") == "passed") != (item.get("exit_code") == 0)):
            codes.append("baseline_evidence_insufficient")
        if any(flag in args for args in effective for flag in ("--help", "-h", "--version", "-V")) or summary_valid and summary.get("help_only") or re.search(r"(?im)^\s*usage:.*\n.*(?:options|commands|flags):", text):
            codes.append("baseline_help_only")
        if typed_leaves is not None and not leaves or leaves and all(leaf[0] in {"echo", "printf", "true"} for leaf in leaves):
            codes.append("baseline_evidence_insufficient")
        if summary_valid and summary.get("zero_tests") or re.search(r"(?im)\bRan 0 tests\b|\bno tests (?:ran|found|collected)\b|^# tests 0\b|\b0 passed\b", text):
            codes.append("baseline_zero_tests")
        if item.get("exit_code") in {126, 127} or re.search(r"ModuleNotFoundError:|ERR_DLOPEN_FAILED|EAI_AGAIN|ENETUNREACH|EROFS|ENOSPC|Cannot find module", text):
            codes.append("environment_command_unavailable")
            environment = True
        if summary_valid and summary.get("environment_reason_codes"):
            codes.extend(summary["environment_reason_codes"])
            environment = True
        if item.get("exit_code") not in {None, 0}:
            if summary_valid and summary.get("assertion_failure") or re.search(r"AssertionError|FAILED\s*\(|\bFAIL(?:ED)?\b|error\[|error TS\d+|Found \d+ errors?", text):
                assertion = True
                actual_assertion = True
                codes.append("baseline_actual_assertion_failure")
            elif not codes:
                codes.append("baseline_failure_unclassified")
        expectation = contract.get("expected_failures", {}).get(command["command_id"])
        if (expectation and item.get("exit_code") == expectation["exit_code"]
                and expectation["test_node"] in command["argv"] and expectation["test_node"] in text
                and expectation["failure_literal"] in text and codes == ["baseline_actual_assertion_failure"]):
            reproduced.add(command["command_id"])
            codes = []
        # Test commands need positive execution evidence, even with exit zero.
        test_command = any(t in args for args in effective for t in ("pytest", "unittest", "vitest", "jest", "test", "--test"))
        count = re.search(r"(?im)\bRan ([1-9]\d*) tests?\b|\b([1-9]\d*) passed\b|^# tests ([1-9]\d*)\b|Tests\s+([1-9]\d*) passed", text)
        test_count = summary.get("test_count") if summary_valid else next((int(g) for g in count.groups() if g), None) if count else None
        if test_command and not test_count and not actual_assertion:
            codes.append("baseline_evidence_insufficient")
        reasons.update(codes)
        results.append({"command_id": command["command_id"], "command_hash": item["command_hash"],
            "exit_code": item.get("exit_code"), "executed": started, "reason_codes": codes,
            "test_count": test_count,
            "stdout_hash": item.get("stdout_hash"), "stderr_hash": item.get("stderr_hash")})
    if len(evidence) != len(commands):
        reasons.add("baseline_evidence_insufficient")
    if environment:
        outcome = "environment_blocked"
    elif not reasons and reproduced and len(results) == len(commands) and all(
            r["command_id"] in reproduced or r["exit_code"] == 0 for r in results):
        outcome = "expected_issue_reproduction"
    elif not reasons and all(r["exit_code"] == 0 for r in results) and len(results) == len(commands):
        outcome = "no_change" if contract.get("expected_failures") else "passed"
        if outcome == "no_change":
            reasons.add("issue_not_reproduced_on_frozen_source")
    elif assertion and reasons <= {"baseline_actual_assertion_failure"}:
        outcome = "preexisting_failure"
    else:
        outcome = "unknown"
    return BaselineVerdict(contract_hash=content_hash(contract), preparation_receipt_hash=preparation_receipt_hash,
        baseline_artifact_hash=baseline_hash, required_command_count=len(commands),
        executed_command_count=sum(r["executed"] for r in results), outcome=outcome,
        reason_codes=tuple(sorted(reasons)), command_results=tuple(results))
