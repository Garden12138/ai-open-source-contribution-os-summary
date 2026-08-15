"""Credential-free dependency preparation through one allowlisted proxy."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import uuid4

from app.provenance import canonical_json, content_hash
from app.sandbox_worker.git_safety import (
    GIT_SAFETY_ENVIRONMENT_NAMES,
    docker_git_safety_argv,
)
from app.sandbox_worker.specs import SandboxPolicy
from app.security import contains_sensitive_text, ensure_no_sensitive_data


DEPENDENCY_PLAN_VERSION = "dependency-preparation-plan-v1"
DEPENDENCY_SIGNATURE_VERSION = "dependency-plan-hmac-sha256-v1"
DEPENDENCY_PROXY_POLICY_VERSION = "dependency-proxy-policy-v1"
DEPENDENCY_RESULT_VERSION = "dependency-preparation-result-v1"
MAX_DEPENDENCY_OUTPUT_BYTES = 1_000_000
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_NETWORK = re.compile(r"^contribos-dependency-network-[0-9a-f]{32}$")
_PROXY_CONTAINER = re.compile(r"^contribos-dependency-proxy-[0-9a-f]{32}$")
_WORKER_CONTAINER = re.compile(r"^contribos-dependency-worker-[0-9a-f]{32}$")
_VOLUME = re.compile(r"^contribos-dependencies-[0-9a-f]{32}$")
_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
)
_ALLOWED_DOCKER_ENV = frozenset(
    {
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "HOME",
        "PATH",
        "TMPDIR",
    }
)


class DependencyEcosystem(StrEnum):
    PYTHON = "python"
    NODE = "node"


class DependencyPreparationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DependencyManifest:
    path: str
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "path",
            _relative_path(self.path, name="dependency manifest"),
        )
        _hash(self.content_hash, name="dependency manifest hash")

    def to_wire(self) -> dict[str, str]:
        return {"path": self.path, "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class DependencyProxyPolicy:
    ecosystem: DependencyEcosystem
    allowed_upstream_hosts: tuple[str, ...]
    proxy_alias: str = "dependency-proxy"
    proxy_port: int = 8080
    version: str = DEPENDENCY_PROXY_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.version != DEPENDENCY_PROXY_POLICY_VERSION:
            raise ValueError("Dependency proxy policy version is unsupported")
        try:
            ecosystem = DependencyEcosystem(self.ecosystem)
        except (TypeError, ValueError) as exc:
            raise ValueError("Dependency ecosystem is invalid") from exc
        object.__setattr__(self, "ecosystem", ecosystem)
        expected = {
            DependencyEcosystem.PYTHON: (
                "files.pythonhosted.org",
                "pypi.org",
            ),
            DependencyEcosystem.NODE: ("registry.npmjs.org",),
        }[ecosystem]
        hosts = tuple(sorted(self.allowed_upstream_hosts))
        if hosts != expected or any(not _HOST.fullmatch(host) for host in hosts):
            raise ValueError(
                "Dependency proxy upstream allowlist is invalid"
            )
        object.__setattr__(self, "allowed_upstream_hosts", hosts)
        if self.proxy_alias != "dependency-proxy" or self.proxy_port != 8080:
            raise ValueError("Dependency proxy endpoint is invalid")
        ensure_no_sensitive_data(self.to_wire(), context="dependency proxy policy")

    @classmethod
    def for_ecosystem(
        cls,
        ecosystem: DependencyEcosystem | str,
    ) -> "DependencyProxyPolicy":
        selected = DependencyEcosystem(ecosystem)
        hosts = {
            DependencyEcosystem.PYTHON: (
                "files.pythonhosted.org",
                "pypi.org",
            ),
            DependencyEcosystem.NODE: ("registry.npmjs.org",),
        }[selected]
        return cls(ecosystem=selected, allowed_upstream_hosts=hosts)

    @property
    def policy_hash(self) -> str:
        return content_hash(self.to_wire())

    @property
    def proxy_url(self) -> str:
        return f"http://{self.proxy_alias}:{self.proxy_port}"

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "ecosystem": self.ecosystem.value,
            "allowed_upstream_hosts": list(self.allowed_upstream_hosts),
            "proxy_alias": self.proxy_alias,
            "proxy_port": self.proxy_port,
        }


@dataclass(frozen=True, slots=True)
class DependencyPreparationPlan:
    plan_id: str
    execution_attempt_id: str
    implement_result_hash: str
    workspace_inventory_hash: str
    ecosystem: DependencyEcosystem
    manifests: tuple[DependencyManifest, ...]
    runner_image_digest: str
    sandbox_policy_hash: str
    proxy_policy_hash: str
    disk_bytes: int
    version: str = DEPENDENCY_PLAN_VERSION

    def __post_init__(self) -> None:
        if self.version != DEPENDENCY_PLAN_VERSION:
            raise ValueError("Dependency plan version is unsupported")
        for value, name in (
            (self.plan_id, "plan ID"),
            (self.execution_attempt_id, "execution attempt ID"),
        ):
            if (
                not isinstance(value, str)
                or not _IDENTIFIER.fullmatch(value)
                or contains_sensitive_text(value)
            ):
                raise ValueError(f"Dependency {name} is invalid")
        for value, name in (
            (self.implement_result_hash, "Implement result hash"),
            (self.workspace_inventory_hash, "workspace inventory hash"),
            (self.sandbox_policy_hash, "sandbox policy hash"),
            (self.proxy_policy_hash, "proxy policy hash"),
        ):
            _hash(value, name=name)
        if not _IMAGE.fullmatch(self.runner_image_digest):
            raise ValueError("Dependency Runner image digest is invalid")
        try:
            ecosystem = DependencyEcosystem(self.ecosystem)
        except (TypeError, ValueError) as exc:
            raise ValueError("Dependency ecosystem is invalid") from exc
        object.__setattr__(self, "ecosystem", ecosystem)
        manifests = tuple(self.manifests)
        if (
            not manifests
            or len(manifests) > 2
            or any(not isinstance(item, DependencyManifest) for item in manifests)
            or len({item.path for item in manifests}) != len(manifests)
        ):
            raise ValueError("Dependency manifests are invalid")
        paths = tuple(sorted(item.path for item in manifests))
        if ecosystem is DependencyEcosystem.PYTHON:
            if len(paths) != 1 or not paths[0].endswith((".txt", ".lock")):
                raise ValueError(
                    "Python dependency preparation requires one hash lockfile"
                )
        elif paths != ("package-lock.json", "package.json"):
            raise ValueError(
                "Node dependency preparation requires package.json "
                "and package-lock.json"
            )
        object.__setattr__(self, "manifests", manifests)
        if (
            isinstance(self.disk_bytes, bool)
            or not isinstance(self.disk_bytes, int)
            or not 64 * 1024 * 1024 <= self.disk_bytes <= 4 * 1024**3
        ):
            raise ValueError("Dependency disk budget is invalid")
        ensure_no_sensitive_data(self.to_wire(), context="dependency plan")

    @property
    def plan_hash(self) -> str:
        return content_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "plan_id": self.plan_id,
            "execution_attempt_id": self.execution_attempt_id,
            "implement_result_hash": self.implement_result_hash,
            "workspace_inventory_hash": self.workspace_inventory_hash,
            "ecosystem": self.ecosystem.value,
            "manifests": [item.to_wire() for item in self.manifests],
            "runner_image_digest": self.runner_image_digest,
            "sandbox_policy_hash": self.sandbox_policy_hash,
            "proxy_policy_hash": self.proxy_policy_hash,
            "disk_bytes": self.disk_bytes,
        }


@dataclass(frozen=True, slots=True)
class SignedDependencyPlan:
    plan: DependencyPreparationPlan
    plan_hash: str
    key_id: str
    signature: str
    signature_version: str = DEPENDENCY_SIGNATURE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.plan, DependencyPreparationPlan):
            raise ValueError("Signed dependency plan is invalid")
        if self.plan_hash != self.plan.plan_hash:
            raise ValueError("Dependency plan hash does not match")
        _hash(self.plan_hash, name="plan hash")
        if (
            not isinstance(self.key_id, str)
            or not _IDENTIFIER.fullmatch(self.key_id)
            or contains_sensitive_text(self.key_id)
        ):
            raise ValueError("Dependency signing key ID is invalid")
        if self.signature_version != DEPENDENCY_SIGNATURE_VERSION:
            raise ValueError("Dependency signature version is unsupported")
        _hash(self.signature, name="signature")
        ensure_no_sensitive_data(self.to_wire(), context="signed dependency plan")

    def signing_payload(self) -> dict[str, object]:
        return {
            "signature_version": self.signature_version,
            "key_id": self.key_id,
            "plan_hash": self.plan_hash,
            "plan": self.plan.to_wire(),
        }

    def to_wire(self) -> dict[str, object]:
        return {**self.signing_payload(), "signature": self.signature}

    def to_bytes(self) -> bytes:
        return (canonical_json(self.to_wire()) + "\n").encode("utf-8")


class DependencyPlanSigner:
    def __init__(self, *, key_id: str, signing_key: bytes) -> None:
        if (
            not isinstance(key_id, str)
            or not _IDENTIFIER.fullmatch(key_id)
            or contains_sensitive_text(key_id)
        ):
            raise ValueError("Dependency signing key ID is invalid")
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError(
                "Dependency signing key must contain at least 32 bytes"
            )
        self.key_id = key_id
        self._key = bytes(signing_key)

    def sign(self, plan: DependencyPreparationPlan) -> SignedDependencyPlan:
        unsigned = SignedDependencyPlan(
            plan=plan,
            plan_hash=plan.plan_hash,
            key_id=self.key_id,
            signature="0" * 64,
        )
        signature = hmac.new(
            self._key,
            canonical_json(unsigned.signing_payload()).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return SignedDependencyPlan(
            plan=plan,
            plan_hash=plan.plan_hash,
            key_id=self.key_id,
            signature=signature,
        )

    def verify(
        self,
        signed: SignedDependencyPlan,
    ) -> DependencyPreparationPlan:
        if not isinstance(signed, SignedDependencyPlan):
            raise ValueError("Signed dependency plan is invalid")
        if signed.key_id != self.key_id:
            raise ValueError("Dependency signing key is unknown")
        expected = hmac.new(
            self._key,
            canonical_json(signed.signing_payload()).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signed.signature, expected):
            raise ValueError("Dependency plan signature is invalid")
        return signed.plan


@dataclass(frozen=True, slots=True)
class DependencyProxyLease:
    network_name: str
    container_name: str
    proxy_url: str
    policy_hash: str

    def __post_init__(self) -> None:
        if (
            not _NETWORK.fullmatch(self.network_name)
            or not _PROXY_CONTAINER.fullmatch(self.container_name)
            or self.proxy_url != "http://dependency-proxy:8080"
        ):
            raise ValueError("Dependency proxy lease identity is invalid")
        _hash(self.policy_hash, name="proxy lease policy hash")


@dataclass(frozen=True, slots=True)
class DependencyWorkspace:
    workspace_id: str
    volume_name: str
    runner_image_digest: str
    sandbox_policy_hash: str
    plan_hash: str
    inventory_hash: str | None = None

    def __post_init__(self) -> None:
        if (
            not _IDENTIFIER.fullmatch(self.workspace_id)
            or not _VOLUME.fullmatch(self.volume_name)
            or not _IMAGE.fullmatch(self.runner_image_digest)
        ):
            raise ValueError("Dependency workspace identity is invalid")
        _hash(self.sandbox_policy_hash, name="workspace policy hash")
        _hash(self.plan_hash, name="workspace plan hash")
        if self.inventory_hash is not None:
            _hash(self.inventory_hash, name="dependency inventory hash")


@dataclass(frozen=True, slots=True)
class DependencyPreparationResult:
    execution_attempt_id: str
    implement_result_hash: str
    plan_hash: str
    proxy_policy_hash: str
    runner_image_digest: str
    sandbox_policy_hash: str
    ecosystem: DependencyEcosystem
    dependency_inventory_hash: str
    file_count: int
    total_bytes: int
    result_hash: str
    version: str = DEPENDENCY_RESULT_VERSION

    def __post_init__(self) -> None:
        if self.version != DEPENDENCY_RESULT_VERSION:
            raise DependencyPreparationError(
                "Dependency result version is unsupported"
            )
        for value in (
            self.implement_result_hash,
            self.plan_hash,
            self.proxy_policy_hash,
            self.sandbox_policy_hash,
            self.dependency_inventory_hash,
            self.result_hash,
        ):
            _hash(value, name="result hash")
        if not _IMAGE.fullmatch(self.runner_image_digest):
            raise DependencyPreparationError(
                "Dependency result image digest is invalid"
            )
        try:
            ecosystem = DependencyEcosystem(self.ecosystem)
        except (TypeError, ValueError) as exc:
            raise DependencyPreparationError(
                "Dependency result ecosystem is invalid"
            ) from exc
        object.__setattr__(self, "ecosystem", ecosystem)
        for value, name in (
            (self.file_count, "file count"),
            (self.total_bytes, "total bytes"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise DependencyPreparationError(
                    f"Dependency result {name} is invalid"
                )
        if self.result_hash != content_hash(self.hash_payload()):
            raise DependencyPreparationError(
                "Dependency result hash does not match"
            )
        ensure_no_sensitive_data(self.to_wire(), context="dependency result")

    @classmethod
    def create(
        cls,
        *,
        plan: DependencyPreparationPlan,
        inventory_hash: str,
        file_count: int,
        total_bytes: int,
    ) -> "DependencyPreparationResult":
        payload = {
            "version": DEPENDENCY_RESULT_VERSION,
            "execution_attempt_id": plan.execution_attempt_id,
            "implement_result_hash": plan.implement_result_hash,
            "plan_hash": plan.plan_hash,
            "proxy_policy_hash": plan.proxy_policy_hash,
            "runner_image_digest": plan.runner_image_digest,
            "sandbox_policy_hash": plan.sandbox_policy_hash,
            "ecosystem": plan.ecosystem.value,
            "dependency_inventory_hash": inventory_hash,
            "file_count": file_count,
            "total_bytes": total_bytes,
        }
        return cls(
            execution_attempt_id=plan.execution_attempt_id,
            implement_result_hash=plan.implement_result_hash,
            plan_hash=plan.plan_hash,
            proxy_policy_hash=plan.proxy_policy_hash,
            runner_image_digest=plan.runner_image_digest,
            sandbox_policy_hash=plan.sandbox_policy_hash,
            ecosystem=plan.ecosystem,
            dependency_inventory_hash=inventory_hash,
            file_count=file_count,
            total_bytes=total_bytes,
            result_hash=content_hash(payload),
        )

    def hash_payload(self) -> dict[str, object]:
        value = self.to_wire()
        value.pop("result_hash")
        return value

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "execution_attempt_id": self.execution_attempt_id,
            "implement_result_hash": self.implement_result_hash,
            "plan_hash": self.plan_hash,
            "proxy_policy_hash": self.proxy_policy_hash,
            "runner_image_digest": self.runner_image_digest,
            "sandbox_policy_hash": self.sandbox_policy_hash,
            "ecosystem": self.ecosystem.value,
            "dependency_inventory_hash": self.dependency_inventory_hash,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "result_hash": self.result_hash,
        }


@dataclass(frozen=True, slots=True)
class DependencyExecution:
    result: DependencyPreparationResult
    workspace: DependencyWorkspace


class DockerDependencyRuntime:
    def __init__(
        self,
        *,
        docker_environment: Mapping[str, str],
        docker_executable: str = "docker",
    ) -> None:
        unexpected = sorted(set(docker_environment) - _ALLOWED_DOCKER_ENV)
        if unexpected:
            raise ValueError(
                "Dependency Docker environment contains disallowed variables: "
                + ", ".join(unexpected)
            )
        if not docker_executable or os.path.sep in docker_executable:
            raise ValueError("Dependency Docker executable is invalid")
        self.docker_environment = dict(docker_environment)
        self.docker_executable = docker_executable

    async def prepare(
        self,
        *,
        signed_plan: SignedDependencyPlan,
        signer: DependencyPlanSigner,
        repository_volume: str,
        proxy_policy: DependencyProxyPolicy,
        proxy_lease: DependencyProxyLease,
        sandbox_policy: SandboxPolicy,
    ) -> DependencyExecution:
        try:
            plan = signer.verify(signed_plan)
        except ValueError as exc:
            raise DependencyPreparationError(str(exc)) from exc
        if (
            plan.sandbox_policy_hash != sandbox_policy.policy_hash
            or plan.disk_bytes != sandbox_policy.disk_bytes
            or plan.proxy_policy_hash != proxy_policy.policy_hash
            or plan.ecosystem is not proxy_policy.ecosystem
            or proxy_lease.policy_hash != proxy_policy.policy_hash
            or proxy_lease.proxy_url != proxy_policy.proxy_url
        ):
            raise DependencyPreparationError(
                "Dependency plan does not match image, policy, or proxy"
            )
        workspace = await self.create_workspace(
            image_digest=plan.runner_image_digest,
            sandbox_policy=sandbox_policy,
            plan_hash=plan.plan_hash,
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="contribos-dependency-plan-"
            ) as temporary:
                plan_path = os.path.join(temporary, "dependency-plan.json")
                with open(plan_path, "xb") as handle:
                    handle.write(signed_plan.to_bytes())
                os.chmod(plan_path, 0o444)
                name = f"contribos-dependency-worker-{uuid4().hex}"
                output = await self._run(
                    self.build_prepare_argv(
                        repository_volume=repository_volume,
                        dependency_workspace=workspace,
                        signed_plan_path=plan_path,
                        proxy_lease=proxy_lease,
                        policy=sandbox_policy,
                        container_name=name,
                    ),
                    container_name=name,
                    timeout_seconds=sandbox_policy.timeout_seconds,
                )
            try:
                raw = json.loads(output)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DependencyPreparationError(
                    "Dependency preparation returned invalid JSON"
                ) from exc
            if not isinstance(raw, dict) or set(raw) != {
                "inventory_hash",
                "file_count",
                "total_bytes",
            }:
                raise DependencyPreparationError(
                    "Dependency preparation evidence is invalid"
                )
            inventory_hash = _hash(
                raw["inventory_hash"],
                name="inventory hash",
            )
            for field in ("file_count", "total_bytes"):
                if (
                    isinstance(raw[field], bool)
                    or not isinstance(raw[field], int)
                    or raw[field] < 0
                ):
                    raise DependencyPreparationError(
                        "Dependency preparation evidence is invalid"
                    )
            if raw["total_bytes"] > plan.disk_bytes:
                raise DependencyPreparationError(
                    "Dependency preparation exceeded its disk budget"
                )
            result = DependencyPreparationResult.create(
                plan=plan,
                inventory_hash=inventory_hash,
                file_count=raw["file_count"],
                total_bytes=raw["total_bytes"],
            )
            completed_workspace = DependencyWorkspace(
                workspace_id=workspace.workspace_id,
                volume_name=workspace.volume_name,
                runner_image_digest=workspace.runner_image_digest,
                sandbox_policy_hash=workspace.sandbox_policy_hash,
                plan_hash=workspace.plan_hash,
                inventory_hash=inventory_hash,
            )
            return DependencyExecution(
                result=result,
                workspace=completed_workspace,
            )
        except Exception:
            await self.destroy(workspace)
            raise

    async def create_workspace(
        self,
        *,
        image_digest: str,
        sandbox_policy: SandboxPolicy,
        plan_hash: str,
    ) -> DependencyWorkspace:
        if not _IMAGE.fullmatch(image_digest):
            raise ValueError("Dependency workspace image digest is invalid")
        _hash(plan_hash, name="workspace plan hash")
        volume = f"contribos-dependencies-{uuid4().hex}"
        workspace = DependencyWorkspace(
            workspace_id=f"dependencies:{uuid4()}",
            volume_name=volume,
            runner_image_digest=image_digest,
            sandbox_policy_hash=sandbox_policy.policy_hash,
            plan_hash=plan_hash,
        )
        try:
            await self._command(
                (
                    self.docker_executable,
                    "volume",
                    "create",
                    "--label",
                    "io.contribos.dependencies=true",
                    "--label",
                    f"io.contribos.plan-hash={plan_hash}",
                    volume,
                ),
                timeout_seconds=30,
            )
            await self._command(
                (
                    self.docker_executable,
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
                    f"type=volume,src={volume},dst=/dependencies",
                    "--entrypoint",
                    "python3",
                    image_digest,
                    "-I",
                    "-c",
                    (
                        "import os;"
                        "os.mkdir('/dependencies/output');"
                        "os.chmod('/dependencies/output',0o777)"
                    ),
                ),
                timeout_seconds=30,
            )
        except Exception:
            await self.destroy(workspace, ignore_missing=True)
            raise DependencyPreparationError(
                "Dependency workspace initialization failed"
            ) from None
        return workspace

    async def destroy(
        self,
        workspace: DependencyWorkspace,
        *,
        ignore_missing: bool = False,
    ) -> None:
        if not _VOLUME.fullmatch(workspace.volume_name):
            raise DependencyPreparationError(
                "Dependency workspace name is invalid"
            )
        try:
            await self._command(
                (
                    self.docker_executable,
                    "volume",
                    "rm",
                    "--force",
                    workspace.volume_name,
                ),
                timeout_seconds=30,
            )
        except DependencyPreparationError:
            if not ignore_missing:
                raise

    def build_prepare_argv(
        self,
        *,
        repository_volume: str,
        dependency_workspace: DependencyWorkspace,
        signed_plan_path: str,
        proxy_lease: DependencyProxyLease,
        policy: SandboxPolicy,
        container_name: str,
    ) -> tuple[str, ...]:
        if (
            not re.fullmatch(r"^contribos-workspace-[0-9a-f]{32}$", repository_volume)
            or not _WORKER_CONTAINER.fullmatch(container_name)
            or not os.path.isabs(signed_plan_path)
            or "," in signed_plan_path
            or not os.path.isfile(signed_plan_path)
            or dependency_workspace.sandbox_policy_hash
            != policy.policy_hash
        ):
            raise ValueError("Dependency runtime input is invalid")
        return (
            self.docker_executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            proxy_lease.network_name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(policy.pids_limit),
            "--memory",
            str(policy.memory_bytes),
            "--memory-swap",
            str(policy.memory_bytes),
            "--cpus",
            str(policy.cpu_limit),
            "--user",
            f"{policy.run_as_uid}:{policy.run_as_gid}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=134217728,mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "PIP_CONFIG_FILE=/dev/null",
            "--env",
            "NPM_CONFIG_USERCONFIG=/dev/null",
            "--env",
            f"HTTP_PROXY={proxy_lease.proxy_url}",
            "--env",
            f"HTTPS_PROXY={proxy_lease.proxy_url}",
            "--env",
            "NO_PROXY=",
            *docker_git_safety_argv(),
            "--mount",
            (
                f"type=volume,src={repository_volume},"
                "dst=/workspace,readonly"
            ),
            "--mount",
            (
                f"type=volume,src={dependency_workspace.volume_name},"
                "dst=/dependencies"
            ),
            "--mount",
            (
                f"type=bind,src={signed_plan_path},"
                "dst=/input/dependency-plan.json,readonly,"
                "bind-propagation=rprivate"
            ),
            "--entrypoint",
            "python3",
            dependency_workspace.runner_image_digest,
            "-I",
            "-c",
            _PREPARE_SCRIPT,
        )

    async def _run(
        self,
        argv: Sequence[str],
        *,
        container_name: str,
        timeout_seconds: int,
    ) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.docker_environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise DependencyPreparationError(
                "Dependency container could not start"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            await self._terminate(process, container_name)
            raise DependencyPreparationError(
                "Dependency preparation timed out"
            ) from None
        if (
            process.returncode != 0
            or stderr
            or len(stdout) > MAX_DEPENDENCY_OUTPUT_BYTES
        ):
            raise DependencyPreparationError(
                "Dependency preparation failed"
            )
        return stdout

    async def _command(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> bytes:
        try:
            completed = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self.docker_environment,
                    start_new_session=True,
                ),
                timeout=10,
            )
            stdout, stderr = await asyncio.wait_for(
                completed.communicate(),
                timeout=timeout_seconds,
            )
        except (OSError, TimeoutError) as exc:
            raise DependencyPreparationError(
                "Dependency Docker command failed"
            ) from exc
        if (
            completed.returncode != 0
            or stderr
            or len(stdout) > MAX_DEPENDENCY_OUTPUT_BYTES
        ):
            raise DependencyPreparationError(
                "Dependency Docker command failed"
            )
        return stdout

    async def _terminate(
        self,
        process: asyncio.subprocess.Process,
        container_name: str,
    ) -> None:
        try:
            await self._command(
                (
                    self.docker_executable,
                    "kill",
                    "--signal",
                    "KILL",
                    container_name,
                ),
                timeout_seconds=10,
            )
        except DependencyPreparationError:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            pass


class DockerDependencyProxyRuntime:
    """Trusted fixed proxy on an internal-only Worker network."""

    def __init__(
        self,
        *,
        docker_environment: Mapping[str, str],
        docker_executable: str = "docker",
    ) -> None:
        unexpected = sorted(set(docker_environment) - _ALLOWED_DOCKER_ENV)
        if unexpected:
            raise ValueError(
                "Dependency proxy environment contains disallowed variables: "
                + ", ".join(unexpected)
            )
        if not docker_executable or os.path.sep in docker_executable:
            raise ValueError("Dependency proxy Docker executable is invalid")
        self.docker_environment = dict(docker_environment)
        self.docker_executable = docker_executable

    def build_proxy_argv(
        self,
        *,
        image_digest: str,
        policy: DependencyProxyPolicy,
        container_name: str,
    ) -> tuple[str, ...]:
        if (
            not _IMAGE.fullmatch(image_digest)
            or not _PROXY_CONTAINER.fullmatch(container_name)
        ):
            raise ValueError("Dependency proxy runtime input is invalid")
        return (
            self.docker_executable,
            "run",
            "--detach",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "bridge",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "64",
            "--memory",
            "268435456",
            "--memory-swap",
            "268435456",
            "--cpus",
            "0.5",
            "--user",
            "65532:65532",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
            "--entrypoint",
            "python3",
            image_digest,
            "-I",
            "-c",
            _PROXY_SCRIPT,
            ",".join(policy.allowed_upstream_hosts),
        )

    async def start(
        self,
        *,
        image_digest: str,
        policy: DependencyProxyPolicy,
    ) -> DependencyProxyLease:
        network = f"contribos-dependency-network-{uuid4().hex}"
        container = f"contribos-dependency-proxy-{uuid4().hex}"
        lease = DependencyProxyLease(
            network_name=network,
            container_name=container,
            proxy_url=policy.proxy_url,
            policy_hash=policy.policy_hash,
        )
        try:
            await self._command(
                (
                    self.docker_executable,
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    "io.contribos.dependency-network=true",
                    network,
                )
            )
            await self._command(
                self.build_proxy_argv(
                    image_digest=image_digest,
                    policy=policy,
                    container_name=container,
                )
            )
            await self._command(
                (
                    self.docker_executable,
                    "network",
                    "connect",
                    "--alias",
                    policy.proxy_alias,
                    network,
                    container,
                )
            )
            await self._wait_until_ready(container)
        except Exception:
            await self.stop(lease, ignore_missing=True)
            raise DependencyPreparationError(
                "Dependency proxy could not start"
            ) from None
        return lease

    async def _wait_until_ready(self, container_name: str) -> None:
        probe = (
            self.docker_executable,
            "exec",
            container_name,
            "python3",
            "-I",
            "-c",
            (
                "import socket;"
                "connection=socket.create_connection("
                "('127.0.0.1',8080),1);"
                "connection.close()"
            ),
        )
        for _ in range(20):
            try:
                await self._command(probe)
                return
            except DependencyPreparationError:
                await asyncio.sleep(0.05)
        raise DependencyPreparationError(
            "Dependency proxy readiness check failed"
        )

    async def stop(
        self,
        lease: DependencyProxyLease,
        *,
        ignore_missing: bool = False,
    ) -> None:
        failures = 0
        for argv in (
            (
                self.docker_executable,
                "kill",
                "--signal",
                "KILL",
                lease.container_name,
            ),
            (
                self.docker_executable,
                "network",
                "rm",
                lease.network_name,
            ),
        ):
            try:
                await self._command(argv)
            except DependencyPreparationError:
                failures += 1
        if failures and not ignore_missing:
            raise DependencyPreparationError(
                "Dependency proxy cleanup failed"
            )

    async def _command(self, argv: Sequence[str]) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.docker_environment,
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30,
            )
        except (OSError, TimeoutError) as exc:
            raise DependencyPreparationError(
                "Dependency proxy Docker command failed"
            ) from exc
        if (
            process.returncode != 0
            or stderr
            or len(stdout) > MAX_DEPENDENCY_OUTPUT_BYTES
        ):
            raise DependencyPreparationError(
                "Dependency proxy Docker command failed"
            )
        return stdout


_PREPARE_SCRIPT = r"""
import hashlib
import json
import os
import shutil
import stat
import subprocess

git_environment_names = __GIT_ENVIRONMENT_NAMES__
git_environment = {}
for name in git_environment_names:
    value = os.environ.get(name)
    if value is None:
        raise SystemExit(87)
    git_environment[name] = value
with open("/input/dependency-plan.json", encoding="utf-8") as handle:
    envelope = json.load(handle)
if set(envelope) != {
    "signature_version", "key_id", "plan_hash", "plan", "signature"
}:
    raise SystemExit(80)
plan = envelope["plan"]
encoded = json.dumps(
    plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
).encode("utf-8")
if hashlib.sha256(encoded).hexdigest() != envelope["plan_hash"]:
    raise SystemExit(81)
proxy = os.environ.get("HTTPS_PROXY")
if proxy != "http://dependency-proxy:8080":
    raise SystemExit(82)
for manifest in plan["manifests"]:
    path = manifest["path"]
    source = os.path.join("/workspace", *path.split("/"))
    if not os.path.isfile(source) or os.path.islink(source):
        raise SystemExit(83)
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        while chunk := handle.read(1048576):
            digest.update(chunk)
    if digest.hexdigest() != manifest["content_hash"]:
        raise SystemExit(84)

environment = {
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PIP_CONFIG_FILE": "/dev/null",
    "NPM_CONFIG_USERCONFIG": "/dev/null",
    "HTTP_PROXY": proxy,
    "HTTPS_PROXY": proxy,
    "NO_PROXY": "",
    **git_environment,
}
if plan["ecosystem"] == "python":
    dependency_root = "/dependencies/output"
    lockfile = os.path.join(
        "/workspace", *plan["manifests"][0]["path"].split("/")
    )
    subprocess.run(
        ["python3", "-m", "venv", f"{dependency_root}/venv"],
        check=True,
        stdin=subprocess.DEVNULL,
        env=environment,
    )
    subprocess.run(
        [
            f"{dependency_root}/venv/bin/python", "-m", "pip", "install",
            "--isolated", "--disable-pip-version-check", "--no-input",
            "--no-cache-dir", "--require-hashes", "--only-binary=:all:",
            "--index-url", "https://pypi.org/simple",
            "--proxy", proxy, "-r", lockfile,
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        env=environment,
    )
else:
    dependency_root = "/dependencies/output"
    project = f"{dependency_root}/project"
    os.makedirs(project, exist_ok=False)
    for manifest in plan["manifests"]:
        shutil.copyfile(
            os.path.join("/workspace", *manifest["path"].split("/")),
            os.path.join(project, os.path.basename(manifest["path"])),
        )
    subprocess.run(
        [
            "npm", "ci", "--prefix", project, "--ignore-scripts",
            "--no-audit", "--no-fund",
            "--registry=https://registry.npmjs.org",
            f"--proxy={proxy}", f"--https-proxy={proxy}",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        env=environment,
    )

entries = []
total = 0
for current, directories, files in os.walk(
    dependency_root, topdown=True, followlinks=False
):
    directories.sort()
    files.sort()
    for name in files:
        absolute = os.path.join(current, name)
        metadata = os.lstat(absolute)
        relative = os.path.relpath(absolute, dependency_root)
        if stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256()
            with open(absolute, "rb") as handle:
                while chunk := handle.read(1048576):
                    digest.update(chunk)
            total += metadata.st_size
            entries.append([relative, "file", metadata.st_size, digest.hexdigest()])
        elif stat.S_ISLNK(metadata.st_mode):
            entries.append([relative, "symlink", os.readlink(absolute)])
        else:
            raise SystemExit(85)
        if total > plan["disk_bytes"]:
            raise SystemExit(86)
inventory = hashlib.sha256(
    json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
print(json.dumps({
    "inventory_hash": inventory,
    "file_count": len(entries),
    "total_bytes": total,
}, sort_keys=True, separators=(",", ":")))
""".replace(
    "__GIT_ENVIRONMENT_NAMES__",
    repr(GIT_SAFETY_ENVIRONMENT_NAMES),
)

_PROXY_SCRIPT = r"""
import ipaddress
import select
import socket
import socketserver
import sys
import time
from urllib.parse import urlsplit

allowed = set(sys.argv[1].split(","))
denied_headers = {
    "authorization", "cookie", "proxy-authorization",
    "x-api-key", "x-auth-token",
}

def connect_allowed(host, port):
    host = host.lower().rstrip(".")
    if host not in allowed or port not in {80, 443}:
        raise ValueError("denied")
    candidates = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    for family, socktype, proto, _, address in candidates:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global:
            continue
        upstream = socket.socket(family, socktype, proto)
        upstream.settimeout(15)
        try:
            upstream.connect(address)
            return upstream
        except OSError:
            upstream.close()
    raise ValueError("unreachable")

def relay(left, right):
    sockets = [left, right]
    deadline = time.monotonic() + 600
    transferred = 0
    while time.monotonic() < deadline and transferred <= 1073741824:
        readable, _, _ = select.select(sockets, [], [], 5)
        if not readable:
            continue
        for source in readable:
            chunk = source.recv(65536)
            if not chunk:
                return
            target = right if source is left else left
            target.sendall(chunk)
            transferred += len(chunk)

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
            if len(data) > 65536:
                return
        head, remainder = data.split(b"\r\n\r\n", 1)
        try:
            lines = head.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers = []
            for line in lines[1:]:
                name, value = line.split(":", 1)
                if name.strip().lower() not in denied_headers:
                    headers.append((name.strip(), value.strip()))
            if method.upper() == "CONNECT":
                host, port_text = target.rsplit(":", 1)
                upstream = connect_allowed(host, int(port_text))
                self.request.sendall(
                    b"HTTP/1.1 200 Connection Established\r\n\r\n"
                )
                if remainder:
                    upstream.sendall(remainder)
                try:
                    relay(self.request, upstream)
                finally:
                    upstream.close()
                return
            parsed = urlsplit(target)
            host = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if parsed.scheme != "http":
                return
            upstream = connect_allowed(host, port)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            filtered = [
                (name, value) for name, value in headers
                if name.lower() not in {"connection", "proxy-connection", "host"}
            ]
            request = (
                f"{method} {path} {version}\r\nHost: {host}\r\n"
                + "".join(f"{name}: {value}\r\n" for name, value in filtered)
                + "Connection: close\r\n\r\n"
            ).encode("iso-8859-1")
            upstream.sendall(request + remainder)
            try:
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    self.request.sendall(chunk)
            finally:
                upstream.close()
        except (OSError, ValueError):
            try:
                self.request.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

with Server(("0.0.0.0", 8080), Handler) as server:
    server.serve_forever(poll_interval=0.5)
"""


def _relative_path(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError(f"{name.capitalize()} path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in value
        or "\x00" in value
        or str(path) != value
    ):
        raise ValueError(f"{name.capitalize()} path is invalid")
    return value


def _hash(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"Dependency {name} is invalid")
    return value
