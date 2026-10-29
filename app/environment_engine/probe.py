"""Fixed entrypoint probe, executed only by the existing offline verifier."""
from __future__ import annotations

from inspect import getsource
from app.environment_engine.contracts import ExecutableReceipt, ToolchainPreparationReceipt
from app.provenance import content_hash, canonical_json
from app.sandbox_worker.specs import SandboxCommand


def probe_entrypoints(configuration: dict) -> dict:
    """Self-contained container program: metadata, PATH and actual invocation."""
    import importlib.metadata
    import importlib.machinery
    import base64
    import hashlib
    import json
    import os
    import platform
    import shutil
    import subprocess
    import sys
    import sysconfig
    entries = []
    actual_platform = "linux/" + {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine(), "unknown")
    for command in configuration["commands"]:
        requested = command.get("entrypoints") or [[command["working_directory"], command["argv"][0]]]
        for cwd, name in requested:
            path = os.environ.get("PATH", "")
            # A package manager supplies package-local bins while dispatching
            # scripts. Probe those exact bins, inside the frozen dependency tree.
            if configuration["ecosystem"] == "node":
                path = "/dependencies/output/project/" + ("" if cwd == "." else cwd + "/") + "node_modules/.bin:" + path
            executable = shutil.which(name, path=path)
            reason = None
            distribution = version = None
            executable_hash = ownership = None
            if actual_platform != configuration["target_platform"]:
                reason = "platform_binary_unavailable"
            elif executable is None:
                reason = "entrypoint_missing"
            elif command["distributions"]:
                distribution = command["distributions"][0]
                try:
                    package = importlib.metadata.distribution(distribution)
                    version = package.version
                    if name not in {"python", "python3"}:
                        # Native wheels may install .data/scripts instead of
                        # console_scripts. Verify actual ownership AND RECORD
                        # integrity, never infer ownership from the CLI name.
                        owned = [f for f in package.files or [] if os.path.realpath(package.locate_file(f)) == os.path.realpath(executable)]
                        if not executable.startswith("/dependencies/output/venv/bin/") or len(owned) != 1 or not owned[0].hash or owned[0].hash.mode != "sha256":
                            reason = "entrypoint_distribution_mismatch"
                        else:
                            with open(executable, "rb") as handle:
                                digest = hashlib.file_digest(handle, "sha256").digest()
                            executable_hash = digest.hex()
                            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != owned[0].hash.value:
                                reason = "entrypoint_distribution_mismatch"
                            ownership = "wheel_record"
                except importlib.metadata.PackageNotFoundError:
                    reason = "entrypoint_distribution_missing"
            module = command.get("required_module")
            if reason is None and module:
                # Resolve as the real approved -m command would, without
                # importing repository code. Reject source-shadowed test tools.
                search = ["/workspace/" + cwd, *os.environ.get("PYTHONPATH", "").split(os.pathsep), *sys.path]
                spec = importlib.machinery.PathFinder.find_spec(module.split(".")[0], [p for p in search if p])
                origin = os.path.realpath(spec.origin) if spec and spec.origin else ""
                if not origin or origin.startswith(("/workspace/", "/tmp/")):
                    reason = "entrypoint_module_unavailable_or_shadowed"
                elif distribution:
                    owned = [f for f in package.files or [] if os.path.realpath(package.locate_file(f)) == origin]
                    if len(owned) != 1:
                        reason = "entrypoint_distribution_mismatch"
                elif not origin.startswith(sysconfig.get_path("stdlib") + "/"):
                    reason = "entrypoint_module_unavailable_or_shadowed"
            if reason is None:
                argv = [executable, "--version" if name in {"npm", "pnpm", "yarn", "bun", "node"} else "--help"]
                if name in {"python", "python3"}:
                    argv = [executable, "-m", module, "--help"] if module else [executable, "-I", "-c", "import sys; assert sys.version_info >= (3,11)"]
                try:
                    # Help is an ENTRYPOINT invocation only. It is never a
                    # baseline result, test execution or permission to code.
                    done = subprocess.run(argv, cwd="/workspace/" + cwd, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
                        env={**os.environ, "PATH": path})
                    if done.returncode:
                        reason = "platform_binary_unavailable"
                    elif executable_hash is None:
                        with open(executable, "rb") as handle:
                            executable_hash = hashlib.file_digest(handle, "sha256").hexdigest()
                        ownership = "frozen_dependency_inventory" if executable.startswith("/dependencies/output/") else "runner_image"
                except (OSError, subprocess.TimeoutExpired):
                    reason = "platform_binary_unavailable"
            entries.append({"command_id": command["command_id"], "argv": command["argv"],
                "working_directory": cwd, "executable": executable, "requested_executable": name, "distribution": distribution,
                "distribution_version": version, "executable_hash": executable_hash, "ownership": ownership,
                "status": "missing" if reason else "available", "reason_code": reason})
    return {"target_platform": actual_platform, "executables": entries}


async def probe_prepared(verifier, *, contract: dict, dependency, workspace, policy) -> tuple[ToolchainPreparationReceipt, dict]:
    """No new runner authority: use the same networkless, read-only verifier."""
    import json
    configuration = {"ecosystem": dependency.result.ecosystem.value,
        "target_platform": contract["binding"]["target_platform"], "commands": contract["commands"]}
    script = getsource(probe_entrypoints) + "\nimport json\nprint(json.dumps(probe_entrypoints(" + repr(configuration) + "),sort_keys=True))"
    # Respect the existing argv bounds; this is fixed trusted supervisor code,
    # never a model/repository-authored command or a relaxed shell interface.
    chunks = tuple(script[i:i + 1800] for i in range(0, len(script), 1800))
    check = SandboxCommand(command_id="upeve-entrypoint-probe", argv=("python3", "-I", "-c", "import sys;exec(''.join(sys.argv[1:]))", *chunks))
    inspection = await verifier.run(workspace=workspace, commands=(check,), policy=policy, dependency_workspace=dependency.workspace)
    event = inspection.command_evidence[0]
    if event.exit_code != 0 or event.logs_truncated or event.raw_logs_omitted:
        raise ValueError("entrypoint_probe_unproven")
    data = json.loads(event.stdout_log)
    receipt = ToolchainPreparationReceipt(contract_hash=content_hash(contract),
        dependency_plan_hash=dependency.result.plan_hash, dependency_result_hash=dependency.result.result_hash,
        target_platform=data["target_platform"], image_digest=dependency.workspace.execution_image_digest or contract["binding"]["runner_image_digest"],
        policy_hash=policy.policy_hash, executables=tuple(ExecutableReceipt.model_validate_json(canonical_json(e)) for e in data["executables"]),
        data_cache=dependency.workspace.python_cache,
        status="prepared" if data["executables"] and all(e["status"] == "available" for e in data["executables"]) else "partial")
    if inspection.before_inventory_hash != inspection.after_inventory_hash or inspection.before_inventory_hash != workspace.inventory_hash:
        raise ValueError("entrypoint_source_changed")
    return receipt, {"command_evidence": [e.to_wire() for e in inspection.command_evidence],
        "before_inventory_hash": inspection.before_inventory_hash, "after_inventory_hash": inspection.after_inventory_hash}
