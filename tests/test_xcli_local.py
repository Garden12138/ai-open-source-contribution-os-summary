from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.model_settings import ConnectionInput, validate_base_url
from app.providers.contracts import ProviderRunError
from app.providers.studio import ProfileUpstream
from app.providers.xcli_local import XcliLocalClient, local_endpoint, registered_endpoint, require_model


ENDPOINT = "http://127.0.0.1:18741/v1"
TOKEN = "synthetic-xcli-token-canary"
ALIAS = "xcli/text-default"


def catalog(*capabilities, validated=True):
    return {"object": "list", "data": [{"id": ALIAS, "xcli": {
        "capabilities": list(capabilities), "validated": validated, "inference_location": "remote",
    }}]}


@pytest.fixture
def registered(monkeypatch):
    monkeypatch.setenv("CONTRIBOS_XCLI_LOCAL_ENDPOINT", ENDPOINT)
    monkeypatch.setattr("app.providers.xcli_local.native_runtime", lambda: True)


def test_local_urls_do_not_relax_existing_public_https_validation(registered):
    assert registered_endpoint(ENDPOINT) == ENDPOINT
    for invalid in ["http://localhost:18741/v1", "http://192.168.1.1:18741/v1",
                    "http://user@127.0.0.1:18741/v1", ENDPOINT + "?secret=canary",
                    ENDPOINT + "/other", "http://127.0.0.1:18742/v1"]:
        with pytest.raises(ValueError):
            registered_endpoint(invalid)
    with pytest.raises(ValueError):
        validate_base_url(ENDPOINT)
    with pytest.raises(ValueError):
        ConnectionInput(name="untrusted", provider="openai_compatible", base_url=ENDPOINT)
    with pytest.raises(ValueError):
        ConnectionInput(name="xcli", provider="xcli_local", base_url=ENDPOINT, credential_ref="env-nvidia")
    value = ConnectionInput(name="xcli", provider="xcli_local", base_url=ENDPOINT, credential_ref="cred-" + "1" * 32)
    assert value.base_url == ENDPOINT
    assert local_endpoint("http://[::1]:18741/v1/") == "http://[::1]:18741/v1"


def test_missing_registration_is_not_inferred_from_loopback(monkeypatch):
    monkeypatch.delenv("CONTRIBOS_XCLI_LOCAL_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="registered"):
        registered_endpoint(ENDPOINT)


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500, 502, 504])
def test_http_failure_is_single_attempt_without_redirect_or_private_body_leak(registered, status):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        return httpx.Response(status, text=TOKEN, headers={"location": "http://169.254.169.254/private"})
    client = XcliLocalClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderRunError) as error:
        asyncio.run(client.request(ENDPOINT, "/models", credential=TOKEN))
    assert len(calls) == 1
    assert TOKEN not in str(error.value)
    assert not error.value.retryable


def test_container_namespace_does_not_enable_a_host_bridge(registered, monkeypatch):
    monkeypatch.setattr("app.providers.xcli_local.native_runtime", lambda: False)
    calls = []
    client = XcliLocalClient(transport=httpx.MockTransport(lambda request: calls.append(request)))
    with pytest.raises(ProviderRunError) as error:
        asyncio.run(client.request(ENDPOINT, "/models", credential=TOKEN))
    assert error.value.code == "xcli_container_bridge_unsupported"
    assert calls == []


def test_catalog_requires_verified_tools_and_schema_before_private_upload():
    for response in [{}, {"object": "list", "data": [{}]}, catalog("text.generate", "tool_calls", validated=False), catalog("text.generate")]:
        with pytest.raises(ProviderRunError):
            require_model(response, ALIAS, {"tools": [{"type": "function"}]})
    require_model(catalog("text.generate", "json_schema"), ALIAS, {"response_format": {"type": "json_schema"}})
    with pytest.raises(ProviderRunError):
        require_model(catalog("text.generate"), ALIAS, {"response_format": {"type": "json_schema"}})
    with pytest.raises(ProviderRunError):
        require_model(catalog("text.generate"), ALIAS, {"response_format": {"type": "json_object"}})
    require_model(catalog("text.generate", "json_object"), ALIAS, {"response_format": {"type": "json_object"}})


def test_frozen_profile_keeps_model_output_budget_and_catalog_preflight(registered):
    calls = []
    class Store:
        def read(self, reference):
            assert reference == "cred-" + "1" * 32
            return TOKEN
    def handler(request):
        calls.append(request)
        if request.url.path == "/v1/models":
            assert request.content == b""
            return httpx.Response(200, json=catalog("text.generate", "json_schema"))
        body = json.loads(request.content)
        assert body["model"] == ALIAS
        assert body["max_tokens"] == 32
        assert body["response_format"]["type"] == "json_schema"
        assert "_contribos_profile" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    profile = {"model": ALIAS, "max_tokens": 32, "timeout_seconds": 5, "connection": {
        "provider": "xcli_local", "base_url": ENDPOINT, "credential_ref": "cred-" + "1" * 32,
    }}
    transport = XcliLocalClient(transport=httpx.MockTransport(handler))
    upstream = ProfileUpstream(profile, Store(), transport=transport)
    payload = {"model": ALIAS, "messages": [{"role": "user", "content": "synthetic input"}],
               "max_tokens": 99, "response_format": {"type": "json_schema"}, "_contribos_profile": profile}
    result = asyncio.run(upstream.create_response(payload))
    assert result["choices"]
    assert [request.url.path for request in calls] == ["/v1/models", "/v1/chat/completions"]
    with pytest.raises(ProviderRunError) as error:
        asyncio.run(upstream.create_response({**payload, "model": "different-alias"}))
    assert error.value.code == "xcli_frozen_model_mismatch"
    assert len(calls) == 2
