"""Actual OCI environment measurement; no model, approval or business patch.

Input/output paths are explicit. Never load a production .env. H/D/U selection
and frozen source identities belong in a manifest BEFORE invoking this script.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import platform
import subprocess
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.environment_engine.service import inspect_contract
from app.environment_engine.probe import probe_prepared
from app.environment_engine.baseline import classify_baseline
from app.execution_environment import environment_plan
from app.execution_preflight import inspect_environment
from app.provenance import canonical_json, content_hash
from app.sandbox_worker.dependencies import (DependencyManifest, DependencyPreparationPlan,
    DependencyPlanSigner, DependencyProxyPolicy, DockerDependencyRuntime, DockerDependencyProxyRuntime)
from app.sandbox_worker.explore import RepositoryArchive
from app.sandbox_worker.implementation import DockerImplementRuntime
from app.sandbox_worker.planning_context import DockerPlanningContextRuntime
from app.sandbox_worker.specs import SandboxCommand, SandboxPolicy
from app.sandbox_worker.verification import DockerVerifyRuntime


def frozen_inventory(archive: RepositoryArchive, scope: tuple[str, ...]) -> str:
    """Read archive bytes only; never extract files or execute repository code."""
    entries = []
    with tarfile.open(archive.path) as bundle:
        for member in bundle:
            parts = member.name.split("/", 1)
            if len(parts) != 2 or not parts[1] or member.isdir():
                continue
            path = parts[1]
            if scope and not any(path == p or path.startswith(p + "/") for p in scope):
                continue
            if member.issym():
                entries.append({"path": path, "type": "symlink", "target": member.linkname})
            elif member.isfile():
                if member.size > 16 * 1024 * 1024:
                    raise ValueError("acceptance_archive_file_budget")
                body = bundle.extractfile(member).read()
                entries.append({"path": path, "type": "file", "size": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(), "executable": bool(member.mode & 0o111)})
            else:
                raise ValueError("acceptance_archive_type_unsupported")
    return content_hash(sorted(entries, key=lambda e: e["path"]))


async def measure(manifest: dict, target: Path) -> dict:
    target.mkdir(parents=True, exist_ok=False)
    def save(name: str, data: dict) -> None:
        from app.security import ensure_no_sensitive_data
        ensure_no_sensitive_data(data, context="upeve acceptance")
        (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    engine_files = [*Path("app").rglob("*.py"), Path("pyproject.toml"), Path(__file__)]
    engine_source_hash = content_hash({str(p.relative_to(Path.cwd()) if p.is_absolute() else p):
        hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(engine_files)})
    save("manifest.json", {**{k: v for k, v in manifest.items() if k != "archive_path"},
        "engine_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "engine_source_hash": engine_source_hash, "measured_at": datetime.now(timezone.utc).isoformat(),
        "host_platform": {"system": platform.system(), "architecture": platform.machine()}})
    archive = RepositoryArchive.capture(repository_full_name=manifest["repository"], base_commit_sha=manifest["base_sha"],
        path=Path(manifest["archive_path"]), allowed_root=Path(manifest["archive_path"]).parent)
    if archive.archive_hash != manifest["archive_hash"]:
        raise ValueError("acceptance_archive_hash_mismatch")
    env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"}}
    policy = SandboxPolicy()
    if manifest["policy_hash"] != policy.policy_hash:
        raise ValueError("acceptance_policy_mismatch")
    inspector = DockerPlanningContextRuntime(docker_environment=env)
    evidence = await inspector.inspect(archive=archive, image_digest=manifest["image"], policy=policy, query="", paths=())
    context = {"archive_hash": archive.archive_hash, "base_sha": archive.base_commit_sha,
        "image": manifest["image"], "policy_hash": policy.policy_hash}
    for _ in range(64):
        selected = inspect_environment(environment_plan(evidence["inventory"], change_paths=manifest["change_paths"]), evidence, engine_version="upeve-v1")
        if selected["status"] == "supported":
            selected = inspect_contract(selected, evidence, manifest["commands"], task_id=manifest["sample_id"],
                snapshot_hash=manifest["snapshot_hash"], plan_hash=manifest["plan_hash"], context=context,
                configuration={"version": "upeve-v1", "target_platform": manifest["target_platform"]})
        if selected["status"] != "pending_read":
            break
        paths = selected["required_paths"][:32]
        extra = await inspector.inspect(archive=archive, image_digest=manifest["image"], policy=policy, query="", paths=tuple(paths))
        old = {e["path"]: e for e in evidence["entries"]}
        old.update({e["path"]: e for e in extra["entries"]})
        evidence["entries"] = list(old.values())
        if any(old.get(p, {}).get("content") is None for p in paths):
            break
    save("command-graph.json", selected)
    result = {"classification": manifest["classification"], "native": False, "model_calls": 0,
        "operator_business_patches": 0, "external_writes": 0, "status": selected["status"],
        "contract_hash": selected.get("verification_contract_hash"), "reason_code": selected.get("reason_code")}
    if selected["status"] != "supported":
        save("classification.json", result)
        return result
    scope = tuple(manifest.get("workspace_paths", ()))
    materializer = DockerImplementRuntime(docker_environment=env).for_workspace_scope(scope)
    runtime, proxy = DockerDependencyRuntime(docker_environment=env), DockerDependencyProxyRuntime(docker_environment=env)
    verifier = DockerVerifyRuntime(docker_environment=env)
    inventory = frozen_inventory(archive, scope)
    workspace = await materializer.create_workspace(image_digest=manifest["image"], policy=policy)
    lease = dependency = None
    try:
        await materializer.materialize(workspace=workspace, archive=archive, policy=policy)
        workspace = replace(workspace, inventory_hash=inventory)
        proxy_policy = DependencyProxyPolicy.for_ecosystem(selected["ecosystem"])
        # New protocol: actual source, graph and exact commands are signed together.
        plan = DependencyPreparationPlan(plan_id=manifest["sample_id"], execution_attempt_id=manifest["sample_id"],
            implement_result_hash=content_hash({"archive_hash": archive.archive_hash, "inventory_hash": inventory}),
            workspace_inventory_hash=inventory, ecosystem=selected["ecosystem"],
            manifests=tuple(DependencyManifest(p, next(e["prior_hash"] for e in evidence["entries"] if e["path"] == p)) for p in selected["manifests"]),
            runner_image_digest=manifest["image"], sandbox_policy_hash=policy.policy_hash,
            proxy_policy_hash=proxy_policy.policy_hash, disk_bytes=policy.disk_bytes,
            version="workspace-dependency-plan-v6", source_kind="explore",
            project_directory=selected.get("project_directory", "."), preparation_version=selected["preparation_version"],
            metadata_strategy=selected.get("metadata_strategy", "declarative-v1"),
            node_environment=selected.get("node_environment"), environment_contract=selected["verification_contract"])
        signer = DependencyPlanSigner(key_id="upeve-acceptance", signing_key=os.urandom(32))
        save("dependency-plan.json", plan.to_wire())
        lease = await proxy.start(image_digest=manifest["image"], policy=proxy_policy)
        dependency = await runtime.prepare(signed_plan=signer.sign(plan), signer=signer,
            repository_volume=workspace.volume_name, proxy_policy=proxy_policy, proxy_lease=lease, sandbox_policy=policy)
        await proxy.stop(lease, ignore_missing=True)
        lease = None
        save("dependency-result.json", dependency.result.hash_payload())
        receipt, probes = await probe_prepared(verifier, contract=selected["verification_contract"],
            dependency=dependency, workspace=workspace, policy=policy)
        save("preparation-receipt.json", receipt.model_dump(mode="json"))
        save("entrypoint-receipts.json", probes)
        if receipt.status != "prepared":
            result.update(status="environment_blocked", reason_code="entrypoint_unproven")
        else:
            inspection = await verifier.run(workspace=workspace, commands=tuple(SandboxCommand.from_wire(c) for c in manifest["commands"]),
                policy=policy, dependency_workspace=dependency.workspace)
            wire = {"before_inventory_hash": inspection.before_inventory_hash, "after_inventory_hash": inspection.after_inventory_hash,
                "command_evidence": [e.to_wire() for e in inspection.command_evidence]}
            assert wire["before_inventory_hash"] == wire["after_inventory_hash"] == inventory
            save("baseline-results.json", wire)
            verdict = classify_baseline(selected["verification_contract"], content_hash(receipt.model_dump(mode="json")),
                wire["command_evidence"], content_hash(wire))
            save("baseline-verdict.json", verdict.model_dump(mode="json"))
            result.update(status=verdict.outcome, reason_codes=list(verdict.reason_codes),
                executed=verdict.executed_command_count, required=verdict.required_command_count)
    except Exception as exc:
        # No raw external errors, credentials or subprocess output in evidence.
        result.update(status="failed", reason_code=getattr(exc, "code", "acceptance_" + type(exc).__name__))
        if getattr(exc, "diagnostic", None):
            result["diagnostic"] = exc.diagnostic
        save("classification.json", result)
        raise
    finally:
        if lease:
            await proxy.stop(lease, ignore_missing=True)
        if dependency:
            await runtime.destroy(dependency.workspace, ignore_missing=True)
        await materializer.destroy(workspace)
    save("classification.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(canonical_json(asyncio.run(measure(json.loads(args.manifest.read_text()), args.output))))
