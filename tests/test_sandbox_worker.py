from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.providers as providers
from app.sandbox_worker import (
    FakeSandboxWorkerClient,
    SANDBOX_WORKER_PROTOCOL_VERSION,
    SandboxWorkerProcessClient,
    SandboxWorkerRemoteError,
    SandboxWorkerRequest,
)
from app.sandbox_worker.container import DockerCodexExecRunner
from app.sandbox_worker.contracts import SandboxWorkerContractError


def _request(
    *,
    operation: str = "probe",
    payload: dict[str, object] | None = None,
) -> SandboxWorkerRequest:
    return SandboxWorkerRequest(
        request_id="sandbox-request-1",
        correlation_id="sandbox-correlation-1",
        operation=operation,
        payload=payload or {},
    )


def test_standalone_worker_uses_a_separate_process_and_strict_protocol() -> None:
    client = SandboxWorkerProcessClient(executable=sys.executable)
    response = asyncio.run(client.request(_request()))

    assert response.ok is True
    assert response.result["process_boundary"] is True
    assert response.result["protocol_version"] == (
        SANDBOX_WORKER_PROTOCOL_VERSION
    )
    assert response.result["supported_operations"] == ["probe"]
    assert response.result["worker_pid"] != os.getpid()
    environment_keys = set(response.result["environment_keys"])
    assert environment_keys.issubset(
        {
            "LANG",
            "LC_CTYPE",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONUNBUFFERED",
        }
    )
    assert {
        "LANG",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }.issubset(environment_keys)
    assert set(client.environment) == {
        "LANG",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }


def test_worker_fails_closed_on_unknown_operation_or_nonempty_probe() -> None:
    client = SandboxWorkerProcessClient(executable=sys.executable)
    with pytest.raises(SandboxWorkerRemoteError) as unsupported:
        asyncio.run(
            client.request(
                _request(operation="execute_untrusted_repository")
            )
        )
    assert unsupported.value.code == "unsupported_operation"

    with pytest.raises(SandboxWorkerRemoteError) as invalid_probe:
        asyncio.run(client.request(_request(payload={"unexpected": True})))
    assert invalid_probe.value.code == "invalid_probe_payload"


def test_worker_contract_and_fake_are_deterministic_and_credential_safe() -> None:
    request = _request()
    assert SandboxWorkerRequest.from_bytes(request.to_bytes()) == request
    fake = FakeSandboxWorkerClient(result={"capability": "probe"})
    response = asyncio.run(fake.request(request))
    assert response.result == {"capability": "probe"}
    assert fake.requests == [request]

    with pytest.raises(ValueError, match="Credential-like"):
        _request(payload={"token": "ghp_sandboxworkercanary12345678"})
    with pytest.raises(SandboxWorkerContractError):
        SandboxWorkerRequest.from_bytes(b'{"unexpected":true}')
    with pytest.raises(ValueError, match="exact allowlist"):
        SandboxWorkerProcessClient(
            executable=sys.executable,
            environment={
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "GITHUB_TOKEN": "not-forwarded",
            },
        )


def test_docker_runtime_is_owned_only_by_the_sandbox_worker() -> None:
    assert DockerCodexExecRunner.__module__ == (
        "app.sandbox_worker.container"
    )
    assert not hasattr(providers, "DockerCodexExecRunner")
    assert not hasattr(providers, "ContainerIsolationPolicy")
    assert not hasattr(providers, "ReadOnlySnapshot")

    repository_root = Path(__file__).resolve().parents[1]
    protected_markers = (
        "DockerCodexExecRunner",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "/var/run/docker.sock",
    )
    violations: list[str] = []
    protected_sources = [
        repository_root / "app" / "api.py",
        *(repository_root / "app" / "providers").glob("*.py"),
        *(repository_root / "app").glob("publisher*.py"),
    ]
    for source in sorted(protected_sources):
        text = source.read_text(encoding="utf-8")
        for marker in protected_markers:
            if marker in text:
                violations.append(f"{source.relative_to(repository_root)}:{marker}")
    assert violations == []


def test_api_and_provider_imports_do_not_load_the_docker_runtime() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    script = "\n".join(
        (
            "import os",
            "import sys",
            "assert os.environ['DOCKER_HOST'] == 'unix:///forbidden.sock'",
            "import app.api",
            "import app.providers",
            "assert 'app.sandbox_worker.container' not in sys.modules",
            "assert not hasattr(app.providers, 'DockerCodexExecRunner')",
        )
    )
    environment = dict(os.environ)
    environment.update(
        {
            "DOCKER_HOST": "unix:///forbidden.sock",
            "DOCKER_CONTEXT": "forbidden-context",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
