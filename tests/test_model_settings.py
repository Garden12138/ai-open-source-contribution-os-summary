from __future__ import annotations
import asyncio
from dataclasses import replace
import json
import os
import socket

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.jobs import JobService
from app.model_settings import (
    ModelSettingsService,
    ConnectionInput,
    ProfileInput,
    ModelSettingsError,
    MINIMAX_BASE_URL,
    MINIMAX_MODEL,
    model_presets,
    job_profile,
)
from app.models import ModelConfigVersion, JobModelBinding
from app.provenance import canonical_json, content_hash
from app.providers.contracts import ProviderStage, ProviderRunError
from app.providers.codex_cli import CodexExecInvocation
from app.providers.gateway import (
    GatewayTaskAuthorizer,
    GatewayTaskTokenCodec,
    InternalModelGateway,
)
from app.providers.gateway_http import create_model_gateway_app
from app.providers.model_secrets import GatewaySecretStore, b64, management_auth
from app.providers.studio import (
    StudioGateway,
    PublicHTTPSClient,
    ProfileUpstream,
    runner_for_profile,
)

CANARY = "ghp_StudioSecretCanary1234567890"
KEY = bytes.fromhex("cd" * 32)


def configure(
    session,
    *,
    model="model-one",
    provider="openai_compatible",
    credential_ref=None,
):
    service = ModelSettingsService(session)
    connection = service.append(
        "connection:" + model,
        ConnectionInput(
            name="Test",
            provider=provider,
            base_url={
                "nvidia_nim": "https://integrate.api.nvidia.com/v1",
                "minimax": MINIMAX_BASE_URL,
            }.get(provider, "https://models.example.com/v1"),
            credential_ref=(
                credential_ref
                or ("cred-" + "1" * 32 if provider == "minimax" else "env-nvidia")
            ),
        ).model_dump(),
        None,
    )
    profile = service.append(
        "profile:" + model,
        ProfileInput(name=model, connection_id=connection.id, model=model).model_dump(),
        None,
    )
    prior = service.latest("defaults")
    service.append(
        "defaults", {"default": profile.id}, prior.record_hash if prior else None
    )
    return profile


def envelope(store, grant, secret=CANARY):
    aes = AESGCM.generate_key(bit_length=256)
    iv = os.urandom(12)
    aad = canonical_json(
        {
            "connection_id": grant["connection_id"],
            "grant_id": grant["grant_id"],
            "key_id": grant["key_id"],
        }
    ).encode()
    wrapped = store.private_key.public_key().encrypt(
        aes,
        padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
        ),
    )
    return {
        "grant_id": grant["grant_id"],
        "key_id": grant["key_id"],
        "wrapped_key": b64(wrapped),
        "iv": b64(iv),
        "ciphertext": b64(AESGCM(aes).encrypt(iv, secret.encode(), aad)),
    }


def test_settings_versions_cas_and_frozen_jobs(tmp_path):
    db = Database(f"sqlite:///{tmp_path/'models.db'}")
    db.create_schema()
    with db.session() as session:
        first = configure(session)
        job, _ = JobService(session).enqueue(
            kind="model_connection_test",
            idempotency_key="test-model",
            payload={"model_profile_id": first.id, "operation": "test"},
        )
        second = configure(session, model="model-two")
        assert ModelSettingsService(session).defaults()["analysis"] == second.id
        assert job_profile(session, job.id)["model"] == "model-one"
        assert (
            JobService(session)
            .enqueue(
                kind="model_connection_test",
                idempotency_key="test-model",
                payload={"model_profile_id": first.id, "operation": "test"},
            )[0]
            .id
            == job.id
        )
        with pytest.raises(ModelSettingsError):
            ModelSettingsService(session).append(
                "defaults", {"default": first.id}, None
            )
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE model_config_versions SET sequence=999"))
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(text("DELETE FROM job_model_bindings"))
        session.rollback()
    db.close()


def test_gateway_envelope_recovery_replay_and_private_storage(tmp_path):
    store = GatewaySecretStore(str(tmp_path / "secrets"))
    grant = store.grant("connection-one")
    sealed = envelope(store, grant)
    result = store.save("connection-one", sealed)
    assert store.read(result["credential_ref"]) == CANARY
    assert store.save("connection-one", sealed) == result
    reopened = GatewaySecretStore(str(tmp_path / "secrets"))
    assert reopened.read(result["credential_ref"]) == CANARY
    with pytest.raises(ValueError):
        reopened.save("connection-two", sealed)
    with pytest.raises(ValueError):
        reopened.save("connection-one", {**sealed, "iv": b64(os.urandom(12))})
    for path in (tmp_path / "secrets").iterdir():
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes()
            assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_connection_probe_job_lifecycle(tmp_path, monkeypatch, outcome):
    from types import SimpleNamespace
    from app.model_test_worker import ModelTestWorker

    database = Database(f"sqlite:///{tmp_path/'probe.db'}")
    database.create_schema()
    with database.session() as session:
        profile = configure(session)
        job, _ = JobService(session).enqueue(
            kind="model_connection_test",
            idempotency_key="probe-lifecycle",
            payload={"model_profile_id": profile.id, "operation": "test"},
        )

    class Runner:
        async def complete(self, invocation):
            assert invocation.model == "model-one"
            if outcome == "failure":
                raise RuntimeError(CANARY)
            if outcome == "cancelled":
                with database.session() as session:
                    JobService(session).request_cancel(job.id)
            return SimpleNamespace(content='{"ok": true}')

    monkeypatch.setattr(
        "app.model_test_worker.runner_for_profile", lambda *args: Runner()
    )
    result = asyncio.run(
        ModelTestWorker(database, Settings(), worker_id="probe").run_once()
    )
    assert (
        result.state
        == {"success": "succeeded", "failure": "failed", "cancelled": "cancelled"}[
            outcome
        ]
    )
    assert CANARY not in str(result.error_message)
    if outcome == "success":
        assert result.result_data["structured_output"] is True
    database.close()


def test_model_bound_jobs_are_not_leased_by_fake_coordinator(tmp_path):
    database = Database(f"sqlite:///{tmp_path/'leasing.db'}")
    database.create_schema()
    with database.session() as session:
        profile = configure(session)
        jobs = JobService(session)
        real, _ = jobs.enqueue(
            kind="provider_analysis",
            idempotency_key="real",
            payload={
                "model_profile_id": profile.id,
                "expected_provider": {"provider": "openai_compatible"},
            },
        )
        fake, _ = jobs.enqueue(
            kind="provider_analysis",
            idempotency_key="fake",
            payload={"expected_provider": {"provider": "fake"}},
        )
        assert (
            jobs.lease_next(
                worker_id="coordinator",
                kinds=("provider_analysis",),
                exclude_model_bound=True,
            ).id
            == fake.id
        )
        assert (
            jobs.lease_next(
                worker_id="provider",
                kinds=("provider_analysis",),
                exclude_fake_provider=True,
            ).id
            == real.id
        )
    database.close()


def test_new_defaults_do_not_rebind_legacy_task_jobs(tmp_path):
    from app.model_settings import task_identity
    from app.contribution_workflow import model_identity

    database = Database(f"sqlite:///{tmp_path/'legacy.db'}")
    database.create_schema()
    settings = Settings(implementation_provider="nvidia_nim")
    with database.session() as session:
        configure(session)
        job, _ = JobService(session).enqueue(
            kind="coding_turn",
            idempotency_key="legacy-turn",
            payload={"task_id": "legacy-task"},
        )
        assert job_profile(session, job.id) is None
        assert task_identity(
            session, settings, "legacy-task", "implementation"
        ) == model_identity(settings.implementation_model)
    database.close()


def test_api_settings_auth_and_sealed_key_never_persisted(tmp_path, caplog):
    store = GatewaySecretStore(str(tmp_path / "private"))
    settings = Settings(
        database_url=f"sqlite:///{tmp_path/'api.db'}", model_gateway_management_key=KEY
    )
    app = create_app(settings)

    async def manage(_settings, operation, payload):
        if operation == "grant":
            return store.grant(payload["connection_id"])
        if operation == "save":
            return store.save(payload["connection_id"], payload["envelope"])
        return {"configured": store.has(payload["credential_ref"])}

    app.state.gateway_manage = manage
    with TestClient(app) as client:
        csrf = client.get("/api/v1/meta").json()["csrf_token"]
        bad = client.post(
            "/api/v1/model-connections/credential-grants",
            json={"connection_id": "connection-one"},
            headers={"Origin": "https://evil.example"},
        )
        assert bad.status_code == 403
        assert (
            client.post(
                "/api/v1/model-connections/credential-grants",
                json={"connection_id": "connection-one"},
                headers={"Origin": "http://testserver"},
            ).status_code
            == 403
        )
        grant = client.post(
            "/api/v1/model-connections/credential-grants",
            json={"connection_id": "connection-one"},
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
        ).json()
        result = client.post(
            "/api/v1/model-connections/credentials",
            json={
                "connection_id": "connection-one",
                "envelope": envelope(store, grant),
            },
        )
        assert result.status_code == 200, result.text
        connection = client.post(
            "/api/v1/model-connections",
            json={
                "scope_id": "one",
                "connection": {
                    "name": "My API",
                    "base_url": "https://models.example.com/v1",
                    "credential_ref": result.json()["credential_ref"],
                },
            },
        )
        assert connection.status_code == 200, connection.text
        profile = client.post(
            "/api/v1/model-profiles",
            json={
                "scope_id": "one",
                "profile": {
                    "name": "Model",
                    "model": "example/model",
                    "connection_id": connection.json()["id"],
                },
            },
        )
        assert profile.status_code == 200, profile.text
        defaults = client.put(
            "/api/v1/model-settings",
            json={"settings": {"default": profile.json()["id"]}},
        )
        assert defaults.status_code == 200, defaults.text
        assert (
            client.get("/api/v1/meta").json()["analysis_provider"]
            == "openai_compatible"
        )
        preset = client.get("/api/v1/model-settings").json()["presets"][0]
        assert preset["provider"] == "minimax"
        assert preset["base_url"] == MINIMAX_BASE_URL
        assert preset["model"] == MINIMAX_MODEL
        assert (
            client.get("/api/v1/opportunities/daily").json()["analysis"]["mode"]
            == "provider_ready"
        )
        job = client.post(
            "/api/v1/model-connections/test-jobs",
            json={"profile_id": profile.json()["id"]},
            headers={"Idempotency-Key": "probe"},
        )
        assert job.status_code == 202, job.text
        assert (
            client.put(
                "/api/v1/model-settings", json={"settings": {"default": None}}
            ).status_code
            == 409
        )
        assert CANARY not in client.get("/api/v1/model-settings").text
    import sqlite3

    with sqlite3.connect(tmp_path / "api.db") as db:
        dump = "\n".join(db.iterdump())
        assert CANARY not in dump
        assert "wrapped_key" not in dump
    assert CANARY not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "http://models.example.com/v1",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/v1",
        "https://user:password@models.example.com/v1",
        "https://models.example.com/v1?api_key=hello",
        "https://localhost/v1",
        "https://models.example.com/%2e%2e/internal",
        "https://models.example.com/\nv1",
    ],
)
def test_model_endpoints_reject_unsafe_urls(url):
    with pytest.raises(ValueError):
        ConnectionInput(name="unsafe", base_url=url)


def test_minimax_preset_is_credential_free_and_uses_fixed_domestic_endpoint():
    preset = model_presets()[0]
    assert preset == {
        "id": "minimax-m3-cn",
        "name": "MiniMax M3（国内官方）",
        "provider": "minimax",
        "base_url": MINIMAX_BASE_URL,
        "model": MINIMAX_MODEL,
        "profile": {
            "max_tokens": 16_384,
            "timeout_seconds": 300,
            "temperature": None,
            "reasoning_effort": None,
            "structured_output": "tools",
        },
    }
    with pytest.raises(ValueError, match="MiniMax 国内连接必须使用官方地址"):
        ConnectionInput(
            name="MiniMax",
            provider="minimax",
            base_url="https://api.minimaxi.com/v1",
            credential_ref="cred-" + "1" * 32,
        )
    with pytest.raises(ValueError, match="独立保存的 MiniMax API Key"):
        ConnectionInput(
            name="MiniMax",
            provider="minimax",
            base_url=MINIMAX_BASE_URL,
            credential_ref="env-nvidia",
        )


def test_minimax_profile_maps_official_m3_request_parameters(tmp_path):
    store = GatewaySecretStore(str(tmp_path / "private"))
    grant = store.grant("minimax-request")
    credential_ref = store.save(
        "minimax-request", envelope(store, grant)
    )["credential_ref"]
    db = Database(f"sqlite:///{tmp_path/'minimax.db'}")
    db.create_schema()
    with db.session() as session:
        row = configure(
            session,
            model=MINIMAX_MODEL,
            provider="minimax",
            credential_ref=credential_ref,
        )
        profile = ModelSettingsService(session).profile(row.id)
    captured = {}

    class Transport:
        async def request(self, base_url, suffix, **kwargs):
            captured.update(
                base_url=base_url,
                suffix=suffix,
                credential=kwargs["credential"],
                payload=kwargs["payload"],
            )
            return {"choices": []}

    payload = {
        "model": MINIMAX_MODEL,
        "messages": [{"role": "user", "content": "return result"}],
        "max_tokens": 1,
        "reasoning_effort": "high",
        "tools": [{"type": "function", "function": {"name": "result"}}],
    }
    result = asyncio.run(
        ProfileUpstream(profile, store, transport=Transport()).create_response(payload)
    )
    assert result == {"choices": []}
    assert captured["base_url"] == MINIMAX_BASE_URL
    assert captured["suffix"] == "/chat/completions"
    assert captured["credential"] == CANARY
    request = captured["payload"]
    assert request["model"] == MINIMAX_MODEL
    assert request["max_completion_tokens"] == profile["max_tokens"]
    assert request["thinking"] == {"type": "adaptive"}
    assert request["reasoning_split"] is True
    assert "max_tokens" not in request
    assert "reasoning_effort" not in request
    assert request["tools"] == payload["tools"]
    db.close()


def test_minimax_profile_contract_and_default_apply_to_all_stages(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path/'minimax-api.db'}")
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.database.session() as session:
            connection = ModelSettingsService(session).append(
                "connection:minimax-api",
                ConnectionInput(
                    name="MiniMax",
                    provider="minimax",
                    base_url=MINIMAX_BASE_URL,
                    credential_ref="cred-" + "2" * 32,
                ).model_dump(),
                None,
            )
        common = {
            "scope_id": "minimax-m3",
            "profile": {
                "name": "MiniMax M3",
                "connection_id": connection.id,
                "model": MINIMAX_MODEL,
                "structured_output": "tools",
            },
        }
        wrong_model = client.post(
            "/api/v1/model-profiles",
            json={**common, "profile": {**common["profile"], "model": "MiniMax-M2"}},
        )
        assert wrong_model.status_code == 422
        wrong_reasoning = client.post(
            "/api/v1/model-profiles",
            json={
                **common,
                "profile": {**common["profile"], "reasoning_effort": "high"},
            },
        )
        assert wrong_reasoning.status_code == 422
        profile = client.post("/api/v1/model-profiles", json=common)
        assert profile.status_code == 200, profile.text
        saved = client.put(
            "/api/v1/model-settings",
            json={"settings": {"default": profile.json()["id"]}},
        )
        assert saved.status_code == 200, saved.text
        effective = client.get("/api/v1/model-settings").json()["effective"]
        assert set(effective.values()) == {profile.json()["id"]}
        meta = client.get("/api/v1/meta").json()
        assert meta["analysis_provider"] == "minimax"
        assert meta["implementation_provider"] == "minimax"
        assert meta["review_provider"] == "minimax"


def test_ssrf_resolves_all_addresses_before_sending(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(ProviderRunError, match="公共 HTTPS"):
        asyncio.run(
            PublicHTTPSClient().request(
                "https://models.example.com/v1", "/models", credential=CANARY
            )
        )


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai_compatible", "model-one"),
        ("nvidia_nim", "model-one"),
        ("minimax", MINIMAX_MODEL),
    ],
)
def test_profile_gateway_binds_endpoint_and_structured_result(
    tmp_path, provider, model
):
    store = GatewaySecretStore(str(tmp_path / "private"), environment_key=CANARY)
    credential_ref = None
    if provider == "minimax":
        grant = store.grant("minimax-gateway")
        credential_ref = store.save(
            "minimax-gateway", envelope(store, grant)
        )["credential_ref"]
    db = Database(f"sqlite:///{tmp_path/'model.db'}")
    db.create_schema()
    with db.session() as session:
        row = configure(
            session,
            provider=provider,
            model=model,
            credential_ref=credential_ref,
        )
        profile = ModelSettingsService(session).profile(row.id)

    class Legacy:
        async def create_response(self, payload):
            raise AssertionError("Unexpected legacy call")

    class Transport:
        async def request(self, *args, **kwargs):
            assert kwargs["credential"] == CANARY
            return {
                "choices": [
                    {"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            }

    legacy = InternalModelGateway(
        authorizer=GatewayTaskAuthorizer(GatewayTaskTokenCodec(KEY)),
        upstream=Legacy(),
        provider_name="nvidia_nim",
    )
    gateway = StudioGateway(legacy, store, transport=Transport())
    gateway_app = create_model_gateway_app(
        gateway, secret_store=store, management_key=bytes.fromhex("ef" * 32)
    )
    if provider == "nvidia_nim":
        # The legacy NVIDIA transport is covered separately; this asserts profile routing.
        from unittest.mock import patch, AsyncMock

        context = patch(
            "app.providers.studio.NvidiaNimHostedTransport.send",
            new_callable=AsyncMock,
            return_value={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )
    else:
        from contextlib import nullcontext

        context = nullcontext()

    async def invoke():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app),
            base_url="http://contribos-model-gateway:8001",
        ) as client:
            runner = runner_for_profile(
                Settings(model_gateway_signing_key=KEY), profile, client=client
            )
            invocation = CodexExecInvocation(
                stage=ProviderStage.PLANNING,
                request_id="test",
                correlation_id="test",
                snapshot_id="snapshot",
                input_hash="a" * 64,
                prompt="Return ok",
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
                model=profile["model"],
            )
            result = await runner.complete(invocation)
            assert json.loads(result.content) == {"ok": True}
            credential = runner.broker.issue(invocation)
            bad = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + credential.token},
                json={
                    "model": profile["model"],
                    "_contribos_profile": {
                        **profile,
                        "connection": {
                            **profile["connection"],
                            "base_url": "https://other.example.com/v1",
                        },
                    },
                },
            )
            assert bad.status_code == 403
            assert (
                await client.post(
                    "/internal/settings",
                    content=b"{}",
                    headers={"X-Gateway-Management": credential.token},
                )
            ).status_code == 403

    with context:
        asyncio.run(invoke())


@pytest.mark.parametrize(
    ("provider", "model"),
    [("openai_compatible", "model-one"), ("minimax", MINIMAX_MODEL)],
)
def test_four_stages_use_task_profiles_and_global_changes_do_not_break_execution(
    tmp_path, provider, model
):
    from tests.test_workbench import automatic_fixture, populate_round, drive_round
    from app.models import ReviewRun
    from app.planner import PlanningService

    def configure_task(db, settings, task_id):
        with db.session() as session:
            configure(session, provider=provider, model=model)
            service = ModelSettingsService(session)
            service.task_profiles(task_id, bind=True)
            session.commit()

    db, settings, task_id, workers, code, review = automatic_fixture(
        tmp_path, configure_models=configure_task
    )
    with db.session() as session:
        service = ModelSettingsService(session)
        profile_id = service.task_profiles(task_id)["implementation"]
        identity = service.identity(profile_id)
        configure(session, model="changed-default")
        assert service.defaults()["implementation"] != profile_id
    workers[2].identity = identity
    workers[3].identity = identity
    populate_round(code, review)
    drive_round(db, settings, task_id, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task_id, "awaiting_acceptance")
        reviewed = session.scalar(select(ReviewRun))
        assert reviewed.reviewer_kind == provider
        bindings = list(session.scalars(select(JobModelBinding)))
        assert len(bindings) >= 4
        assert {b.profile_id for b in bindings} == {profile_id}


def test_task_model_switch_rejects_stale_execution_and_keeps_draft(tmp_path):
    from tests.test_workbench import seeded, run_turn, result
    from app.planner import PlanningService

    db, settings, task_id, content, context = seeded(tmp_path)
    with db.session() as session:
        profile = configure(session)
    app = create_app(settings)
    route = f"/api/v1/tasks/{task_id}/workbench"
    with TestClient(app) as client:
        response = client.post(
            route + "/messages",
            json={"text": "Plan"},
            headers={"Idempotency-Key": "first"},
        )
        assert response.status_code == 202, response.text
    assert run_turn(db, settings, result(content))[0].state == "succeeded"
    with db.session() as session:
        second = configure(session, model="second-model")
    with TestClient(app) as client:
        detail = client.get(route).json()
        plan = detail["plans"][-1]
        stale_hash = detail["model_binding_hash"]
        new_profiles = {**detail["model_profiles"], "implementation": second.id}
        switched = client.post(
            route + "/models",
            json={"expected_hash": stale_hash, "profiles": new_profiles},
            headers={"Idempotency-Key": "switch"},
        )
        assert switched.status_code == 200, switched.text
        execute = {
            "plan_id": plan["id"],
            "plan_hash": plan["record_hash"],
            "approve_plan": True,
            "start_execution": True,
            "model_binding_hash": stale_hash,
        }
        assert (
            client.post(
                route + "/execute",
                json=execute,
                headers={"Idempotency-Key": "exec-stale"},
            ).status_code
            == 409
        )
        execute["model_binding_hash"] = switched.json()["record_hash"]
        response = client.post(
            route + "/execute", json=execute, headers={"Idempotency-Key": "exec-new"}
        )
        assert response.status_code == 409, response.text
        assert client.get(route).json()["plans"][-1]["id"] == plan["id"]
