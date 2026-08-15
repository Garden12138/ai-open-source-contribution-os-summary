"""Plan-bound changes inside one opaque disposable Docker workspace."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol
from uuid import uuid4

from app.provenance import content_hash
from app.sandbox_worker.explore import (
    ExploreResult,
    RepositoryArchive,
    _MATERIALIZE_SCRIPT,
)
from app.sandbox_worker.git_safety import docker_git_safety_argv
from app.sandbox_worker.specs import (
    JobSpecSignatureError,
    JobSpecSigner,
    SandboxPolicy,
    SandboxStage,
    SignedJobSpec,
)
from app.security import contains_sensitive_text, ensure_no_sensitive_data


CHANGE_SET_VERSION = "implementation-change-set-v1"
IMPLEMENT_RESULT_VERSION = "sandbox-implement-result-v2"
MAX_CHANGE_OPERATIONS = 500
MAX_CHANGE_FILE_BYTES = 1_000_000
MAX_CHANGE_SET_BYTES = 4_000_000
MAX_IMPLEMENT_OUTPUT_BYTES = 32_000_000
MAX_UNIFIED_DIFF_BYTES = 8_000_000
MAX_FILE_INVENTORY_ENTRIES = 20_000
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_VOLUME = re.compile(r"^contribos-workspace-[0-9a-f]{32}$")
_CONTAINER = re.compile(
    r"^contribos-implement-(?:materialize|apply)-[0-9a-f]{32}$"
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


class ImplementationError(RuntimeError):
    pass


class ChangeOperationKind(StrEnum):
    WRITE = "write"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class ChangeOperation:
    path: str
    kind: ChangeOperationKind
    expected_prior_hash: str | None
    content: str | None = None
    executable: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "path",
            _relative_path(self.path, name="change path"),
        )
        try:
            kind = ChangeOperationKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("Change operation kind is invalid") from exc
        object.__setattr__(self, "kind", kind)
        if self.expected_prior_hash is not None:
            _hash(self.expected_prior_hash, name="expected prior hash")
        if kind is ChangeOperationKind.WRITE:
            if (
                not isinstance(self.content, str)
                or len(self.content.encode("utf-8")) > MAX_CHANGE_FILE_BYTES
                or not isinstance(self.executable, bool)
            ):
                raise ValueError("Write operation content is invalid")
        elif (
            self.expected_prior_hash is None
            or self.content is not None
            or self.executable is not None
        ):
            raise ValueError("Delete operation fields are invalid")
        ensure_no_sensitive_data(
            self.to_wire(),
            context="implementation change operation",
        )

    @property
    def content_hash(self) -> str | None:
        return (
            None
            if self.content is None
            else content_hash({"utf8": self.content})
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "expected_prior_hash": self.expected_prior_hash,
            "content": self.content,
            "content_hash": self.content_hash,
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class ImplementationChangeSet:
    change_set_id: str
    plan_version_id: str
    plan_content_hash: str
    plan_record_hash: str
    operations: tuple[ChangeOperation, ...]
    version: str = CHANGE_SET_VERSION

    def __post_init__(self) -> None:
        if self.version != CHANGE_SET_VERSION:
            raise ValueError("Implementation ChangeSet version is unsupported")
        for value, name in (
            (self.change_set_id, "change set ID"),
            (self.plan_version_id, "plan version ID"),
        ):
            if (
                not isinstance(value, str)
                or not _IDENTIFIER.fullmatch(value)
                or contains_sensitive_text(value)
            ):
                raise ValueError(f"Implementation {name} is invalid")
        _hash(self.plan_content_hash, name="plan content hash")
        _hash(self.plan_record_hash, name="plan record hash")
        operations = tuple(self.operations)
        if (
            not operations
            or len(operations) > MAX_CHANGE_OPERATIONS
            or any(not isinstance(item, ChangeOperation) for item in operations)
            or len({item.path for item in operations}) != len(operations)
        ):
            raise ValueError("Implementation ChangeSet operations are invalid")
        object.__setattr__(self, "operations", operations)
        ensure_no_sensitive_data(self.to_wire(), context="Implementation ChangeSet")
        if len(self.to_bytes()) > MAX_CHANGE_SET_BYTES:
            raise ValueError("Implementation ChangeSet exceeds the byte limit")

    @property
    def change_set_hash(self) -> str:
        return content_hash(self.to_wire())

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(operation.path for operation in self.operations))

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "change_set_id": self.change_set_id,
            "plan_version_id": self.plan_version_id,
            "plan_content_hash": self.plan_content_hash,
            "plan_record_hash": self.plan_record_hash,
            "operations": [operation.to_wire() for operation in self.operations],
        }

    def to_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_wire(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_wire(cls, value: object) -> "ImplementationChangeSet":
        if not isinstance(value, dict):
            raise ValueError("Implementation ChangeSet is invalid")
        raw_operations = value.get("operations")
        if not isinstance(raw_operations, list):
            raise ValueError("Implementation ChangeSet operations are invalid")
        operations = []
        for item in raw_operations:
            if not isinstance(item, dict):
                raise ValueError("Implementation ChangeSet operations are invalid")
            operations.append(
                ChangeOperation(
                    path=str(item["path"]),
                    kind=ChangeOperationKind(item["kind"]),
                    expected_prior_hash=item.get("expected_prior_hash"),
                    content=item.get("content"),
                    executable=item.get("executable"),
                )
            )
        return cls(
            change_set_id=str(value["change_set_id"]),
            plan_version_id=str(value["plan_version_id"]),
            plan_content_hash=str(value["plan_content_hash"]),
            plan_record_hash=str(value["plan_record_hash"]),
            operations=tuple(operations),
            version=str(value.get("version", CHANGE_SET_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class DisposableWorkspace:
    workspace_id: str
    volume_name: str
    runner_image_digest: str
    sandbox_policy_hash: str
    inventory_hash: str | None = None

    def __post_init__(self) -> None:
        if (
            not _IDENTIFIER.fullmatch(self.workspace_id)
            or not _VOLUME.fullmatch(self.volume_name)
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                self.runner_image_digest,
            )
        ):
            raise ValueError("Disposable workspace identity is invalid")
        _hash(self.sandbox_policy_hash, name="workspace policy hash")
        if self.inventory_hash is not None:
            _hash(self.inventory_hash, name="workspace inventory hash")


@dataclass(frozen=True, slots=True)
class FileInventoryEntry:
    path: str
    kind: str
    size: int | None = None
    sha256: str | None = None
    executable: bool | None = None
    target: str | None = None

    def __post_init__(self) -> None:
        _relative_path(self.path, name="inventory path")
        if self.kind == "file":
            if (
                isinstance(self.size, bool)
                or not isinstance(self.size, int)
                or self.size < 0
                or not isinstance(self.executable, bool)
                or self.target is not None
            ):
                raise ImplementationError(
                    "Implementation file inventory entry is invalid"
                )
            _hash(self.sha256, name="inventory file hash")
        elif self.kind == "symlink":
            if (
                self.size is not None
                or self.sha256 is not None
                or self.executable is not None
                or not isinstance(self.target, str)
                or not self.target
                or len(self.target) > 1_000
                or "\x00" in self.target
            ):
                raise ImplementationError(
                    "Implementation symlink inventory entry is invalid"
                )
        else:
            raise ImplementationError(
                "Implementation inventory entry kind is invalid"
            )

    @classmethod
    def from_wire(cls, value: object) -> "FileInventoryEntry":
        if not isinstance(value, dict):
            raise ImplementationError(
                "Implementation inventory entry is invalid"
            )
        if set(value) == {
            "path",
            "type",
            "size",
            "sha256",
            "executable",
        }:
            return cls(
                path=value["path"],
                kind=value["type"],
                size=value["size"],
                sha256=value["sha256"],
                executable=value["executable"],
            )
        if set(value) == {"path", "type", "target"}:
            return cls(
                path=value["path"],
                kind=value["type"],
                target=value["target"],
            )
        raise ImplementationError(
            "Implementation inventory entry is invalid"
        )

    def to_wire(self) -> dict[str, object]:
        if self.kind == "file":
            return {
                "path": self.path,
                "type": self.kind,
                "size": self.size,
                "sha256": self.sha256,
                "executable": self.executable,
            }
        return {
            "path": self.path,
            "type": self.kind,
            "target": self.target,
        }


@dataclass(frozen=True, slots=True)
class ImplementationInspection:
    baseline_inventory_hash: str
    result_inventory_hash: str
    baseline_file_count: int
    result_file_count: int
    baseline_total_bytes: int
    result_total_bytes: int
    changed_paths: tuple[str, ...]
    baseline_inventory: tuple[FileInventoryEntry, ...]
    result_inventory: tuple[FileInventoryEntry, ...]
    unified_diff: str
    diff_hash: str

    def __post_init__(self) -> None:
        for name in ("baseline_inventory_hash", "result_inventory_hash"):
            _hash(getattr(self, name), name=name)
        for name in (
            "baseline_file_count",
            "result_file_count",
            "baseline_total_bytes",
            "result_total_bytes",
        ):
            number = getattr(self, name)
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or number < 0
            ):
                raise ImplementationError(
                    f"Implementation {name.replace('_', ' ')} is invalid"
                )
        object.__setattr__(
            self,
            "changed_paths",
            _path_tuple(self.changed_paths),
        )
        object.__setattr__(
            self,
            "baseline_inventory",
            _inventory(self.baseline_inventory, name="baseline"),
        )
        object.__setattr__(
            self,
            "result_inventory",
            _inventory(self.result_inventory, name="result"),
        )
        if (
            not isinstance(self.unified_diff, str)
            or len(self.unified_diff.encode("utf-8"))
            > MAX_UNIFIED_DIFF_BYTES
        ):
            raise ImplementationError(
                "Implementation unified diff is invalid"
            )
        _hash(self.diff_hash, name="diff hash")
        self.verify()

    @classmethod
    def from_wire(cls, value: object) -> "ImplementationInspection":
        if not isinstance(value, dict) or set(value) != {
            "baseline_inventory_hash",
            "result_inventory_hash",
            "baseline_file_count",
            "result_file_count",
            "baseline_total_bytes",
            "result_total_bytes",
            "changed_paths",
            "baseline_inventory",
            "result_inventory",
            "unified_diff",
            "diff_hash",
        }:
            raise ImplementationError(
                "Implementation inspection response is invalid"
            )
        for name in ("baseline_inventory_hash", "result_inventory_hash"):
            _hash(value[name], name=name)
        for name in (
            "baseline_file_count",
            "result_file_count",
            "baseline_total_bytes",
            "result_total_bytes",
        ):
            number = value[name]
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise ImplementationError(
                    f"Implementation {name.replace('_', ' ')} is invalid"
                )
        changed_paths = _path_tuple(value["changed_paths"])
        baseline_inventory = _inventory(
            value["baseline_inventory"],
            name="baseline",
        )
        result_inventory = _inventory(
            value["result_inventory"],
            name="result",
        )
        if (
            not isinstance(value["unified_diff"], str)
            or len(value["unified_diff"].encode("utf-8"))
            > MAX_UNIFIED_DIFF_BYTES
        ):
            raise ImplementationError(
                "Implementation unified diff is invalid"
            )
        _hash(value["diff_hash"], name="diff hash")
        return cls(
            baseline_inventory_hash=value["baseline_inventory_hash"],
            result_inventory_hash=value["result_inventory_hash"],
            baseline_file_count=value["baseline_file_count"],
            result_file_count=value["result_file_count"],
            baseline_total_bytes=value["baseline_total_bytes"],
            result_total_bytes=value["result_total_bytes"],
            changed_paths=changed_paths,
            baseline_inventory=baseline_inventory,
            result_inventory=result_inventory,
            unified_diff=value["unified_diff"],
            diff_hash=value["diff_hash"],
        )

    def verify(self) -> None:
        baseline_wire = [item.to_wire() for item in self.baseline_inventory]
        result_wire = [item.to_wire() for item in self.result_inventory]
        if (
            content_hash(baseline_wire) != self.baseline_inventory_hash
            or content_hash(result_wire) != self.result_inventory_hash
            or len(baseline_wire) != self.baseline_file_count
            or len(result_wire) != self.result_file_count
            or sum(
                item.size or 0
                for item in self.baseline_inventory
                if item.kind == "file"
            )
            != self.baseline_total_bytes
            or sum(
                item.size or 0
                for item in self.result_inventory
                if item.kind == "file"
            )
            != self.result_total_bytes
        ):
            raise ImplementationError(
                "Implementation inventory evidence does not match"
            )
        before = {item.path: item.to_wire() for item in self.baseline_inventory}
        after = {item.path: item.to_wire() for item in self.result_inventory}
        observed = tuple(
            sorted(
                path
                for path in set(before) | set(after)
                if before.get(path) != after.get(path)
            )
        )
        if observed != self.changed_paths:
            raise ImplementationError(
                "Implementation inventory changes do not match"
            )
        if (
            hashlib.sha256(self.unified_diff.encode("utf-8")).hexdigest()
            != self.diff_hash
        ):
            raise ImplementationError(
                "Implementation diff hash does not match"
            )
        ensure_no_sensitive_data(
            self.to_wire(),
            context="Implementation inspection",
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "baseline_inventory_hash": self.baseline_inventory_hash,
            "result_inventory_hash": self.result_inventory_hash,
            "baseline_file_count": self.baseline_file_count,
            "result_file_count": self.result_file_count,
            "baseline_total_bytes": self.baseline_total_bytes,
            "result_total_bytes": self.result_total_bytes,
            "changed_paths": list(self.changed_paths),
            "baseline_inventory": [
                item.to_wire() for item in self.baseline_inventory
            ],
            "result_inventory": [
                item.to_wire() for item in self.result_inventory
            ],
            "unified_diff": self.unified_diff,
            "diff_hash": self.diff_hash,
        }


@dataclass(frozen=True, slots=True)
class ImplementResult:
    spec_id: str
    spec_hash: str
    execution_attempt_id: str
    explore_result_hash: str
    change_set_id: str
    change_set_hash: str
    repository_full_name: str
    base_commit_sha: str
    repository_archive_hash: str
    plan_version_id: str
    plan_content_hash: str
    plan_record_hash: str
    runner_image_digest: str
    sandbox_policy_version: str
    sandbox_policy_hash: str
    baseline_inventory_hash: str
    result_inventory_hash: str
    baseline_file_count: int
    result_file_count: int
    baseline_total_bytes: int
    result_total_bytes: int
    changed_paths: tuple[str, ...]
    baseline_inventory: tuple[FileInventoryEntry, ...]
    result_inventory: tuple[FileInventoryEntry, ...]
    unified_diff: str
    diff_hash: str
    result_hash: str
    version: str = IMPLEMENT_RESULT_VERSION

    def __post_init__(self) -> None:
        if self.version != IMPLEMENT_RESULT_VERSION:
            raise ImplementationError(
                "Implement result version is unsupported"
            )
        changed_paths = _path_tuple(self.changed_paths)
        object.__setattr__(self, "changed_paths", changed_paths)
        baseline_inventory = _inventory(
            self.baseline_inventory,
            name="baseline",
        )
        result_inventory = _inventory(
            self.result_inventory,
            name="result",
        )
        object.__setattr__(
            self,
            "baseline_inventory",
            baseline_inventory,
        )
        object.__setattr__(self, "result_inventory", result_inventory)
        for name in (
            "spec_hash",
            "explore_result_hash",
            "change_set_hash",
            "repository_archive_hash",
            "plan_content_hash",
            "plan_record_hash",
            "sandbox_policy_hash",
            "baseline_inventory_hash",
            "result_inventory_hash",
            "diff_hash",
            "result_hash",
        ):
            _hash(getattr(self, name), name=name)
        ImplementationInspection(
            baseline_inventory_hash=self.baseline_inventory_hash,
            result_inventory_hash=self.result_inventory_hash,
            baseline_file_count=self.baseline_file_count,
            result_file_count=self.result_file_count,
            baseline_total_bytes=self.baseline_total_bytes,
            result_total_bytes=self.result_total_bytes,
            changed_paths=self.changed_paths,
            baseline_inventory=self.baseline_inventory,
            result_inventory=self.result_inventory,
            unified_diff=self.unified_diff,
            diff_hash=self.diff_hash,
        ).verify()
        if self.result_hash != content_hash(self.hash_payload()):
            raise ImplementationError(
                "Implement result hash does not match its payload"
            )
        ensure_no_sensitive_data(self.to_wire(), context="Implement result")

    @classmethod
    def create(
        cls,
        *,
        signed_spec: SignedJobSpec,
        explore: ExploreResult,
        change_set: ImplementationChangeSet,
        inspection: ImplementationInspection,
    ) -> "ImplementResult":
        spec = signed_spec.spec
        payload = {
            "version": IMPLEMENT_RESULT_VERSION,
            "spec_id": spec.spec_id,
            "spec_hash": signed_spec.spec_hash,
            "execution_attempt_id": spec.execution_attempt_id,
            "explore_result_hash": explore.result_hash,
            "change_set_id": change_set.change_set_id,
            "change_set_hash": change_set.change_set_hash,
            "repository_full_name": spec.repository_full_name,
            "base_commit_sha": spec.base_commit_sha,
            "repository_archive_hash": spec.repository_archive_hash,
            "plan_version_id": spec.plan_version_id,
            "plan_content_hash": spec.plan_content_hash,
            "plan_record_hash": spec.plan_record_hash,
            "runner_image_digest": spec.runner_image_digest,
            "sandbox_policy_version": spec.sandbox_policy_version,
            "sandbox_policy_hash": spec.sandbox_policy_hash,
            **inspection.to_wire(),
        }
        ensure_no_sensitive_data(payload, context="Implement result")
        constructor = dict(payload)
        constructor["changed_paths"] = inspection.changed_paths
        constructor["baseline_inventory"] = inspection.baseline_inventory
        constructor["result_inventory"] = inspection.result_inventory
        return cls(**constructor, result_hash=content_hash(payload))

    def hash_payload(self) -> dict[str, object]:
        value = self.to_wire()
        value.pop("result_hash")
        return value

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "spec_id": self.spec_id,
            "spec_hash": self.spec_hash,
            "execution_attempt_id": self.execution_attempt_id,
            "explore_result_hash": self.explore_result_hash,
            "change_set_id": self.change_set_id,
            "change_set_hash": self.change_set_hash,
            "repository_full_name": self.repository_full_name,
            "base_commit_sha": self.base_commit_sha,
            "repository_archive_hash": self.repository_archive_hash,
            "plan_version_id": self.plan_version_id,
            "plan_content_hash": self.plan_content_hash,
            "plan_record_hash": self.plan_record_hash,
            "runner_image_digest": self.runner_image_digest,
            "sandbox_policy_version": self.sandbox_policy_version,
            "sandbox_policy_hash": self.sandbox_policy_hash,
            "baseline_inventory_hash": self.baseline_inventory_hash,
            "result_inventory_hash": self.result_inventory_hash,
            "baseline_file_count": self.baseline_file_count,
            "result_file_count": self.result_file_count,
            "baseline_total_bytes": self.baseline_total_bytes,
            "result_total_bytes": self.result_total_bytes,
            "changed_paths": list(self.changed_paths),
            "baseline_inventory": [
                item.to_wire() for item in self.baseline_inventory
            ],
            "result_inventory": [
                item.to_wire() for item in self.result_inventory
            ],
            "unified_diff": self.unified_diff,
            "diff_hash": self.diff_hash,
            "result_hash": self.result_hash,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ImplementResult":
        if not isinstance(value, dict):
            raise ImplementationError("Implement result payload is invalid")
        payload = dict(value)
        result_hash = payload.get("result_hash")
        if not isinstance(result_hash, str):
            hashed = dict(payload)
            hashed.pop("result_hash", None)
            result_hash = content_hash(hashed)
        return cls(
            spec_id=str(payload["spec_id"]),
            spec_hash=str(payload["spec_hash"]),
            execution_attempt_id=str(payload["execution_attempt_id"]),
            explore_result_hash=str(payload["explore_result_hash"]),
            change_set_id=str(payload["change_set_id"]),
            change_set_hash=str(payload["change_set_hash"]),
            repository_full_name=str(payload["repository_full_name"]),
            base_commit_sha=str(payload["base_commit_sha"]),
            repository_archive_hash=str(payload["repository_archive_hash"]),
            plan_version_id=str(payload["plan_version_id"]),
            plan_content_hash=str(payload["plan_content_hash"]),
            plan_record_hash=str(payload["plan_record_hash"]),
            runner_image_digest=str(payload["runner_image_digest"]),
            sandbox_policy_version=str(payload["sandbox_policy_version"]),
            sandbox_policy_hash=str(payload["sandbox_policy_hash"]),
            baseline_inventory_hash=str(payload["baseline_inventory_hash"]),
            result_inventory_hash=str(payload["result_inventory_hash"]),
            baseline_file_count=int(payload["baseline_file_count"]),
            result_file_count=int(payload["result_file_count"]),
            baseline_total_bytes=int(payload["baseline_total_bytes"]),
            result_total_bytes=int(payload["result_total_bytes"]),
            changed_paths=tuple(payload["changed_paths"]),
            baseline_inventory=tuple(
                FileInventoryEntry.from_wire(item)
                for item in payload["baseline_inventory"]
            ),
            result_inventory=tuple(
                FileInventoryEntry.from_wire(item)
                for item in payload["result_inventory"]
            ),
            unified_diff=str(payload["unified_diff"]),
            diff_hash=str(payload["diff_hash"]),
            result_hash=result_hash,
            version=str(payload.get("version", IMPLEMENT_RESULT_VERSION)),
        )


@dataclass(frozen=True, slots=True)
class ImplementExecution:
    workspace: DisposableWorkspace
    result: ImplementResult


class ImplementRuntime(Protocol):
    async def create_workspace(
        self,
        *,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> DisposableWorkspace: ...

    async def materialize(
        self,
        *,
        workspace: DisposableWorkspace,
        archive: RepositoryArchive,
        policy: SandboxPolicy,
    ) -> None: ...

    async def apply(
        self,
        *,
        workspace: DisposableWorkspace,
        change_set: ImplementationChangeSet,
        allowed_change_paths: tuple[str, ...],
        policy: SandboxPolicy,
    ) -> ImplementationInspection: ...

    async def destroy(self, workspace: DisposableWorkspace) -> None: ...


class ImplementService:
    def __init__(
        self,
        *,
        signer: JobSpecSigner,
        policy: SandboxPolicy,
        runtime: ImplementRuntime,
    ) -> None:
        self.signer = signer
        self.policy = policy
        self.runtime = runtime

    async def run(
        self,
        signed_spec: SignedJobSpec,
        archive: RepositoryArchive,
        explore: ExploreResult,
        change_set: ImplementationChangeSet,
        workspace: DisposableWorkspace | None = None,
    ) -> ImplementExecution:
        try:
            spec = self.signer.verify(
                signed_spec,
                expected_policy=self.policy,
            )
        except JobSpecSignatureError as exc:
            raise ImplementationError(str(exc)) from exc
        if spec.stage is not SandboxStage.IMPLEMENT:
            raise ImplementationError(
                "Implementation requires an Implement JobSpec"
            )
        if spec.commands:
            raise ImplementationError(
                "Implementation JobSpec cannot execute repository commands"
            )
        if explore.result_hash != content_hash(explore.hash_payload()):
            raise ImplementationError("Explore result provenance is invalid")
        if (
            spec.execution_attempt_id != explore.execution_attempt_id
            or spec.repository_full_name != explore.repository_full_name
            or spec.base_commit_sha != explore.base_commit_sha
            or spec.repository_archive_hash != explore.repository_archive_hash
            or spec.runner_image_digest != explore.runner_image_digest
            or spec.sandbox_policy_version != explore.sandbox_policy_version
            or spec.sandbox_policy_hash != explore.sandbox_policy_hash
            or archive.repository_full_name != spec.repository_full_name
            or archive.base_commit_sha != spec.base_commit_sha
            or archive.archive_hash != spec.repository_archive_hash
        ):
            raise ImplementationError(
                "Implementation inputs do not match Explore provenance"
            )
        if (
            change_set.plan_version_id != spec.plan_version_id
            or change_set.plan_content_hash != spec.plan_content_hash
            or change_set.plan_record_hash != spec.plan_record_hash
        ):
            raise ImplementationError(
                "ChangeSet does not match the approved PlanVersion"
            )
        if spec.input_artifact_hashes != (
            explore.result_hash,
            change_set.change_set_hash,
        ):
            raise ImplementationError(
                "Implementation artifact inputs do not match the JobSpec"
            )
        allowed = set(spec.allowed_change_paths)
        if not set(change_set.paths).issubset(allowed):
            raise ImplementationError(
                "ChangeSet contains a path outside the approved plan"
            )
        archive.verify()
        if workspace is None:
            workspace = await self.runtime.create_workspace(
                image_digest=spec.runner_image_digest,
                policy=self.policy,
            )
        elif (
            workspace.runner_image_digest != spec.runner_image_digest
            or workspace.sandbox_policy_hash != self.policy.policy_hash
        ):
            raise ImplementationError(
                "Implementation workspace does not match the JobSpec"
            )
        try:
            await self.runtime.materialize(
                workspace=workspace,
                archive=archive,
                policy=self.policy,
            )
            archive.verify()
            inspection = await self.runtime.apply(
                workspace=workspace,
                change_set=change_set,
                allowed_change_paths=spec.allowed_change_paths,
                policy=self.policy,
            )
            archive.verify()
            if inspection.changed_paths != change_set.paths:
                raise ImplementationError(
                    "Implementation changed paths do not match the ChangeSet"
                )
            result = ImplementResult.create(
                signed_spec=signed_spec,
                explore=explore,
                change_set=change_set,
                inspection=inspection,
            )
            return ImplementExecution(
                workspace=replace(
                    workspace,
                    inventory_hash=inspection.result_inventory_hash,
                ),
                result=result,
            )
        except BaseException:
            await self.runtime.destroy(workspace)
            raise


class DockerImplementRuntime:
    def __init__(
        self,
        *,
        docker_environment: Mapping[str, str],
        docker_executable: str = "docker",
    ) -> None:
        unexpected = sorted(set(docker_environment) - _ALLOWED_DOCKER_ENV)
        if unexpected:
            raise ValueError(
                "Implementation Docker environment contains disallowed variables: "
                + ", ".join(unexpected)
            )
        if not docker_executable or os.path.sep in docker_executable:
            raise ValueError("Implementation Docker executable is invalid")
        self.docker_environment = dict(docker_environment)
        self.docker_executable = docker_executable

    async def create_workspace(
        self,
        *,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> DisposableWorkspace:
        workspace_id = f"workspace:{uuid4()}"
        volume_name = f"contribos-workspace-{uuid4().hex}"
        result = await self._command(
            (
                self.docker_executable,
                "volume",
                "create",
                "--label",
                "io.contribos.workspace=disposable",
                "--label",
                f"io.contribos.policy={policy.version}",
                volume_name,
            ),
            timeout_seconds=30,
        )
        if result.strip() != volume_name.encode("ascii"):
            raise ImplementationError(
                "Disposable workspace creation returned an invalid response"
            )
        workspace = DisposableWorkspace(
            workspace_id=workspace_id,
            volume_name=volume_name,
            runner_image_digest=image_digest,
            sandbox_policy_hash=policy.policy_hash,
        )
        try:
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
                    (
                        f"type=volume,src={volume_name},"
                        "dst=/workspace"
                    ),
                    "--entrypoint",
                    "chmod",
                    image_digest,
                    "0777",
                    "/workspace",
                ),
                timeout_seconds=30,
            )
        except (ImplementationError, TimeoutError):
            await self.destroy(workspace)
            raise ImplementationError(
                "Disposable workspace initialization failed"
            ) from None
        return workspace

    async def materialize(
        self,
        *,
        workspace: DisposableWorkspace,
        archive: RepositoryArchive,
        policy: SandboxPolicy,
    ) -> None:
        _workspace_matches(workspace, policy)
        name = f"contribos-implement-materialize-{uuid4().hex}"
        argv = self.build_materialize_argv(
            workspace=workspace,
            archive=archive,
            policy=policy,
            container_name=name,
        )
        result = await self._container(
            argv,
            container_name=name,
            timeout_seconds=policy.timeout_seconds,
        )
        if result.strip() != b"sandbox-materialize-v1":
            raise ImplementationError(
                "Implementation materializer returned an invalid response"
            )

    def build_materialize_argv(
        self,
        *,
        workspace: DisposableWorkspace,
        archive: RepositoryArchive,
        policy: SandboxPolicy,
        container_name: str,
    ) -> tuple[str, ...]:
        _runtime_inputs(workspace, container_name=container_name)
        if "," in str(archive.path):
            raise ValueError("Implementation archive mount path is invalid")
        return (
            *self._base_argv(
                workspace=workspace,
                policy=policy,
                container_name=container_name,
            ),
            "--mount",
            (
                f"type=bind,src={archive.path},dst=/input/repository.tar,"
                "readonly,bind-propagation=rprivate"
            ),
            "--mount",
            f"type=volume,src={workspace.volume_name},dst=/output",
            "--entrypoint",
            "python3",
            workspace.runner_image_digest,
            "-I",
            "-c",
            _MATERIALIZE_SCRIPT,
            archive.repository_full_name.rsplit("/", 1)[1],
            archive.base_commit_sha,
        )

    async def apply(
        self,
        *,
        workspace: DisposableWorkspace,
        change_set: ImplementationChangeSet,
        allowed_change_paths: tuple[str, ...],
        policy: SandboxPolicy,
    ) -> ImplementationInspection:
        _workspace_matches(workspace, policy)
        wrapper = {
            "allowed_change_paths": list(allowed_change_paths),
            "change_set": change_set.to_wire(),
            "change_set_hash": change_set.change_set_hash,
        }
        encoded = (
            json.dumps(
                wrapper,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_CHANGE_SET_BYTES + 100_000:
            raise ImplementationError(
                "Implementation ChangeSet wrapper exceeds the byte limit"
            )
        with tempfile.TemporaryDirectory(
            prefix="contribos-change-set-"
        ) as temporary:
            change_path = os.path.join(temporary, "change-set.json")
            with open(change_path, "xb") as handle:
                handle.write(encoded)
            os.chmod(change_path, 0o444)
            name = f"contribos-implement-apply-{uuid4().hex}"
            result = await self._container(
                self.build_apply_argv(
                    workspace=workspace,
                    change_set_path=change_path,
                    policy=policy,
                    container_name=name,
                ),
                container_name=name,
                timeout_seconds=policy.timeout_seconds,
            )
        try:
            raw = json.loads(result)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImplementationError(
                "Implementation returned invalid JSON"
            ) from exc
        return ImplementationInspection.from_wire(raw)

    def build_apply_argv(
        self,
        *,
        workspace: DisposableWorkspace,
        change_set_path: str,
        policy: SandboxPolicy,
        container_name: str,
    ) -> tuple[str, ...]:
        _runtime_inputs(workspace, container_name=container_name)
        if (
            not os.path.isabs(change_set_path)
            or "," in change_set_path
            or not os.path.isfile(change_set_path)
        ):
            raise ValueError("Implementation ChangeSet mount is invalid")
        return (
            *self._base_argv(
                workspace=workspace,
                policy=policy,
                container_name=container_name,
            ),
            "--workdir",
            "/workspace",
            "--mount",
            (
                f"type=volume,src={workspace.volume_name},"
                "dst=/workspace"
            ),
            "--mount",
            (
                f"type=bind,src={change_set_path},"
                "dst=/input/change-set.json,readonly,"
                "bind-propagation=rprivate"
            ),
            "--entrypoint",
            "python3",
            workspace.runner_image_digest,
            "-I",
            "-c",
            _APPLY_SCRIPT,
        )

    async def destroy(self, workspace: DisposableWorkspace) -> None:
        if not _VOLUME.fullmatch(workspace.volume_name):
            raise ImplementationError("Disposable workspace name is invalid")
        if not await self._volume_exists(workspace.volume_name):
            return
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
        except ImplementationError:
            if not await self._volume_exists(workspace.volume_name):
                return
            raise ImplementationError(
                "Disposable workspace cleanup failed"
            ) from None

    async def _volume_exists(self, volume_name: str) -> bool:
        if not _VOLUME.fullmatch(volume_name):
            raise ImplementationError("Disposable workspace name is invalid")
        result = await self._command(
            (
                self.docker_executable,
                "volume",
                "ls",
                "--quiet",
                "--filter",
                f"name=^{volume_name}$",
            ),
            timeout_seconds=30,
        )
        values = tuple(
            line.strip()
            for line in result.decode("ascii").splitlines()
            if line.strip()
        )
        if values not in {(), (volume_name,)}:
            raise ImplementationError(
                "Disposable workspace lookup returned an invalid response"
            )
        return bool(values)

    def _base_argv(
        self,
        *,
        workspace: DisposableWorkspace,
        policy: SandboxPolicy,
        container_name: str,
    ) -> tuple[str, ...]:
        return (
            self.docker_executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            policy.network_mode,
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
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "LANG=C.UTF-8",
            *docker_git_safety_argv(),
        )

    async def _container(
        self,
        argv: Sequence[str],
        *,
        container_name: str,
        timeout_seconds: int,
    ) -> bytes:
        try:
            return await self._command(
                argv,
                timeout_seconds=timeout_seconds,
                start_new_session=True,
            )
        except TimeoutError as exc:
            await self._force_remove(container_name)
            raise ImplementationError(
                "Implementation container timed out"
            ) from exc

    async def _command(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
        start_new_session: bool = False,
    ) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.docker_environment,
            start_new_session=start_new_session,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            if start_new_session:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            await process.wait()
            raise
        if (
            process.returncode != 0
            or stderr
            or len(stdout) > MAX_IMPLEMENT_OUTPUT_BYTES
        ):
            raise ImplementationError(
                "Implementation runtime failed with a redacted error"
            )
        return stdout

    async def _force_remove(self, container_name: str) -> None:
        try:
            await self._command(
                (
                    self.docker_executable,
                    "rm",
                    "--force",
                    container_name,
                ),
                timeout_seconds=10,
            )
        except (ImplementationError, TimeoutError):
            pass


def _workspace_matches(
    workspace: DisposableWorkspace,
    policy: SandboxPolicy,
) -> None:
    if workspace.sandbox_policy_hash != policy.policy_hash:
        raise ImplementationError(
            "Disposable workspace policy does not match"
        )


def _runtime_inputs(
    workspace: DisposableWorkspace,
    *,
    container_name: str,
) -> None:
    if not _VOLUME.fullmatch(workspace.volume_name):
        raise ValueError("Implementation workspace volume is invalid")
    if not _CONTAINER.fullmatch(container_name):
        raise ValueError("Implementation container name is invalid")


def _relative_path(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1_000
        or "\x00" in value
        or "\\" in value
        or contains_sensitive_text(value)
    ):
        raise ValueError(f"Implementation {name} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or ".git" in path.parts
        or str(path) != value
        or value == "."
    ):
        raise ValueError(f"Implementation {name} is invalid")
    return value


def _hash(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"Implementation {name.replace('_', ' ')} is invalid")
    return value


def _path_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ImplementationError("Implementation changed paths are invalid")
    try:
        result = tuple(value)
    except TypeError as exc:
        raise ImplementationError(
            "Implementation changed paths are invalid"
        ) from exc
    if tuple(sorted(result)) != result or len(result) != len(set(result)):
        raise ImplementationError("Implementation changed paths are invalid")
    for path in result:
        _relative_path(path, name="changed path")
    return result


def _inventory(
    value: object,
    *,
    name: str,
) -> tuple[FileInventoryEntry, ...]:
    if isinstance(value, (str, bytes)):
        raise ImplementationError(
            f"Implementation {name} inventory is invalid"
        )
    try:
        raw = tuple(value)
    except TypeError as exc:
        raise ImplementationError(
            f"Implementation {name} inventory is invalid"
        ) from exc
    if len(raw) > MAX_FILE_INVENTORY_ENTRIES:
        raise ImplementationError(
            f"Implementation {name} inventory is invalid"
        )
    entries = tuple(
        item
        if isinstance(item, FileInventoryEntry)
        else FileInventoryEntry.from_wire(item)
        for item in raw
    )
    paths = tuple(item.path for item in entries)
    if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
        raise ImplementationError(
            f"Implementation {name} inventory is invalid"
        )
    return entries


_APPLY_SCRIPT = r"""
import difflib
import hashlib
import json
import os
import stat

root = "/workspace"
with open("/input/change-set.json", encoding="utf-8") as handle:
    wrapper = json.load(handle)
if set(wrapper) != {"allowed_change_paths", "change_set", "change_set_hash"}:
    raise SystemExit(50)
change_set = wrapper["change_set"]
if set(change_set) != {
    "version", "change_set_id", "plan_version_id", "plan_content_hash",
    "plan_record_hash", "operations",
}:
    raise SystemExit(51)
encoded_change_set = json.dumps(
    change_set,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
if hashlib.sha256(encoded_change_set).hexdigest() != wrapper["change_set_hash"]:
    raise SystemExit(70)
allowed = set(wrapper["allowed_change_paths"])
operations = change_set["operations"]
if not operations or len(operations) > 500:
    raise SystemExit(52)

def scan():
    entries = []
    total = 0
    for current, directories, files in os.walk(
        root, topdown=True, followlinks=False
    ):
        directories.sort()
        files.sort()
        for name in list(directories):
            absolute = os.path.join(current, name)
            if os.path.islink(absolute):
                directories.remove(name)
                entries.append({
                    "path": os.path.relpath(absolute, root),
                    "type": "symlink",
                    "target": os.readlink(absolute),
                })
        for name in files:
            absolute = os.path.join(current, name)
            relative = os.path.relpath(absolute, root)
            metadata = os.lstat(absolute)
            if stat.S_ISLNK(metadata.st_mode):
                entries.append({
                    "path": relative,
                    "type": "symlink",
                    "target": os.readlink(absolute),
                })
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise SystemExit(53)
            digest = hashlib.sha256()
            with open(absolute, "rb") as source:
                while True:
                    chunk = source.read(1048576)
                    if not chunk:
                        break
                    digest.update(chunk)
            total += metadata.st_size
            entries.append({
                "path": relative,
                "type": "file",
                "size": metadata.st_size,
                "sha256": digest.hexdigest(),
                "executable": bool(metadata.st_mode & 0o111),
            })
    entries.sort(key=lambda item: item["path"])
    encoded = json.dumps(
        entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return entries, hashlib.sha256(encoded).hexdigest(), total

def regular_hash(path):
    if os.path.islink(path) or not os.path.isfile(path):
        raise SystemExit(54)
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            chunk = source.read(1048576)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()

def snapshot_target(path):
    if not os.path.lexists(path):
        return None
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(71)
    with open(path, "rb") as source:
        content = source.read(8000001)
    return {
        "content": None if len(content) > 8000000 else content,
        "executable": bool(metadata.st_mode & 0o111),
        "oversized": len(content) > 8000000,
    }

def file_mode(executable):
    return "100755" if executable else "100644"

def build_diff(paths, before_content, after_content):
    chunks = []
    total_bytes = 0

    def append(value):
        nonlocal total_bytes
        total_bytes += len(value.encode("utf-8"))
        if total_bytes > 8000000:
            raise SystemExit(72)
        chunks.append(value)

    for relative in sorted(paths):
        old = before_content[relative]
        new = after_content[relative]
        append(f"diff --git a/{relative} b/{relative}\n")
        if old is None:
            append(f"new file mode {file_mode(new['executable'])}\n")
        elif new is None:
            append(
                f"deleted file mode {file_mode(old['executable'])}\n",
            )
        elif old["executable"] != new["executable"]:
            append(f"old mode {file_mode(old['executable'])}\n")
            append(f"new mode {file_mode(new['executable'])}\n")
        if (
            old is not None
            and new is not None
            and old["content"] == new["content"]
            and old["oversized"] == new["oversized"]
        ):
            continue
        old_label = "/dev/null" if old is None else f"a/{relative}"
        new_label = "/dev/null" if new is None else f"b/{relative}"
        if (
            (old is not None and old["oversized"])
            or (new is not None and new["oversized"])
        ):
            append(
                f"Binary files {old_label} and {new_label} differ\n",
            )
            continue
        old_bytes = b"" if old is None else old["content"]
        new_bytes = b"" if new is None else new["content"]
        try:
            old_text = old_bytes.decode("utf-8")
            new_text = new_bytes.decode("utf-8")
        except UnicodeDecodeError:
            append(
                f"Binary files {old_label} and {new_label} differ\n",
            )
            continue
        for piece in difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=old_label,
            tofile=new_label,
            n=3,
            lineterm="\n",
        ):
            append(piece)
            if not piece.endswith("\n"):
                append("\n\\ No newline at end of file\n")
    return "".join(chunks)

def target_path(relative):
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or "\\" in relative
        or "\x00" in relative
    ):
        raise SystemExit(55)
    parts = relative.split("/")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise SystemExit(56)
    current = root
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.lexists(current):
            if os.path.islink(current) or not os.path.isdir(current):
                raise SystemExit(57)
        else:
            os.mkdir(current, mode=0o755)
    candidate = os.path.join(root, *parts)
    if os.path.commonpath((root, os.path.realpath(candidate))) != root:
        raise SystemExit(58)
    return candidate

before, baseline_hash, baseline_bytes = scan()
operation_paths = []
before_content = {}
for operation in operations:
    if set(operation) != {
        "path", "kind", "expected_prior_hash", "content",
        "content_hash", "executable",
    }:
        raise SystemExit(59)
    relative = operation["path"]
    if relative not in allowed or relative in operation_paths:
        raise SystemExit(60)
    operation_paths.append(relative)
    target = target_path(relative)
    expected = operation["expected_prior_hash"]
    exists = os.path.lexists(target)
    if expected is None:
        if exists:
            raise SystemExit(61)
    elif (
        not isinstance(expected, str)
        or len(expected) != 64
        or not exists
        or regular_hash(target) != expected
    ):
        raise SystemExit(62)
    before_content[relative] = snapshot_target(target)
    if operation["kind"] == "delete":
        if operation["content"] is not None or operation["executable"] is not None:
            raise SystemExit(63)
        os.unlink(target)
    elif operation["kind"] == "write":
        content = operation["content"]
        executable = operation["executable"]
        if not isinstance(content, str) or not isinstance(executable, bool):
            raise SystemExit(64)
        encoded = content.encode("utf-8")
        if len(encoded) > 1000000:
            raise SystemExit(65)
        expected_content_hash = hashlib.sha256(
            json.dumps(
                {"utf8": content},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if operation["content_hash"] != expected_content_hash:
            raise SystemExit(66)
        temporary = target + ".contribos-new"
        if os.path.lexists(temporary):
            raise SystemExit(67)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o755 if executable else 0o644,
        )
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(encoded)
        os.replace(temporary, target)
    else:
        raise SystemExit(68)

after, result_hash, result_bytes = scan()
after_content = {
    relative: snapshot_target(target_path(relative))
    for relative in operation_paths
}
before_map = {item["path"]: item for item in before}
after_map = {item["path"]: item for item in after}
changed = sorted(
    path for path in set(before_map) | set(after_map)
    if before_map.get(path) != after_map.get(path)
)
if changed != sorted(operation_paths):
    raise SystemExit(69)
unified_diff = build_diff(changed, before_content, after_content)
result = {
    "baseline_inventory_hash": baseline_hash,
    "result_inventory_hash": result_hash,
    "baseline_file_count": len(before),
    "result_file_count": len(after),
    "baseline_total_bytes": baseline_bytes,
    "result_total_bytes": result_bytes,
    "changed_paths": changed,
    "baseline_inventory": before,
    "result_inventory": after,
    "unified_diff": unified_diff,
    "diff_hash": hashlib.sha256(unified_diff.encode("utf-8")).hexdigest(),
}
print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
""".strip()
