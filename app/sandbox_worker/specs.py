"""Versioned, authenticated execution inputs for the Sandbox Worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from app.provenance import canonical_json, content_hash
from app.sandbox_worker.git_safety import GIT_SAFETY_POLICY_VERSION
from app.security import contains_sensitive_text, ensure_no_sensitive_data


SANDBOX_POLICY_VERSION = "sandbox-policy-v1"
JOB_SPEC_VERSION = "sandbox-job-spec-v1"
JOB_SPEC_SIGNATURE_VERSION = "sandbox-job-spec-hmac-sha256-v1"
# Leaves headroom inside the 64 KiB sandbox-worker-v1 request envelope.
MAX_JOB_SPEC_BYTES = 60_000
DEFAULT_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_DISK_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_LOG_BYTES = 4 * 1024 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")
_BASE_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_COMMAND_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class SandboxSpecError(ValueError):
    pass


class JobSpecSignatureError(SandboxSpecError):
    pass


class SandboxStage(StrEnum):
    EXPLORE = "explore"
    IMPLEMENT = "implement"
    VERIFY = "verify"


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Fail-closed v1 policy; callers may only reduce resource ceilings."""

    cpu_limit: float = 2.0
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    pids_limit: int = 256
    timeout_seconds: int = 600
    disk_bytes: int = DEFAULT_DISK_BYTES
    max_log_bytes: int = DEFAULT_LOG_BYTES
    run_as_uid: int = 65_532
    run_as_gid: int = 65_532
    network_mode: str = "none"
    read_only_root: bool = True
    workspace_mount: str = "/workspace"
    writable_workspace_stages: tuple[str, ...] = (
        SandboxStage.IMPLEMENT.value,
    )
    tmpfs_mounts: tuple[str, ...] = ("/tmp", "/run")
    cap_drop: tuple[str, ...] = ("ALL",)
    no_new_privileges: bool = True
    host_pid: bool = False
    host_ipc: bool = False
    allow_devices: bool = False
    mount_docker_socket: bool = False
    mount_host_home: bool = False
    mount_ssh_agent: bool = False
    inject_credentials: bool = False
    allow_model_access: bool = False
    git_safety_policy_version: str = GIT_SAFETY_POLICY_VERSION
    version: str = SANDBOX_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.version != SANDBOX_POLICY_VERSION:
            raise SandboxSpecError("Sandbox policy version is unsupported")
        if (
            isinstance(self.cpu_limit, bool)
            or not isinstance(self.cpu_limit, (int, float))
            or not 0.1 <= float(self.cpu_limit) <= 2.0
        ):
            raise SandboxSpecError("Sandbox CPU limit is invalid")
        object.__setattr__(self, "cpu_limit", float(self.cpu_limit))
        _bounded_integer(
            self.memory_bytes,
            name="memory",
            minimum=64 * 1024 * 1024,
            maximum=DEFAULT_MEMORY_BYTES,
        )
        _bounded_integer(
            self.pids_limit,
            name="PID",
            minimum=16,
            maximum=256,
        )
        _bounded_integer(
            self.timeout_seconds,
            name="timeout",
            minimum=1,
            maximum=600,
        )
        _bounded_integer(
            self.disk_bytes,
            name="disk",
            minimum=64 * 1024 * 1024,
            maximum=DEFAULT_DISK_BYTES,
        )
        _bounded_integer(
            self.max_log_bytes,
            name="log",
            minimum=1_024,
            maximum=DEFAULT_LOG_BYTES,
        )
        _bounded_integer(
            self.run_as_uid,
            name="UID",
            minimum=1,
            maximum=2_147_483_647,
        )
        _bounded_integer(
            self.run_as_gid,
            name="GID",
            minimum=1,
            maximum=2_147_483_647,
        )
        required = {
            "network_mode": self.network_mode == "none",
            "read_only_root": self.read_only_root is True,
            "workspace_mount": self.workspace_mount == "/workspace",
            "writable_workspace_stages": (
                tuple(self.writable_workspace_stages)
                == (SandboxStage.IMPLEMENT.value,)
            ),
            "tmpfs_mounts": tuple(self.tmpfs_mounts) == ("/tmp", "/run"),
            "cap_drop": tuple(self.cap_drop) == ("ALL",),
            "no_new_privileges": self.no_new_privileges is True,
            "host_pid": self.host_pid is False,
            "host_ipc": self.host_ipc is False,
            "allow_devices": self.allow_devices is False,
            "mount_docker_socket": self.mount_docker_socket is False,
            "mount_host_home": self.mount_host_home is False,
            "mount_ssh_agent": self.mount_ssh_agent is False,
            "inject_credentials": self.inject_credentials is False,
            "allow_model_access": self.allow_model_access is False,
            "git_safety_policy_version": (
                self.git_safety_policy_version
                == GIT_SAFETY_POLICY_VERSION
            ),
        }
        invalid = sorted(name for name, accepted in required.items() if not accepted)
        if invalid:
            raise SandboxSpecError(
                "Sandbox policy weakens required isolation: "
                + ", ".join(invalid)
            )
        ensure_no_sensitive_data(self.to_wire(), context="Sandbox policy")

    @property
    def policy_hash(self) -> str:
        return content_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "cpu_limit": self.cpu_limit,
            "memory_bytes": self.memory_bytes,
            "pids_limit": self.pids_limit,
            "timeout_seconds": self.timeout_seconds,
            "disk_bytes": self.disk_bytes,
            "max_log_bytes": self.max_log_bytes,
            "run_as_uid": self.run_as_uid,
            "run_as_gid": self.run_as_gid,
            "network_mode": self.network_mode,
            "read_only_root": self.read_only_root,
            "workspace_mount": self.workspace_mount,
            "writable_workspace_stages": list(
                self.writable_workspace_stages
            ),
            "tmpfs_mounts": list(self.tmpfs_mounts),
            "cap_drop": list(self.cap_drop),
            "no_new_privileges": self.no_new_privileges,
            "host_pid": self.host_pid,
            "host_ipc": self.host_ipc,
            "allow_devices": self.allow_devices,
            "mount_docker_socket": self.mount_docker_socket,
            "mount_host_home": self.mount_host_home,
            "mount_ssh_agent": self.mount_ssh_agent,
            "inject_credentials": self.inject_credentials,
            "allow_model_access": self.allow_model_access,
            "git_safety_policy_version": self.git_safety_policy_version,
        }

    @classmethod
    def from_wire(cls, value: object) -> "SandboxPolicy":
        raw = _exact_object(value, fields=set(cls().to_wire()), name="policy")
        try:
            return cls(
                version=raw["version"],
                cpu_limit=raw["cpu_limit"],
                memory_bytes=raw["memory_bytes"],
                pids_limit=raw["pids_limit"],
                timeout_seconds=raw["timeout_seconds"],
                disk_bytes=raw["disk_bytes"],
                max_log_bytes=raw["max_log_bytes"],
                run_as_uid=raw["run_as_uid"],
                run_as_gid=raw["run_as_gid"],
                network_mode=raw["network_mode"],
                read_only_root=raw["read_only_root"],
                workspace_mount=raw["workspace_mount"],
                writable_workspace_stages=tuple(
                    raw["writable_workspace_stages"]
                ),
                tmpfs_mounts=tuple(raw["tmpfs_mounts"]),
                cap_drop=tuple(raw["cap_drop"]),
                no_new_privileges=raw["no_new_privileges"],
                host_pid=raw["host_pid"],
                host_ipc=raw["host_ipc"],
                allow_devices=raw["allow_devices"],
                mount_docker_socket=raw["mount_docker_socket"],
                mount_host_home=raw["mount_host_home"],
                mount_ssh_agent=raw["mount_ssh_agent"],
                inject_credentials=raw["inject_credentials"],
                allow_model_access=raw["allow_model_access"],
                git_safety_policy_version=raw[
                    "git_safety_policy_version"
                ],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, SandboxSpecError):
                raise
            raise SandboxSpecError("Sandbox policy fields are invalid") from exc


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    command_id: str
    argv: tuple[str, ...]
    working_directory: str = "."

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command_id, str)
            or not _COMMAND_ID.fullmatch(self.command_id)
        ):
            raise SandboxSpecError("Sandbox command ID is invalid")
        if isinstance(self.argv, (str, bytes)):
            raise SandboxSpecError("Sandbox command argv is invalid")
        argv = tuple(self.argv)
        if (
            not argv
            or len(argv) > 50
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 2_000
                or "\x00" in value
                for value in argv
            )
        ):
            raise SandboxSpecError("Sandbox command argv is invalid")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(
            self,
            "working_directory",
            _relative_path(
                self.working_directory,
                name="command working directory",
                allow_root=True,
            ),
        )
        ensure_no_sensitive_data(self.to_wire(), context="Sandbox command")

    def to_wire(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "working_directory": self.working_directory,
        }

    @classmethod
    def from_wire(cls, value: object) -> "SandboxCommand":
        raw = _exact_object(
            value,
            fields={"command_id", "argv", "working_directory"},
            name="command",
        )
        try:
            return cls(
                command_id=raw["command_id"],
                argv=tuple(raw["argv"]),
                working_directory=raw["working_directory"],
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, SandboxSpecError):
                raise
            raise SandboxSpecError("Sandbox command fields are invalid") from exc


@dataclass(frozen=True, slots=True)
class JobSpec:
    spec_id: str
    execution_attempt_id: str
    correlation_id: str
    stage: SandboxStage
    repository_full_name: str
    base_commit_sha: str
    task_id: str
    task_record_hash: str
    analysis_version_id: str
    analysis_record_hash: str
    analysis_output_hash: str
    snapshot_id: str
    snapshot_inputs_hash: str
    plan_version_id: str
    plan_content_hash: str
    plan_record_hash: str
    plan_approval_id: str
    approval_hash: str
    approved_state_version_id: str
    approved_state_record_hash: str
    provider_contract_hash: str
    repository_archive_hash: str
    runner_image_digest: str
    sandbox_policy_version: str
    sandbox_policy_hash: str
    input_artifact_hashes: tuple[str, ...]
    allowed_change_paths: tuple[str, ...]
    commands: tuple[SandboxCommand, ...]
    version: str = JOB_SPEC_VERSION

    def __post_init__(self) -> None:
        if self.version != JOB_SPEC_VERSION:
            raise SandboxSpecError("JobSpec version is unsupported")
        try:
            stage = SandboxStage(self.stage)
        except (TypeError, ValueError) as exc:
            raise SandboxSpecError("JobSpec stage is invalid") from exc
        object.__setattr__(self, "stage", stage)
        for name in (
            "spec_id",
            "execution_attempt_id",
            "correlation_id",
            "task_id",
            "analysis_version_id",
            "snapshot_id",
            "plan_version_id",
            "plan_approval_id",
            "approved_state_version_id",
        ):
            _identifier(getattr(self, name), name=name)
        if (
            not isinstance(self.repository_full_name, str)
            or not _REPOSITORY.fullmatch(self.repository_full_name)
            or contains_sensitive_text(self.repository_full_name)
        ):
            raise SandboxSpecError("JobSpec repository is invalid")
        if not isinstance(self.base_commit_sha, str) or not _BASE_SHA.fullmatch(
            self.base_commit_sha
        ):
            raise SandboxSpecError("JobSpec base commit SHA is invalid")
        for name in (
            "task_record_hash",
            "analysis_record_hash",
            "analysis_output_hash",
            "snapshot_inputs_hash",
            "plan_content_hash",
            "plan_record_hash",
            "approval_hash",
            "approved_state_record_hash",
            "provider_contract_hash",
            "repository_archive_hash",
            "sandbox_policy_hash",
        ):
            _hash(getattr(self, name), name=name)
        if self.sandbox_policy_version != SANDBOX_POLICY_VERSION:
            raise SandboxSpecError("JobSpec SandboxPolicy version is unsupported")
        if (
            not isinstance(self.runner_image_digest, str)
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                self.runner_image_digest,
            )
        ):
            raise SandboxSpecError("JobSpec runner image digest is invalid")
        artifacts = _hash_tuple(
            self.input_artifact_hashes,
            name="input artifact hashes",
            maximum=100,
        )
        paths = tuple(
            _relative_path(value, name="allowed change path")
            for value in self.allowed_change_paths
        )
        if len(paths) > 500 or len(paths) != len(set(paths)):
            raise SandboxSpecError(
                "JobSpec allowed change paths are invalid"
            )
        commands = tuple(self.commands)
        if (
            len(commands) > 100
            or any(not isinstance(command, SandboxCommand) for command in commands)
            or len({command.command_id for command in commands}) != len(commands)
        ):
            raise SandboxSpecError("JobSpec commands are invalid")
        if stage is SandboxStage.EXPLORE and (artifacts or paths):
            raise SandboxSpecError(
                "Explore JobSpec cannot consume artifacts or allow changes"
            )
        if stage is SandboxStage.IMPLEMENT and (not artifacts or not paths):
            raise SandboxSpecError(
                "Implement JobSpec requires input artifacts and allowed paths"
            )
        if stage is SandboxStage.VERIFY and (
            len(artifacts) not in {1, 2} or paths or not commands
        ):
            raise SandboxSpecError(
                "Verify JobSpec requires Implement plus optional dependency "
                "artifacts and commands without changes"
            )
        object.__setattr__(self, "input_artifact_hashes", artifacts)
        object.__setattr__(self, "allowed_change_paths", paths)
        object.__setattr__(self, "commands", commands)
        ensure_no_sensitive_data(self.to_wire(), context="JobSpec")
        if len(canonical_json(self.to_wire()).encode("utf-8")) > MAX_JOB_SPEC_BYTES:
            raise SandboxSpecError("JobSpec exceeds the byte limit")

    @property
    def spec_hash(self) -> str:
        return content_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "spec_id": self.spec_id,
            "execution_attempt_id": self.execution_attempt_id,
            "correlation_id": self.correlation_id,
            "stage": self.stage.value,
            "repository_full_name": self.repository_full_name,
            "base_commit_sha": self.base_commit_sha,
            "task_id": self.task_id,
            "task_record_hash": self.task_record_hash,
            "analysis_version_id": self.analysis_version_id,
            "analysis_record_hash": self.analysis_record_hash,
            "analysis_output_hash": self.analysis_output_hash,
            "snapshot_id": self.snapshot_id,
            "snapshot_inputs_hash": self.snapshot_inputs_hash,
            "plan_version_id": self.plan_version_id,
            "plan_content_hash": self.plan_content_hash,
            "plan_record_hash": self.plan_record_hash,
            "plan_approval_id": self.plan_approval_id,
            "approval_hash": self.approval_hash,
            "approved_state_version_id": self.approved_state_version_id,
            "approved_state_record_hash": self.approved_state_record_hash,
            "provider_contract_hash": self.provider_contract_hash,
            "repository_archive_hash": self.repository_archive_hash,
            "runner_image_digest": self.runner_image_digest,
            "sandbox_policy_version": self.sandbox_policy_version,
            "sandbox_policy_hash": self.sandbox_policy_hash,
            "input_artifact_hashes": list(self.input_artifact_hashes),
            "allowed_change_paths": list(self.allowed_change_paths),
            "commands": [command.to_wire() for command in self.commands],
        }

    @classmethod
    def from_wire(cls, value: object) -> "JobSpec":
        fields = {
            "version",
            "spec_id",
            "execution_attempt_id",
            "correlation_id",
            "stage",
            "repository_full_name",
            "base_commit_sha",
            "task_id",
            "task_record_hash",
            "analysis_version_id",
            "analysis_record_hash",
            "analysis_output_hash",
            "snapshot_id",
            "snapshot_inputs_hash",
            "plan_version_id",
            "plan_content_hash",
            "plan_record_hash",
            "plan_approval_id",
            "approval_hash",
            "approved_state_version_id",
            "approved_state_record_hash",
            "provider_contract_hash",
            "repository_archive_hash",
            "runner_image_digest",
            "sandbox_policy_version",
            "sandbox_policy_hash",
            "input_artifact_hashes",
            "allowed_change_paths",
            "commands",
        }
        raw = _exact_object(value, fields=fields, name="JobSpec")
        try:
            return cls(
                version=raw["version"],
                spec_id=raw["spec_id"],
                execution_attempt_id=raw["execution_attempt_id"],
                correlation_id=raw["correlation_id"],
                stage=SandboxStage(raw["stage"]),
                repository_full_name=raw["repository_full_name"],
                base_commit_sha=raw["base_commit_sha"],
                task_id=raw["task_id"],
                task_record_hash=raw["task_record_hash"],
                analysis_version_id=raw["analysis_version_id"],
                analysis_record_hash=raw["analysis_record_hash"],
                analysis_output_hash=raw["analysis_output_hash"],
                snapshot_id=raw["snapshot_id"],
                snapshot_inputs_hash=raw["snapshot_inputs_hash"],
                plan_version_id=raw["plan_version_id"],
                plan_content_hash=raw["plan_content_hash"],
                plan_record_hash=raw["plan_record_hash"],
                plan_approval_id=raw["plan_approval_id"],
                approval_hash=raw["approval_hash"],
                approved_state_version_id=raw[
                    "approved_state_version_id"
                ],
                approved_state_record_hash=raw[
                    "approved_state_record_hash"
                ],
                provider_contract_hash=raw["provider_contract_hash"],
                repository_archive_hash=raw["repository_archive_hash"],
                runner_image_digest=raw["runner_image_digest"],
                sandbox_policy_version=raw["sandbox_policy_version"],
                sandbox_policy_hash=raw["sandbox_policy_hash"],
                input_artifact_hashes=tuple(raw["input_artifact_hashes"]),
                allowed_change_paths=tuple(raw["allowed_change_paths"]),
                commands=tuple(
                    SandboxCommand.from_wire(command)
                    for command in raw["commands"]
                ),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, SandboxSpecError):
                raise
            raise SandboxSpecError("JobSpec fields are invalid") from exc


@dataclass(frozen=True, slots=True)
class SignedJobSpec:
    spec: JobSpec
    spec_hash: str
    key_id: str
    signature: str
    signature_version: str = JOB_SPEC_SIGNATURE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.spec, JobSpec):
            raise SandboxSpecError("Signed JobSpec payload is invalid")
        if self.spec_hash != self.spec.spec_hash:
            raise JobSpecSignatureError("JobSpec hash does not match its payload")
        _hash(self.spec_hash, name="spec hash")
        if (
            not isinstance(self.key_id, str)
            or not _KEY_ID.fullmatch(self.key_id)
            or contains_sensitive_text(self.key_id)
        ):
            raise SandboxSpecError("JobSpec signing key ID is invalid")
        if self.signature_version != JOB_SPEC_SIGNATURE_VERSION:
            raise JobSpecSignatureError(
                "JobSpec signature version is unsupported"
            )
        _hash(self.signature, name="signature")
        ensure_no_sensitive_data(self.to_wire(), context="Signed JobSpec")
        if len(self.to_bytes()) > MAX_JOB_SPEC_BYTES:
            raise SandboxSpecError("Signed JobSpec exceeds the byte limit")

    def signing_payload(self) -> dict[str, object]:
        return {
            "signature_version": self.signature_version,
            "key_id": self.key_id,
            "spec_hash": self.spec_hash,
            "spec": self.spec.to_wire(),
        }

    def to_wire(self) -> dict[str, object]:
        return {
            **self.signing_payload(),
            "signature": self.signature,
        }

    def to_bytes(self) -> bytes:
        return (canonical_json(self.to_wire()) + "\n").encode("utf-8")

    @classmethod
    def from_bytes(cls, value: bytes) -> "SignedJobSpec":
        if (
            not isinstance(value, bytes)
            or not value
            or len(value) > MAX_JOB_SPEC_BYTES
        ):
            raise SandboxSpecError("Signed JobSpec size is invalid")
        try:
            raw = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxSpecError("Signed JobSpec JSON is invalid") from exc
        envelope = _exact_object(
            raw,
            fields={
                "signature_version",
                "key_id",
                "spec_hash",
                "spec",
                "signature",
            },
            name="signed JobSpec",
        )
        return cls(
            signature_version=envelope["signature_version"],
            key_id=envelope["key_id"],
            spec_hash=envelope["spec_hash"],
            spec=JobSpec.from_wire(envelope["spec"]),
            signature=envelope["signature"],
        )


class JobSpecSigner:
    """HMAC signer/verifier; the signing key never enters a JobSpec."""

    def __init__(self, *, key_id: str, signing_key: bytes) -> None:
        if (
            not isinstance(key_id, str)
            or not _KEY_ID.fullmatch(key_id)
            or contains_sensitive_text(key_id)
        ):
            raise ValueError("JobSpec signing key ID is invalid")
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError(
                "JobSpec signing key must contain at least 32 bytes"
            )
        self.key_id = key_id
        self._signing_key = bytes(signing_key)

    def sign(self, spec: JobSpec) -> SignedJobSpec:
        if not isinstance(spec, JobSpec):
            raise TypeError("JobSpec signer requires a JobSpec")
        unsigned = SignedJobSpec(
            spec=spec,
            spec_hash=spec.spec_hash,
            key_id=self.key_id,
            signature="0" * 64,
        )
        signature = self._signature(unsigned.signing_payload())
        return SignedJobSpec(
            spec=spec,
            spec_hash=spec.spec_hash,
            key_id=self.key_id,
            signature=signature,
        )

    def verify(
        self,
        signed: SignedJobSpec,
        *,
        expected_policy: SandboxPolicy,
    ) -> JobSpec:
        if not isinstance(signed, SignedJobSpec):
            raise JobSpecSignatureError("Signed JobSpec is invalid")
        if signed.key_id != self.key_id:
            raise JobSpecSignatureError("JobSpec signing key is unknown")
        expected_signature = self._signature(signed.signing_payload())
        if not hmac.compare_digest(expected_signature, signed.signature):
            raise JobSpecSignatureError("JobSpec signature is invalid")
        if (
            signed.spec.sandbox_policy_version != expected_policy.version
            or signed.spec.sandbox_policy_hash != expected_policy.policy_hash
        ):
            raise JobSpecSignatureError(
                "JobSpec does not match the accepted SandboxPolicy"
            )
        return signed.spec

    def _signature(self, payload: dict[str, object]) -> str:
        message = (
            JOB_SPEC_SIGNATURE_VERSION
            + "\n"
            + canonical_json(payload)
        ).encode("utf-8")
        return hmac.new(
            self._signing_key,
            message,
            hashlib.sha256,
        ).hexdigest()


def _bounded_integer(
    value: object,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise SandboxSpecError(f"Sandbox {name} limit is invalid")
    return value


def _identifier(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or contains_sensitive_text(value)
    ):
        raise SandboxSpecError(
            f"JobSpec {name.replace('_', ' ')} is invalid"
        )
    return value


def _hash(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise SandboxSpecError(
            f"JobSpec {name.replace('_', ' ')} is invalid"
        )
    return value


def _hash_tuple(
    values: object,
    *,
    name: str,
    maximum: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SandboxSpecError(f"JobSpec {name} are invalid")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise SandboxSpecError(f"JobSpec {name} are invalid") from exc
    if len(result) > maximum or len(result) != len(set(result)):
        raise SandboxSpecError(f"JobSpec {name} are invalid")
    for value in result:
        _hash(value, name=name)
    return result


def _relative_path(
    value: object,
    *,
    name: str,
    allow_root: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1_000
        or "\x00" in value
        or "\\" in value
        or contains_sensitive_text(value)
    ):
        raise SandboxSpecError(f"Sandbox {name} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or ".git" in path.parts
        or (not allow_root and value == ".")
        or str(path) != value
    ):
        raise SandboxSpecError(f"Sandbox {name} is invalid")
    return value


def _exact_object(
    value: object,
    *,
    fields: set[str],
    name: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SandboxSpecError(f"Sandbox {name} fields are invalid")
    return value
