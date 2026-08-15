"""Fresh-container, credential-free verification of one Implement result."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from app.provenance import content_hash
from app.sandbox_worker.dependencies import (
    DependencyExecution,
    DependencyWorkspace,
)
from app.sandbox_worker.git_safety import (
    GIT_SAFETY_ENVIRONMENT_NAMES,
    docker_git_safety_argv,
)
from app.sandbox_worker.implementation import (
    DisposableWorkspace,
    ImplementResult,
)
from app.sandbox_worker.specs import (
    JobSpecSignatureError,
    JobSpecSigner,
    SandboxCommand,
    SandboxPolicy,
    SandboxStage,
    SignedJobSpec,
)
from app.security import ensure_no_sensitive_data, redact_text


VERIFY_RESULT_VERSION = "sandbox-verify-result-v2"
NORMALIZED_TEST_RESULT_VERSION = "normalized-test-result-v1"
MAX_VERIFY_COMMANDS = 20
MAX_VERIFY_OUTPUT_BYTES = 8_000_000
MAX_SANITIZED_LOG_CHARS = 16_384
TRUNCATED_LOG_MARKER = "\n[TRUNCATED]"
OMITTED_LOG_MARKER = "[OMITTED: output limit exceeded]"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
_VOLUME = re.compile(r"^contribos-workspace-[0-9a-f]{32}$")
_CONTAINER = re.compile(r"^contribos-verify-[0-9a-f]{32}$")
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


class VerificationError(RuntimeError):
    pass


class VerifyCommandStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT = "output_limit"
    NOT_RUN = "not_run"


@dataclass(frozen=True, slots=True)
class VerifyResourceUsage:
    user_cpu_ms: int
    system_cpu_ms: int
    max_rss_bytes: int
    minor_page_faults: int
    major_page_faults: int
    voluntary_context_switches: int
    involuntary_context_switches: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise VerificationError(
                    f"Verify resource {name.replace('_', ' ')} is invalid"
                )

    @classmethod
    def from_wire(cls, value: object) -> "VerifyResourceUsage":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) != fields:
            raise VerificationError("Verify resource usage is invalid")
        return cls(**value)

    def to_wire(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }

    @property
    def is_zero(self) -> bool:
        return not any(self.to_wire().values())


@dataclass(frozen=True, slots=True)
class VerifyCommandEvidence:
    command_id: str
    command_hash: str
    status: VerifyCommandStatus
    exit_code: int | None
    started_at: str | None
    completed_at: str | None
    resource_usage: VerifyResourceUsage
    stdout_hash: str
    stdout_bytes: int
    stdout_log: str
    stderr_hash: str
    stderr_bytes: int
    stderr_log: str
    logs_truncated: bool
    raw_logs_omitted: bool
    duration_ms: int

    def __post_init__(self) -> None:
        try:
            status = VerifyCommandStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise VerificationError(
                "Verify command status is invalid"
            ) from exc
        object.__setattr__(self, "status", status)
        if not isinstance(self.command_id, str) or not self.command_id:
            raise VerificationError("Verify command ID is invalid")
        for name in ("command_hash", "stdout_hash", "stderr_hash"):
            _hash(getattr(self, name), name=name)
        if not isinstance(self.resource_usage, VerifyResourceUsage):
            raise VerificationError("Verify resource usage is invalid")
        exit_code = self.exit_code
        if (
            exit_code is not None
            and (isinstance(exit_code, bool) or not isinstance(exit_code, int))
        ):
            raise VerificationError("Verify command exit code is invalid")
        for name in ("stdout_bytes", "stderr_bytes", "duration_ms"):
            number = getattr(self, name)
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or number < 0
            ):
                raise VerificationError(
                    f"Verify {name.replace('_', ' ')} is invalid"
                )
        if not isinstance(self.logs_truncated, bool) or not isinstance(
            self.raw_logs_omitted,
            bool,
        ):
            raise VerificationError("Verify log state is invalid")
        if not isinstance(self.stdout_log, str) or not isinstance(
            self.stderr_log,
            str,
        ):
            raise VerificationError("Verify sanitized logs are invalid")
        _timestamp_pair(
            self.started_at,
            self.completed_at,
            allow_none=status is VerifyCommandStatus.NOT_RUN,
        )
        if (
            status is VerifyCommandStatus.PASSED
            and exit_code != 0
        ) or (
            status is VerifyCommandStatus.FAILED
            and (exit_code is None or exit_code == 0)
        ) or (
            status
            in {
                VerifyCommandStatus.TIMED_OUT,
                VerifyCommandStatus.NOT_RUN,
            }
            and exit_code is not None
        ):
            raise VerificationError(
                "Verify command status and exit code do not match"
            )
        if status is VerifyCommandStatus.NOT_RUN and (
            self.started_at is not None
            or self.completed_at is not None
            or self.duration_ms != 0
            or not self.resource_usage.is_zero
            or self.stdout_bytes != 0
            or self.stderr_bytes != 0
            or self.stdout_hash != hashlib.sha256(b"").hexdigest()
            or self.stderr_hash != hashlib.sha256(b"").hexdigest()
            or self.stdout_log
            or self.stderr_log
            or self.logs_truncated
            or self.raw_logs_omitted
        ):
            raise VerificationError(
                "Verify not-run command contains execution evidence"
            )
        if self.raw_logs_omitted:
            if (
                status is not VerifyCommandStatus.OUTPUT_LIMIT
                or self.stdout_log != OMITTED_LOG_MARKER
                or self.stderr_log != OMITTED_LOG_MARKER
            ):
                raise VerificationError(
                    "Verify omitted logs do not match output-limit evidence"
                )
        elif status is VerifyCommandStatus.OUTPUT_LIMIT:
            raise VerificationError(
                "Verify output-limit evidence must omit raw logs"
            )
        ensure_no_sensitive_data(
            {
                "stdout_log": self.stdout_log,
                "stderr_log": self.stderr_log,
            },
            context="Verify sanitized logs",
        )

    @classmethod
    def from_wire(cls, value: object) -> "VerifyCommandEvidence":
        if not isinstance(value, dict) or set(value) != {
            "command_id",
            "command_hash",
            "status",
            "exit_code",
            "started_at",
            "completed_at",
            "resource_usage",
            "stdout_hash",
            "stdout_bytes",
            "stdout_base64",
            "stderr_hash",
            "stderr_bytes",
            "stderr_base64",
            "raw_logs_omitted",
            "duration_ms",
        }:
            raise VerificationError("Verify command evidence is invalid")
        try:
            status = VerifyCommandStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise VerificationError("Verify command status is invalid") from exc
        raw_logs_omitted = value["raw_logs_omitted"]
        if not isinstance(raw_logs_omitted, bool):
            raise VerificationError("Verify log state is invalid")
        if raw_logs_omitted:
            if (
                value["stdout_base64"] is not None
                or value["stderr_base64"] is not None
            ):
                raise VerificationError(
                    "Verify omitted logs contain raw captures"
                )
            stdout_log = OMITTED_LOG_MARKER
            stderr_log = OMITTED_LOG_MARKER
            logs_truncated = False
        else:
            stdout = _captured_log(
                value["stdout_base64"],
                expected_hash=value["stdout_hash"],
                expected_bytes=value["stdout_bytes"],
                name="stdout",
            )
            stderr = _captured_log(
                value["stderr_base64"],
                expected_hash=value["stderr_hash"],
                expected_bytes=value["stderr_bytes"],
                name="stderr",
            )
            stdout_log, stdout_truncated = _sanitize_log(stdout)
            stderr_log, stderr_truncated = _sanitize_log(stderr)
            logs_truncated = stdout_truncated or stderr_truncated
        return cls(
            command_id=value["command_id"],
            command_hash=value["command_hash"],
            status=status,
            exit_code=value["exit_code"],
            started_at=value["started_at"],
            completed_at=value["completed_at"],
            resource_usage=VerifyResourceUsage.from_wire(
                value["resource_usage"]
            ),
            stdout_hash=value["stdout_hash"],
            stdout_bytes=value["stdout_bytes"],
            stdout_log=stdout_log,
            stderr_hash=value["stderr_hash"],
            stderr_bytes=value["stderr_bytes"],
            stderr_log=stderr_log,
            logs_truncated=logs_truncated,
            raw_logs_omitted=raw_logs_omitted,
            duration_ms=value["duration_ms"],
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "command_hash": self.command_hash,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "resource_usage": self.resource_usage.to_wire(),
            "stdout_hash": self.stdout_hash,
            "stdout_bytes": self.stdout_bytes,
            "stdout_log": self.stdout_log,
            "stderr_hash": self.stderr_hash,
            "stderr_bytes": self.stderr_bytes,
            "stderr_log": self.stderr_log,
            "logs_truncated": self.logs_truncated,
            "raw_logs_omitted": self.raw_logs_omitted,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class NormalizedTestResult:
    command_id: str
    command_hash: str
    outcome: VerifyCommandStatus
    exit_code: int | None
    started_at: str | None
    completed_at: str | None
    duration_ms: int
    stdout_hash: str
    stdout_bytes: int
    stderr_hash: str
    stderr_bytes: int
    version: str = NORMALIZED_TEST_RESULT_VERSION

    def __post_init__(self) -> None:
        if self.version != NORMALIZED_TEST_RESULT_VERSION:
            raise VerificationError(
                "Normalized test result version is unsupported"
            )
        try:
            outcome = VerifyCommandStatus(self.outcome)
        except (TypeError, ValueError) as exc:
            raise VerificationError(
                "Normalized test result outcome is invalid"
            ) from exc
        object.__setattr__(self, "outcome", outcome)
        if not isinstance(self.command_id, str) or not self.command_id:
            raise VerificationError(
                "Normalized test result command ID is invalid"
            )
        for name in ("command_hash", "stdout_hash", "stderr_hash"):
            _hash(getattr(self, name), name=name)
        if (
            self.exit_code is not None
            and (
                isinstance(self.exit_code, bool)
                or not isinstance(self.exit_code, int)
            )
        ):
            raise VerificationError(
                "Normalized test result exit code is invalid"
            )
        for name in ("duration_ms", "stdout_bytes", "stderr_bytes"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise VerificationError(
                    f"Normalized test result {name.replace('_', ' ')} is invalid"
                )
        _timestamp_pair(
            self.started_at,
            self.completed_at,
            allow_none=outcome is VerifyCommandStatus.NOT_RUN,
        )

    @classmethod
    def from_evidence(
        cls,
        evidence: VerifyCommandEvidence,
    ) -> "NormalizedTestResult":
        if not isinstance(evidence, VerifyCommandEvidence):
            raise VerificationError(
                "Normalized test result evidence is invalid"
            )
        return cls(
            command_id=evidence.command_id,
            command_hash=evidence.command_hash,
            outcome=evidence.status,
            exit_code=evidence.exit_code,
            started_at=evidence.started_at,
            completed_at=evidence.completed_at,
            duration_ms=evidence.duration_ms,
            stdout_hash=evidence.stdout_hash,
            stdout_bytes=evidence.stdout_bytes,
            stderr_hash=evidence.stderr_hash,
            stderr_bytes=evidence.stderr_bytes,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "command_id": self.command_id,
            "command_hash": self.command_hash,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "stdout_hash": self.stdout_hash,
            "stdout_bytes": self.stdout_bytes,
            "stderr_hash": self.stderr_hash,
            "stderr_bytes": self.stderr_bytes,
        }


@dataclass(frozen=True, slots=True)
class VerifyInspection:
    before_inventory_hash: str
    after_inventory_hash: str
    command_evidence: tuple[VerifyCommandEvidence, ...]

    @classmethod
    def from_wire(cls, value: object) -> "VerifyInspection":
        if not isinstance(value, dict) or set(value) != {
            "before_inventory_hash",
            "after_inventory_hash",
            "command_evidence",
        }:
            raise VerificationError("Verify inspection response is invalid")
        _hash(value["before_inventory_hash"], name="before inventory hash")
        _hash(value["after_inventory_hash"], name="after inventory hash")
        if not isinstance(value["command_evidence"], list):
            raise VerificationError("Verify command evidence is invalid")
        evidence = tuple(
            VerifyCommandEvidence.from_wire(item)
            for item in value["command_evidence"]
        )
        if not evidence or len(evidence) > MAX_VERIFY_COMMANDS:
            raise VerificationError("Verify command evidence count is invalid")
        return cls(
            before_inventory_hash=value["before_inventory_hash"],
            after_inventory_hash=value["after_inventory_hash"],
            command_evidence=evidence,
        )

    @property
    def succeeded(self) -> bool:
        return all(
            item.status is VerifyCommandStatus.PASSED
            for item in self.command_evidence
        )


@dataclass(frozen=True, slots=True)
class VerifyResult:
    spec_id: str
    spec_hash: str
    execution_attempt_id: str
    implement_result_hash: str
    repository_full_name: str
    base_commit_sha: str
    plan_version_id: str
    plan_record_hash: str
    runner_image_digest: str
    sandbox_policy_version: str
    sandbox_policy_hash: str
    workspace_inventory_hash: str
    before_inventory_hash: str
    after_inventory_hash: str
    succeeded: bool
    commands: tuple[SandboxCommand, ...]
    command_evidence: tuple[VerifyCommandEvidence, ...]
    test_results: tuple[NormalizedTestResult, ...]
    test_results_hash: str
    dependency_result_hash: str | None
    dependency_inventory_hash: str | None
    result_hash: str
    version: str = VERIFY_RESULT_VERSION

    def __post_init__(self) -> None:
        if self.version != VERIFY_RESULT_VERSION:
            raise VerificationError("Verify result version is unsupported")
        evidence = tuple(self.command_evidence)
        commands = tuple(self.commands)
        if (
            not commands
            or len(commands) != len(evidence)
            or any(not isinstance(item, SandboxCommand) for item in commands)
        ):
            raise VerificationError("Verify result commands are invalid")
        object.__setattr__(self, "commands", commands)
        if not evidence or any(
            not isinstance(item, VerifyCommandEvidence) for item in evidence
        ):
            raise VerificationError("Verify result evidence is invalid")
        object.__setattr__(self, "command_evidence", evidence)
        test_results = tuple(self.test_results)
        expected_test_results = tuple(
            NormalizedTestResult.from_evidence(item)
            for item in evidence
        )
        if test_results != expected_test_results:
            raise VerificationError(
                "Verify normalized test results do not match evidence"
            )
        object.__setattr__(self, "test_results", test_results)
        _hash(self.test_results_hash, name="test results hash")
        if self.test_results_hash != content_hash(
            [item.to_wire() for item in test_results]
        ):
            raise VerificationError(
                "Verify normalized test results hash does not match"
            )
        for command, item in zip(commands, evidence, strict=True):
            if (
                item.command_id != command.command_id
                or item.command_hash != content_hash(command.to_wire())
            ):
                raise VerificationError(
                    "Verify result command evidence does not match"
                )
        for name in (
            "spec_hash",
            "implement_result_hash",
            "plan_record_hash",
            "sandbox_policy_hash",
            "workspace_inventory_hash",
            "before_inventory_hash",
            "after_inventory_hash",
            "result_hash",
        ):
            _hash(getattr(self, name), name=name)
        if (
            self.dependency_result_hash is None
            and self.dependency_inventory_hash is not None
        ) or (
            self.dependency_result_hash is not None
            and self.dependency_inventory_hash is None
        ):
            raise VerificationError(
                "Verify dependency evidence is incomplete"
            )
        if self.dependency_result_hash is not None:
            _hash(
                self.dependency_result_hash,
                name="dependency result hash",
            )
            _hash(
                self.dependency_inventory_hash,
                name="dependency inventory hash",
            )
        if self.result_hash != content_hash(self.hash_payload()):
            raise VerificationError(
                "Verify result hash does not match its payload"
            )
        ensure_no_sensitive_data(self.to_wire(), context="Verify result")

    @classmethod
    def create(
        cls,
        *,
        signed_spec: SignedJobSpec,
        implement: ImplementResult,
        inspection: VerifyInspection,
        dependency: DependencyExecution | None = None,
    ) -> "VerifyResult":
        spec = signed_spec.spec
        payload = {
            "version": VERIFY_RESULT_VERSION,
            "spec_id": spec.spec_id,
            "spec_hash": signed_spec.spec_hash,
            "execution_attempt_id": spec.execution_attempt_id,
            "implement_result_hash": implement.result_hash,
            "repository_full_name": spec.repository_full_name,
            "base_commit_sha": spec.base_commit_sha,
            "plan_version_id": spec.plan_version_id,
            "plan_record_hash": spec.plan_record_hash,
            "runner_image_digest": spec.runner_image_digest,
            "sandbox_policy_version": spec.sandbox_policy_version,
            "sandbox_policy_hash": spec.sandbox_policy_hash,
            "workspace_inventory_hash": implement.result_inventory_hash,
            "before_inventory_hash": inspection.before_inventory_hash,
            "after_inventory_hash": inspection.after_inventory_hash,
            "succeeded": inspection.succeeded,
            "dependency_result_hash": (
                dependency.result.result_hash
                if dependency is not None
                else None
            ),
            "dependency_inventory_hash": (
                dependency.result.dependency_inventory_hash
                if dependency is not None
                else None
            ),
            "commands": [item.to_wire() for item in spec.commands],
            "command_evidence": [
                item.to_wire() for item in inspection.command_evidence
            ],
            "test_results": [
                NormalizedTestResult.from_evidence(item).to_wire()
                for item in inspection.command_evidence
            ],
        }
        payload["test_results_hash"] = content_hash(payload["test_results"])
        ensure_no_sensitive_data(payload, context="Verify result")
        constructor = dict(payload)
        constructor["commands"] = spec.commands
        constructor["command_evidence"] = inspection.command_evidence
        constructor["test_results"] = tuple(
            NormalizedTestResult.from_evidence(item)
            for item in inspection.command_evidence
        )
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
            "implement_result_hash": self.implement_result_hash,
            "repository_full_name": self.repository_full_name,
            "base_commit_sha": self.base_commit_sha,
            "plan_version_id": self.plan_version_id,
            "plan_record_hash": self.plan_record_hash,
            "runner_image_digest": self.runner_image_digest,
            "sandbox_policy_version": self.sandbox_policy_version,
            "sandbox_policy_hash": self.sandbox_policy_hash,
            "workspace_inventory_hash": self.workspace_inventory_hash,
            "before_inventory_hash": self.before_inventory_hash,
            "after_inventory_hash": self.after_inventory_hash,
            "succeeded": self.succeeded,
            "dependency_result_hash": self.dependency_result_hash,
            "dependency_inventory_hash": self.dependency_inventory_hash,
            "commands": [item.to_wire() for item in self.commands],
            "command_evidence": [
                item.to_wire() for item in self.command_evidence
            ],
            "test_results": [
                item.to_wire() for item in self.test_results
            ],
            "test_results_hash": self.test_results_hash,
            "result_hash": self.result_hash,
        }


class VerifyRuntime(Protocol):
    async def run(
        self,
        *,
        workspace: DisposableWorkspace,
        commands: tuple[SandboxCommand, ...],
        policy: SandboxPolicy,
        dependency_workspace: DependencyWorkspace | None = None,
    ) -> VerifyInspection: ...


class VerifyService:
    def __init__(
        self,
        *,
        signer: JobSpecSigner,
        policy: SandboxPolicy,
        runtime: VerifyRuntime,
    ) -> None:
        self.signer = signer
        self.policy = policy
        self.runtime = runtime

    async def run(
        self,
        signed_spec: SignedJobSpec,
        implement: ImplementResult,
        workspace: DisposableWorkspace,
        dependency: DependencyExecution | None = None,
    ) -> VerifyResult:
        try:
            spec = self.signer.verify(
                signed_spec,
                expected_policy=self.policy,
            )
        except JobSpecSignatureError as exc:
            raise VerificationError(str(exc)) from exc
        if spec.stage is not SandboxStage.VERIFY:
            raise VerificationError("Verify requires a Verify JobSpec")
        if len(spec.commands) > MAX_VERIFY_COMMANDS:
            raise VerificationError("Verify command count exceeds the limit")
        if implement.result_hash != content_hash(implement.hash_payload()):
            raise VerificationError("Implement result provenance is invalid")
        if (
            spec.execution_attempt_id != implement.execution_attempt_id
            or spec.repository_full_name != implement.repository_full_name
            or spec.base_commit_sha != implement.base_commit_sha
            or spec.repository_archive_hash
            != implement.repository_archive_hash
            or spec.plan_version_id != implement.plan_version_id
            or spec.plan_content_hash != implement.plan_content_hash
            or spec.plan_record_hash != implement.plan_record_hash
            or spec.runner_image_digest != implement.runner_image_digest
            or spec.sandbox_policy_version
            != implement.sandbox_policy_version
            or spec.sandbox_policy_hash != implement.sandbox_policy_hash
            or workspace.runner_image_digest != spec.runner_image_digest
            or workspace.sandbox_policy_hash != spec.sandbox_policy_hash
            or workspace.inventory_hash != implement.result_inventory_hash
        ):
            raise VerificationError(
                "Verify inputs do not match Implement provenance"
            )
        expected_artifacts = (implement.result_hash,)
        if dependency is not None:
            if (
                dependency.result.result_hash
                != content_hash(dependency.result.hash_payload())
                or dependency.result.execution_attempt_id
                != implement.execution_attempt_id
                or dependency.result.implement_result_hash
                != implement.result_hash
                or dependency.result.runner_image_digest
                != implement.runner_image_digest
                or dependency.result.sandbox_policy_hash
                != implement.sandbox_policy_hash
                or dependency.workspace.runner_image_digest
                != implement.runner_image_digest
                or dependency.workspace.sandbox_policy_hash
                != implement.sandbox_policy_hash
                or dependency.workspace.inventory_hash
                != dependency.result.dependency_inventory_hash
            ):
                raise VerificationError(
                    "Verify dependency provenance does not match Implement"
                )
            expected_artifacts = (
                implement.result_hash,
                dependency.result.result_hash,
            )
        if spec.input_artifact_hashes != expected_artifacts:
            raise VerificationError(
                "Verify artifact input does not match the JobSpec"
            )
        if dependency is None:
            inspection = await self.runtime.run(
                workspace=workspace,
                commands=spec.commands,
                policy=self.policy,
            )
        else:
            inspection = await self.runtime.run(
                workspace=workspace,
                commands=spec.commands,
                policy=self.policy,
                dependency_workspace=dependency.workspace,
            )
        if (
            inspection.before_inventory_hash
            != implement.result_inventory_hash
            or inspection.after_inventory_hash
            != implement.result_inventory_hash
        ):
            raise VerificationError(
                "Verify workspace inventory changed or does not match"
            )
        if len(inspection.command_evidence) != len(spec.commands):
            raise VerificationError(
                "Verify command evidence count does not match"
            )
        for command, evidence in zip(
            spec.commands,
            inspection.command_evidence,
            strict=True,
        ):
            if (
                evidence.command_id != command.command_id
                or evidence.command_hash != content_hash(command.to_wire())
            ):
                raise VerificationError(
                    "Verify command evidence does not match the JobSpec"
                )
        return VerifyResult.create(
            signed_spec=signed_spec,
            implement=implement,
            inspection=inspection,
            dependency=dependency,
        )


class DockerVerifyRuntime:
    def __init__(
        self,
        *,
        docker_environment: Mapping[str, str],
        docker_executable: str = "docker",
    ) -> None:
        unexpected = sorted(set(docker_environment) - _ALLOWED_DOCKER_ENV)
        if unexpected:
            raise ValueError(
                "Verify Docker environment contains disallowed variables: "
                + ", ".join(unexpected)
            )
        if not docker_executable or os.path.sep in docker_executable:
            raise ValueError("Verify Docker executable is invalid")
        self.docker_environment = dict(docker_environment)
        self.docker_executable = docker_executable

    async def run(
        self,
        *,
        workspace: DisposableWorkspace,
        commands: tuple[SandboxCommand, ...],
        policy: SandboxPolicy,
        dependency_workspace: DependencyWorkspace | None = None,
    ) -> VerifyInspection:
        if workspace.inventory_hash is None:
            raise VerificationError(
                "Verify workspace has no frozen inventory hash"
            )
        wrapper = {
            "expected_inventory_hash": workspace.inventory_hash,
            "max_log_bytes": policy.max_log_bytes,
            "timeout_seconds": policy.timeout_seconds,
            "commands": [command.to_wire() for command in commands],
            "dependency_inventory_hash": (
                dependency_workspace.inventory_hash
                if dependency_workspace is not None
                else None
            ),
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
        if len(encoded) > 500_000:
            raise VerificationError("Verify command input exceeds the limit")
        with tempfile.TemporaryDirectory(
            prefix="contribos-verify-"
        ) as temporary:
            command_path = os.path.join(temporary, "commands.json")
            with open(command_path, "xb") as handle:
                handle.write(encoded)
            os.chmod(command_path, 0o444)
            name = f"contribos-verify-{uuid4().hex}"
            stdout = await self._run_container(
                self.build_argv(
                    workspace=workspace,
                    commands_path=command_path,
                    policy=policy,
                    container_name=name,
                    dependency_workspace=dependency_workspace,
                ),
                container_name=name,
                timeout_seconds=policy.timeout_seconds + 5,
            )
        try:
            raw = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError("Verify returned invalid JSON") from exc
        return VerifyInspection.from_wire(raw)

    def build_argv(
        self,
        *,
        workspace: DisposableWorkspace,
        commands_path: str,
        policy: SandboxPolicy,
        container_name: str,
        dependency_workspace: DependencyWorkspace | None = None,
    ) -> tuple[str, ...]:
        if (
            not _VOLUME.fullmatch(workspace.volume_name)
            or not _CONTAINER.fullmatch(container_name)
            or not os.path.isabs(commands_path)
            or "," in commands_path
            or not os.path.isfile(commands_path)
        ):
            raise ValueError("Verify runtime input is invalid")
        tmpfs_bytes = policy.max_log_bytes * 2 + 16 * 1024 * 1024
        dependency_mount: tuple[str, ...] = ()
        if dependency_workspace is not None:
            if (
                dependency_workspace.runner_image_digest
                != workspace.runner_image_digest
                or dependency_workspace.sandbox_policy_hash
                != workspace.sandbox_policy_hash
                or dependency_workspace.inventory_hash is None
            ):
                raise ValueError("Verify dependency workspace is invalid")
            dependency_mount = (
                "--mount",
                (
                    "type=volume,"
                    f"src={dependency_workspace.volume_name},"
                    "dst=/dependencies,readonly"
                ),
            )
        return (
            self.docker_executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
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
            (
                f"/tmp:rw,noexec,nosuid,nodev,size={tmpfs_bytes},"
                "mode=1777"
            ),
            "--env",
            "HOME=/tmp",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "CI=1",
            *docker_git_safety_argv(),
            "--workdir",
            "/workspace",
            "--mount",
            (
                f"type=volume,src={workspace.volume_name},"
                "dst=/workspace,readonly"
            ),
            *dependency_mount,
            "--mount",
            (
                f"type=bind,src={commands_path},"
                "dst=/input/commands.json,readonly,"
                "bind-propagation=rprivate"
            ),
            "--entrypoint",
            "python3",
            workspace.runner_image_digest,
            "-I",
            "-c",
            _VERIFY_SCRIPT,
        )

    async def _run_container(
        self,
        argv: Sequence[str],
        *,
        container_name: str,
        timeout_seconds: int,
    ) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.docker_environment,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            await self._force_remove(container_name)
            raise VerificationError("Verify container timed out") from exc
        if (
            process.returncode != 0
            or stderr
            or len(stdout) > MAX_VERIFY_OUTPUT_BYTES
        ):
            raise VerificationError(
                "Verify container failed with a redacted error"
            )
        return stdout

    async def _force_remove(self, container_name: str) -> None:
        cleanup = await asyncio.create_subprocess_exec(
            self.docker_executable,
            "rm",
            "--force",
            container_name,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.docker_environment,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        except TimeoutError:
            cleanup.kill()
            await cleanup.wait()


def _captured_log(
    value: object,
    *,
    expected_hash: object,
    expected_bytes: object,
    name: str,
) -> bytes:
    _hash(expected_hash, name=f"{name} hash")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
        or not isinstance(value, str)
    ):
        raise VerificationError(f"Verify {name} capture is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise VerificationError(
            f"Verify {name} capture is invalid"
        ) from exc
    if (
        len(decoded) != expected_bytes
        or hashlib.sha256(decoded).hexdigest() != expected_hash
    ):
        raise VerificationError(
            f"Verify {name} capture does not match its evidence"
        )
    return decoded


def _sanitize_log(value: bytes) -> tuple[str, bool]:
    sanitized = redact_text(value.decode("utf-8", errors="replace"))
    if len(sanitized) <= MAX_SANITIZED_LOG_CHARS:
        return sanitized, False
    keep = MAX_SANITIZED_LOG_CHARS - len(TRUNCATED_LOG_MARKER)
    return sanitized[:keep] + TRUNCATED_LOG_MARKER, True


def _timestamp_pair(
    started_at: object,
    completed_at: object,
    *,
    allow_none: bool,
) -> None:
    if started_at is None and completed_at is None and allow_none:
        return
    if (
        not isinstance(started_at, str)
        or not isinstance(completed_at, str)
        or not _UTC_TIMESTAMP.fullmatch(started_at)
        or not _UTC_TIMESTAMP.fullmatch(completed_at)
    ):
        raise VerificationError("Verify command timestamps are invalid")
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    if (
        started.utcoffset() != timezone.utc.utcoffset(started)
        or completed.utcoffset() != timezone.utc.utcoffset(completed)
        or completed < started
    ):
        raise VerificationError("Verify command timestamps are invalid")


def _hash(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise VerificationError(f"Verify {name.replace('_', ' ')} is invalid")
    return value


_VERIFY_SCRIPT = r"""
import base64
from datetime import datetime, timezone
import hashlib
import glob
import json
import os
import resource
import stat
import subprocess
import time

root = "/workspace"
git_environment_names = __GIT_ENVIRONMENT_NAMES__
git_environment = {}
for name in git_environment_names:
    value = os.environ.get(name)
    if value is None:
        raise SystemExit(90)
    git_environment[name] = value
with open("/input/commands.json", encoding="utf-8") as handle:
    specification = json.load(handle)
if set(specification) != {
    "expected_inventory_hash", "max_log_bytes", "timeout_seconds", "commands",
    "dependency_inventory_hash",
}:
    raise SystemExit(80)
commands = specification["commands"]
if not commands or len(commands) > 20:
    raise SystemExit(81)
max_log_bytes = specification["max_log_bytes"]
deadline = time.monotonic() + specification["timeout_seconds"]

def inventory_hash():
    entries = []
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
                raise SystemExit(82)
            digest = hashlib.sha256()
            with open(absolute, "rb") as source:
                while True:
                    chunk = source.read(1048576)
                    if not chunk:
                        break
                    digest.update(chunk)
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
    return hashlib.sha256(encoded).hexdigest()

def file_evidence(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as source:
        while True:
            chunk = source.read(1048576)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size

def file_capture(path):
    with open(path, "rb") as source:
        return base64.b64encode(source.read()).decode("ascii")

def utc_timestamp():
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")

def zero_usage():
    return {
        "user_cpu_ms": 0,
        "system_cpu_ms": 0,
        "max_rss_bytes": 0,
        "minor_page_faults": 0,
        "major_page_faults": 0,
        "voluntary_context_switches": 0,
        "involuntary_context_switches": 0,
    }

def usage_delta(before, after):
    return {
        "user_cpu_ms": max(
            0, int((after.ru_utime - before.ru_utime) * 1000)
        ),
        "system_cpu_ms": max(
            0, int((after.ru_stime - before.ru_stime) * 1000)
        ),
        "max_rss_bytes": max(0, int(after.ru_maxrss) * 1024),
        "minor_page_faults": max(
            0, int(after.ru_minflt - before.ru_minflt)
        ),
        "major_page_faults": max(
            0, int(after.ru_majflt - before.ru_majflt)
        ),
        "voluntary_context_switches": max(
            0, int(after.ru_nvcsw - before.ru_nvcsw)
        ),
        "involuntary_context_switches": max(
            0, int(after.ru_nivcsw - before.ru_nivcsw)
        ),
    }

def dependency_inventory_hash():
    dependency_root = "/dependencies/output"
    entries = []
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
                digest, size = file_evidence(absolute)
                entries.append([relative, "file", size, digest])
            elif stat.S_ISLNK(metadata.st_mode):
                entries.append([relative, "symlink", os.readlink(absolute)])
            else:
                raise SystemExit(87)
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

before_hash = inventory_hash()
if before_hash != specification["expected_inventory_hash"]:
    raise SystemExit(83)
dependency_hash = specification["dependency_inventory_hash"]
dependency_environment = {}
if dependency_hash is not None:
    if (
        not os.path.isdir("/dependencies/output")
        or dependency_inventory_hash() != dependency_hash
    ):
        raise SystemExit(88)
    dependency_environment["VIRTUAL_ENV"] = "/dependencies/output/venv"
    dependency_environment["NODE_PATH"] = (
        "/dependencies/output/project/node_modules"
    )
    sites = sorted(
        glob.glob("/dependencies/output/venv/lib/python*/site-packages")
    )
    if sites:
        dependency_environment["PYTHONPATH"] = os.pathsep.join(sites)
evidence = []
halt = False
empty_hash = hashlib.sha256(b"").hexdigest()
for index, command in enumerate(commands):
    if set(command) != {"command_id", "argv", "working_directory"}:
        raise SystemExit(84)
    command_hash = hashlib.sha256(
        json.dumps(
            command,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if halt:
        evidence.append({
            "command_id": command["command_id"],
            "command_hash": command_hash,
            "status": "not_run",
            "exit_code": None,
            "started_at": None,
            "completed_at": None,
            "resource_usage": zero_usage(),
            "stdout_hash": empty_hash,
            "stdout_bytes": 0,
            "stdout_base64": "",
            "stderr_hash": empty_hash,
            "stderr_bytes": 0,
            "stderr_base64": "",
            "raw_logs_omitted": False,
            "duration_ms": 0,
        })
        continue
    directory = os.path.realpath(
        os.path.join(root, command["working_directory"])
    )
    if os.path.commonpath((root, directory)) != root or not os.path.isdir(directory):
        raise SystemExit(85)
    stdout_path = f"/tmp/verify-{index}.stdout"
    stderr_path = f"/tmp/verify-{index}.stderr"
    started_at = utc_timestamp()
    started = time.monotonic()
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    remaining = deadline - started
    status = "failed"
    exit_code = None
    if remaining <= 0:
        status = "timed_out"
        halt = True
    else:
        with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
            try:
                completed = subprocess.run(
                    command["argv"],
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env={
                        "HOME": "/tmp",
                        "LANG": "C.UTF-8",
                        "PATH": (
                            "/dependencies/output/venv/bin:"
                            + os.environ.get("PATH", "")
                            if dependency_hash is not None
                            else os.environ.get("PATH", "")
                        ),
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONUNBUFFERED": "1",
                        "CI": "1",
                        **git_environment,
                        **dependency_environment,
                    },
                    timeout=remaining,
                    check=False,
                    shell=False,
                )
                exit_code = completed.returncode
                status = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                status = "timed_out"
                halt = True
    if os.path.exists(stdout_path):
        stdout_hash, stdout_bytes = file_evidence(stdout_path)
    else:
        stdout_hash, stdout_bytes = empty_hash, 0
    if os.path.exists(stderr_path):
        stderr_hash, stderr_bytes = file_evidence(stderr_path)
    else:
        stderr_hash, stderr_bytes = empty_hash, 0
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    completed_at = utc_timestamp()
    raw_logs_omitted = stdout_bytes + stderr_bytes > max_log_bytes
    if raw_logs_omitted:
        status = "output_limit"
        halt = True
        stdout_base64 = None
        stderr_base64 = None
    else:
        stdout_base64 = (
            file_capture(stdout_path)
            if os.path.exists(stdout_path)
            else ""
        )
        stderr_base64 = (
            file_capture(stderr_path)
            if os.path.exists(stderr_path)
            else ""
        )
    for path in (stdout_path, stderr_path):
        if os.path.exists(path):
            os.unlink(path)
    evidence.append({
        "command_id": command["command_id"],
        "command_hash": command_hash,
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "completed_at": completed_at,
        "resource_usage": usage_delta(usage_before, usage_after),
        "stdout_hash": stdout_hash,
        "stdout_bytes": stdout_bytes,
        "stdout_base64": stdout_base64,
        "stderr_hash": stderr_hash,
        "stderr_bytes": stderr_bytes,
        "stderr_base64": stderr_base64,
        "raw_logs_omitted": raw_logs_omitted,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
    })
after_hash = inventory_hash()
if after_hash != before_hash:
    raise SystemExit(86)
if (
    dependency_hash is not None
    and dependency_inventory_hash() != dependency_hash
):
    raise SystemExit(89)
print(json.dumps({
    "before_inventory_hash": before_hash,
    "after_inventory_hash": after_hash,
    "command_evidence": evidence,
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
""".replace(
    "__GIT_ENVIRONMENT_NAMES__",
    repr(GIT_SAFETY_ENVIRONMENT_NAMES),
).strip()
