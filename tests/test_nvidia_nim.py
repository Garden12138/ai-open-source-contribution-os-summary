from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import app.providers.nvidia_nim as nvidia_nim
from app.provenance import content_hash
from app.providers import (
    GatewayTaskCredentialBroker,
    GatewayTaskTokenCodec,
    NvidiaNimGatewayRunner,
    NvidiaNimHostedTransport,
    ProviderRunError,
    ProviderStage,
)
from app.providers.nvidia_nim import (
    KIMI_K3_OFFICIAL_PARAMETERS,
    MINIMAX_M3_PARAMETERS,
    NEMOTRON_3_5_LIGHTNING_MODEL,
    NEMOTRON_3_5_LIGHTNING_PARAMETERS,
    MODEL_GATEWAY_REQUEST_TIMEOUT_SECONDS,
    NVIDIA_NIM_REQUEST_TIMEOUT_SECONDS,
    NvidiaNimParameters,
    _nvidia_tool_schema,
    analysis_parameters_for_model,
)
from app.providers.codex_cli import CodexExecInvocation


def _completion(content: str = '{"reply":"ok"}') -> dict[str, object]:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": 11,
            "cached_input_tokens": 3,
            "completion_tokens": 7,
        },
    }


def _tool_completion(
    arguments: str | dict[str, object] = '{"reply":"ok"}',
) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "contribos_record_structured_output",
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "cached_input_tokens": 3,
            "completion_tokens": 7,
        },
    }


def _sse_completion(content: str = '{"reply":"ok"}') -> str:
    return (
        "data: "
        + json.dumps(
            {
                "choices": [{"delta": {"content": content}}],
                "usage": {
                    "prompt_tokens": 11,
                    "cached_input_tokens": 3,
                    "completion_tokens": 7,
                },
            }
        )
        + "\n\n"
        + "data: [DONE]\n\n"
    )


def _sse_tool_completion() -> str:
    first = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "contribos_record_structured_output",
                                "arguments": '{"reply":',
                            },
                        }
                    ]
                }
            }
        ]
    }
    second = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '"ok"}'}}
                    ]
                }
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "cached_input_tokens": 3,
            "completion_tokens": 7,
        },
    }
    return "data: " + json.dumps(first) + "\n\n" + "data: " + json.dumps(second) + "\n\n" + "data: [DONE]\n\n"


def _sse_tool_completion_with_object_arguments() -> str:
    event = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "contribos_record_structured_output",
                                "arguments": {"reply": "ok"},
                            },
                        }
                    ]
                }
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "cached_input_tokens": 3,
            "completion_tokens": 7,
        },
    }
    return "data: " + json.dumps(event) + "\n\n" + "data: [DONE]\n\n"


def _invocation() -> CodexExecInvocation:
    return CodexExecInvocation(
        stage=ProviderStage.CODING,
        request_id="job-1:1",
        correlation_id="session-1",
        snapshot_id="execution-1",
        input_hash=content_hash({"prompt": "help"}),
        prompt="Help with the frozen implementation.",
        output_schema={
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
            "additionalProperties": False,
        },
        model="deepseek-ai/deepseek-v4-pro-0813",
    )


def test_hosted_transport_posts_only_to_allowlisted_nvidia_endpoint() -> None:
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=_completion())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = NvidiaNimHostedTransport(client=client)
    result = asyncio.run(
        transport.send(
            {"model": "minimaxai/minimax-m3", "messages": []},
            authorization_header="Bearer gateway-owned-key",
        )
    )
    asyncio.run(client.aclose())

    assert result == _completion()
    assert len(observed) == 1
    assert str(observed[0].url) == (
        "https://integrate.api.nvidia.com/v1/chat/completions"
    )
    assert observed[0].headers["authorization"] == "Bearer gateway-owned-key"
    assert observed[0].headers["accept"] == "application/json"
    assert observed[0].headers["connection"] == "close"
    assert "stream" not in json.loads(observed[0].content)


def test_hosted_transport_uses_requests_path_for_explicit_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_requests_request(**kwargs: object) -> httpx.Response:
        observed.update(kwargs)
        return httpx.Response(200, json=_completion())

    monkeypatch.setattr(
        nvidia_nim,
        "_requests_request",
        fake_requests_request,
    )
    result = asyncio.run(
        NvidiaNimHostedTransport(
            proxy_url="http://proxy.example.test:8080",
            max_retries=0,
        ).send(
            {"model": "minimaxai/minimax-m3", "messages": []},
            authorization_header="Bearer gateway-owned-key",
        )
    )

    assert result == _completion()
    assert observed["method"] == "POST"
    assert observed["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert observed["proxy_url"] == "http://proxy.example.test:8080"
    assert observed["payload"] == {"model": "minimaxai/minimax-m3", "messages": []}


def test_hosted_transport_aggregates_nvidia_sse_completion() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_completion(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = NvidiaNimHostedTransport(client=client)
    result = asyncio.run(
        transport.send(
            {
                "model": "moonshotai/kimi-k3",
                "messages": [],
                "stream": True,
            },
            authorization_header="Bearer gateway-owned-key",
        )
    )
    asyncio.run(client.aclose())

    assert result == _completion()


def test_hosted_transport_aggregates_streamed_tool_call_arguments() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_tool_completion(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(
        NvidiaNimHostedTransport(client=client).send(
            {"model": "minimaxai/minimax-m3", "messages": [], "stream": True},
            authorization_header="Bearer gateway-owned-key",
        )
    )
    asyncio.run(client.aclose())

    assert result == _tool_completion()


def test_hosted_transport_canonicalizes_streamed_object_tool_arguments() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_tool_completion_with_object_arguments(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(
        NvidiaNimHostedTransport(client=client).send(
            {"model": NEMOTRON_3_5_LIGHTNING_MODEL, "stream": True},
            authorization_header="Bearer gateway-owned-key",
        )
    )
    asyncio.run(client.aclose())

    assert result == _tool_completion()


def test_hosted_transport_enforces_total_stream_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_completion(),
        )

    async def slow_aggregation(_: httpx.Response) -> dict[str, object]:
        await asyncio.sleep(0.05)
        return _completion()

    monkeypatch.setattr(
        nvidia_nim,
        "_stream_response_mapping",
        slow_aggregation,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = NvidiaNimHostedTransport(
        client=client,
        timeout_seconds=0.01,
        max_retries=0,
    )
    with pytest.raises(ProviderRunError) as captured:
        asyncio.run(
            transport.send(
                {"model": NEMOTRON_3_5_LIGHTNING_MODEL, "stream": True},
                authorization_header="Bearer gateway-owned-key",
            )
        )
    asyncio.run(client.aclose())

    assert captured.value.code == "nvidia_nim_network_timeout"
    assert captured.value.retryable is True


def test_nvidia_timeouts_allow_a_360_second_upstream_request() -> None:
    transport = NvidiaNimHostedTransport()
    runner = NvidiaNimGatewayRunner(broker=None)

    assert transport.timeout_seconds == NVIDIA_NIM_REQUEST_TIMEOUT_SECONDS == 360
    assert (
        runner.timeout_seconds
        == MODEL_GATEWAY_REQUEST_TIMEOUT_SECONDS
        > transport.timeout_seconds
    )
    assert KIMI_K3_OFFICIAL_PARAMETERS == type(KIMI_K3_OFFICIAL_PARAMETERS)(
        temperature=1.0,
        reasoning_effort="max",
        max_tokens=16_384,
        seed=0,
        stream=True,
    )
    assert MINIMAX_M3_PARAMETERS == type(MINIMAX_M3_PARAMETERS)(
        temperature=1.0,
        max_tokens=8_192,
        seed=None,
        top_p=0.95,
        stream=False,
    )
    assert NEMOTRON_3_5_LIGHTNING_PARAMETERS == type(
        NEMOTRON_3_5_LIGHTNING_PARAMETERS
    )(
        temperature=0.0,
        max_tokens=6_144,
        seed=None,
        top_p=None,
        stream=True,
    )


def test_analysis_parameters_follow_the_selected_model_profile() -> None:
    assert (
        analysis_parameters_for_model(NEMOTRON_3_5_LIGHTNING_MODEL)
        == NEMOTRON_3_5_LIGHTNING_PARAMETERS
    )
    assert (
        analysis_parameters_for_model("minimaxai/minimax-m3")
        == MINIMAX_M3_PARAMETERS
    )
    assert analysis_parameters_for_model("example/unknown") == NvidiaNimParameters()


def test_nvidia_tool_schema_keeps_contract_constraints_and_drops_only_hosted_incompatibilities() -> None:
    original = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "citations": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "enum": ["issue"]},
            }
        },
        "required": ["citations"],
        "additionalProperties": False,
    }

    projected = _nvidia_tool_schema(original)

    assert projected == {
        "type": "object",
        "properties": {
            "citations": {
                "type": "array",
                "items": {"type": "string", "enum": ["issue"]},
            }
        },
        "required": ["citations"],
        "additionalProperties": False,
    }
    assert original["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert original["properties"]["citations"]["uniqueItems"] is True


def test_nemotron_analysis_tool_schema_bounds_values_and_citations_together() -> None:
    schema = {
        "type": "object",
        "properties": {
            "fit_reasons": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {"type": "string"},
            },
            "citation_map": {
                "type": "object",
                "properties": {
                    "fit_reasons": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {"type": "array"},
                    }
                },
            },
        },
    }

    projected = _nvidia_tool_schema(schema, single_item_analysis=True)

    assert projected["properties"]["fit_reasons"]["minItems"] == 1
    assert projected["properties"]["fit_reasons"]["maxItems"] == 1
    assert projected["properties"]["fit_reasons"]["items"]["minLength"] == 12
    citation_schema = projected["properties"]["citation_map"]["properties"]
    assert citation_schema["fit_reasons"]["minItems"] == 1
    assert citation_schema["fit_reasons"]["maxItems"] == 1
    assert schema["properties"]["fit_reasons"]["maxItems"] == 5


def test_hosted_transport_accepts_only_explicit_credential_free_proxy() -> None:
    transport = NvidiaNimHostedTransport(
        proxy_url="http://proxy.example.test:8080",
    )
    assert transport.proxy_url == "http://proxy.example.test:8080"

    for invalid in (
        "http://user:password@proxy.example.test:8080",
        "socks5://proxy.example.test:1080",
        "https://proxy.example.test/path",
        "https://proxy.example.test/?redirect=bad",
    ):
        with pytest.raises(ValueError, match="proxy is invalid"):
            NvidiaNimHostedTransport(proxy_url=invalid)


def test_hosted_transport_polls_nvidia_pending_result() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"requestId": "123e4567-e89b-12d3-a456-426614174000"},
            )
        return httpx.Response(200, json=_completion('{"verdict":"pass"}'))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = NvidiaNimHostedTransport(
        client=client,
        poll_interval_seconds=0.001,
    )
    result = asyncio.run(
        transport.send(
            {"model": "moonshotai/kimi-k3", "messages": []},
            authorization_header="Bearer gateway-owned-key",
        )
    )
    asyncio.run(client.aclose())

    assert result["choices"] == _completion('{"verdict":"pass"}')["choices"]
    assert paths == [
        "/v1/chat/completions",
        "/v1/status/123e4567-e89b-12d3-a456-426614174000",
    ]


def test_hosted_transport_rejects_redirect_and_non_allowlisted_base() -> None:
    with pytest.raises(ValueError, match="allowlisted"):
        NvidiaNimHostedTransport(base_url="https://example.test/v1")

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            307,
            headers={"Location": "https://example.test/steal"},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    transport = NvidiaNimHostedTransport(client=client)
    with pytest.raises(ProviderRunError, match="unsafe redirect"):
        asyncio.run(
            transport.send(
                {"model": "moonshotai/kimi-k3", "messages": []},
                authorization_header="Bearer task-token",
            )
        )
    asyncio.run(client.aclose())


def test_hosted_transport_rejects_unsafe_pending_request_id() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"requestId": "../chat/completions"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = NvidiaNimHostedTransport(client=client)
    with pytest.raises(ProviderRunError, match="invalid pending response"):
        asyncio.run(
            transport.send(
                {"model": "moonshotai/kimi-k3", "messages": []},
                authorization_header="Bearer task-token",
            )
        )
    asyncio.run(client.aclose())


def test_hosted_transport_retries_protocol_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(200, json=_completion())

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.providers.nvidia_nim.asyncio.sleep", no_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = NvidiaNimHostedTransport(client=client, max_retries=1)
    result = asyncio.run(
        transport.send(
            {"model": "minimaxai/minimax-m3", "messages": []},
            authorization_header="Bearer gateway-owned-key",
        )
    )
    asyncio.run(client.aclose())

    assert attempts == 2
    assert result == _completion()


def test_hosted_transport_labels_timeout_without_exposing_exception_text() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("proxy detail that must not escape")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = NvidiaNimHostedTransport(client=client, max_retries=0)
    with pytest.raises(ProviderRunError) as captured:
        asyncio.run(
            transport.send(
                {"model": "minimaxai/minimax-m3", "messages": []},
                authorization_header="Bearer gateway-owned-key",
            )
        )
    asyncio.run(client.aclose())

    assert captured.value.code == "nvidia_nim_network_timeout"
    assert "proxy detail" not in captured.value.safe_message


def test_gateway_runner_mints_scoped_token_and_returns_bounded_usage() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["authorization"]
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"g" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway",
        provider_name="nvidia_nim",
    )
    runner = NvidiaNimGatewayRunner(broker=broker, client=client)
    completion = asyncio.run(runner.complete(_invocation()))
    asyncio.run(client.aclose())

    assert str(observed["authorization"]).startswith("Bearer cgt1.")
    payload = observed["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "deepseek-ai/deepseek-v4-pro-0813"
    assert payload["stream"] is False
    assert "CONTRIBOS_REQUIRED_OUTPUT_SCHEMA=" in payload["messages"][1]["content"]
    assert completion.content == '{"reply":"ok"}'
    assert completion.input_tokens == 11
    assert completion.cached_input_tokens == 3
    assert completion.output_tokens == 7


def test_gateway_runner_uses_official_minimax_json_parameters() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"m" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway",
        provider_name="nvidia_nim",
    )
    runner = NvidiaNimGatewayRunner(
        broker=broker,
        parameters=MINIMAX_M3_PARAMETERS,
        client=client,
    )
    asyncio.run(runner.complete(_invocation()))
    asyncio.run(client.aclose())

    payload = observed["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "deepseek-ai/deepseek-v4-pro-0813"
    assert isinstance(payload["messages"], list)
    assert payload["temperature"] == 1.0
    assert payload["top_p"] == 0.95
    assert payload["max_tokens"] == 8_192
    assert payload["stream"] is False
    assert "seed" not in payload
    assert "reasoning_effort" not in payload
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "contribos_record_structured_output"},
    }


@pytest.mark.parametrize("stage", [
    ProviderStage.PLANNING, ProviderStage.CODING,
    ProviderStage.CHANGE_SET, ProviderStage.REVIEW,
])
def test_non_analysis_turns_do_not_receive_analysis_citation_requirements(stage):
    from dataclasses import replace

    observed = {}

    async def handler(request):
        observed.update(json.loads(request.content))
        return httpx.Response(200, json=_completion())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"m" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway", provider_name="nvidia_nim",
    )
    invocation = replace(_invocation(), stage=stage)
    asyncio.run(NvidiaNimGatewayRunner(broker=broker, client=client).complete(invocation))
    asyncio.run(client.aclose())
    instruction = observed["messages"][0]["content"]
    assert "citation_map" not in instruction
    assert "allowed_evidence_ids" not in instruction
    assert "stage-specific schema" in instruction
    assert observed["tools"][0]["function"]["parameters"] == invocation.output_schema


def test_gateway_runner_uses_hosted_nemotron_tool_mode_without_visible_thinking() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    invocation = _invocation()
    invocation = CodexExecInvocation(
        stage=ProviderStage.ANALYZE,
        request_id=invocation.request_id,
        correlation_id=invocation.correlation_id,
        snapshot_id=invocation.snapshot_id,
        input_hash=invocation.input_hash,
        prompt=invocation.prompt,
        output_schema=invocation.output_schema,
        model=NEMOTRON_3_5_LIGHTNING_MODEL,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"n" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway",
        provider_name="nvidia_nim",
    )
    runner = NvidiaNimGatewayRunner(
        broker=broker,
        parameters=NEMOTRON_3_5_LIGHTNING_PARAMETERS,
        client=client,
    )
    asyncio.run(runner.complete(invocation))
    asyncio.run(client.aclose())

    payload = observed["payload"]
    assert isinstance(payload, dict)
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "contribos_record_structured_output"},
    }
    assert payload["tools"][0]["function"]["parameters"] == {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }
    assert "response_format" not in payload
    assert "nvext" not in payload
    assert "Call the required function exactly once" in (
        payload["messages"][0]["content"]
    )
    assert 'exactly these keys, with no wrapper and no omissions: ["reply"]' in (
        payload["messages"][0]["content"]
    )
    assert "hours_min <= hours_max" in payload["messages"][0]["content"]
    assert "amount_usd must be null" in payload["messages"][0]["content"]
    assert "corresponding value array is empty" in payload["messages"][0]["content"]
    assert "exactly equal the union" in payload["messages"][0]["content"]
    assert "acceptance_criteria, missing_information" in (
        payload["messages"][0]["content"]
    )
    assert "project_summary, requirement_summary, current_behavior" in (
        payload["messages"][0]["content"]
    )
    assert "never use a bare evidence ID" in payload["messages"][0]["content"]
    assert "emit exactly one substantive item" in (
        payload["messages"][0]["content"]
    )
    assert "never invent or substitute an example URL" in (
        payload["messages"][0]["content"]
    )
    assert "complete, testable behavior sentence" in (
        payload["messages"][0]["content"]
    )


def test_gateway_runner_reads_required_tool_call_arguments() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_tool_completion())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"t" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway",
        provider_name="nvidia_nim",
    )
    completion = asyncio.run(
        NvidiaNimGatewayRunner(broker=broker, client=client).complete(_invocation())
    )
    asyncio.run(client.aclose())

    assert completion.content == '{"reply":"ok"}'


def test_gateway_runner_prefers_tool_call_arguments_over_explanation() -> None:
    response = _tool_completion()
    response["choices"][0]["message"]["content"] = "I have recorded the result."

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"u" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway",
        provider_name="nvidia_nim",
    )
    completion = asyncio.run(
        NvidiaNimGatewayRunner(broker=broker, client=client).complete(_invocation())
    )
    asyncio.run(client.aclose())

    assert completion.content == '{"reply":"ok"}'


def test_gateway_runner_canonicalizes_object_tool_call_arguments() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_tool_completion({"reply": "ok"}))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"o" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway",
        provider_name="nvidia_nim",
    )
    completion = asyncio.run(
        NvidiaNimGatewayRunner(broker=broker, client=client).complete(_invocation())
    )
    asyncio.run(client.aclose())

    assert completion.content == '{"reply":"ok"}'


def test_gateway_runner_rejects_truncated_tool_completion() -> None:
    response = _tool_completion('{"reply":')
    response["choices"][0]["finish_reason"] = "length"

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"l" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway",
        provider_name="nvidia_nim",
    )
    with pytest.raises(ProviderRunError) as captured:
        asyncio.run(
            NvidiaNimGatewayRunner(broker=broker, client=client).complete(
                _invocation()
            )
        )
    asyncio.run(client.aclose())

    assert captured.value.code == "nvidia_nim_completion_truncated"
    assert captured.value.retryable is False


def test_gateway_runner_redacts_internal_protocol_disconnect() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    broker = GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(b"g" * 32),
        base_url="http://contribos-model-gateway:8001/v1",
        network="contribos-model-gateway-local",
        service_name="contribos-model-gateway",
        provider_name="nvidia_nim",
    )
    runner = NvidiaNimGatewayRunner(broker=broker, client=client)
    with pytest.raises(ProviderRunError) as captured:
        asyncio.run(runner.complete(_invocation()))
    asyncio.run(client.aclose())

    assert captured.value.code == "model_gateway_network_failure"
    assert captured.value.retryable is True


def test_gateway_runner_fails_closed_without_credential_broker() -> None:
    runner = NvidiaNimGatewayRunner(broker=None)
    with pytest.raises(ProviderRunError, match="not configured"):
        asyncio.run(runner.complete(_invocation()))
