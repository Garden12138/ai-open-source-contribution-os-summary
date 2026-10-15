"""Same-origin settings proxy; only browser-sealed credentials cross the API."""
from __future__ import annotations

from typing import Any, Literal
import httpx
from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.provenance import canonical_json
from app.providers.model_secrets import management_auth


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    key_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    iv: str = Field(min_length=16, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    ciphertext: str = Field(min_length=20, max_length=6000, pattern=r"^[A-Za-z0-9_-]+$")
    wrapped_key: str = Field(min_length=256, max_length=1024, pattern=r"^[A-Za-z0-9_-]+$")


class AccountChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CredentialGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,80}$")


class CredentialSave(AccountChange, CredentialGrant):
    envelope: Envelope


class PublisherStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: bool
    connected: bool = False
    state: Literal["disconnected", "pending", "connected", "verification_failed", "unavailable"]
    account_hash: str | None = None
    login: str | None = None
    scopes: list[str] = Field(default_factory=list)
    checked_at: str | None = None
    job_id: str | None = None
    job_state: str | None = None
    error: str | None = None


class AccountJobResponse(BaseModel):
    job_id: str


class DisconnectResponse(BaseModel):
    disconnected: bool


async def publisher_manage(settings: Settings, operation: str, payload: dict[str, Any]) -> dict:
    if not settings.publisher_management_key:
        raise HTTPException(409, "GitHub 发布服务尚未启用，请检查本地部署")
    body = canonical_json({"operation": operation, **payload}).encode()
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = await client.post(settings.publisher_management_url.rstrip("/") + "/internal/settings",
                content=body, headers={"Content-Type": "application/json",
                    "X-Publisher-Management": management_auth(settings.publisher_management_key, body)})
        value = response.json()
        if response.status_code == 409:
            # Do not reflect arbitrary Publisher response text through the API.
            raise HTTPException(409, "发布账号设置已变化或有未完成任务，请刷新设置并检查发布进度后重试")
        if response.status_code == 422:
            raise HTTPException(422, "发布凭证无效，请使用带 public_repo 权限的 Classic Token，刷新后重新连接")
        if response.status_code != 200 or len(response.content) > 24000 or not isinstance(value, dict):
            raise ValueError()
        return value
    except (httpx.TransportError, ValueError):
        raise HTTPException(502, "GitHub 发布服务无法连接，请稍后重试") from None


def register_publisher_settings_routes(app, require_mutation_access) -> None:
    from app.model_settings_api import CredentialGrantResponse

    @app.get("/api/v1/publisher-settings", response_model=PublisherStatus)
    async def status(request: Request):
        try:
            value = await app.state.publisher_manage(request.app.state.settings, "status", {})
            return PublisherStatus.model_validate(value)
        except (HTTPException, ValueError):
            return PublisherStatus(available=False, state="unavailable")

    @app.post("/api/v1/publisher-settings/credential-grants", response_model=CredentialGrantResponse,
              dependencies=[Depends(require_mutation_access)])
    async def grant(payload: CredentialGrant, request: Request):
        return await app.state.publisher_manage(request.app.state.settings, "grant", payload.model_dump())

    @app.post("/api/v1/publisher-settings/credentials", response_model=AccountJobResponse,
              dependencies=[Depends(require_mutation_access)])
    async def save(payload: CredentialSave, request: Request,
                   key: str = Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9-]{1,80}$")):
        return await app.state.publisher_manage(request.app.state.settings, "save", {**payload.model_dump(), "key": key})

    @app.post("/api/v1/publisher-settings/test", response_model=AccountJobResponse,
              dependencies=[Depends(require_mutation_access)])
    async def test(payload: AccountChange, request: Request,
                   key: str = Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9-]{1,80}$")):
        return await app.state.publisher_manage(request.app.state.settings, "test", {**payload.model_dump(), "key": key})

    @app.post("/api/v1/publisher-settings/disconnect", response_model=DisconnectResponse,
              dependencies=[Depends(require_mutation_access)])
    async def disconnect(payload: AccountChange, request: Request,
                         key: str = Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9-]{1,80}$")):
        return await app.state.publisher_manage(request.app.state.settings, "disconnect", {**payload.model_dump(), "key": key})

    app.state.publisher_manage = publisher_manage
