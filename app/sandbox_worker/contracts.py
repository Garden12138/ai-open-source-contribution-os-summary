from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.provenance import canonical_json
from app.security import ensure_no_sensitive_data


SANDBOX_WORKER_PROTOCOL_VERSION = "sandbox-worker-v1"
MAX_WORKER_MESSAGE_BYTES = 65_536
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


class SandboxWorkerContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SandboxWorkerRequest:
    request_id: str
    correlation_id: str
    operation: str
    payload: dict[str, object]
    protocol_version: str = SANDBOX_WORKER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != SANDBOX_WORKER_PROTOCOL_VERSION:
            raise SandboxWorkerContractError(
                "Sandbox Worker protocol version is unsupported"
            )
        _identifier(self.request_id, name="request ID")
        _identifier(self.correlation_id, name="correlation ID")
        if not _OPERATION.fullmatch(self.operation):
            raise SandboxWorkerContractError(
                "Sandbox Worker operation is invalid"
            )
        if not isinstance(self.payload, dict):
            raise SandboxWorkerContractError(
                "Sandbox Worker payload must be an object"
            )
        ensure_no_sensitive_data(
            self.payload,
            context="Sandbox Worker request",
        )
        _bounded_wire(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "operation": self.operation,
            "payload": self.payload,
        }

    @classmethod
    def from_bytes(cls, value: bytes) -> "SandboxWorkerRequest":
        raw = _decode_object(value, name="request")
        if set(raw) != {
            "protocol_version",
            "request_id",
            "correlation_id",
            "operation",
            "payload",
        }:
            raise SandboxWorkerContractError(
                "Sandbox Worker request fields are invalid"
            )
        return cls(
            protocol_version=raw["protocol_version"],
            request_id=raw["request_id"],
            correlation_id=raw["correlation_id"],
            operation=raw["operation"],
            payload=raw["payload"],
        )

    def to_bytes(self) -> bytes:
        return (canonical_json(self.to_wire()) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class SandboxWorkerResponse:
    request_id: str
    correlation_id: str
    ok: bool
    result: dict[str, object]
    error_code: str | None = None
    error_message: str | None = None
    protocol_version: str = SANDBOX_WORKER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != SANDBOX_WORKER_PROTOCOL_VERSION:
            raise SandboxWorkerContractError(
                "Sandbox Worker response protocol is unsupported"
            )
        _identifier(self.request_id, name="response request ID")
        _identifier(self.correlation_id, name="response correlation ID")
        if not isinstance(self.ok, bool) or not isinstance(self.result, dict):
            raise SandboxWorkerContractError(
                "Sandbox Worker response shape is invalid"
            )
        if self.ok:
            if self.error_code is not None or self.error_message is not None:
                raise SandboxWorkerContractError(
                    "Successful Sandbox Worker response contains an error"
                )
        else:
            if (
                not isinstance(self.error_code, str)
                or not _ERROR_CODE.fullmatch(self.error_code)
                or not isinstance(self.error_message, str)
                or not self.error_message.strip()
                or len(self.error_message) > 500
            ):
                raise SandboxWorkerContractError(
                    "Sandbox Worker error response is invalid"
                )
        ensure_no_sensitive_data(
            {
                "result": self.result,
                "error_code": self.error_code,
                "error_message": self.error_message,
            },
            context="Sandbox Worker response",
        )
        _bounded_wire(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "ok": self.ok,
            "result": self.result,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_bytes(cls, value: bytes) -> "SandboxWorkerResponse":
        raw = _decode_object(value, name="response")
        if set(raw) != {
            "protocol_version",
            "request_id",
            "correlation_id",
            "ok",
            "result",
            "error_code",
            "error_message",
        }:
            raise SandboxWorkerContractError(
                "Sandbox Worker response fields are invalid"
            )
        return cls(
            protocol_version=raw["protocol_version"],
            request_id=raw["request_id"],
            correlation_id=raw["correlation_id"],
            ok=raw["ok"],
            result=raw["result"],
            error_code=raw["error_code"],
            error_message=raw["error_message"],
        )

    def to_bytes(self) -> bytes:
        return (canonical_json(self.to_wire()) + "\n").encode("utf-8")


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SandboxWorkerContractError(f"Sandbox Worker {name} is invalid")
    return value


def _decode_object(value: bytes, *, name: str) -> dict[str, object]:
    if not isinstance(value, bytes) or not value or len(value) > (
        MAX_WORKER_MESSAGE_BYTES
    ):
        raise SandboxWorkerContractError(
            f"Sandbox Worker {name} size is invalid"
        )
    try:
        decoded = value.decode("utf-8")
        raw = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxWorkerContractError(
            f"Sandbox Worker {name} JSON is invalid"
        ) from exc
    if not isinstance(raw, dict):
        raise SandboxWorkerContractError(
            f"Sandbox Worker {name} must be an object"
        )
    return raw


def _bounded_wire(value: dict[str, object]) -> None:
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SandboxWorkerContractError(
            "Sandbox Worker message contains unsupported JSON"
        ) from exc
    if len(encoded) > MAX_WORKER_MESSAGE_BYTES:
        raise SandboxWorkerContractError(
            "Sandbox Worker message exceeds the byte limit"
        )
