"""Immutable wire contracts shared by inspection, preparation and admission."""
from __future__ import annotations

from typing import Literal, Self
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.provenance import content_hash

Hash = str


def safe_path(value: str, *, directory: bool = False) -> str:
    if directory and value == ".":
        return value
    if (not value or len(value) > 500 or value.startswith("/") or "\\" in value
            or any(ord(c) < 32 for c in value)
            or any(p in {"", ".", "..", ".git", "node_modules"} for p in value.split("/"))):
        raise ValueError("source_path_unsafe")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="after")
    def no_secrets(self) -> Self:
        from app.security import ensure_no_sensitive_data
        ensure_no_sensitive_data(self.model_dump(mode="json"), context="environment contract")
        return self


class SourceRef(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser: str
    _path = field_validator("path")(safe_path)


class Gap(StrictModel):
    reason_code: str
    paths: tuple[str, ...] = ()
    command_id: str | None = None


class BaselineExpectation(StrictModel):
    version: Literal["expected-issue-reproduction-v1"] = "expected-issue-reproduction-v1"
    issue_url: str = Field(pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*$")
    test_node: str = Field(min_length=5, max_length=500)
    failure_literal: str = Field(min_length=12, max_length=300)
    exit_code: Literal[1] = 1


class CommandNode(StrictModel):
    command_id: str
    argv: tuple[str, ...] = Field(min_length=1, max_length=100)
    working_directory: str
    source_refs: tuple[SourceRef, ...] = ()
    chain: tuple[str, ...] = ()
    entrypoints: tuple[tuple[str, str], ...] = ()
    required_executable: str
    required_module: str | None = None
    distributions: tuple[str, ...] = ()
    resolution: Literal["resolved", "unsupported"] = "resolved"

    @field_validator("working_directory")
    @classmethod
    def cwd(cls, value: str) -> str:
        return safe_path(value, directory=True)


class EnvironmentBinding(StrictModel):
    task_id: str
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_archive_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_commit_sha: str = Field(pattern=r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
    plan_version_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_commands_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_platform: str = Field(pattern=r"^linux/(?:arm64|amd64)$")
    runner_image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sandbox_policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class RequirementDeclaration(StrictModel):
    requirement: str
    distribution: str
    constraint: bool
    source: SourceRef
    line: int | None = Field(default=None, ge=1)


class VerificationEnvironmentContract(StrictModel):
    version: Literal["verification-environment-contract-v1"] = "verification-environment-contract-v1"
    binding: EnvironmentBinding
    resolver_versions: dict[str, str]
    commands: tuple[CommandNode, ...] = Field(min_length=1, max_length=100)
    declarations: tuple[RequirementDeclaration, ...] = ()
    evidence: tuple[SourceRef, ...] = ()
    preparation: dict
    unresolved: tuple[Gap, ...] = ()
    admission: Literal["static_admitted", "unsupported", "unverified"]
    expected_failures: dict[str, BaselineExpectation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exact_commands(self) -> VerificationEnvironmentContract:
        commands = [{"command_id": c.command_id, "argv": list(c.argv),
                     "working_directory": c.working_directory} for c in self.commands]
        if (len({c.command_id for c in self.commands}) != len(self.commands)
                or content_hash(commands) != self.binding.approved_commands_hash
                or self.admission == "static_admitted" and (self.unresolved or any(c.resolution != "resolved" for c in self.commands))):
            raise ValueError("environment_command_binding_mismatch")
        if self.expected_failures and (not set(self.expected_failures) < {c.command_id for c in self.commands}
                or any(e.test_node not in next(c.argv for c in self.commands if c.command_id == key)
                       for key, e in self.expected_failures.items())):
            raise ValueError("expected_reproduction_requires_exact_target_and_control")
        if self.admission == "static_admitted":
            python = self.resolver_versions.get("adapter") == "python-toolchain-v1"
            required = {"requirements", "constraints", "requirements_roots"} if python else {"independent_directories"}
            if set(self.preparation) != required:
                raise ValueError("environment_recipe_schema_invalid")
            if python and (self.preparation["requirements"] != [d.requirement for d in self.declarations if not d.constraint]
                    or self.preparation["constraints"] != [d.requirement for d in self.declarations if d.constraint]):
                raise ValueError("environment_recipe_declaration_mismatch")
        return self

    @property
    def contract_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class ExecutableReceipt(StrictModel):
    command_id: str
    argv: tuple[str, ...]
    working_directory: str
    executable: str | None
    requested_executable: str | None = None
    distribution: str | None = None
    distribution_version: str | None = None
    executable_hash: str | None = None
    ownership: str | None = None
    status: Literal["available", "missing", "unsupported"]
    reason_code: str | None = None


class ToolchainPreparationReceipt(StrictModel):
    version: Literal["toolchain-preparation-receipt-v1"] = "toolchain-preparation-receipt-v1"
    contract_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    dependency_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    dependency_result_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_platform: str
    image_digest: str
    policy_hash: str
    executables: tuple[ExecutableReceipt, ...]
    status: Literal["prepared", "partial", "unsupported", "failed"]

    @model_validator(mode="after")
    def complete(self) -> ToolchainPreparationReceipt:
        if self.status == "prepared" and (not self.executables or any(e.status != "available" for e in self.executables)):
            raise ValueError("entrypoint_evidence_missing")
        return self


class BaselineVerdict(StrictModel):
    version: Literal["baseline-verdict-v1"] = "baseline-verdict-v1"
    contract_hash: str
    preparation_receipt_hash: str
    baseline_artifact_hash: str
    required_command_count: int
    executed_command_count: int
    outcome: Literal["passed", "expected_issue_reproduction", "no_change", "preexisting_failure", "environment_blocked", "unsupported", "unknown"]
    reason_codes: tuple[str, ...]
    command_results: tuple[dict, ...]
