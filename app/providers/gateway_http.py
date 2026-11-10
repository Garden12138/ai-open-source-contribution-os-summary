from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.providers.contracts import ProviderRunError
from app.providers.gateway_storage import GatewayStorageError
from app.providers.gateway import (
    GatewayAuthorizationError,
    GatewayAuthorizationExpiredError,
    GatewayAuthorizationReplayError,
    GatewayAuthorizationScopeError,
    InternalModelGateway,
)


logger = logging.getLogger(__name__)


def create_model_gateway_app(gateway: InternalModelGateway, *, secret_store=None, management_key=None, readiness_key=None) -> FastAPI:
    """Create the internal-only HTTP boundary consumed by Codex CLI."""

    app = FastAPI(
        title="ContribOS Internal Model Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(GatewayStorageError)
    async def storage_error(_: Request, exc: GatewayStorageError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": {
            "code": exc.code, "message": "Credential storage unavailable; explicit configuration may be required",
            "retryable": False}})

    @app.exception_handler(Exception)
    async def unexpected_error(
        _: Request,
        __: Exception,
    ) -> JSONResponse:
        return _error(
            500,
            "gateway_internal_error",
            "Internal model gateway request failed",
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/readiness")
    async def readiness(request: Request):
        import re
        import time
        from app.provenance import canonical_json, content_hash
        from app.providers.model_secrets import management_auth, verify_management
        from app.model_settings import ProfileInput, ConnectionInput
        from app.providers.gateway_readiness import readonly_key
        key = readiness_key or (readonly_key(management_key) if management_key else None)
        if key is None:
            return _error(403, "management_disabled", "Gateway management is disabled")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 24000:
                return _error(413, "management_limit", "Readiness request is too large")
        try:
            verify_management(key, bytes(body), request.headers.get("X-Gateway-Readiness", ""))
            value = json.loads(body)
            if not re.fullmatch(r"[a-f0-9]{32}", value["nonce"]):
                raise ValueError()
            operation = value["operation"]
            if operation not in {"storage_readiness", "credential_readiness"}:
                raise ValueError()
            profile = value.get("profile")
            if operation == "credential_readiness":
                ProfileInput.model_validate({k: v for k, v in profile.items() if k not in {"id", "record_hash", "connection"}})
                ConnectionInput.model_validate({k: v for k, v in profile["connection"].items() if k not in {"id", "record_hash"}})
        except (ValueError, KeyError, TypeError, AttributeError):
            return _error(403, "management_rejected", "Readiness authorization or binding rejected")
        if not hasattr(secret_store, "storage_readiness") or value.get("store_id") != secret_store.store_id:
            raise GatewayStorageError("identity_changed")
        result = (secret_store.credential_readiness(profile["connection"]["credential_ref"])
                  if operation == "credential_readiness" else secret_store.storage_readiness())
        result.update(checked_at=int(time.time()), nonce=value["nonce"])
        if profile is not None:
            result["profile_hash"] = content_hash(profile)
        result["proof_hash"] = content_hash(result)
        result["proof_auth"] = management_auth(key, canonical_json(result).encode())
        return result

    @app.post("/internal/settings")
    async def manage(request: Request):
        from app.providers.model_secrets import verify_management
        if secret_store is None or management_key is None:
            return _error(403, "management_disabled", "Gateway management is disabled")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 24000:
                return _error(413, "management_limit", "Gateway management request is too large")
        try:
            verify_management(management_key, bytes(body), request.headers.get("X-Gateway-Management", ""))
            payload = json.loads(body)
            operation = payload["operation"]
            if operation == "grant":
                import re
                if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", payload["connection_id"]):
                    raise ValueError()
                return secret_store.grant(payload["connection_id"])
            if operation == "save":
                return secret_store.save(payload["connection_id"], payload["envelope"])
            if operation == "has":
                return {"configured": secret_store.has(payload["credential_ref"])}
            raise ValueError()
        except (ValueError, KeyError, TypeError):
            return _error(403, "management_rejected", "Gateway management request was rejected")

    @app.post("/v1/responses")
    @app.post("/v1/chat/completions")
    async def create_response(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return _error(
                415,
                "gateway_content_type",
                "Model gateway accepts application/json only",
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > gateway.max_request_bytes:
                return _error(
                    413,
                    "gateway_request_limit",
                    "Model gateway request exceeds its byte limit",
                )
        try:
            payload: Any = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error(
                400,
                "gateway_invalid_json",
                "Model gateway request is invalid",
            )
        if not isinstance(payload, Mapping):
            return _error(
                400,
                "gateway_invalid_json",
                "Model gateway request is invalid",
            )
        try:
            response = await gateway.create_response(
                authorization_header=request.headers.get(
                    "authorization",
                    "",
                ),
                payload=payload,
            )
        except GatewayAuthorizationExpiredError:
            return _error(
                401,
                "gateway_authorization_expired",
                "Model gateway authorization expired",
            )
        except GatewayAuthorizationReplayError:
            return _error(
                429,
                "gateway_authorization_exhausted",
                "Model gateway authorization request limit was exhausted",
            )
        except GatewayAuthorizationScopeError:
            return _error(
                403,
                "gateway_authorization_scope",
                "Model gateway authorization scope was rejected",
            )
        except GatewayAuthorizationError:
            return _error(
                401,
                "gateway_authorization_invalid",
                "Model gateway authorization was rejected",
            )
        except ProviderRunError as exc:
            logger.warning(
                "Model gateway upstream request failed: code=%s retryable=%s message=%s",
                exc.code,
                exc.retryable,
                exc.safe_message,
            )
            code_map = {
                "nvidia_nim_model_eol": (
                    "gateway_model_eol",
                    "Selected model has reached end of life and is no longer available",
                ),
                "nvidia_nim_model_not_found": (
                    "gateway_model_not_found",
                    "Selected model was not found",
                ),
                "nvidia_nim_rate_limited": (
                    "gateway_rate_limited",
                    "Model provider rate limit was exceeded",
                ),
                "nvidia_nim_auth_failure": (
                    "gateway_auth_failure",
                    "Model provider authentication failed",
                ),
                "model_endpoint_rejected": (
                    "gateway_endpoint_rejected",
                    exc.safe_message or "Model endpoint was rejected",
                ),
                "model_unauthorized": (
                    "gateway_auth_failure",
                    "Model provider authentication failed",
                ),
                "model_forbidden": (
                    "gateway_auth_failure",
                    "Model provider access forbidden",
                ),
                "model_rate_limited": (
                    "gateway_rate_limited",
                    "Model provider rate limit was exceeded",
                ),
                "model_transport_failed": (
                    "gateway_transport_failure",
                    exc.safe_message or "Model transport request failed",
                ),
                "model_upstream_failed": (
                    "gateway_upstream_failure",
                    exc.safe_message or "Model upstream service failed",
                ),
            }
            error_code, error_message = code_map.get(
                exc.code,
                ("gateway_upstream_failure", "Model gateway upstream request failed"),
            )
            return _error(
                502,
                error_code,
                error_message,
            )
        return JSONResponse(
            status_code=200,
            content=dict(response),
        )

    return app


def _error(
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )
