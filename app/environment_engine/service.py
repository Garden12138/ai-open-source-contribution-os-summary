"""Project contracts are projections of the existing immutable journal."""
from __future__ import annotations

from app.environment_engine.contracts import EnvironmentBinding
from app.environment_engine.resolver import resolve_contract
from app.provenance import content_hash, canonical_json


def inspect_contract(selected: dict, evidence: dict, commands: list[dict], *, task_id: str,
                     snapshot_hash: str, plan_hash: str, context: dict, configuration: dict,
                     expected_failures: dict | None = None) -> dict:
    """Read-only projection, suitable before approval and after a restart."""
    required = set(selected.get("manifests", []))
    if selected.get("ecosystem") == "python":
        prefix = "" if selected.get("project_directory", ".") == "." else selected["project_directory"] + "/"
        required.update(p for p in evidence["inventory"] if p == prefix + "Makefile"
            or p.startswith(prefix + ".github/workflows/") and p.endswith((".yml", ".yaml")))
    read = {e["path"] for e in evidence["entries"] if e.get("content") is not None}
    if required - read:
        return {**selected, "status": "pending_read", "required_paths": sorted(required - read),
            "environment_engine": configuration, "reason_code": "environment_evidence_missing",
            "reason": "通用环境契约需要补读冻结声明；尚未准备工具或运行基线。"}
    binding = EnvironmentBinding(task_id=task_id, snapshot_hash=snapshot_hash,
        plan_version_hash=plan_hash, approved_commands_hash=content_hash(commands),
        source_archive_hash=context["archive_hash"], base_commit_sha=context["base_sha"],
        target_platform=configuration["target_platform"], runner_image_digest=context["image"],
        sandbox_policy_hash=context["policy_hash"])
    contract = resolve_contract(binding, commands, selected, evidence)
    if expected_failures:
        from app.environment_engine.contracts import VerificationEnvironmentContract
        contract = VerificationEnvironmentContract.model_validate_json(
            canonical_json({**contract.model_dump(mode="json"), "expected_failures": expected_failures}))
    value = contract.model_dump(mode="json")
    if contract.unresolved:
        missing = sorted({p for gap in contract.unresolved if gap.reason_code == "environment_evidence_missing" for p in gap.paths})
        return {**selected, "status": "pending_read" if missing else "unsupported",
            "required_paths": missing, "environment_engine": configuration,
            "reason_code": contract.unresolved[0].reason_code,
            "reason": "环境命令依赖图尚未闭合：" + contract.unresolved[0].reason_code + "。不能开始编码；需补充冻结证据或生成新方案并重新确认。",
            "verification_contract": value, "verification_contract_hash": contract.contract_hash}
    # Full declaration closure becomes part of the exact preview and signed plan.
    manifests = sorted(set(selected["manifests"]) | {r.path for r in contract.evidence if r.path.endswith((".toml", ".txt", ".lock"))})
    if selected.get("ecosystem") == "node":
        from app.node_environments import LOCKS
        from pathlib import PurePosixPath
        manifests = sorted(set(manifests) | {r.path for r in contract.evidence if PurePosixPath(r.path).name in LOCKS})
        independent = contract.preparation.get("independent_directories", [])
        if independent:
            selected = {**selected, "node_environment": {**selected["node_environment"],
                "package_roots": sorted(set(selected["node_environment"]["package_roots"]) | set(independent))}}
    if len(manifests) > (64 if selected.get("ecosystem") == "node" else 16):
        return {**selected, "status": "unsupported", "reason_code": "environment_manifest_budget",
            "reason": "完整环境声明超过当前签名准备协议预算；不会省略声明。"}
    return {**selected, "manifests": manifests, "environment_engine": configuration,
        "verification_contract": value, "verification_contract_hash": contract.contract_hash,
        "readiness": {"static_admitted": True, "toolchain_prepared": "unverified", "baseline_verified": "unverified"}}
