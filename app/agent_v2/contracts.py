"""Provider-neutral, bounded agent and sandbox tool contracts."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol, Self
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.security import ensure_no_sensitive_data

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="after")
    def safe(self) -> Self:
        ensure_no_sensitive_data(self.model_dump(mode="json"), context="agent v2")
        return self


def source_path(value: str) -> str:
    if (not value or len(value) > 500 or "\\" in value or "\x00" in value
            or value.startswith("/") or any(p in {"", ".", "..", ".git"} for p in value.split("/"))):
        raise ValueError("unsafe_source_path")
    return value


class Binding(StrictModel):
    task_id: Identifier
    snapshot_id: Identifier
    snapshot_hash: Hash
    attempt_id: Identifier
    attempt_hash: Hash
    plan_id: Identifier
    plan_hash: Hash
    plan_content_hash: Hash
    authorization_hash: Hash
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    archive_hash: Hash
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy_hash: Hash
    baseline_hash: Hash
    model_binding_hash: Hash


class Limits(StrictModel):
    model_calls: int = Field(default=24, ge=1, le=64)
    reads: int = Field(default=64, ge=1, le=256)
    writes: int = Field(default=24, ge=1, le=64)
    checks: int = Field(default=6, ge=1, le=12)
    protocol_repairs: int = Field(default=2, ge=0, le=4)
    provider_retries: int = Field(default=2, ge=0, le=3)
    code_repairs: int = Field(default=2, ge=0, le=3)
    input_tokens: int = Field(default=200000, ge=1000, le=2000000)
    output_tokens: int = Field(default=32000, ge=1000, le=100000)
    output_bytes: int = Field(default=500000, ge=4096, le=4000000)
    seconds: int = Field(default=1800, ge=30, le=7200)


class TaskSpec(StrictModel):
    version: Literal["agent-runtime-v2"] = "agent-runtime-v2"
    binding: Binding
    limits: Limits = Field(default_factory=Limits)
    goal: str = Field(min_length=1, max_length=12000)
    write_paths: list[str] = Field(min_length=1, max_length=500)
    commands: list[dict[str, Any]] = Field(min_length=1, max_length=100)

    @field_validator("write_paths")
    @classmethod
    def paths(cls, paths: list[str]) -> list[str]:
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate_write_path")
        return [source_path(p) for p in paths]


class Page(StrictModel):
    offset: int = Field(default=0, ge=0, le=8000000)
    max_bytes: int = Field(default=4096, ge=128, le=16000)


class ReadFile(Page):
    path: str
    expected_hash: Hash | None = Field(default=None,
        description="Optional FILE content_hash from read_file or file_content_hash from search_code. Omit on the first read. Never use the workspace source_hash here.")

    _path = field_validator("path")(source_path)


class ListPaths(Page):
    prefix: str = Field(default="", max_length=500)

    @field_validator("prefix")
    @classmethod
    def prefix_path(cls, value: str) -> str:
        if value:
            source_path(value.rstrip("/"))
        return value


class SearchCode(ListPaths):
    """Literal search; rows include exact byte_offset and file_content_hash for read_file."""
    query: str = Field(min_length=1, max_length=200)


class ApplyPatch(StrictModel):
    """One atomic exact edit; a later call may edit the same file again."""
    path: str
    expected_hash: Hash | None = Field(description="Exact FILE content_hash returned by read_file or the last successful patch, never workspace source_hash. Null only for a new file.")
    old_text: str = Field(max_length=100000)
    new_text: str = Field(max_length=100000)

    _path = field_validator("path")(source_path)

    @model_validator(mode="after")
    def anchor(self) -> Self:
        if self.expected_hash is not None and not self.old_text:
            raise ValueError("patch_anchor_required")
        if self.expected_hash is None and self.old_text:
            raise ValueError("new_file_anchor_invalid")
        return self


class RunChecks(StrictModel):
    """No argv supplied by the model: all approved commands always run."""


class InspectFailure(Page):
    artifact_id: Hash


class ToolCall(StrictModel):
    call_id: Identifier
    name: Literal["list_paths", "search_code", "read_file", "apply_patch", "read_diff",
                  "run_approved_checks", "inspect_failure", "read_artifact", "get_task_status"]
    arguments: dict[str, Any]


class Decision(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, json_schema_extra={
        "oneOf": [
            {"properties": {"outcome": {"const": "continue"}, "calls": {"minItems": 1}}, "required": ["calls"]},
            {"properties": {"outcome": {"enum": ["finish", "blocked"]}, "calls": {"maxItems": 0}}, "required": ["outcome"]},
        ]})
    reply: str = Field(default="", max_length=4000)
    calls: list[ToolCall] = Field(default_factory=list, max_length=4,
        description="Call IDs must be unique within this array. IDs are namespaced by the durable model turn.")
    outcome: Literal["continue", "finish", "blocked"] = "continue"

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.outcome == "continue" and not self.calls:
            raise ValueError("tool_or_terminal_required")
        if self.outcome != "continue" and self.calls:
            raise ValueError("terminal_with_tools")
        if len({c.call_id for c in self.calls}) != len(self.calls):
            raise ValueError("duplicate_call_id")
        return self


class Failure(StrictModel):
    category: Literal["protocol", "model", "environment", "baseline", "regression", "platform",
                      "external_unknown", "stale_upstream", "security", "budget"]
    safe_reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    retryable: bool
    remaining_budget: dict[str, int]
    next_allowed_action: str
    blocking_scope: str
    evidence_ref: Hash | None = None


class ToolResult(StrictModel):
    call_id: Identifier
    input_hash: Hash
    source_hash: Hash
    output_source_hash: Hash
    status: Literal["succeeded", "rejected", "unknown"]
    artifact_id: Hash | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    failure: Failure | None = None


class AgentCheckpoint(StrictModel):
    session_id: Identifier
    revision: Hash
    state: Literal["ready", "model", "tools", "paused", "blocked", "finished", "cancelled"]
    source_hash: Hash
    snapshot_artifact: Hash | None = None
    pending_calls: list[ToolCall] = Field(default_factory=list, max_length=4)
    usage: dict[str, int] = Field(default_factory=dict)
    last_result_hash: Hash | None = None


class AgentRun(StrictModel):
    session_id: Identifier
    checkpoint: AgentCheckpoint


class AgentModelAdapter(Protocol):
    capability: Literal["structured_tools", "native_tools", "text_only"]

    async def decide(self, prompt: dict, *, request_id: str, max_output_tokens: int) -> tuple[Decision, dict[str, int]]: ...


class ToolRegistry(Protocol):
    def validate(self, call: ToolCall, spec: TaskSpec) -> BaseModel: ...


class AgentExecutor(Protocol):
    async def start(self, task_spec: TaskSpec, tools: ToolRegistry) -> AgentRun: ...
    async def resume(self, checkpoint: AgentCheckpoint, feedback: ToolResult | None) -> AgentRun: ...
    async def cancel(self, run_id: str) -> None: ...
