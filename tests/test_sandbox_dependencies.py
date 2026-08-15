from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from app.provenance import content_hash
from app.sandbox_worker.implementation import DisposableWorkspace
from app.sandbox_worker.dependencies import (
    DEPENDENCY_PROXY_POLICY_VERSION,
    DependencyEcosystem,
    DependencyManifest,
    DependencyPlanSigner,
    DependencyPreparationError,
    DependencyPreparationPlan,
    DependencyProxyLease,
    DependencyProxyPolicy,
    DependencyWorkspace,
    DockerDependencyProxyRuntime,
    DockerDependencyRuntime,
)
from app.sandbox_worker.specs import SandboxCommand, SandboxPolicy
from app.sandbox_worker.verification import DockerVerifyRuntime


IMAGE = "sha256:" + "1" * 64
HASH = "2" * 64


def _plan(
    policy: SandboxPolicy,
    proxy: DependencyProxyPolicy,
    *,
    ecosystem: DependencyEcosystem = DependencyEcosystem.PYTHON,
) -> DependencyPreparationPlan:
    manifests = (
        DependencyManifest(
            path="requirements.lock",
            content_hash="3" * 64,
        ),
    )
    if ecosystem is DependencyEcosystem.NODE:
        manifests = (
            DependencyManifest(
                path="package-lock.json",
                content_hash="4" * 64,
            ),
            DependencyManifest(
                path="package.json",
                content_hash="5" * 64,
            ),
        )
    return DependencyPreparationPlan(
        plan_id="dependency-plan-1",
        execution_attempt_id="attempt-1",
        implement_result_hash=HASH,
        workspace_inventory_hash="6" * 64,
        ecosystem=ecosystem,
        manifests=manifests,
        runner_image_digest=IMAGE,
        sandbox_policy_hash=policy.policy_hash,
        proxy_policy_hash=proxy.policy_hash,
        disk_bytes=policy.disk_bytes,
    )


def _lease(proxy: DependencyProxyPolicy) -> DependencyProxyLease:
    return DependencyProxyLease(
        network_name="contribos-dependency-network-" + "7" * 32,
        container_name="contribos-dependency-proxy-" + "8" * 32,
        proxy_url=proxy.proxy_url,
        policy_hash=proxy.policy_hash,
    )


def test_dependency_plan_is_signed_and_proxy_allowlist_is_fixed() -> None:
    policy = SandboxPolicy()
    python_proxy = DependencyProxyPolicy.for_ecosystem("python")
    node_proxy = DependencyProxyPolicy.for_ecosystem("node")
    signer = DependencyPlanSigner(
        key_id="dependency-signing-key",
        signing_key=b"d" * 32,
    )
    plan = _plan(policy, python_proxy)
    signed = signer.sign(plan)

    assert python_proxy.version == DEPENDENCY_PROXY_POLICY_VERSION
    assert python_proxy.allowed_upstream_hosts == (
        "files.pythonhosted.org",
        "pypi.org",
    )
    assert node_proxy.allowed_upstream_hosts == ("registry.npmjs.org",)
    assert signer.verify(signed) == plan
    assert b"d" * 32 not in signed.to_bytes()

    with pytest.raises(ValueError, match="allowlist"):
        replace(
            python_proxy,
            allowed_upstream_hosts=(
                "files.pythonhosted.org",
                "metadata.google.internal",
                "pypi.org",
            ),
        )
    with pytest.raises(ValueError, match="signature"):
        signer.verify(replace(signed, signature="f" * 64))
    with pytest.raises(ValueError, match="package.json"):
        replace(
            _plan(
                policy,
                node_proxy,
                ecosystem=DependencyEcosystem.NODE,
            ),
            manifests=(
                DependencyManifest(
                    path="package-lock.json",
                    content_hash="4" * 64,
                ),
            ),
        )


def test_dependency_worker_uses_internal_proxy_and_separate_writable_volume(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy()
    proxy = DependencyProxyPolicy.for_ecosystem("python")
    lease = _lease(proxy)
    plan_path = tmp_path / "dependency-plan.json"
    plan_path.write_text("{}\n", encoding="utf-8")
    dependencies = DependencyWorkspace(
        workspace_id="dependencies:fixture",
        volume_name="contribos-dependencies-" + "9" * 32,
        runner_image_digest=IMAGE,
        sandbox_policy_hash=policy.policy_hash,
        plan_hash=HASH,
    )
    runtime = DockerDependencyRuntime(
        docker_environment={"PATH": "/usr/bin", "HOME": "/tmp/worker"}
    )
    argv = runtime.build_prepare_argv(
        repository_volume="contribos-workspace-" + "a" * 32,
        dependency_workspace=dependencies,
        signed_plan_path=str(plan_path),
        proxy_lease=lease,
        policy=policy,
        container_name="contribos-dependency-worker-" + "b" * 32,
    )
    joined = " ".join(argv)

    for expected in (
        f"--network {lease.network_name}",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges=true",
        "--user 65532:65532",
        "dst=/workspace,readonly",
        (
            f"type=volume,src={dependencies.volume_name},"
            "dst=/dependencies"
        ),
        "dst=/input/dependency-plan.json,readonly",
        "HTTP_PROXY=http://dependency-proxy:8080",
        "HTTPS_PROXY=http://dependency-proxy:8080",
        "PIP_CONFIG_FILE=/dev/null",
        "NPM_CONFIG_USERCONFIG=/dev/null",
        "GIT_CONFIG_NOSYSTEM=1",
        "core.hooksPath",
        "protocol.allow",
    ):
        assert expected in joined
    assert "--network none" not in joined
    assert joined.count("type=volume") == 2
    assert "/var/run/docker.sock" not in joined
    for forbidden in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "SSH_AUTH_SOCK",
    ):
        assert forbidden not in joined

    with pytest.raises(ValueError, match="disallowed"):
        DockerDependencyRuntime(
            docker_environment={
                "PATH": "/usr/bin",
                "GITHUB_TOKEN": "github_pat_dependencycanary12345678",
            }
        )


def test_dependency_proxy_is_the_only_outbound_component() -> None:
    policy = DependencyProxyPolicy.for_ecosystem("python")
    runtime = DockerDependencyProxyRuntime(
        docker_environment={"PATH": "/usr/bin", "HOME": "/tmp/worker"}
    )
    argv = runtime.build_proxy_argv(
        image_digest=IMAGE,
        policy=policy,
        container_name="contribos-dependency-proxy-" + "c" * 32,
    )
    joined = " ".join(argv)

    for expected in (
        "--network bridge",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges=true",
        "--user 65532:65532",
        "files.pythonhosted.org,pypi.org",
    ):
        assert expected in joined
    assert "169.254.169.254" not in joined
    assert "host.docker.internal" not in joined
    assert "/var/run/docker.sock" not in joined


class _FakeDependencyRuntime(DockerDependencyRuntime):
    def __init__(self) -> None:
        super().__init__(docker_environment={"PATH": "/usr/bin"})
        self.commands: list[tuple[str, ...]] = []
        self.prepares: list[tuple[str, ...]] = []
        self.fail_prepare = False

    async def _command(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> bytes:
        self.commands.append(tuple(argv))
        return b"fixture\n"

    async def _run(
        self,
        argv: tuple[str, ...],
        *,
        container_name: str,
        timeout_seconds: int,
    ) -> bytes:
        self.prepares.append(tuple(argv))
        if self.fail_prepare:
            raise DependencyPreparationError("fixture failure")
        return (
            b'{"file_count":3,"inventory_hash":"'
            + b"e" * 64
            + b'","total_bytes":1024}\n'
        )


def test_dependency_preparation_result_is_hash_bound_and_failure_cleans_volume(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy()
    proxy = DependencyProxyPolicy.for_ecosystem("python")
    plan = _plan(policy, proxy)
    signer = DependencyPlanSigner(
        key_id="dependency-runtime-key",
        signing_key=b"r" * 32,
    )
    runtime = _FakeDependencyRuntime()
    execution = asyncio.run(
        runtime.prepare(
            signed_plan=signer.sign(plan),
            signer=signer,
            repository_volume="contribos-workspace-" + "a" * 32,
            proxy_policy=proxy,
            proxy_lease=_lease(proxy),
            sandbox_policy=policy,
        )
    )

    assert execution.result.plan_hash == plan.plan_hash
    assert execution.result.dependency_inventory_hash == "e" * 64
    assert execution.workspace.inventory_hash == "e" * 64
    assert execution.result.result_hash
    assert runtime.prepares
    assert any(command[1:3] == ("volume", "create") for command in runtime.commands)

    runtime.fail_prepare = True
    with pytest.raises(DependencyPreparationError, match="fixture"):
        asyncio.run(
            runtime.prepare(
                signed_plan=signer.sign(plan),
                signer=signer,
                repository_volume="contribos-workspace-" + "a" * 32,
                proxy_policy=proxy,
                proxy_lease=_lease(proxy),
                sandbox_policy=policy,
            )
        )
    assert any(
        command[1:4] == ("volume", "rm", "--force")
        for command in runtime.commands
    )


@pytest.mark.skipif(
    os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
def test_real_dependency_proxy_and_hash_locked_preparation() -> None:
    image = os.environ.get("CONTRIBOS_SANDBOX_RUNNER_IMAGE", "")
    if not image.startswith("sha256:"):
        pytest.fail("CONTRIBOS_SANDBOX_RUNNER_IMAGE must be a local image digest")
    docker_environment = {
        name: os.environ[name]
        for name in (
            "DOCKER_CERT_PATH",
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "HOME",
            "PATH",
            "TMPDIR",
        )
        if name in os.environ
    }
    policy = SandboxPolicy(timeout_seconds=120)
    proxy_policy = DependencyProxyPolicy.for_ecosystem("python")
    proxy_runtime = DockerDependencyProxyRuntime(
        docker_environment=docker_environment
    )
    dependency_runtime = DockerDependencyRuntime(
        docker_environment=docker_environment
    )
    repository_volume = "contribos-workspace-" + "f" * 32
    proxy_lease: DependencyProxyLease | None = None
    execution = None

    async def acceptance() -> None:
        nonlocal proxy_lease, execution
        await dependency_runtime._command(
            (
                "docker",
                "volume",
                "create",
                repository_volume,
            ),
            timeout_seconds=30,
        )
        try:
            await dependency_runtime._command(
                (
                    "docker",
                    "run",
                    "--rm",
                    "--pull",
                    "never",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--user",
                    "0:0",
                    "--mount",
                    (
                        f"type=volume,src={repository_volume},"
                        "dst=/workspace"
                    ),
                    "--entrypoint",
                    "python3",
                    image,
                    "-I",
                    "-c",
                    "open('/workspace/requirements.lock','xb').close()",
                ),
                timeout_seconds=30,
            )
            proxy_lease = await proxy_runtime.start(
                image_digest=image,
                policy=proxy_policy,
            )
            network_probe = r"""
import json
import socket
import urllib.error
import urllib.request

evidence = {}
try:
    direct = socket.create_connection(("1.1.1.1", 80), 2)
except OSError:
    evidence["direct"] = "blocked"
else:
    direct.close()
    evidence["direct"] = "connected"
try:
    urllib.request.urlopen(
        "http://169.254.169.254/latest/meta-data/", timeout=5
    )
except urllib.error.HTTPError as exc:
    evidence["metadata"] = exc.code
except OSError:
    evidence["metadata"] = "network_error"
else:
    evidence["metadata"] = "allowed"
try:
    with urllib.request.urlopen(
        "https://pypi.org/simple/", timeout=20
    ) as response:
        evidence["pypi"] = response.status
except Exception as exc:
    evidence["pypi"] = type(exc).__name__
print(json.dumps(evidence, sort_keys=True))
""".strip()
            probe_output = await dependency_runtime._command(
                (
                    "docker",
                    "run",
                    "--rm",
                    "--pull",
                    "never",
                    "--network",
                    proxy_lease.network_name,
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges=true",
                    "--user",
                    f"{policy.run_as_uid}:{policy.run_as_gid}",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
                    "--env",
                    f"HTTP_PROXY={proxy_lease.proxy_url}",
                    "--env",
                    f"HTTPS_PROXY={proxy_lease.proxy_url}",
                    "--env",
                    "NO_PROXY=",
                    "--entrypoint",
                    "python3",
                    image,
                    "-I",
                    "-c",
                    network_probe,
                ),
                timeout_seconds=30,
            )
            probe_evidence = json.loads(probe_output)
            assert probe_evidence == {
                "direct": "blocked",
                "metadata": 403,
                "pypi": 200,
            }
            plan = replace(
                _plan(policy, proxy_policy),
                runner_image_digest=image,
                manifests=(
                    DependencyManifest(
                        path="requirements.lock",
                        content_hash=hashlib.sha256(b"").hexdigest(),
                    ),
                ),
            )
            signer = DependencyPlanSigner(
                key_id="real-dependency-key",
                signing_key=b"z" * 32,
            )
            execution = await dependency_runtime.prepare(
                signed_plan=signer.sign(plan),
                signer=signer,
                repository_volume=repository_volume,
                proxy_policy=proxy_policy,
                proxy_lease=proxy_lease,
                sandbox_policy=policy,
            )
            assert execution.result.file_count > 0
            assert execution.result.total_bytes > 0
            assert execution.workspace.inventory_hash == (
                execution.result.dependency_inventory_hash
            )
            empty_hash = hashlib.sha256(b"").hexdigest()
            repository_inventory_hash = content_hash(
                [
                    {
                        "path": "requirements.lock",
                        "type": "file",
                        "size": 0,
                        "sha256": empty_hash,
                        "executable": False,
                    }
                ]
            )
            verification = await DockerVerifyRuntime(
                docker_environment=docker_environment
            ).run(
                workspace=DisposableWorkspace(
                    workspace_id="workspace:dependency-verify",
                    volume_name=repository_volume,
                    runner_image_digest=image,
                    sandbox_policy_hash=policy.policy_hash,
                    inventory_hash=repository_inventory_hash,
                ),
                commands=(
                    SandboxCommand(
                        command_id="dependency-venv",
                        argv=(
                            "python3",
                            "-I",
                            "-c",
                            (
                                "import sys;"
                                "assert sys.prefix == "
                                "'/dependencies/output/venv'"
                            ),
                        ),
                    ),
                ),
                policy=policy,
                dependency_workspace=execution.workspace,
            )
            assert verification.succeeded is True
            assert verification.before_inventory_hash == (
                verification.after_inventory_hash
            )
        finally:
            if execution is not None:
                await dependency_runtime.destroy(execution.workspace)
            if proxy_lease is not None:
                await proxy_runtime.stop(proxy_lease)
            await dependency_runtime._command(
                (
                    "docker",
                    "volume",
                    "rm",
                    "--force",
                    repository_volume,
                ),
                timeout_seconds=30,
            )

    asyncio.run(acceptance())
