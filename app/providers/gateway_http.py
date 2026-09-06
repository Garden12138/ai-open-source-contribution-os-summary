from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.providers.contracts import ProviderRunError
from app.providers.gateway import (
    GatewayAuthorizationError,
    GatewayAuthorizationExpiredError,
    GatewayAuthorizationReplayError,
    GatewayAuthorizationScopeError,
    InternalModelGateway,
)


logger = logging.getLogger(__name__)


def create_model_gateway_app(gateway: InternalModelGateway) -> FastAPI:
    """Create the internal-only HTTP boundary consumed by Codex CLI."""

    app = FastAPI(
        title="ContribOS Internal Model Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

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
                "Model gateway upstream request failed: code=%s retryable=%s",
                exc.code,
                exc.retryable,
            )
            return _error(
                502,
                "gateway_upstream_failure",
                "Model gateway upstream request failed",
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
