"""Immutable wire contracts shared by inspection, preparation and admission."""
from __future__ import annotations

from typing import Any, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer
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


class ResolvedCommand(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1, max_length=100)
    working_directory: str

    @field_validator("working_directory")
    @classmethod
    def cwd(cls, value: str) -> str:
        return safe_path(value, directory=True)


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
    leaf_commands: tuple[ResolvedCommand, ...] | None = None

    @model_serializer(mode="wrap")
    def legacy_wire(self, handler: Any) -> dict:
        value = handler(self)
        if self.leaf_commands is None and "leaf_commands" not in self.model_fields_set:
            value.pop("leaf_commands", None)
        if self.required_module is None and "required_module" not in self.model_fields_set:
            value.pop("required_module", None)
        return value

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


class CIToolchainDeclaration(StrictModel):
    tool: Literal["pnpm", "node"]
    range: str = Field(min_length=1, max_length=160, pattern=r"^[0-9vVxX.*<>=~^| +\-]+$")
    action: str = Field(pattern=r"^(?:pnpm/action-setup|actions/setup-node)@(?:v[0-9]+(?:\.[0-9]+)*|[a-f0-9]{40})$")
    input: Literal["version", "node-version", "node-version-file"]
    path: str
    line: int = Field(ge=1, le=1000000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    value_source: SourceRef | None = None

    @field_validator("path")
    @classmethod
    def path_safe(cls, value: str) -> str:
        return safe_path(value)

    @model_validator(mode="after")
    def coherent(self) -> CIToolchainDeclaration:
        if (not any(c in "0123456789xX*" for c in self.range)
                or (self.tool == "pnpm") != self.action.startswith("pnpm/")
                or (self.input == "version") != (self.tool == "pnpm")
                or (self.input == "node-version-file") != (self.value_source is not None)):
            raise ValueError("environment_ci_declaration_invalid")
        return self


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
            adapter = self.resolver_versions.get("adapter")
            if adapter not in {"python-toolchain-v1", "python-toolchain-v2", "python-toolchain-v3", "python-toolchain-v4", "node-toolchain-v1", "node-toolchain-v2", "node-toolchain-v3", "node-toolchain-v4"}:
                raise ValueError("environment_adapter_version_unsupported")
            python = adapter in {"python-toolchain-v1", "python-toolchain-v2", "python-toolchain-v3", "python-toolchain-v4"}
            required = {"requirements", "constraints", "requirements_roots"} if python else {"independent_directories"}
            if adapter == "node-toolchain-v4" and "toolchain_declarations" in self.preparation:
                required.add("toolchain_declarations")
                values = self.preparation["toolchain_declarations"]
                if not isinstance(values, list) or not 1 <= len(values) <= 32:
                    raise ValueError("environment_ci_declaration_invalid")
                for value in values:
                    declaration = CIToolchainDeclaration.model_validate(value)
                    refs = [SourceRef(path=declaration.path, sha256=declaration.sha256, parser="node-ci-toolchains-v1")]
                    if declaration.value_source:
                        refs.append(declaration.value_source)
                    if any(not any(ref.path == e.path and ref.sha256 == e.sha256 for e in self.evidence) for ref in refs):
                        raise ValueError("environment_ci_source_missing")
            if adapter in {"python-toolchain-v3", "python-toolchain-v4"}:
                from app.sandbox_worker.python_cache import MYPY_CACHE_CAPABILITY, MYPY_CACHE_CAPABILITY_V1
                required.add("cache_policy")
                expected_cache = MYPY_CACHE_CAPABILITY_V1 if adapter == "python-toolchain-v3" else MYPY_CACHE_CAPABILITY
                if self.preparation.get("cache_policy") not in (None, expected_cache):
                    raise ValueError("environment_cache_policy_invalid")
            if set(self.preparation) != required:
                raise ValueError("environment_recipe_schema_invalid")
            if adapter in {"node-toolchain-v2", "node-toolchain-v3", "node-toolchain-v4"} and any(c.leaf_commands is None for c in self.commands):
                raise ValueError("environment_command_graph_incomplete")
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
    data_cache: dict | None = None

    @model_serializer(mode="wrap")
    def legacy_wire(self, handler: Any) -> dict:
        value = handler(self)
        if self.data_cache is None:
            value.pop("data_cache", None)
        return value

    @model_validator(mode="after")
    def complete(self) -> ToolchainPreparationReceipt:
        if self.status == "prepared" and (not self.executables or any(e.status != "available" for e in self.executables)):
            raise ValueError("entrypoint_evidence_missing")
        if self.data_cache is not None:
            from app.sandbox_worker.python_cache import validate_python_cache
            validate_python_cache(self.data_cache)
            if self.data_cache["contract_hash"] != self.contract_hash or self.data_cache["plan_hash"] != self.dependency_plan_hash:
                raise ValueError("environment_cache_receipt_stale")
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
