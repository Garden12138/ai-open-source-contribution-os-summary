"""Fail closed on a missing/stale preparation or baseline provenance chain."""
from __future__ import annotations

from app.environment_engine.contracts import VerificationEnvironmentContract, ToolchainPreparationReceipt
from app.environment_engine.baseline import classify_baseline
from app.provenance import canonical_json, content_hash
from app.workbench import WorkbenchError


def require_environment_evidence(wb, attempt, authorization, baseline: dict) -> dict[str, str]:
    try:
        expected = authorization.payload["environment"]["verification_contract_hash"]
        if baseline["version"] != "execution-baseline-v2" or baseline["environment_contract_hash"] != expected:
            raise ValueError("environment_contract_stale")
        contract = VerificationEnvironmentContract.model_validate_json(canonical_json(wb.read(expected)))
        receipt_hash = baseline["preparation_receipt_hash"]
        receipt = ToolchainPreparationReceipt.model_validate_json(canonical_json(wb.read(receipt_hash)))
        binding = contract.binding
        spec = wb.read(baseline["environment_spec_hash"])
        dependency = wb.read(baseline["dependency_result_hash"])
        expected_entries = {(c.command_id, cwd, name) for c in contract.commands
            for cwd, name in (c.entrypoints or ((c.working_directory, c.argv[0]),))}
        actual_entries = {(e.command_id, e.working_directory, e.requested_executable) for e in receipt.executables}
        commands = {c.command_id: c for c in contract.commands}
        if (binding.plan_version_hash != authorization.payload["plan_hash"]
                or binding.snapshot_hash != attempt.snapshot_inputs_hash
                or binding.base_commit_sha != attempt.base_commit_sha
                or binding.task_id != attempt.task_id
                or binding.source_archive_hash != attempt.repository_archive_hash
                or binding.runner_image_digest != attempt.runner_image_digest
                or binding.sandbox_policy_hash != attempt.sandbox_policy_hash
                or receipt.contract_hash != expected or receipt.status != "prepared"
                or receipt.target_platform != binding.target_platform
                or receipt.image_digest != ((spec.get("builder_image_digest") or binding.runner_image_digest)
                    if spec.get("node_environment") else binding.runner_image_digest)
                or receipt.policy_hash != binding.sandbox_policy_hash
                or receipt.dependency_result_hash != baseline["dependency_result_hash"]
                or receipt.dependency_plan_hash != content_hash(spec)
                or dependency["plan_hash"] != content_hash(spec)
                or content_hash(spec["environment_contract"]) != expected
                or actual_entries != expected_entries or len(actual_entries) != len(receipt.executables)
                or any(e.command_id not in commands or e.argv != commands[e.command_id].argv for e in receipt.executables)):
            raise ValueError("environment_receipt_stale")
        evidence = baseline["inspection"]
        verdict = classify_baseline(contract.model_dump(mode="json"), receipt_hash,
            evidence["command_evidence"], content_hash(evidence))
        if verdict.model_dump(mode="json") != baseline["baseline_verdict"] or verdict.outcome not in {"passed", "expected_issue_reproduction"}:
            raise ValueError("environment_baseline_unproven")
        return {"contract_hash": expected, "preparation_receipt_hash": receipt_hash,
            "baseline_evidence_hash": verdict.baseline_artifact_hash,
            "entrypoint_evidence_hash": baseline["entrypoint_evidence_hash"]}
    except (ValueError, TypeError, KeyError):
        raise WorkbenchError("环境、入口或基线证据已过期或不完整；需要新的冻结准备与授权。", code="environment_stale") from None
