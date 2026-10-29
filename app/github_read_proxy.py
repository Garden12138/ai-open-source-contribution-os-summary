"""Publisher-owned GitHub credential, exposed only as bounded public GETs."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.provenance import canonical_json
from app.providers.model_secrets import management_auth, verify_management
from app.publisher_accounts import require_account
from app.workbench import WorkbenchError

MAX_BODY = 2 * 1024 * 1024
HEADERS = {"etag", "location", "retry-after", "x-ratelimit-remaining", "x-ratelimit-reset"}
REPOSITORY = re.compile(r"^/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(.*)$")


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str = Field(pattern=r"^github-public-read-v1$")
    path: str = Field(min_length=1, max_length=1500)
    params: list[list[str]] = Field(default_factory=list, max_length=5)
    etag: str | None = Field(default=None, max_length=300)

    def target(self) -> tuple[str | None, dict[str, str]]:
        """Exact allowlist; no URL, method, headers or write endpoint input."""
        decoded = unquote(self.path)
        if (any(c in decoded for c in "\\?#%") or any(ord(c) < 33 for c in decoded)
                or any(p in {".", ".."} for p in decoded.split("/"))):
            raise ValueError("read path rejected")
        params: dict[str, str] = {}
        for pair in self.params:
            if len(pair) != 2 or pair[0] in params or len(pair[1]) > 1200:
                raise ValueError("read parameters rejected")
            params[pair[0]] = pair[1]
        if self.etag and any(ord(c) < 32 or ord(c) > 126 for c in self.etag):
            raise ValueError("read etag rejected")
        if decoded == "/search/issues":
            if not set(params) <= {"q", "sort", "order", "per_page", "page"} or not params.get("q"):
                raise ValueError("read search rejected")
            for key, maximum in (("per_page", 100), ("page", 10)):
                if key in params and not (params[key].isdigit() and 1 <= int(params[key]) <= maximum):
                    raise ValueError("read pagination rejected")
            if params.get("sort", "updated") != "updated" or params.get("order", "desc") not in {"asc", "desc"}:
                raise ValueError("read sorting rejected")
            params["q"] += " is:public"
            return None, params
        match = REPOSITORY.fullmatch(decoded)
        if not match or params:
            raise ValueError("read endpoint rejected")
        owner, name, suffix = match.groups()
        if suffix not in {"", "/community/profile"} and not (
            re.fullmatch(r"/commits/[A-Za-z0-9_./+~-]{1,255}", suffix)
            or re.fullmatch(r"/tarball/[a-f0-9]{40}(?:[a-f0-9]{24})?", suffix)
        ):
            raise ValueError("read endpoint rejected")
        return f"/repos/{owner}/{name}", params


class PublicGitHubRelay:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport
        self.semaphore = asyncio.Semaphore(4)

    async def read(self, token: str, request: ReadRequest) -> dict[str, Any]:
        repository, params = request.target()
        async with asyncio.timeout(40), self.semaphore:
            async with httpx.AsyncClient(base_url="https://api.github.com", timeout=15,
                    trust_env=False, follow_redirects=False, transport=self.transport,
                    headers={"Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json", "User-Agent": "contribos-public-read/1",
                        "X-GitHub-Api-Version": "2022-11-28"}) as client:
                if repository:
                    status, headers, raw = await self._get(client, repository, {}, token)
                    if status != 200:
                        return self._envelope(status, headers, raw)
                    metadata = json.loads(raw)
                    if not isinstance(metadata, dict) or metadata.get("private") is not False:
                        raise ValueError("public repository required")
                    if request.path == repository:
                        return self._envelope(status, headers, raw)
                status, headers, raw = await self._get(client, request.path, params, token, request.etag)
                return self._envelope(status, headers, raw)

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, str],
                   token: str, etag: str | None = None) -> tuple[int, dict[str, str], bytes]:
        async with client.stream("GET", path, params=params,
                headers={"If-None-Match": etag} if etag else {}) as response:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BODY:
                    raise ValueError("read response limit")
            headers = {k: v for k, v in response.headers.items() if k in HEADERS}
            if token.encode() in raw or any(token in v for v in headers.values()):
                raise ValueError("read response rejected")
            return response.status_code, headers, bytes(raw)

    @staticmethod
    def _envelope(status: int, headers: dict[str, str], raw: bytes) -> dict[str, Any]:
        return {"status": status, "headers": headers, "body": base64.b64encode(raw).decode()}


def register_public_read(app: Any, settings: Settings, database: Any, vault: Any,
                         relay: PublicGitHubRelay) -> None:
    from fastapi.responses import JSONResponse

    @app.post("/internal/github-read")
    async def read(request: Request) -> Any:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 6000:
                return JSONResponse(status_code=413, content={"detail": "只读请求过大"})
        try:
            if not settings.github_read_proxy_key:
                raise ValueError()
            verify_management(settings.github_read_proxy_key, bytes(raw), request.headers.get("X-GitHub-Read", ""))
        except ValueError:
            return JSONResponse(status_code=403, content={"detail": "只读接口授权无效"})
        try:
            payload = ReadRequest.model_validate_json(raw)
            payload.target()  # Validate before reading any credential.
            with database.session() as session:
                account = require_account(session)
                reference = account.payload["credential_ref"]
                account_hash = account.event_hash
            result = await relay.read(vault.read(reference), payload)
            with database.session() as session:
                require_account(session, account_hash)  # Account changes invalidate in-flight reads.
            return result
        except WorkbenchError:
            return JSONResponse(status_code=409, content={"detail": "GitHub 连接尚未就绪或已变化"})
        except Exception:
            return JSONResponse(status_code=502, content={"detail": "GitHub 只读请求未完成"})


class PublisherReadTransport(httpx.AsyncBaseTransport):
    """The Worker gets only a read capability; never a GitHub bearer token."""
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=45, trust_env=False, follow_redirects=False, transport=transport)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.method != "GET" or request.url.scheme != "https" or request.url.host != "api.github.com"
                or request.url.port not in {None, 443} or "authorization" in request.headers):
            raise httpx.TransportError("GitHub read capability rejected", request=request)
        payload = ReadRequest(version="github-public-read-v1", path=request.url.raw_path.decode().split("?", 1)[0],
            params=[list(pair) for pair in request.url.params.multi_items()], etag=request.headers.get("if-none-match"))
        payload.target()
        body = canonical_json(payload.model_dump()).encode()
        try:
            async with self.client.stream("POST", self.settings.github_read_proxy_url.rstrip("/") + "/internal/github-read",
                    content=body, headers={"Content-Type": "application/json",
                        "X-GitHub-Read": management_auth(self.settings.github_read_proxy_key, body)}) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BODY * 2:
                        raise ValueError()
                if response.status_code != 200:
                    raise ValueError()
                value = json.loads(raw)
            if (set(value) != {"status", "headers", "body"} or type(value["status"]) is not int
                    or not 200 <= value["status"] <= 599 or not isinstance(value["headers"], dict)
                    or not set(value["headers"]) <= HEADERS):
                raise ValueError()
            decoded = base64.b64decode(value["body"], validate=True)
            if len(decoded) > MAX_BODY:
                raise ValueError()
            return httpx.Response(value["status"], headers=value["headers"], content=decoded, request=request)
        except Exception:
            raise httpx.TransportError("GitHub read service unavailable", request=request) from None

    async def aclose(self) -> None:
        await self.client.aclose()
