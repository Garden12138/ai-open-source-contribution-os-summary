"""Same-origin Studio settings; only sealed credentials cross this API."""

from __future__ import annotations
import json
from typing import Any
import httpx
from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.model_settings import (
    ModelSettingsService,
    ModelSettingsError,
    ConnectionInput,
    ProfileInput,
    DefaultsInput,
    MINIMAX_MODEL,
    MINIMAX_PROVIDER,
    model_presets,
)
from app.jobs import JobService
from app.providers.model_secrets import management_auth
from app.provenance import canonical_json
from app.schemas import JobResponse
from app.models import ModelConfigVersion


class ModelVersionResponse(BaseModel):
    id: str
    scope: str
    sequence: int
    record_hash: str
    payload: dict[str, Any]


class ModelSettingsResponse(BaseModel):
    version: ModelVersionResponse | None
    effective: dict[str, str | None]
    connections: list[ModelVersionResponse]
    profiles: list[ModelVersionResponse]
    presets: list[dict[str, Any]]
    credential_management_available: bool


class CredentialGrantResponse(BaseModel):
    grant_id: str
    connection_id: str
    expires_at: int
    key_id: str
    public_key: dict[str, Any]


class CredentialSaveResponse(BaseModel):
    credential_ref: str
    configured: bool


class SaveConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_id: str = Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")
    expected_hash: str | None = None
    connection: ConnectionInput


class SaveProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_id: str = Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")
    expected_hash: str | None = None
    profile: ProfileInput


class SaveDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_hash: str | None = None
    settings: DefaultsInput


class CredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str = Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")
    envelope: dict[str, str] | None = None


class TestConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str
    operation: str = Field(default="test", pattern=r"^(test|models)$")


def serialize(row: ModelConfigVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "scope": row.scope,
        "sequence": row.sequence,
        "record_hash": row.record_hash,
        "payload": row.payload,
    }


async def gateway_manage(settings, operation: str, payload: dict) -> dict:
    key = settings.model_gateway_management_key
    if key is None:
        raise HTTPException(
            409, "请先配置独立模型网关管理连接（MODEL_GATEWAY_MANAGEMENT_KEY）"
        )
    body = canonical_json({"operation": operation, **payload}).encode()
    try:
        async with httpx.AsyncClient(
            timeout=10, trust_env=False, follow_redirects=False
        ) as client:
            response = await client.post(
                settings.model_gateway_base_url.removesuffix("/v1")
                + "/internal/settings",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Gateway-Management": management_auth(key, body),
                },
            )
        if response.status_code != 200:
            raise ValueError()
        return response.json()
    except (httpx.TransportError, ValueError):
        raise HTTPException(502, "模型网关未能完成配置，请检查网关状态后重试") from None


def register_model_settings_routes(app, get_session, require_mutation_access) -> None:
    @app.exception_handler(ModelSettingsError)
    async def configuration_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"error": {"message": str(exc)}})

    @app.get("/api/v1/model-settings", response_model=ModelSettingsResponse)
    def settings(request: Request, session: Session = Depends(get_session)):
        service = ModelSettingsService(session)
        row = service.latest("defaults")
        return {
            "version": serialize(row) if row else None,
            "effective": service.defaults(),
            "connections": [serialize(r) for r in service.list_kind("connection")],
            "profiles": [serialize(r) for r in service.list_kind("profile")],
            "presets": model_presets(),
            "credential_management_available": bool(
                request.app.state.settings.model_gateway_management_key
            ),
        }

    @app.get("/api/v1/model-connections", response_model=list[ModelVersionResponse])
    def connections(session: Session = Depends(get_session)):
        return [
            serialize(r) for r in ModelSettingsService(session).list_kind("connection")
        ]

    @app.get("/api/v1/model-profiles", response_model=list[ModelVersionResponse])
    def profiles(session: Session = Depends(get_session)):
        return [
            serialize(r) for r in ModelSettingsService(session).list_kind("profile")
        ]

    @app.post(
        "/api/v1/model-connections",
        dependencies=[Depends(require_mutation_access)],
        response_model=ModelVersionResponse,
    )
    async def save_connection(
        payload: SaveConnection,
        request: Request,
        session: Session = Depends(get_session),
    ):
        if payload.connection.credential_ref:
            result = await app.state.gateway_manage(
                request.app.state.settings,
                "has",
                {"credential_ref": payload.connection.credential_ref},
            )
            if not result.get("configured"):
                raise HTTPException(409, "请先保存此连接的 API Key")
        return save_version(
            session,
            "connection:" + payload.scope_id,
            payload.connection.model_dump(),
            payload.expected_hash,
        )

    @app.post(
        "/api/v1/model-profiles",
        dependencies=[Depends(require_mutation_access)],
        response_model=ModelVersionResponse,
    )
    def save_profile(payload: SaveProfile, session: Session = Depends(get_session)):
        connection = ModelSettingsService(session).get(
            payload.profile.connection_id, "connection"
        )
        provider = connection.payload["provider"]
        if provider in {"nvidia_nim", MINIMAX_PROVIDER} and (
            payload.profile.structured_output != "tools"
        ):
            raise HTTPException(422, "此模型适配器使用工具调用结构化响应")
        if (
            connection.payload["provider"] == "nvidia_nim"
            and payload.profile.reasoning_effort == "medium"
        ):
            raise HTTPException(422, "NVIDIA 当前适配器仅支持低或高推理强度")
        if provider == MINIMAX_PROVIDER:
            if payload.profile.model != MINIMAX_MODEL:
                raise HTTPException(422, "MiniMax 国内预置当前仅支持 MiniMax-M3")
            if payload.profile.reasoning_effort is not None:
                raise HTTPException(
                    422,
                    "MiniMax-M3 使用 adaptive thinking，不接受推理强度参数",
                )
        return save_version(
            session,
            "profile:" + payload.scope_id,
            payload.profile.model_dump(),
            payload.expected_hash,
        )

    @app.put(
        "/api/v1/model-settings",
        dependencies=[Depends(require_mutation_access)],
        response_model=ModelVersionResponse,
    )
    def save_settings(payload: SaveDefaults, session: Session = Depends(get_session)):
        service = ModelSettingsService(session)
        for identifier in payload.settings.model_dump().values():
            if identifier:
                service.get(identifier, "profile")
        return save_version(
            session, "defaults", payload.settings.model_dump(), payload.expected_hash
        )

    @app.post(
        "/api/v1/model-connections/credential-grants",
        response_model=CredentialGrantResponse,
        dependencies=[Depends(require_mutation_access)],
    )
    async def credential_grant(payload: CredentialRequest, request: Request):
        return await app.state.gateway_manage(
            request.app.state.settings,
            "grant",
            {"connection_id": payload.connection_id},
        )

    @app.post(
        "/api/v1/model-connections/credentials",
        response_model=CredentialSaveResponse,
        dependencies=[Depends(require_mutation_access)],
    )
    async def credential_save(payload: CredentialRequest, request: Request):
        if payload.envelope is None or len(json.dumps(payload.envelope)) > 16000:
            raise HTTPException(422, "加密密钥信封无效")
        return await app.state.gateway_manage(
            request.app.state.settings, "save", payload.model_dump()
        )

    @app.post(
        "/api/v1/model-connections/test-jobs",
        response_model=JobResponse,
        status_code=202,
        dependencies=[Depends(require_mutation_access)],
    )
    def test_connection(
        payload: TestConnection,
        session: Session = Depends(get_session),
        key: str = Header(alias="Idempotency-Key", min_length=1, max_length=80),
    ):
        ModelSettingsService(session).get(payload.profile_id, "profile")
        job, _ = JobService(session).enqueue(
            kind="model_connection_test",
            idempotency_key=key,
            payload={
                "model_profile_id": payload.profile_id,
                "operation": payload.operation,
            },
            timeout_seconds=330,
            max_attempts=1,
        )
        return JobResponse.model_validate(job)

    app.state.gateway_manage = gateway_manage


def save_version(session, scope, payload, expected_hash):
    try:
        return serialize(
            ModelSettingsService(session).append(scope, payload, expected_hash)
        )
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "设置已更新，请重新载入后再保存") from None
