"""Credential-free dependency preparation through one allowlisted proxy."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import tempfile
import time

logger = logging.getLogger(__name__)
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
WORKSPACE_DEPENDENCY_PLAN_VERSION = "workspace-dependency-plan-v2"
DYNAMIC_WORKSPACE_DEPENDENCY_PLAN_VERSION = "workspace-dependency-plan-v3"
SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION = "workspace-dependency-plan-v4"
NODE_WORKSPACE_DEPENDENCY_PLAN_VERSION = "workspace-dependency-plan-v5"
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
    def __init__(self, message: str, *, code: str = "environment_preparation_failed", safe_message: str = "沙箱依赖准备未完成；尚未进入代码修改。", diagnostic: dict | None = None) -> None:
        super().__init__(message)
        self.code, self.safe_message = code, safe_message
        self.diagnostic = diagnostic


class SourceBuildRequired(DependencyPreparationError):
    def __init__(self, request: dict) -> None:
        super().__init__("Source build required")
        self.request = request


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
    source_kind: str = "implement"
    project_directory: str = "."
    local_projects: tuple[str, ...] = ()
    metadata_strategy: str = "declarative-v1"
    distribution_strategy: str = "wheel-only-v1"
    builder_image_digest: str | None = None
    preparation_version: str = "python-workspace-preparation-v1"
    native_resource: dict | None = None
    project_directories: tuple[str, ...] = ()
    node_environment: dict | None = None

    def __post_init__(self) -> None:
        if self.version not in {DEPENDENCY_PLAN_VERSION, WORKSPACE_DEPENDENCY_PLAN_VERSION, DYNAMIC_WORKSPACE_DEPENDENCY_PLAN_VERSION, SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION, NODE_WORKSPACE_DEPENDENCY_PLAN_VERSION}:
            raise ValueError("Dependency plan version is unsupported")
        workspace = self.version != DEPENDENCY_PLAN_VERSION
        if self.native_resource is not None:
            from app.sandbox_worker.native_resources import validate_resource_binding
            if not workspace or self.ecosystem != "python":
                raise ValueError("Native resource binding is invalid")
            validate_resource_binding(self.native_resource)
        source_build = self.version == SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION
        node = self.version == NODE_WORKSPACE_DEPENDENCY_PLAN_VERSION
        if node:
            from app.node_environments import validate_node_recipe
            validate_node_recipe(self.node_environment)
            if (self.ecosystem != "node" or self.preparation_version != "node-workspace-preparation-v1"
                    or self.node_environment["project_directory"] != self.project_directory):
                raise ValueError("Node environment recipe does not match its plan")
        elif self.node_environment is not None:
            raise ValueError("Legacy plans cannot contain Node recipes")
        if self.preparation_version not in {"python-workspace-preparation-v1", "python-workspace-preparation-v2", "python-workspace-preparation-v3", "node-workspace-preparation-v1"} or (
            self.preparation_version != "python-workspace-preparation-v1" and not node and (not workspace or self.ecosystem != "python")
        ):
            raise ValueError("Python preparation recipe is unsupported")
        if self.preparation_version == "python-workspace-preparation-v3":
            if (not isinstance(self.project_directories, tuple) or not 2 <= len(self.project_directories) <= 4
                    or any(not isinstance(directory, str) for directory in self.project_directories)
                    or self.project_directories != tuple(sorted(set(self.project_directories)))
                    or self.project_directory != self.project_directories[0]
                    or set(self.project_directories) & set(self.local_projects)
                    or len(self.project_directories) + len(self.local_projects) > 8):
                raise ValueError("Workspace selected project roots are invalid")
            for directory in self.project_directories:
                if directory != ".":
                    _relative_path(directory, name="selected project directory")
        elif self.project_directories:
            raise ValueError("Legacy recipes cannot contain multiple selected projects")
        if source_build:
            if self.distribution_strategy != "source-wheel-v1" or not isinstance(self.builder_image_digest,str) or not _IMAGE.fullmatch(self.builder_image_digest):
                raise ValueError("Source builds require a signed strategy and builder image")
        elif node:
            if self.builder_image_digest is not None:
                if self.distribution_strategy != "node-offline-build-v1" or not _IMAGE.fullmatch(self.builder_image_digest):
                    raise ValueError("Node builds require a signed builder image")
            elif self.distribution_strategy != "wheel-only-v1":
                raise ValueError("Node build strategy requires its builder image")
        elif self.distribution_strategy != "wheel-only-v1" or self.builder_image_digest is not None:
            raise ValueError("Legacy dependency strategies remain wheel-only")
        if (source_build and self.metadata_strategy not in {"wheel-metadata-v1","declarative-v1"}) or (not source_build and self.metadata_strategy != ("wheel-metadata-v1" if self.version == DYNAMIC_WORKSPACE_DEPENDENCY_PLAN_VERSION else "declarative-v1")):
            raise ValueError("Dependency metadata strategy does not match its version")
        if workspace:
            if self.source_kind != "explore":
                raise ValueError("Workspace dependencies require an Explore source")
            if self.project_directory != ".":
                _relative_path(self.project_directory, name="project directory")
            if len(self.local_projects) > 6 or len(set(self.local_projects)) != len(self.local_projects):
                raise ValueError("Workspace local project count is invalid")
            for project in self.local_projects:
                _relative_path(project, name="local project directory")
        elif self.source_kind != "implement" or self.project_directory != "." or self.local_projects:
            raise ValueError("Legacy dependency plan cannot contain workspace fields")
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
        if self.version in {DYNAMIC_WORKSPACE_DEPENDENCY_PLAN_VERSION,SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION} and ecosystem is not DependencyEcosystem.PYTHON:
            raise ValueError("Dynamic metadata preparation requires Python")
        if (
            not manifests
            or len(manifests) > (64 if node else 16 if workspace else 2)
            or any(not isinstance(item, DependencyManifest) for item in manifests)
            or len({item.path for item in manifests}) != len(manifests)
        ):
            raise ValueError("Dependency manifests are invalid")
        paths = tuple(sorted(item.path for item in manifests))
        if workspace:
            prefix = "" if self.project_directory == "." else self.project_directory + "/"
            if ecosystem is DependencyEcosystem.PYTHON:
                if ((prefix + "pyproject.toml" not in paths and not (len(paths) == 1 and paths[0].endswith((".txt", ".lock"))))
                        or any(not p.endswith((".toml", ".txt", ".lock")) for p in paths)):
                    raise ValueError("Workspace Python dependencies require a pyproject manifest")
                if any(p + "/pyproject.toml" not in paths for p in self.local_projects):
                    raise ValueError("Local workspace projects require frozen manifests")
                if any(("" if p == "." else p + "/") + "pyproject.toml" not in paths for p in self.project_directories):
                    raise ValueError("Selected workspace projects require frozen manifests")
            elif node:
                modern = self.node_environment["version"] == "node-environment-v2"
                lock = self.node_environment["lockfile"] if modern else prefix + {"npm": "package-lock.json", "yarn": "yarn.lock", "pnpm": "pnpm-lock.yaml"}[self.node_environment["manager"]]
                allowed_names = {"package.json", ".nvmrc", ".node-version", ".bun-version", ".tool-versions", ".yarnrc.yml", "pnpm-workspace.yaml", "bunfig.toml"}
                if lock: allowed_names.add(PurePosixPath(lock).name)
                if (prefix + "package.json" not in paths or lock is not None and lock not in paths
                        or any(prefix and not p.startswith(prefix) for p in paths)
                        or any(PurePosixPath(p).name not in allowed_names
                               and not (p.startswith(prefix+".github/workflows/") and p.endswith((".yml",".yaml"))) for p in paths)
                        or any(("" if p == "." else p + "/") + "package.json" not in paths for p in self.node_environment["package_roots"])):
                    raise ValueError("Node workspace manifest binding is invalid")
            elif paths != tuple(sorted((prefix + "package-lock.json", prefix + "package.json"))):
                raise ValueError("Workspace Node dependencies require package.json and package-lock.json")
        elif ecosystem is DependencyEcosystem.PYTHON:
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
        value = {
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
        if self.version != DEPENDENCY_PLAN_VERSION:
            value["source_result_hash"] = value.pop("implement_result_hash")
            value.update(source_kind=self.source_kind, project_directory=self.project_directory,
                         local_projects=list(self.local_projects))
        if self.version in {DYNAMIC_WORKSPACE_DEPENDENCY_PLAN_VERSION,SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION}:
            value["metadata_strategy"] = self.metadata_strategy
        if self.version == SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION:
            value.update(distribution_strategy=self.distribution_strategy,builder_image_digest=self.builder_image_digest)
        elif self.node_environment is not None and self.builder_image_digest is not None:
            value.update(distribution_strategy=self.distribution_strategy, builder_image_digest=self.builder_image_digest)
        if self.preparation_version != "python-workspace-preparation-v1":
            value["preparation_version"] = self.preparation_version
        if self.native_resource is not None:
            value["native_resource"] = self.native_resource
        if self.project_directories:
            value["project_directories"] = list(self.project_directories)
            value["environment_layout"] = "per-package-v1"
        if self.node_environment is not None:
            value["node_environment"] = self.node_environment
        return value


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
    native_resource: dict | None = None
    node_environment: dict | None = None
    execution_image_digest: str | None = None

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
        if self.native_resource is not None:
            from app.sandbox_worker.native_resources import validate_resource_binding
            validate_resource_binding(self.native_resource)
        if self.node_environment is not None:
            from app.node_environments import validate_node_recipe
            validate_node_recipe(self.node_environment)
        if self.execution_image_digest is not None and (self.node_environment is None or not _IMAGE.fullmatch(self.execution_image_digest)):
            raise ValueError("Execution image requires a frozen Node environment")


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
    source_kind: str = "implement"
    native_resource: dict | None = None
    environment_receipt: dict | None = None

    def __post_init__(self) -> None:
        if self.native_resource is not None:
            from app.sandbox_worker.native_resources import validate_resource_binding
            if self.source_kind != "explore":
                raise ValueError("Native resources require workspace preparation")
            validate_resource_binding(self.native_resource)
        if self.environment_receipt is not None:
            from app.node_environments import validate_node_receipt
            if self.version != "workspace-dependency-result-v3" or self.ecosystem != "node":
                raise ValueError("Node receipts require a versioned Node result")
            validate_node_receipt(self.environment_receipt)
        elif self.version == "workspace-dependency-result-v3":
            raise ValueError("Node result requires environment evidence")
        if self.version not in {DEPENDENCY_RESULT_VERSION, "workspace-dependency-result-v2", "workspace-dependency-result-v3"}:
            raise DependencyPreparationError(
                "Dependency result version is unsupported"
            )
        if self.source_kind != ("explore" if self.version in {"workspace-dependency-result-v2", "workspace-dependency-result-v3"} else "implement"):
            raise DependencyPreparationError("Dependency result source kind is invalid")
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
        environment_receipt: dict | None = None,
    ) -> "DependencyPreparationResult":
        payload = {
            "version": "workspace-dependency-result-v2" if plan.source_kind == "explore" else DEPENDENCY_RESULT_VERSION,
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
        if plan.source_kind == "explore":
            payload["source_result_hash"] = payload.pop("implement_result_hash")
            payload["source_kind"] = "explore"
        if plan.native_resource is not None:
            payload["native_resource"] = plan.native_resource
        if plan.node_environment is not None:
            from app.node_environments import validate_node_receipt
            validate_node_receipt(environment_receipt, plan.node_environment)
            if environment_receipt["execution_image_digest"] != (plan.builder_image_digest or plan.runner_image_digest):
                raise ValueError("Prepared execution image differs from signed environment")
            payload.update(version="workspace-dependency-result-v3", environment_receipt=environment_receipt)
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
            version=payload["version"], source_kind=plan.source_kind,
            native_resource=plan.native_resource,
            environment_receipt=environment_receipt,
        )

    def hash_payload(self) -> dict[str, object]:
        value = self.to_wire()
        value.pop("result_hash")
        return value

    def to_wire(self) -> dict[str, object]:
        value = {
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
        if self.source_kind == "explore":
            value["source_result_hash"] = value.pop("implement_result_hash")
            value["source_kind"] = self.source_kind
        if self.native_resource is not None:
            value["native_resource"] = self.native_resource
        if self.environment_receipt is not None:
            value["environment_receipt"] = self.environment_receipt
        return value

    @classmethod
    def from_payload(cls, value: dict) -> "DependencyPreparationResult":
        values = dict(value)
        if values.get("version") in {"workspace-dependency-result-v2", "workspace-dependency-result-v3"}:
            values["implement_result_hash"] = values.pop("source_result_hash")
        return cls(**values)


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
        on_workspace=None,
        native_resource_path: str | None = None,
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
        if plan.native_resource is not None or plan.node_environment is not None:
            from dataclasses import replace
            workspace = replace(workspace, native_resource=plan.native_resource, node_environment=plan.node_environment,
                                execution_image_digest=(plan.builder_image_digest or plan.runner_image_digest) if plan.node_environment else None)
        try:
            if on_workspace is not None:
                on_workspace(workspace)
            with tempfile.TemporaryDirectory(
                prefix="contribos-dependency-plan-"
            ) as temporary:
                os.chmod(temporary, 0o755)
                plan_path = os.path.join(temporary, "dependency-plan.json")
                with open(plan_path, "xb") as handle:
                    handle.write(signed_plan.to_bytes())
                os.chmod(plan_path, 0o444)
                deadline=time.monotonic()+sandbox_policy.timeout_seconds
                for build_round in range(9):
                    name = f"contribos-dependency-worker-{uuid4().hex}"
                    argv=self.build_prepare_argv(
                        repository_volume=repository_volume,
                        dependency_workspace=workspace,
                        signed_plan_path=plan_path,
                        proxy_lease=proxy_lease,
                        policy=sandbox_policy,
                        container_name=name,
                        scratch_bytes=min(536870912,sandbox_policy.memory_bytes//2,sandbox_policy.disk_bytes//4)
                            if plan.version in {SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION, NODE_WORKSPACE_DEPENDENCY_PLAN_VERSION} else 134217728)
                    if plan.native_resource is not None:
                        from pathlib import Path
                        from app.sandbox_worker.native_resources import verify_cache
                        if native_resource_path is None or not os.path.isabs(native_resource_path) or "," in native_resource_path:
                            raise DependencyPreparationError("Native resources are missing")
                        verify_cache(Path(native_resource_path), plan.native_resource["files"])
                        index = argv.index(plan.runner_image_digest)
                        argv = (*argv[:index], "--mount", f"type=bind,src={native_resource_path},dst=/native-input,readonly", *argv[index:])
                    elif native_resource_path is not None:
                        raise DependencyPreparationError("Native resources require a signed binding")
                    remaining=int(deadline-time.monotonic())
                    if remaining<=0: raise DependencyPreparationError('Dependency preparation timed out')
                    try:
                        output=await self._run(argv,container_name=name,timeout_seconds=remaining)
                        if plan.node_environment is not None:
                            offline_name = f"contribos-dependency-worker-{uuid4().hex}"
                            offline_argv = []
                            position = 0
                            while position < len(argv):
                                item = argv[position]
                                if item in {"--network", "--name", "--env"}:
                                    value = argv[position + 1]
                                    position += 2
                                    if item == "--network": value = "none"
                                    elif item == "--name": value = offline_name
                                    elif value.split("=", 1)[0] in {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}: continue
                                    offline_argv.extend((item, value))
                                else:
                                    offline_argv.append(item)
                                    position += 1
                            at = offline_argv.index(plan.runner_image_digest)
                            offline_argv[at:at] = ["--env", "CONTRIBOS_NODE_OFFLINE=1"]
                            if plan.builder_image_digest:
                                offline_argv[offline_argv.index(plan.runner_image_digest)] = plan.builder_image_digest
                            output = await self._run(tuple(offline_argv), container_name=offline_name,
                                timeout_seconds=max(1, int(deadline-time.monotonic())))
                        break
                    except SourceBuildRequired as request:
                        if plan.version!=SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION or build_round>=8:
                            raise DependencyPreparationError('Source build not authorized or budget exhausted') from None
                        from app.sandbox_worker.source_wheels import validate_source_build_request
                        value=validate_source_build_request(request.request)
                        builder=f"contribos-dependency-worker-{uuid4().hex}"
                        offline=self.build_source_argv(argv,workspace=workspace,plan=plan,request=value,container_name=builder)
                        remaining=int(deadline-time.monotonic())
                        if remaining<=0: raise DependencyPreparationError('Dependency preparation timed out')
                        built=await self._run(offline,container_name=builder,timeout_seconds=remaining,max_output_bytes=44_000_000)
                        built_value=json.loads(built)
                        if set(built_value)!={'receipt','wheel_data'} or built_value['receipt'].get('request')!=value:
                            raise DependencyPreparationError('Invalid source build result')
                        wheel_path=os.path.join(temporary,'wheel.json')
                        with open(wheel_path,'wb') as handle: handle.write(built)
                        os.chmod(wheel_path,0o444)
                        from app.sandbox_worker.source_wheels import IMPORT_SOURCE_WHEEL_SCRIPT
                        importer=f'contribos-dependency-worker-{uuid4().hex}'
                        export=self.build_source_import_argv(argv,workspace=workspace,wheel_path=wheel_path,container_name=importer)
                        result=await self._run(export,container_name=importer,timeout_seconds=max(1,int(deadline-time.monotonic())))
                        if json.loads(result)!={'version':'source-wheel-built-v1','package':value['package']}:
                            raise DependencyPreparationError('Invalid source import result')
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
            } | ({"environment_receipt"} if plan.node_environment is not None else set()):
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
                environment_receipt=raw.get("environment_receipt"),
            )
            completed_workspace = DependencyWorkspace(
                workspace_id=workspace.workspace_id,
                volume_name=workspace.volume_name,
                runner_image_digest=workspace.runner_image_digest,
                sandbox_policy_hash=workspace.sandbox_policy_hash,
                plan_hash=workspace.plan_hash,
                inventory_hash=inventory_hash,
                native_resource=plan.native_resource,
                node_environment=plan.node_environment,
                execution_image_digest=(plan.builder_image_digest or plan.runner_image_digest) if plan.node_environment else None,
            )
            return DependencyExecution(
                result=result,
                workspace=completed_workspace,
            )
        except BaseException:
            await self.destroy(workspace, ignore_missing=True)
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

    def build_source_argv(self, argv: tuple[str,...], *, workspace: DependencyWorkspace,
                          plan: DependencyPreparationPlan, request: dict, container_name: str) -> tuple[str,...]:
        """Offline compiler gets only the bounded dependency volume, no repository."""
        from inspect import getsource
        from app.sandbox_worker.source_wheels import OFFLINE_SOURCE_BUILD_SCRIPT, validate_source_build_request
        from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure
        validate_source_build_request(request)
        if plan.version!=SOURCE_WORKSPACE_DEPENDENCY_PLAN_VERSION or not _WORKER_CONTAINER.fullmatch(container_name):
            raise ValueError('Source build is not authorized')
        result=[]
        index=0
        while index<len(argv):
            item=argv[index]
            if item in {'--network','--name','--env','--mount'}:
                value=argv[index+1]; index+=2
                if item=='--network': value='none'
                elif item=='--name': value=container_name
                elif item=='--env' and value.split('=',1)[0] in {'HTTP_PROXY','HTTPS_PROXY','NO_PROXY'}: continue
                elif item=='--mount': continue
                result.extend((item,value)); continue
            result.append(plan.builder_image_digest if item==workspace.runner_image_digest else item)
            index+=1
        script=getsource(summarize_dependency_failure)+OFFLINE_SOURCE_BUILD_SCRIPT.replace('__GIT_ENVIRONMENT_NAMES__',repr(GIT_SAFETY_ENVIRONMENT_NAMES))
        image_index=result.index(plan.builder_image_digest)
        result[image_index:image_index]=['--mount',f'type=volume,src={workspace.volume_name},dst=/source-inputs,readonly,volume-subpath=output/{request["build_root"]}',
            ]
        result[-1]=script
        result.append(canonical_json(request))
        return tuple(result)

    def build_source_import_argv(self, argv: tuple[str,...], *, workspace: DependencyWorkspace,
                                 wheel_path: str, container_name: str) -> tuple[str,...]:
        from app.sandbox_worker.source_wheels import IMPORT_SOURCE_WHEEL_SCRIPT
        result=[]; index=0
        while index<len(argv):
            item=argv[index]
            if item in {'--network','--name','--env','--mount'}:
                value=argv[index+1]; index+=2
                if item=='--network': value='none'
                elif item=='--name': value=container_name
                elif item=='--env' and value.split('=',1)[0] in {'HTTP_PROXY','HTTPS_PROXY','NO_PROXY'}: continue
                elif item=='--mount' and ('dst=/workspace,' in value or 'dst=/input/' in value or 'dst=/native-input,' in value): continue
                result.extend((item,value)); continue
            result.append(item); index+=1
        image_index=result.index(workspace.runner_image_digest)
        result[image_index:image_index]=['--mount',f'type=bind,src={wheel_path},dst=/input/wheel.json,readonly']
        result[-1]=IMPORT_SOURCE_WHEEL_SCRIPT
        return tuple(result)

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
        scratch_bytes: int = 134217728,
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
        if type(scratch_bytes) is not int or not 16*1024*1024<=scratch_bytes<=min(policy.disk_bytes,policy.memory_bytes//2):
            raise ValueError('Dependency scratch budget is invalid')
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
            f"/tmp:rw,noexec,nosuid,nodev,size={scratch_bytes},mode=1777",
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
                "bind-propagation=rslave"
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
        max_output_bytes: int = MAX_DEPENDENCY_OUTPUT_BYTES,
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
            async def bounded(stream,limit):
                output=bytearray()
                while chunk:=await stream.read(8192):
                    output.extend(chunk)
                    if len(output)>limit: raise ValueError('Dependency output budget exceeded')
                return bytes(output)
            if hasattr(process,'stdout'):
                stdout,stderr=await asyncio.wait_for(asyncio.gather(bounded(process.stdout,max_output_bytes),bounded(process.stderr,4096)),timeout=timeout_seconds)
                await process.wait()
            else:
                stdout,stderr=await asyncio.wait_for(process.communicate(),timeout=timeout_seconds)
        except asyncio.CancelledError:
            await self._terminate(process, container_name)
            raise
        except TimeoutError:
            await self._terminate(process, container_name)
            raise DependencyPreparationError(
                "Dependency preparation timed out"
            ) from None
        except ValueError:
            await self._terminate(process,container_name)
            raise DependencyPreparationError('Dependency output budget exceeded') from None
        if (
            process.returncode != 0
            or stderr
            or len(stdout) > max_output_bytes
        ):
            logger.error("Sandbox container failed: code=%s stderr_bytes=%s stdout_bytes=%s",
                         process.returncode, len(stderr), len(stdout))
            phase = {
                110: ("environment_node_recipe_invalid", "Node 环境版本、工作区链接或冻结清单校验失败；尚未修改代码。"),
                111: ("environment_node_preparation_failed", "Node 工具、依赖解析或离线构建未完成；尚未修改代码。"),
                94: ("environment_preparation_failed", "隔离 Python 环境创建未完成；尚未修改代码。"),
                91: ("environment_manifest_unsupported", "所选包使用动态依赖声明，需要独立适配；尚未修改代码。"),
                92: ("environment_manifest_unsupported", "依赖数量超出准备预算，请缩小工作区范围。"),
                93: ("environment_manifest_unsupported", "依赖声明包含尚未支持的路径、URL 或安装选项；未执行这些声明。"),
                95: ("environment_dependency_resolution_failed", "受限包代理中的 wheel 解析未完成，请检查版本约束和可用性；尚未修改代码。"),
                97: ("environment_dependency_install_failed", "冻结的 wheel 安装未完成；尚未修改代码。"),
                98: ("environment_build_failed", "所选包的隔离离线构建未完成，需要检查构建后端和包配置；尚未修改代码。"),
                99: ("environment_build_failed", "本地包 wheel 安装未完成；尚未修改代码。"),
                100: ("environment_metadata_invalid", "隔离构建返回的包元数据或依赖声明未通过校验；尚未修改代码。"),
                103: ("environment_source_invalid", "依赖源码、版本约束或构建声明未通过校验；尚未修改代码。"),
                104: ("environment_source_build_failed", "依赖的离线源码编译未完成；尚未修改代码。"),
            }.get(process.returncode)
            if process.returncode==102 and not stderr and len(stdout)<=4096:
                from app.sandbox_worker.source_wheels import validate_source_build_request
                try: request=validate_source_build_request(json.loads(stdout))
                except (ValueError,TypeError,KeyError): pass
                else: raise SourceBuildRequired(request)
            if phase:
                from app.sandbox_worker.dependency_diagnostics import validate_dependency_diagnostic, dependency_diagnostic_message
                diagnostic = None
                if not stderr and len(stdout) <= 4096:
                    try:
                        diagnostic = validate_dependency_diagnostic(json.loads(stdout))
                    except (ValueError, TypeError, KeyError):
                        pass
                if diagnostic:
                    raise DependencyPreparationError("Dependency preparation stopped", code=phase[0],
                        safe_message=dependency_diagnostic_message(diagnostic), diagnostic=diagnostic)
                raise DependencyPreparationError("Dependency preparation stopped", code=phase[0], safe_message=phase[1])
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
        except asyncio.CancelledError:
            await self.stop(lease, ignore_missing=True)
            raise
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
node_offline = plan.get("node_environment") is not None and os.environ.get("CONTRIBOS_NODE_OFFLINE") == "1"
if proxy != "http://dependency-proxy:8080" and not (node_offline and proxy is None):
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
if node_offline:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        environment.pop(key, None)
if plan.get("node_environment") is not None:
    dependency_root = "/dependencies/output"
    __NODE_PREPARATION__
elif plan["ecosystem"] == "python" and plan["version"] in {"workspace-dependency-plan-v2", "workspace-dependency-plan-v3", "workspace-dependency-plan-v4"} and any(m["path"].endswith("pyproject.toml") for m in plan["manifests"]):
    __WORKSPACE_PYTHON_PREPARATION__
elif plan["ecosystem"] == "python":
    dependency_root = "/dependencies/output"
    lockfile = os.path.join(
        "/workspace", *plan["manifests"][0]["path"].split("/")
    )
    subprocess.run(
        ["python3", "-m", "venv", f"{dependency_root}/venv"],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=environment,
    )

native_resource = plan.get("native_resource")
if native_resource is not None:
    native_directory = "/dependencies/output/native-tools"
    os.makedirs(native_directory, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        source = "/native-input/" + name
        expected = native_resource["files"][name]
        if os.path.islink(source) or not os.path.isfile(source) or os.stat(source).st_size != expected["size"]:
            raise SystemExit(85)
        with open(source, "rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != expected["sha256"]:
                raise SystemExit(85)
        target = native_directory + "/" + name
        # Build steps may have written untrusted output. Never follow their links.
        if os.path.islink(native_directory) or os.path.lexists(target):
            raise SystemExit(85)
        shutil.copyfile(source, target)
        os.chmod(target, 0o555)
    with open(native_directory + "/receipt.json", "x", encoding="utf-8") as handle:
        json.dump(native_resource, handle, sort_keys=True, separators=(",", ":"))

entries = []
total = 0
for current, directories, files in os.walk(
    dependency_root, topdown=True, followlinks=False
):
    directories.sort()
    files.sort()
    if plan.get("node_environment") is not None:
        for name in list(directories):
            absolute = os.path.join(current, name)
            if os.path.islink(absolute):
                directories.remove(name)
                entries.append([os.path.relpath(absolute, dependency_root), "symlink", os.readlink(absolute)])
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
if plan.get("node_environment") is not None:
    # A workspace link may be broken here but resolve to a directory in Verify.
    # Bind link identity, not os.walk's target-dependent file/dir grouping.
    entries.sort(key=lambda item: item[0])
inventory = hashlib.sha256(
    json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
print(json.dumps({
    "inventory_hash": inventory,
    "file_count": len(entries),
    "total_bytes": total,
    **({"environment_receipt": json.load(open("/dependencies/output/node-environment.json"))}
       if plan.get("node_environment") is not None else {}),
}, sort_keys=True, separators=(",", ":")))
""".replace(
    "__GIT_ENVIRONMENT_NAMES__",
    repr(GIT_SAFETY_ENVIRONMENT_NAMES),
)

from app.sandbox_worker.workspace_dependencies import WORKSPACE_PYTHON_SCRIPT, MULTIPACKAGE_PYTHON_SCRIPT
from inspect import getsource
from app.node_environments import validate_node_recipe
from app.sandbox_worker.node_dependencies import prepare_node_dependencies, restore_unchanged_json_manifest
from app.sandbox_worker.node_cache import prepare_node_cache_files
from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure
_node_program = getsource(validate_node_recipe) + getsource(summarize_dependency_failure) + getsource(restore_unchanged_json_manifest) + getsource(prepare_node_cache_files) + getsource(prepare_node_dependencies) + "\nprepare_node_dependencies(plan, environment, node_offline)"
_PREPARE_SCRIPT = _PREPARE_SCRIPT.replace("    __NODE_PREPARATION__", "\n".join("    " + line for line in _node_program.splitlines()))
_workspace_program = "if plan.get('preparation_version') == 'python-workspace-preparation-v3':\n" + "\n".join(
    "    " + line for line in MULTIPACKAGE_PYTHON_SCRIPT.splitlines()) + "\nelse:\n" + "\n".join(
    "    " + line for line in WORKSPACE_PYTHON_SCRIPT.splitlines())
_PREPARE_SCRIPT = _PREPARE_SCRIPT.replace("    __WORKSPACE_PYTHON_PREPARATION__", "\n".join("    " + line for line in _workspace_program.splitlines()))

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
    candidates = resolve_package_addresses(host, port, allowed)
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
    raise OSError("unreachable")

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
        except (OSError, ValueError) as error:
            try:
                self.request.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
                    if isinstance(error, ValueError) else
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

with Server(("0.0.0.0", 8080), Handler) as server:
    server.serve_forever(poll_interval=0.5)
"""

from inspect import getsource
from app.sandbox_worker.package_dns import fetch_public_package_dns, resolve_package_addresses
_PROXY_SCRIPT = getsource(fetch_public_package_dns) + "\n" + getsource(resolve_package_addresses) + "\n" + _PROXY_SCRIPT


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
