from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.providers import (
    CodexExecInvocation,
    CredentialedModelGatewayUpstream,
    GatewayAuthorizationError,
    GatewayAuthorizationExpiredError,
    GatewayAuthorizationReplayError,
    GatewayAuthorizationScopeError,
    GatewayProviderCredential,
    GatewayTaskAuthorizer,
    GatewayTaskCredentialBroker,
    GatewayTaskTokenCodec,
    InternalModelGateway,
    ProviderStage,
    create_model_gateway_app,
)
from app.sandbox_worker.container import (
    GATEWAY_CONTAINER_POLICY_VERSION,
    ContainerIsolationPolicy,
    DockerCodexExecRunner,
    ReadOnlySnapshot,
)


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
IMAGE = "fixture/codex@sha256:" + "2" * 64
NETWORK = "contribos-model-gateway-test"
SERVICE = "contribos-model-gateway"
BASE_URL = f"http://{SERVICE}:8080/v1"


def _codec() -> GatewayTaskTokenCodec:
    return GatewayTaskTokenCodec(b"g" * 32)


def _invocation(*, model: str = "fixture-model") -> CodexExecInvocation:
    return CodexExecInvocation(
        stage=ProviderStage.ANALYZE,
        request_id="job-1:analyze:1",
        correlation_id="analysis-correlation-1",
        snapshot_id="snapshot-1",
        input_hash="1" * 64,
        prompt="Analyze the frozen input without external actions.",
        output_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        model=model,
    )


def _broker(
    codec: GatewayTaskTokenCodec,
    *,
    max_requests: int = 20,
) -> GatewayTaskCredentialBroker:
    return GatewayTaskCredentialBroker(
        codec=codec,
        base_url=BASE_URL,
        network=NETWORK,
        service_name=SERVICE,
        ttl_seconds=60,
        max_requests=max_requests,
    )


def _snapshot(tmp_path: Path) -> ReadOnlySnapshot:
    root = tmp_path / "root"
    source = root / "snapshot"
    source.mkdir(parents=True)
    (source / "README.md").write_text("gateway fixture\n", encoding="utf-8")
    return ReadOnlySnapshot.capture(
        snapshot_id="snapshot-1",
        source=source,
        allowed_root=root,
    )


def test_gateway_task_token_is_short_lived_scoped_signed_and_bounded() -> None:
    codec = _codec()
    credential = _broker(codec, max_requests=2).issue(
        _invocation(),
        now=NOW,
    )
    authorizer = GatewayTaskAuthorizer(codec)

    decoded = codec.decode(credential.token, now=NOW)
    assert decoded.authorization_id == credential.scope.authorization_id
    assert decoded.request_id == "job-1:analyze:1"
    assert decoded.snapshot_id == "snapshot-1"
    assert decoded.stage is ProviderStage.ANALYZE
    assert decoded.input_hash == "1" * 64
    assert decoded.provider_name == "codex_cli"
    assert decoded.model == "fixture-model"
    assert decoded.max_requests == 2
    assert decoded.expires_at - decoded.issued_at == timedelta(seconds=60)
    assert credential.token_hash

    consumed = authorizer.consume(
        credential.token,
        expected_provider="codex_cli",
        expected_model="fixture-model",
        now=NOW,
    )
    assert consumed.authorization_id == decoded.authorization_id
    second = authorizer.consume(
        credential.token,
        expected_provider="codex_cli",
        expected_model="fixture-model",
        now=NOW,
    )
    assert second.authorization_id == decoded.authorization_id
    with pytest.raises(GatewayAuthorizationReplayError, match="exhausted"):
        authorizer.consume(
            credential.token,
            expected_provider="codex_cli",
            expected_model="fixture-model",
            now=NOW,
        )

    expired = _broker(codec).issue(_invocation(), now=NOW)
    with pytest.raises(GatewayAuthorizationExpiredError, match="expired"):
        codec.decode(expired.token, now=NOW + timedelta(seconds=61))

    scoped = _broker(codec).issue(_invocation(), now=NOW)
    with pytest.raises(GatewayAuthorizationScopeError, match="does not match"):
        GatewayTaskAuthorizer(codec).consume(
            scoped.token,
            expected_provider="codex_cli",
            expected_model="different-model",
            now=NOW,
        )

    parts = credential.token.split(".")
    replacement = "A" if parts[-1][0] != "A" else "B"
    tampered = ".".join((*parts[:-1], replacement + parts[-1][1:]))
    with pytest.raises(GatewayAuthorizationError, match="signature"):
        codec.decode(tampered, now=NOW)


class _FakeUpstream:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def create_response(self, payload):  # type: ignore[no-untyped-def]
        self.payloads.append(dict(payload))
        return {"id": "response-1", "status": "completed"}


class _UnsafeUpstream:
    async def create_response(self, payload):  # type: ignore[no-untyped-def]
        return {
            "id": "github_pat_upstreamcanary12345678",
            "status": "completed",
        }


class _CredentialTransport:
    def __init__(self) -> None:
        self.authorization_headers: list[str] = []

    async def send(  # type: ignore[no-untyped-def]
        self,
        payload,
        *,
        authorization_header,
    ):
        self.authorization_headers.append(authorization_header)
        return {"id": "response-credentialed", "status": "completed"}


def test_internal_gateway_consumes_authorization_before_forwarding() -> None:
    codec = _codec()
    credential = _broker(codec, max_requests=1).issue(
        _invocation(),
        now=NOW,
    )
    upstream = _FakeUpstream()
    gateway = InternalModelGateway(
        authorizer=GatewayTaskAuthorizer(codec),
        upstream=upstream,
    )
    payload = {
        "model": "fixture-model",
        "input": "bounded frozen analysis input",
    }

    response = asyncio.run(
        gateway.create_response(
            authorization_header=f"Bearer {credential.token}",
            payload=payload,
            now=NOW,
        )
    )

    assert response == {"id": "response-1", "status": "completed"}
    assert upstream.payloads == [payload]
    assert credential.token not in str(upstream.payloads)
    with pytest.raises(GatewayAuthorizationReplayError):
        asyncio.run(
            gateway.create_response(
                authorization_header=f"Bearer {credential.token}",
                payload=payload,
                now=NOW,
            )
        )

    wrong_model = _broker(codec).issue(_invocation(), now=NOW)
    with pytest.raises(GatewayAuthorizationScopeError, match="scope"):
        asyncio.run(
            gateway.create_response(
                authorization_header=f"Bearer {wrong_model.token}",
                payload={"model": "other-model"},
                now=NOW,
            )
        )
    assert upstream.payloads == [payload]


def test_real_provider_credential_exists_only_behind_task_gateway() -> None:
    codec = _codec()
    task_credential = _broker(codec, max_requests=1).issue(
        _invocation(),
        now=NOW,
    )
    provider_secret = "github_pat_providercanary12345678"
    credential = GatewayProviderCredential(provider_secret)
    transport = _CredentialTransport()
    gateway = InternalModelGateway(
        authorizer=GatewayTaskAuthorizer(codec),
        upstream=CredentialedModelGatewayUpstream(
            credential=credential,
            transport=transport,
        ),
    )

    response = asyncio.run(
        gateway.create_response(
            authorization_header=f"Bearer {task_credential.token}",
            payload={
                "model": "fixture-model",
                "input": "bounded frozen analysis input",
            },
            now=NOW,
        )
    )

    assert response["id"] == "response-credentialed"
    assert transport.authorization_headers == [f"Bearer {provider_secret}"]
    assert task_credential.token not in str(transport.authorization_headers)
    assert provider_secret not in repr(credential)
    assert provider_secret not in str(credential)


def test_internal_gateway_rejects_credentials_before_authorization_or_upstream() -> None:
    codec = _codec()
    credential = _broker(codec).issue(_invocation(), now=NOW)
    authorizer = GatewayTaskAuthorizer(codec)
    upstream = _FakeUpstream()
    gateway = InternalModelGateway(
        authorizer=authorizer,
        upstream=upstream,
    )

    with pytest.raises(
        GatewayAuthorizationScopeError,
        match="unsafe data",
    ):
        asyncio.run(
            gateway.create_response(
                authorization_header=f"Bearer {credential.token}",
                payload={
                    "model": "fixture-model",
                    "input": "github_pat_gatewaycanary12345678",
                },
                now=NOW,
            )
        )

    assert upstream.payloads == []
    consumed = authorizer.consume(
        credential.token,
        expected_provider="codex_cli",
        expected_model="fixture-model",
        now=NOW,
    )
    assert consumed.authorization_id == credential.scope.authorization_id


def test_internal_gateway_http_route_is_bounded_and_redacted() -> None:
    codec = _codec()
    credential = _broker(codec, max_requests=1).issue(_invocation())
    upstream = _FakeUpstream()
    gateway = InternalModelGateway(
        authorizer=GatewayTaskAuthorizer(codec),
        upstream=upstream,
        max_request_bytes=256,
    )
    app = create_model_gateway_app(gateway)
    headers = {"Authorization": f"Bearer {credential.token}"}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/responses",
            headers=headers,
            json={
                "model": "fixture-model",
                "input": "bounded frozen analysis input",
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            "id": "response-1",
            "status": "completed",
        }
        assert credential.token not in response.text

        exhausted = client.post(
            "/v1/responses",
            headers=headers,
            json={
                "model": "fixture-model",
                "input": "bounded frozen analysis input",
            },
        )
        assert exhausted.status_code == 429
        assert exhausted.json()["error"]["code"] == (
            "gateway_authorization_exhausted"
        )
        assert credential.token not in exhausted.text

        oversized = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer invalid"},
            json={"model": "fixture-model", "input": "x" * 300},
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "gateway_request_limit"
        assert "invalid" not in oversized.text

    unsafe_credential = _broker(codec, max_requests=1).issue(_invocation())
    unsafe_app = create_model_gateway_app(
        InternalModelGateway(
            authorizer=GatewayTaskAuthorizer(codec),
            upstream=_UnsafeUpstream(),
        )
    )
    with TestClient(
        unsafe_app,
        raise_server_exceptions=False,
    ) as unsafe_client:
        unsafe = unsafe_client.post(
            "/v1/responses",
            headers={
                "Authorization": f"Bearer {unsafe_credential.token}",
            },
            json={
                "model": "fixture-model",
                "input": "bounded frozen analysis input",
            },
        )
        assert unsafe.status_code == 502
        assert unsafe.json()["error"]["code"] == "gateway_upstream_failure"
        assert "upstreamcanary" not in unsafe.text
        assert unsafe_credential.token not in unsafe.text


def test_gateway_container_routes_only_to_internal_network_without_token_argv(
    tmp_path: Path,
) -> None:
    codec = _codec()
    broker = _broker(codec)
    policy = ContainerIsolationPolicy(
        version=GATEWAY_CONTAINER_POLICY_VERSION,
        network=NETWORK,
    )
    runner = DockerCodexExecRunner(
        snapshot=_snapshot(tmp_path),
        image=IMAGE,
        docker_environment={"PATH": "/usr/bin:/bin"},
        policy=policy,
        credential_broker=broker,
    )
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    credential = broker.issue(_invocation(), now=NOW)
    argv = runner.build_argv(
        model="fixture-model",
        schema_path=schema,
        container_name="contribos-codex-gateway-fixture",
        gateway_base_url=credential.base_url,
    )

    assert argv[argv.index("--network") + 1] == NETWORK
    assert "--env" in argv
    assert "CODEX_API_KEY" in argv
    assert credential.token not in argv
    assert credential.token_hash not in argv
    assert f'openai_base_url="{BASE_URL}"' in argv
    assert "/var/run/docker.sock" not in " ".join(argv)

    with pytest.raises(ValueError, match="endpoint"):
        GatewayTaskCredentialBroker(
            codec=codec,
            base_url="https://api.openai.com/v1",
            network=NETWORK,
            service_name=SERVICE,
        )
    with pytest.raises(ValueError, match="credential broker"):
        DockerCodexExecRunner(
            snapshot=_snapshot(tmp_path / "other"),
            image=IMAGE,
            docker_environment={"PATH": "/usr/bin:/bin"},
            policy=policy,
        )


_RUN_DOCKER = os.environ.get("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") == "1"


@pytest.mark.skipif(
    not _RUN_DOCKER,
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
def test_real_gateway_network_reaches_only_internal_service(
    tmp_path: Path,
) -> None:
    image = os.environ.get("CONTRIBOS_CODEX_IMAGE", "")
    if not image:
        pytest.fail("CONTRIBOS_CODEX_IMAGE must contain a pinned image ID")
    suffix = uuid4().hex[:12]
    network = f"contribos-model-gateway-{suffix}"
    service = f"contribos-model-gateway-{suffix}"
    snapshot = _snapshot(tmp_path)
    policy = ContainerIsolationPolicy(
        version=GATEWAY_CONTAINER_POLICY_VERSION,
        network=network,
    )
    docker_environment = {
        name: value
        for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "HOME", "PATH")
        if (value := os.environ.get(name))
    }
    created = subprocess.run(
        ("docker", "network", "create", "--internal", network),
        env=docker_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    server: subprocess.Popen[str] | None = None
    try:
        server_argv = policy.build_argv(
            docker_executable="docker",
            image=image,
            snapshot=snapshot,
            schema_path=None,
            container_name=service,
            entrypoint="node",
            command=(
                "-e",
                (
                    "require('node:http').createServer((request,response)=>{"
                    "response.writeHead(200,{'content-type':'text/plain'});"
                    "response.end(request.url==='/health'?'gateway-ok':'not-found')"
                    "}).listen(8080,'0.0.0.0')"
                ),
            ),
        )
        server = subprocess.Popen(
            server_argv,
            env=docker_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        client_script = (
            "(async()=>{"
            "let internal=false;"
            "for(let attempt=0;attempt<30;attempt++){"
            "try{"
            f"const response=await fetch('http://{service}:8080/health');"
            "internal=response.ok&&(await response.text())==='gateway-ok';"
            "if(internal)break"
            "}catch(error){}"
            "await new Promise(resolve=>setTimeout(resolve,100))"
            "}"
            "if(!internal)throw new Error('internal gateway unavailable');"
            "let externalBlocked=false;"
            "try{"
            "await fetch('https://example.com',{signal:AbortSignal.timeout(2000)})"
            "}catch(error){externalBlocked=true}"
            "if(!externalBlocked)throw new Error('external network was reachable');"
            "process.stdout.write('internal-only-ok')"
            "})().catch(error=>{console.error(error.message);process.exit(1)})"
        )
        client_argv = policy.build_argv(
            docker_executable="docker",
            image=image,
            snapshot=snapshot,
            schema_path=None,
            container_name=f"contribos-codex-gateway-{suffix}",
            entrypoint="node",
            command=("-e", client_script),
        )
        client = subprocess.run(
            client_argv,
            env=docker_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert client.returncode == 0, client.stderr
        assert client.stdout == "internal-only-ok"
        inspected = subprocess.run(
            (
                "docker",
                "network",
                "inspect",
                network,
                "--format",
                "{{.Internal}}",
            ),
            env=docker_environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert inspected.returncode == 0, inspected.stderr
        assert inspected.stdout.strip() == "true"
    finally:
        subprocess.run(
            ("docker", "rm", "--force", service),
            env=docker_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if server is not None:
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        removed = subprocess.run(
            ("docker", "network", "rm", network),
            env=docker_environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr
