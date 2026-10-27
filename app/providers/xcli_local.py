"""Operator-registered native xcli transport, separate from public HTTPS routing.

Only the API and model gateway operator may register the same loopback endpoint
with CONTRIBOS_XCLI_LOCAL_ENDPOINT. Container/host bridging remains unsupported.
No sandbox or Publisher credentials are consumed by this transport.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.providers.contracts import ProviderRunError


def local_endpoint(value: str) -> str:
    """Accept only canonical literal loopback addresses and an explicit port."""
    try:
        url = urlsplit(value)
        address = ipaddress.ip_address(url.hostname or "")
        if (url.scheme != "http" or not address.is_loopback or not url.port
                or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path.rstrip("/") != "/v1"
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)
                or "%" in value or "\\" in value):
            raise ValueError()
        authority = f"[{address}]:{url.port}" if address.version == 6 else f"{address}:{url.port}"
        return f"http://{authority}/v1"
    except ValueError:
        raise ValueError("xcli requires a loopback IP, explicit port and /v1 path") from None


def registered_endpoint(value: str) -> str:
    endpoint = local_endpoint(value)
    registered = os.environ.get("CONTRIBOS_XCLI_LOCAL_ENDPOINT", "")
    if not registered or endpoint != local_endpoint(registered):
        raise ValueError("xcli endpoint must first be registered by the native gateway operator")
    return endpoint


def native_runtime() -> bool:
    return not (Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
                or os.environ.get("container"))


class XcliLocalClient:
    """Single-attempt bounded transport with no proxy or redirect inheritance."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    async def request(self, base_url: str, suffix: str, *, credential: str,
                      payload: dict | None = None, timeout: int = 180) -> dict:
        try:
            endpoint = registered_endpoint(base_url)
        except ValueError:
            raise ProviderRunError("xcli_registration_required", "xcli 本机实例未注册或地址已变化", retryable=False) from None
        if not native_runtime():
            raise ProviderRunError("xcli_container_bridge_unsupported", "容器到宿主的 xcli Bridge 尚未实现", retryable=False)
        if suffix not in {"/models", "/chat/completions"} or not credential:
            raise ProviderRunError("xcli_request_invalid", "xcli 请求或独立应用 Token 无效", retryable=False)
        encoded = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        if encoded is not None and len(encoded) > 4 * 1024 * 1024:
            raise ProviderRunError("xcli_request_too_large", "xcli 请求超过大小限制", retryable=False)
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout,
                                         transport=self._transport) as client:
                async with client.stream("POST" if encoded is not None else "GET", endpoint + suffix,
                                         headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
                                         content=encoded) as response:
                    if not 200 <= response.status_code < 300:
                        raise ProviderRunError("xcli_http_failed", f"xcli 请求失败（HTTP {response.status_code}）", retryable=False)
                    limit = (4 if suffix == "/models" else 32) * 1024 * 1024
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > limit:
                            raise ValueError()
                    value = json.loads(content)
                    if not isinstance(value, dict):
                        raise ValueError()
                    return value
        except (httpx.TransportError, ValueError, UnicodeError):
            raise ProviderRunError("xcli_transport_failed", "xcli 服务不可达或响应无效；请求未自动重试", retryable=False) from None


def require_model(catalog: dict, alias: str, request: dict) -> None:
    models = catalog.get("data")
    if catalog.get("object") != "list" or not isinstance(models, list):
        raise ProviderRunError("xcli_catalog_invalid", "无法验证 xcli 模型目录", retryable=False)
    selected = [model for model in models if isinstance(model, dict) and model.get("id") == alias]
    metadata = selected[0].get("xcli") if len(selected) == 1 else None
    if not isinstance(metadata, dict):
        raise ProviderRunError("xcli_model_missing", "xcli 未提供冻结的模型别名", retryable=False)
    required = {"text.generate"}
    if request.get("tools"):
        required.add("tool_calls")
    response_format = request.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in {"json_schema", "json_object"}:
        required.add(response_format["type"])
    caps = metadata.get("capabilities")
    if (not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps)
            or not required.issubset(caps)
            or (len(required) > 1 and metadata.get("validated") is not True)
            or request.get("stream") is True):
        raise ProviderRunError("xcli_capability_missing", "xcli 模型缺少经验证的所需能力", retryable=False)
