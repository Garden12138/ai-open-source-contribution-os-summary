"""Versioned, data-only cache capability; source/dependency code stays read-only."""
from __future__ import annotations

from app.sandbox_worker.specs import SandboxPolicy

MYPY_CACHE_CAPABILITY_V1 = {
    "version": "python-data-cache-v1", "source": "platform_capability_registry",
    "distribution": "mypy", "distribution_version": "2.4.0",
    "directory": ".mypy_cache", "max_bytes": 16 * 1024 * 1024,
}
MYPY_CACHE_CAPABILITY = {
    "version": "python-data-cache-v2", "source": "platform_capability_registry",
    "distribution": "mypy", "distribution_versions": ["2.4.0", "2.4.1"],
    "directory": ".mypy_cache", "max_bytes": 16 * 1024 * 1024,
}


def python_cache_tmpfs(policy: SandboxPolicy) -> str:
    size = min(16 * 1024 * 1024, policy.disk_bytes // 8, policy.memory_bytes // 4)
    return (f"/workspace/.mypy_cache:rw,noexec,nosuid,nodev,size={size},mode=0700,"
        f"uid={policy.run_as_uid},gid={policy.run_as_gid}")


def validate_python_cache(value: dict) -> dict:
    import re
    capability = {"version": "python-data-cache-v2", "source": "platform_capability_registry",
        "distribution": "mypy", "distribution_versions": ["2.4.0", "2.4.1"],
        "directory": ".mypy_cache", "max_bytes": 16 * 1024 * 1024}
    if isinstance(value, dict) and value.get("version") == "python-data-cache-v1":
        capability = {"version": "python-data-cache-v1", "source": "platform_capability_registry",
            "distribution": "mypy", "distribution_version": "2.4.0",
            "directory": ".mypy_cache", "max_bytes": 16 * 1024 * 1024}
    if (not isinstance(value, dict) or set(value) != set(capability) | {"contract_hash", "plan_hash", "distribution_version"}
            or any(value.get(k) != v for k, v in capability.items())
            or value.get("distribution_version") not in capability.get("distribution_versions", ["2.4.0"])
            or any(not isinstance(value.get(k), str) or not re.fullmatch(r"[a-f0-9]{64}", value[k])
                   for k in ("contract_hash", "plan_hash"))):
        raise ValueError("python_data_cache_binding_invalid")
    return value


def record_python_cache(plan: dict, output: str) -> dict | None:
    """Trusted preparation supervisor; inspect metadata, never import project code."""
    import glob
    import hashlib
    import importlib.metadata
    import json
    import os
    contract = plan.get("environment_contract") or {}
    requested = contract.get("preparation", {}).get("cache_policy")
    if requested is None:
        return None
    contract_hash = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    sites = glob.glob(output + "/venv/lib/python*/site-packages")
    installed = [d for d in importlib.metadata.distributions(path=sites)
                 if (d.metadata.get("Name") or "").lower() == "mypy"]
    if len(installed) != 1 or installed[0].version not in requested.get("distribution_versions", [requested.get("distribution_version")]):
        raise ValueError("python_data_cache_tool_version_unsupported")
    record = validate_python_cache({**requested, "distribution_version": installed[0].version,
        "contract_hash": contract_hash, "plan_hash": plan_hash})
    target = output + "/python-data-cache.json"
    if os.path.lexists(target):
        raise ValueError("python_data_cache_receipt_conflict")
    with open(target, "x", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True, separators=(",", ":"))
    return record
