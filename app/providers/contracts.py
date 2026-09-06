from __future__ import annotations

import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from app.provenance import content_hash
from app.security import contains_sensitive_text, ensure_no_sensitive_data


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = (
    JSONScalar | Sequence["JSONValue"] | Mapping[str, "JSONValue"]
)
FrozenJSONMapping: TypeAlias = Mapping[str, JSONValue]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProviderContractError(ValueError):
    """Raised when a provider boundary value is malformed or hash-stale."""


class ProviderRunError(RuntimeError):
    """Sanitized provider failure suitable for orchestration decisions."""

    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool,
    ) -> None:
        _require_safe_text(code, "provider error code", maximum=80)
        _require_safe_text(safe_message, "provider safe message", maximum=500)
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable


class ProviderStage(StrEnum):
    INSPECT = "inspect"
    ANALYZE = "analyze"
    CODING = "coding"
    CHANGE_SET = "change_set"
    REVIEW = "review"


def _require_text(value: str, name: str, *, maximum: int) -> None:
    if not value.strip() or len(value) > maximum:
        raise ProviderContractError(
            f"{name} must contain between 1 and {maximum} characters"
        )


def _require_safe_text(value: str, name: str, *, maximum: int) -> None:
    _require_text(value, name, maximum=maximum)
    if contains_sensitive_text(value):
        raise ProviderContractError(f"{name} must not contain credential-like data")


def _require_sha256(value: str, name: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ProviderContractError(f"{name} must be a lowercase SHA-256 hash")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderContractError("provider event timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _freeze_json(value: JSONValue) -> JSONValue:
    if isinstance(value, Mapping):
        frozen = {
            str(key): _freeze_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        # canonical hashing below rejects NaN and infinity.
        content_hash(value)
        return value
    raise ProviderContractError(
        f"provider JSON value has unsupported type {type(value).__name__}"
    )


def _freeze_mapping(value: Mapping[str, JSONValue]) -> FrozenJSONMapping:
    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise AssertionError("mapping freeze did not return a mapping")
    return frozen


def _unique(values: tuple[str, ...], name: str) -> None:
    if len(values) != len(set(values)):
        raise ProviderContractError(f"{name} must not contain duplicates")


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    provider: str
    adapter_version: str
    model: str
    model_version: str

    def __post_init__(self) -> None:
        _require_safe_text(self.provider, "provider name", maximum=80)
        _require_safe_text(self.adapter_version, "adapter version", maximum=80)
        _require_safe_text(self.model, "model name", maximum=120)
        _require_safe_text(self.model_version, "model version", maximum=120)

    def hash_payload(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "adapter_version": self.adapter_version,
            "model": self.model,
            "model_version": self.model_version,
        }


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_microusd: int = 0
    duration_ms: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "estimated_cost_microusd",
            "duration_ms",
        ):
            if getattr(self, name) < 0:
                raise ProviderContractError(f"{name} must be non-negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ProviderContractError(
                "cached_input_tokens cannot exceed input_tokens"
            )

    def hash_payload(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_microusd": self.estimated_cost_microusd,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class FrozenEvidence:
    evidence_id: str
    kind: str
    source_uri: str
    content: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence ID", maximum=128)
        _require_text(self.kind, "evidence kind", maximum=80)
        _require_text(self.source_uri, "evidence source URI", maximum=1000)
        _require_sha256(self.content_hash, "evidence content hash")
        if self.content_hash != self.calculate_hash(
            kind=self.kind,
            source_uri=self.source_uri,
            content=self.content,
        ):
            raise ProviderContractError("evidence content hash does not match content")

    @classmethod
    def capture(
        cls,
        *,
        evidence_id: str,
        kind: str,
        source_uri: str,
        content: str,
    ) -> "FrozenEvidence":
        return cls(
            evidence_id=evidence_id,
            kind=kind,
            source_uri=source_uri,
            content=content,
            content_hash=cls.calculate_hash(
                kind=kind,
                source_uri=source_uri,
                content=content,
            ),
        )

    @staticmethod
    def calculate_hash(*, kind: str, source_uri: str, content: str) -> str:
        return content_hash(
            {
                "kind": kind,
                "source_uri": source_uri,
                "content": content,
            }
        )

    def hash_payload(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "source_uri": self.source_uri,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class InspectRequest:
    request_id: str
    correlation_id: str
    snapshot_id: str
    score_version_id: str
    evidence: tuple[FrozenEvidence, ...]
    prompt_version: str
    policy_version: str
    output_schema_version: str
    input_hash: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.request_id, "inspect request ID"),
            (self.correlation_id, "correlation ID"),
            (self.snapshot_id, "snapshot ID"),
            (self.score_version_id, "score version ID"),
            (self.prompt_version, "prompt version"),
            (self.policy_version, "policy version"),
            (self.output_schema_version, "output schema version"),
        ):
            _require_text(value, name, maximum=128)
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.evidence:
            raise ProviderContractError("inspect request requires frozen evidence")
        _unique(
            tuple(item.evidence_id for item in self.evidence),
            "evidence IDs",
        )
        _require_sha256(self.input_hash, "inspect input hash")
        if self.input_hash != self.calculate_hash(
            snapshot_id=self.snapshot_id,
            score_version_id=self.score_version_id,
            evidence=self.evidence,
            prompt_version=self.prompt_version,
            policy_version=self.policy_version,
            output_schema_version=self.output_schema_version,
        ):
            raise ProviderContractError("inspect input hash does not match inputs")

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        correlation_id: str,
        snapshot_id: str,
        score_version_id: str,
        evidence: tuple[FrozenEvidence, ...],
        prompt_version: str,
        policy_version: str,
        output_schema_version: str,
    ) -> "InspectRequest":
        return cls(
            request_id=request_id,
            correlation_id=correlation_id,
            snapshot_id=snapshot_id,
            score_version_id=score_version_id,
            evidence=evidence,
            prompt_version=prompt_version,
            policy_version=policy_version,
            output_schema_version=output_schema_version,
            input_hash=cls.calculate_hash(
                snapshot_id=snapshot_id,
                score_version_id=score_version_id,
                evidence=evidence,
                prompt_version=prompt_version,
                policy_version=policy_version,
                output_schema_version=output_schema_version,
            ),
        )

    @staticmethod
    def calculate_hash(
        *,
        snapshot_id: str,
        score_version_id: str,
        evidence: tuple[FrozenEvidence, ...],
        prompt_version: str,
        policy_version: str,
        output_schema_version: str,
    ) -> str:
        return content_hash(
            {
                "snapshot_id": snapshot_id,
                "score_version_id": score_version_id,
                "evidence": [item.hash_payload() for item in evidence],
                "prompt_version": prompt_version,
                "policy_version": policy_version,
                "output_schema_version": output_schema_version,
            }
        )


@dataclass(frozen=True, slots=True)
class InspectionResult:
    request_id: str
    input_hash: str
    provider: ProviderIdentity
    structured_output: FrozenJSONMapping
    cited_evidence_ids: tuple[str, ...]
    usage: ProviderUsage
    output_hash: str

    def __post_init__(self) -> None:
        _require_text(self.request_id, "inspection request ID", maximum=128)
        _require_sha256(self.input_hash, "inspection input hash")
        _require_sha256(self.output_hash, "inspection output hash")
        object.__setattr__(
            self,
            "cited_evidence_ids",
            tuple(self.cited_evidence_ids),
        )
        _unique(self.cited_evidence_ids, "inspection evidence citations")
        for evidence_id in self.cited_evidence_ids:
            _require_text(evidence_id, "cited evidence ID", maximum=128)
        frozen_output = _freeze_mapping(self.structured_output)
        ensure_no_sensitive_data(
            frozen_output,
            context="provider inspection output",
        )
        object.__setattr__(self, "structured_output", frozen_output)
        expected = self.calculate_hash(
            input_hash=self.input_hash,
            provider=self.provider,
            structured_output=frozen_output,
            cited_evidence_ids=self.cited_evidence_ids,
        )
        if self.output_hash != expected:
            raise ProviderContractError(
                "inspection output hash does not match output"
            )

    @classmethod
    def create(
        cls,
        *,
        request: InspectRequest,
        provider: ProviderIdentity,
        structured_output: Mapping[str, JSONValue],
        cited_evidence_ids: tuple[str, ...],
        usage: ProviderUsage,
    ) -> "InspectionResult":
        frozen_output = _freeze_mapping(structured_output)
        return cls(
            request_id=request.request_id,
            input_hash=request.input_hash,
            provider=provider,
            structured_output=frozen_output,
            cited_evidence_ids=cited_evidence_ids,
            usage=usage,
            output_hash=cls.calculate_hash(
                input_hash=request.input_hash,
                provider=provider,
                structured_output=frozen_output,
                cited_evidence_ids=cited_evidence_ids,
            ),
        )

    @staticmethod
    def calculate_hash(
        *,
        input_hash: str,
        provider: ProviderIdentity,
        structured_output: Mapping[str, JSONValue],
        cited_evidence_ids: tuple[str, ...],
    ) -> str:
        return content_hash(
            {
                "input_hash": input_hash,
                "provider": provider.hash_payload(),
                "structured_output": structured_output,
                "cited_evidence_ids": cited_evidence_ids,
            }
        )


@dataclass(frozen=True, slots=True)
class AnalyzeRequest:
    request_id: str
    correlation_id: str
    snapshot_id: str
    score_version_id: str
    inspection: InspectionResult
    evidence: tuple[FrozenEvidence, ...]
    prompt_version: str
    policy_version: str
    output_schema_version: str
    input_hash: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.request_id, "analyze request ID"),
            (self.correlation_id, "correlation ID"),
            (self.snapshot_id, "snapshot ID"),
            (self.score_version_id, "score version ID"),
            (self.prompt_version, "prompt version"),
            (self.policy_version, "policy version"),
            (self.output_schema_version, "output schema version"),
        ):
            _require_text(value, name, maximum=128)
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.evidence:
            raise ProviderContractError("analyze request requires frozen evidence")
        _unique(
            tuple(item.evidence_id for item in self.evidence),
            "analysis evidence IDs",
        )
        _require_sha256(self.input_hash, "analyze input hash")
        expected = self.calculate_hash(
            snapshot_id=self.snapshot_id,
            score_version_id=self.score_version_id,
            inspection=self.inspection,
            evidence=self.evidence,
            prompt_version=self.prompt_version,
            policy_version=self.policy_version,
            output_schema_version=self.output_schema_version,
        )
        if self.input_hash != expected:
            raise ProviderContractError("analyze input hash does not match inputs")

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        correlation_id: str,
        snapshot_id: str,
        score_version_id: str,
        inspection: InspectionResult,
        evidence: tuple[FrozenEvidence, ...],
        prompt_version: str,
        policy_version: str,
        output_schema_version: str,
    ) -> "AnalyzeRequest":
        return cls(
            request_id=request_id,
            correlation_id=correlation_id,
            snapshot_id=snapshot_id,
            score_version_id=score_version_id,
            inspection=inspection,
            evidence=evidence,
            prompt_version=prompt_version,
            policy_version=policy_version,
            output_schema_version=output_schema_version,
            input_hash=cls.calculate_hash(
                snapshot_id=snapshot_id,
                score_version_id=score_version_id,
                inspection=inspection,
                evidence=evidence,
                prompt_version=prompt_version,
                policy_version=policy_version,
                output_schema_version=output_schema_version,
            ),
        )

    @staticmethod
    def calculate_hash(
        *,
        snapshot_id: str,
        score_version_id: str,
        inspection: InspectionResult,
        evidence: tuple[FrozenEvidence, ...],
        prompt_version: str,
        policy_version: str,
        output_schema_version: str,
    ) -> str:
        payload: dict[str, object] = {
            "snapshot_id": snapshot_id,
            "score_version_id": score_version_id,
            "inspection_input_hash": inspection.input_hash,
            "inspection_output_hash": inspection.output_hash,
            "prompt_version": prompt_version,
            "policy_version": policy_version,
            "output_schema_version": output_schema_version,
        }
        if output_schema_version == "analysis-schema-v4":
            payload["evidence"] = [item.hash_payload() for item in evidence]
        return content_hash(payload)

    @property
    def allowed_evidence_ids(self) -> tuple[str, ...]:
        if self.output_schema_version == "analysis-schema-v4":
            return tuple(item.evidence_id for item in self.evidence)
        return self.inspection.cited_evidence_ids


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    request_id: str
    input_hash: str
    provider: ProviderIdentity
    structured_output: FrozenJSONMapping
    cited_evidence_ids: tuple[str, ...]
    usage: ProviderUsage
    output_hash: str

    def __post_init__(self) -> None:
        _require_text(self.request_id, "analysis request ID", maximum=128)
        _require_sha256(self.input_hash, "analysis input hash")
        _require_sha256(self.output_hash, "analysis output hash")
        object.__setattr__(
            self,
            "cited_evidence_ids",
            tuple(self.cited_evidence_ids),
        )
        _unique(self.cited_evidence_ids, "analysis evidence citations")
        for evidence_id in self.cited_evidence_ids:
            _require_text(evidence_id, "cited evidence ID", maximum=128)
        frozen_output = _freeze_mapping(self.structured_output)
        ensure_no_sensitive_data(
            frozen_output,
            context="provider analysis output",
        )
        object.__setattr__(self, "structured_output", frozen_output)
        expected = self.calculate_hash(
            input_hash=self.input_hash,
            provider=self.provider,
            structured_output=frozen_output,
            cited_evidence_ids=self.cited_evidence_ids,
        )
        if self.output_hash != expected:
            raise ProviderContractError("analysis output hash does not match output")

    @classmethod
    def create(
        cls,
        *,
        request: AnalyzeRequest,
        provider: ProviderIdentity,
        structured_output: Mapping[str, JSONValue],
        cited_evidence_ids: tuple[str, ...],
        usage: ProviderUsage,
    ) -> "AnalysisResult":
        frozen_output = _freeze_mapping(structured_output)
        return cls(
            request_id=request.request_id,
            input_hash=request.input_hash,
            provider=provider,
            structured_output=frozen_output,
            cited_evidence_ids=cited_evidence_ids,
            usage=usage,
            output_hash=cls.calculate_hash(
                input_hash=request.input_hash,
                provider=provider,
                structured_output=frozen_output,
                cited_evidence_ids=cited_evidence_ids,
            ),
        )

    @staticmethod
    def calculate_hash(
        *,
        input_hash: str,
        provider: ProviderIdentity,
        structured_output: Mapping[str, JSONValue],
        cited_evidence_ids: tuple[str, ...],
    ) -> str:
        return content_hash(
            {
                "input_hash": input_hash,
                "provider": provider.hash_payload(),
                "structured_output": structured_output,
                "cited_evidence_ids": cited_evidence_ids,
            }
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderEvent:
    event_id: str
    request_id: str
    correlation_id: str
    sequence: int
    occurred_at: datetime
    stage: ProviderStage
    provider: ProviderIdentity

    def __post_init__(self) -> None:
        _require_text(self.event_id, "provider event ID", maximum=128)
        _require_text(self.request_id, "provider event request ID", maximum=128)
        _require_text(self.correlation_id, "correlation ID", maximum=128)
        if self.sequence < 1:
            raise ProviderContractError("provider event sequence must be at least 1")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderStartedEvent(ProviderEvent):
    kind: Literal["started"] = "started"
    input_hash: str
    prompt_version: str
    policy_version: str
    output_schema_version: str

    def __post_init__(self) -> None:
        super(ProviderStartedEvent, self).__post_init__()
        _require_sha256(self.input_hash, "provider event input hash")
        _require_text(self.prompt_version, "prompt version", maximum=128)
        _require_text(self.policy_version, "policy version", maximum=128)
        _require_text(
            self.output_schema_version,
            "output schema version",
            maximum=128,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderProgressEvent(ProviderEvent):
    kind: Literal["progress"] = "progress"
    message_code: str
    completed_units: int
    total_units: int | None = None

    def __post_init__(self) -> None:
        super(ProviderProgressEvent, self).__post_init__()
        _require_safe_text(
            self.message_code,
            "progress message code",
            maximum=80,
        )
        if self.completed_units < 0:
            raise ProviderContractError("completed_units must be non-negative")
        if self.total_units is not None and self.total_units < self.completed_units:
            raise ProviderContractError(
                "total_units cannot be less than completed_units"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderUsageEvent(ProviderEvent):
    kind: Literal["usage"] = "usage"
    usage: ProviderUsage


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderCompletedEvent(ProviderEvent):
    kind: Literal["completed"] = "completed"
    output_hash: str
    usage: ProviderUsage

    def __post_init__(self) -> None:
        super(ProviderCompletedEvent, self).__post_init__()
        _require_sha256(self.output_hash, "provider event output hash")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderFailedEvent(ProviderEvent):
    kind: Literal["failed"] = "failed"
    error_code: str
    safe_message: str
    retryable: bool

    def __post_init__(self) -> None:
        super(ProviderFailedEvent, self).__post_init__()
        _require_safe_text(self.error_code, "provider error code", maximum=80)
        _require_safe_text(
            self.safe_message,
            "provider safe message",
            maximum=500,
        )


ProviderLifecycleEvent: TypeAlias = (
    ProviderStartedEvent
    | ProviderProgressEvent
    | ProviderUsageEvent
    | ProviderCompletedEvent
    | ProviderFailedEvent
)


@runtime_checkable
class EventSink(Protocol):
    def __call__(self, event: ProviderLifecycleEvent) -> Awaitable[None]: ...


@runtime_checkable
class Inspector(Protocol):
    @property
    def identity(self) -> ProviderIdentity: ...

    async def inspect(
        self,
        request: InspectRequest,
        emit: EventSink,
    ) -> InspectionResult: ...


@runtime_checkable
class Analyzer(Protocol):
    @property
    def identity(self) -> ProviderIdentity: ...

    async def analyze(
        self,
        request: AnalyzeRequest,
        emit: EventSink,
    ) -> AnalysisResult: ...


@runtime_checkable
class AnalysisProvider(Inspector, Analyzer, Protocol):
    """Composite provider-neutral interface used by the orchestrator."""
