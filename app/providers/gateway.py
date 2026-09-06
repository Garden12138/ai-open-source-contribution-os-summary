from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from app.provenance import canonical_json
from app.providers.codex_cli import CodexExecInvocation
from app.providers.contracts import ProviderRunError, ProviderStage
from app.security import contains_sensitive_text, ensure_no_sensitive_data


GATEWAY_TOKEN_VERSION = "gateway-task-token-v1"
GATEWAY_AUDIENCE = "contribos-model-gateway"
MAX_GATEWAY_TOKEN_TTL_SECONDS = 300
MAX_GATEWAY_REQUESTS_PER_TASK = 20
MAX_GATEWAY_REQUEST_BYTES = 2_000_000
MAX_GATEWAY_RESPONSE_BYTES = 4_000_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")
_NETWORK = re.compile(r"^contribos-model-gateway-[a-z0-9-]{1,48}$")
_SERVICE = re.compile(r"^contribos-model-gateway(?:-[a-z0-9-]{1,48})?$")


class GatewayAuthorizationError(RuntimeError):
    pass


class GatewayAuthorizationExpiredError(GatewayAuthorizationError):
    pass


class GatewayAuthorizationReplayError(GatewayAuthorizationError):
    pass


class GatewayAuthorizationScopeError(GatewayAuthorizationError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayTaskScope:
    authorization_id: str
    request_id: str
    correlation_id: str
    snapshot_id: str
    stage: ProviderStage
    input_hash: str
    provider_name: str
    model: str
    audience: str
    max_requests: int
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.authorization_id, "authorization ID"),
            (self.request_id, "request ID"),
            (self.correlation_id, "correlation ID"),
            (self.snapshot_id, "snapshot ID"),
            (self.provider_name, "provider name"),
            (self.model, "model"),
            (self.audience, "audience"),
        ):
            if not _SAFE_ID.fullmatch(value) or contains_sensitive_text(value):
                raise ValueError(f"Gateway {name} is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.input_hash):
            raise ValueError("Gateway input hash must be SHA-256")
        issued = _aware(self.issued_at)
        expires = _aware(self.expires_at)
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        lifetime = (expires - issued).total_seconds()
        if not 0 < lifetime <= MAX_GATEWAY_TOKEN_TTL_SECONDS:
            raise ValueError("Gateway authorization lifetime is invalid")
        if self.audience != GATEWAY_AUDIENCE:
            raise ValueError("Gateway authorization audience is invalid")
        if not 1 <= self.max_requests <= MAX_GATEWAY_REQUESTS_PER_TASK:
            raise ValueError("Gateway authorization request limit is invalid")

    def claims(self) -> dict[str, object]:
        return {
            "version": GATEWAY_TOKEN_VERSION,
            "jti": self.authorization_id,
            "sub": self.request_id,
            "correlation_id": self.correlation_id,
            "snapshot_id": self.snapshot_id,
            "stage": self.stage.value,
            "input_hash": self.input_hash,
            "provider": self.provider_name,
            "model": self.model,
            "aud": self.audience,
            "max_requests": self.max_requests,
            "iat": int(self.issued_at.timestamp()),
            "exp": int(self.expires_at.timestamp()),
        }


@dataclass(frozen=True, slots=True)
class GatewayTaskCredential:
    token: str
    token_hash: str
    scope: GatewayTaskScope
    base_url: str
    network: str
    service_name: str

    def __post_init__(self) -> None:
        _validate_endpoint(
            base_url=self.base_url,
            network=self.network,
            service_name=self.service_name,
        )
        if not self.token.startswith("cgt1.") or len(self.token) > 8_192:
            raise ValueError("Gateway task token is invalid")
        expected_hash = hashlib.sha256(self.token.encode("ascii")).hexdigest()
        if self.token_hash != expected_hash:
            raise ValueError("Gateway task token hash does not match")


class GatewayTaskTokenCodec:
    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("Gateway signing key must contain at least 32 bytes")
        self._signing_key = bytes(signing_key)

    def encode(self, scope: GatewayTaskScope) -> str:
        header = _b64(canonical_json({"alg": "HS256", "typ": "CGT"}).encode())
        claims = _b64(canonical_json(scope.claims()).encode())
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = _b64(
            hmac.new(
                self._signing_key,
                signing_input,
                hashlib.sha256,
            ).digest()
        )
        return f"cgt1.{header}.{claims}.{signature}"

    def decode(
        self,
        token: str,
        *,
        now: datetime | None = None,
    ) -> GatewayTaskScope:
        if len(token) > 8_192:
            raise GatewayAuthorizationError("Gateway task token is invalid")
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "cgt1":
            raise GatewayAuthorizationError("Gateway task token is invalid")
        signing_input = f"{parts[1]}.{parts[2]}".encode("ascii")
        expected = hmac.new(
            self._signing_key,
            signing_input,
            hashlib.sha256,
        ).digest()
        try:
            supplied = _unb64(parts[3])
        except (ValueError, UnicodeError) as exc:
            raise GatewayAuthorizationError(
                "Gateway task token is invalid"
            ) from exc
        if not hmac.compare_digest(expected, supplied):
            raise GatewayAuthorizationError(
                "Gateway task token signature is invalid"
            )
        try:
            header = json.loads(_unb64(parts[1]))
            claims = json.loads(_unb64(parts[2]))
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise GatewayAuthorizationError(
                "Gateway task token payload is invalid"
            ) from exc
        if header != {"alg": "HS256", "typ": "CGT"}:
            raise GatewayAuthorizationError(
                "Gateway task token header is invalid"
            )
        scope = _scope_from_claims(claims)
        current = _aware(now)
        if current >= scope.expires_at:
            raise GatewayAuthorizationExpiredError(
                "Gateway task authorization expired"
            )
        if current < scope.issued_at - timedelta(seconds=5):
            raise GatewayAuthorizationError(
                "Gateway task authorization is not active"
            )
        return scope


class GatewayTaskCredentialBroker:
    """Mint one short-lived task credential without persisting its token."""

    def __init__(
        self,
        *,
        codec: GatewayTaskTokenCodec,
        base_url: str,
        network: str,
        service_name: str,
        provider_name: str = "codex_cli",
        ttl_seconds: int = 60,
        max_requests: int = MAX_GATEWAY_REQUESTS_PER_TASK,
    ) -> None:
        if not 1 <= ttl_seconds <= MAX_GATEWAY_TOKEN_TTL_SECONDS:
            raise ValueError("Gateway task Token TTL is invalid")
        _validate_endpoint(
            base_url=base_url,
            network=network,
            service_name=service_name,
        )
        if (
            not _SAFE_ID.fullmatch(provider_name)
            or contains_sensitive_text(provider_name)
        ):
            raise ValueError("Gateway Provider name is invalid")
        if not 1 <= max_requests <= MAX_GATEWAY_REQUESTS_PER_TASK:
            raise ValueError("Gateway task request limit is invalid")
        self.codec = codec
        self.base_url = base_url.rstrip("/")
        self.network = network
        self.service_name = service_name
        self.provider_name = provider_name
        self.ttl_seconds = ttl_seconds
        self.max_requests = max_requests

    def issue(
        self,
        invocation: CodexExecInvocation,
        *,
        now: datetime | None = None,
    ) -> GatewayTaskCredential:
        issued = _aware(now)
        scope = GatewayTaskScope(
            authorization_id=str(uuid4()),
            request_id=invocation.request_id,
            correlation_id=invocation.correlation_id,
            snapshot_id=invocation.snapshot_id,
            stage=invocation.stage,
            input_hash=invocation.input_hash,
            provider_name=self.provider_name,
            model=invocation.model,
            audience=GATEWAY_AUDIENCE,
            max_requests=self.max_requests,
            issued_at=issued,
            expires_at=issued + timedelta(seconds=self.ttl_seconds),
        )
        token = self.codec.encode(scope)
        return GatewayTaskCredential(
            token=token,
            token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
            scope=scope,
            base_url=self.base_url,
            network=self.network,
            service_name=self.service_name,
        )


class GatewayTaskAuthorizer:
    """Validate one task authorization for a bounded multi-turn model run."""

    def __init__(self, codec: GatewayTaskTokenCodec) -> None:
        self.codec = codec
        self._uses: dict[str, tuple[datetime, int]] = {}

    def consume(
        self,
        token: str,
        *,
        expected_provider: str,
        expected_model: str,
        now: datetime | None = None,
    ) -> GatewayTaskScope:
        current = _aware(now)
        self._uses = {
            key: state
            for key, state in self._uses.items()
            if state[0] > current
        }
        scope = self.codec.decode(token, now=current)
        if (
            scope.provider_name != expected_provider
            or scope.model != expected_model
        ):
            raise GatewayAuthorizationScopeError(
                "Gateway task authorization scope does not match"
            )
        _, prior_uses = self._uses.get(
            scope.authorization_id,
            (scope.expires_at, 0),
        )
        if prior_uses >= scope.max_requests:
            raise GatewayAuthorizationReplayError(
                "Gateway task authorization request limit was exhausted"
            )
        self._uses[scope.authorization_id] = (
            scope.expires_at,
            prior_uses + 1,
        )
        return scope


class ModelGatewayUpstream(Protocol):
    async def create_response(
        self,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class CredentialedUpstreamTransport(Protocol):
    async def send(
        self,
        payload: Mapping[str, Any],
        *,
        authorization_header: str,
    ) -> Mapping[str, Any]: ...


class GatewayProviderCredential:
    """Gateway-only real Provider credential with a permanently redacted repr."""

    __slots__ = ("__bearer_token",)

    def __init__(self, bearer_token: str) -> None:
        if (
            not bearer_token
            or len(bearer_token) > 8_192
            or any(character.isspace() for character in bearer_token)
        ):
            raise ValueError("Gateway Provider credential is invalid")
        self.__bearer_token = bearer_token

    def authorization_header(self) -> str:
        return f"Bearer {self.__bearer_token}"

    def __repr__(self) -> str:
        return "GatewayProviderCredential([REDACTED])"

    __str__ = __repr__


class CredentialedModelGatewayUpstream:
    """Apply the real credential only after the task gateway boundary."""

    __slots__ = ("_credential", "_transport")

    def __init__(
        self,
        *,
        credential: GatewayProviderCredential,
        transport: CredentialedUpstreamTransport,
    ) -> None:
        self._credential = credential
        self._transport = transport

    async def create_response(
        self,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await self._transport.send(
            payload,
            authorization_header=self._credential.authorization_header(),
        )


class InternalModelGateway:
    """Authorize one Responses request, then delegate behind the trust boundary."""

    def __init__(
        self,
        *,
        authorizer: GatewayTaskAuthorizer,
        upstream: ModelGatewayUpstream,
        provider_name: str = "codex_cli",
        max_request_bytes: int = MAX_GATEWAY_REQUEST_BYTES,
        max_response_bytes: int = MAX_GATEWAY_RESPONSE_BYTES,
    ) -> None:
        if max_request_bytes < 1 or max_response_bytes < 1:
            raise ValueError("Gateway byte limits must be positive")
        if (
            not _SAFE_ID.fullmatch(provider_name)
            or contains_sensitive_text(provider_name)
        ):
            raise ValueError("Gateway Provider name is invalid")
        self.authorizer = authorizer
        self.upstream = upstream
        self.provider_name = provider_name
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes

    async def create_response(
        self,
        *,
        authorization_header: str,
        payload: Mapping[str, Any],
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        token = _bearer(authorization_header)
        try:
            request_bytes = canonical_json(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise GatewayAuthorizationScopeError(
                "Gateway model request is invalid"
            ) from exc
        if len(request_bytes) > self.max_request_bytes:
            raise GatewayAuthorizationScopeError(
                "Gateway model request exceeds its byte limit"
            )
        try:
            ensure_no_sensitive_data(
                payload,
                context="model gateway request",
            )
        except ValueError as exc:
            raise GatewayAuthorizationScopeError(
                "Gateway model request contains unsafe data"
            ) from exc
        model = payload.get("model")
        if not isinstance(model, str):
            raise GatewayAuthorizationScopeError(
                "Gateway model request has no model"
            )
        self.authorizer.consume(
            token,
            expected_provider=self.provider_name,
            expected_model=model,
            now=now,
        )
        response = await self.upstream.create_response(payload)
        if not isinstance(response, Mapping):
            raise ProviderRunError(
                "gateway_upstream_invalid",
                "Model gateway upstream returned an invalid response",
                retryable=True,
            )
        try:
            response_bytes = canonical_json(response).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderRunError(
                "gateway_upstream_invalid",
                "Model gateway upstream returned an invalid response",
                retryable=True,
            ) from exc
        if len(response_bytes) > self.max_response_bytes:
            raise ProviderRunError(
                "gateway_upstream_limit",
                "Model gateway upstream exceeded its response byte limit",
                retryable=True,
            )
        try:
            ensure_no_sensitive_data(
                response,
                context="model gateway response",
            )
        except ValueError as exc:
            raise ProviderRunError(
                "gateway_upstream_unsafe",
                "Model gateway upstream returned unsafe data",
                retryable=False,
            ) from exc
        normalized = json.loads(response_bytes)
        if not isinstance(normalized, Mapping):
            raise ProviderRunError(
                "gateway_upstream_invalid",
                "Model gateway upstream returned an invalid response",
                retryable=True,
            )
        return normalized


def _scope_from_claims(value: Any) -> GatewayTaskScope:
    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "jti",
        "sub",
        "correlation_id",
        "snapshot_id",
        "stage",
        "input_hash",
        "provider",
        "model",
        "aud",
        "max_requests",
        "iat",
        "exp",
    }:
        raise GatewayAuthorizationError(
            "Gateway task token claims are invalid"
        )
    if value["version"] != GATEWAY_TOKEN_VERSION:
        raise GatewayAuthorizationError(
            "Gateway task token version is unsupported"
        )
    if (
        not isinstance(value["iat"], int)
        or isinstance(value["iat"], bool)
        or not isinstance(value["exp"], int)
        or isinstance(value["exp"], bool)
        or not isinstance(value["max_requests"], int)
        or isinstance(value["max_requests"], bool)
    ):
        raise GatewayAuthorizationError(
            "Gateway task token timestamps are invalid"
        )
    try:
        stage = ProviderStage(value["stage"])
        return GatewayTaskScope(
            authorization_id=str(value["jti"]),
            request_id=str(value["sub"]),
            correlation_id=str(value["correlation_id"]),
            snapshot_id=str(value["snapshot_id"]),
            stage=stage,
            input_hash=str(value["input_hash"]),
            provider_name=str(value["provider"]),
            model=str(value["model"]),
            audience=str(value["aud"]),
            max_requests=value["max_requests"],
            issued_at=datetime.fromtimestamp(value["iat"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(value["exp"], tz=timezone.utc),
        )
    except (ValueError, TypeError, OSError) as exc:
        raise GatewayAuthorizationError(
            "Gateway task token scope is invalid"
        ) from exc


def _validate_endpoint(
    *,
    base_url: str,
    network: str,
    service_name: str,
) -> None:
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {service_name, "127.0.0.1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
        or not _NETWORK.fullmatch(network)
        or not _SERVICE.fullmatch(service_name)
    ):
        raise ValueError("Internal model gateway endpoint is invalid")


def _bearer(value: str) -> str:
    prefix = "Bearer "
    if not value.startswith(prefix) or value.count(" ") != 1:
        raise GatewayAuthorizationError(
            "Gateway authorization header is invalid"
        )
    token = value[len(prefix) :]
    if not token.startswith("cgt1.") or len(token) > 8_192:
        raise GatewayAuthorizationError(
            "Gateway authorization header is invalid"
        )
    return token


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
