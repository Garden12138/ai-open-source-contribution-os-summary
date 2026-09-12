from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import requests

from app.provenance import canonical_json
from app.providers.codex_cli import (
    CodexCLIAdapter,
    CodexExecInvocation,
    CodexExecResult,
)
from app.providers.contracts import ProviderIdentity, ProviderRunError, ProviderStage
from app.providers.gateway import GatewayTaskCredentialBroker
from app.security import ensure_no_sensitive_data


NVIDIA_NIM_PROVIDER = "nvidia_nim"
NVIDIA_NIM_ADAPTER_VERSION = "nvidia-nim-chat-v28"
NVIDIA_NIM_MODEL_VERSION = "nvidia-build-catalog"
NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
MAX_NIM_RESPONSE_BYTES = 4_000_000
NVIDIA_NIM_REQUEST_TIMEOUT_SECONDS = 360
MODEL_GATEWAY_REQUEST_TIMEOUT_SECONDS = 390
NVIDIA_ANALYSIS_JOB_TIMEOUT_SECONDS = 840


@dataclass(frozen=True, slots=True)
class NvidiaNimParameters:
    temperature: float = 1.0
    reasoning_effort: str | None = None
    max_tokens: int = 8_192
    seed: int | None = 42
    top_p: float | None = None
    stream: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.temperature <= 1:
            raise ValueError("NVIDIA NIM temperature must be between 0 and 1")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in {"low", "high", "max"}
        ):
            raise ValueError("NVIDIA NIM reasoning effort is invalid")
        if not 1 <= self.max_tokens <= 65_536:
            raise ValueError("NVIDIA NIM max tokens is invalid")
        if self.seed is not None and (
            not isinstance(self.seed, int) or self.seed < 0
        ):
            raise ValueError("NVIDIA NIM seed is invalid")
        if self.top_p is not None and not 0 < self.top_p <= 1:
            raise ValueError("NVIDIA NIM top-p is invalid")
        if not isinstance(self.stream, bool):
            raise ValueError("NVIDIA NIM stream setting is invalid")


# NVIDIA Build's Kimi K3 reference invocation. Keep this distinct from the
# DeepSeek coding profile, whose lower temperature is intentional.
KIMI_K3_OFFICIAL_PARAMETERS = NvidiaNimParameters(
    temperature=1.0,
    reasoning_effort="max",
    max_tokens=16_384,
    seed=0,
    stream=True,
)


# NVIDIA Build's MiniMax M3 reference invocation, used for real analysis and
# Review. Use the documented non-streaming JSON mode because NVIDIA's current
# MiniMax tool-call response is complete there; the Gateway retains the 360s
# bounded deadline for the full structured response.
MINIMAX_M3_PARAMETERS = NvidiaNimParameters(
    temperature=1.0,
    top_p=0.95,
    max_tokens=8_192,
    seed=None,
    stream=False,
)


# Stream Nemotron through the credential-owning Gateway so a local CONNECT proxy
# observes progress during a long structured generation.  The catalog example
# allows a larger free-form completion, but ContribOS needs a bounded function
# call rather than a long answer: 6K output tokens covers the complex candidates
# that can truncate at 4K while keeping the complete request inside the
# 360-second upstream deadline observed in live acceptance.
NEMOTRON_3_5_LIGHTNING_PARAMETERS = NvidiaNimParameters(
    temperature=0.0,
    top_p=None,
    max_tokens=6_144,
    seed=None,
    stream=True,
)

NEMOTRON_3_5_LIGHTNING_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"

STRUCTURED_OUTPUT_TOOL_NAME = "contribos_record_structured_output"


@dataclass(frozen=True, slots=True)
class NvidiaNimCompletion:
    content: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    duration_ms: int


class NvidiaNimHostedTransport:
    """Gateway-only bounded transport for NVIDIA Build's hosted NIM API."""

    def __init__(
        self,
        *,
        base_url: str = NVIDIA_NIM_BASE_URL,
        proxy_url: str | None = None,
        timeout_seconds: float = NVIDIA_NIM_REQUEST_TIMEOUT_SECONDS,
        max_retries: int = 4,
        poll_interval_seconds: float = 1,
        max_poll_attempts: int = 120,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url.rstrip("/"))
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("NVIDIA NIM base URL is not allowlisted") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "integrate.api.nvidia.com"
            or port not in {None, 443}
            or parsed.path.rstrip("/") != "/v1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("NVIDIA NIM base URL is not allowlisted")
        if timeout_seconds <= 0 or not 0 <= max_retries <= 4:
            raise ValueError("NVIDIA NIM retry policy is invalid")
        if poll_interval_seconds <= 0 or max_poll_attempts < 1:
            raise ValueError("NVIDIA NIM polling policy is invalid")
        self.base_url = base_url.rstrip("/")
        self.proxy_url = _validated_proxy_url(proxy_url)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_attempts = max_poll_attempts
        self._client = client

    async def send(
        self,
        payload: Mapping[str, Any],
        *,
        authorization_header: str,
    ) -> Mapping[str, Any]:
        request_payload = dict(payload)
        stream = request_payload.get("stream") is True
        ensure_no_sensitive_data(request_payload, context="NVIDIA NIM request")
        headers = {
            "Authorization": authorization_header,
            "Accept": "text/event-stream" if stream else "application/json",
            "Content-Type": "application/json",
            # A local CONNECT proxy can discard an idle reused tunnel between
            # the Inspect and Analyze turns. Each bounded upstream request
            # gets a fresh TLS connection instead of relying on keep-alive.
            "Connection": "close",
        }
        owned = self._client is None
        if not stream and owned and self.proxy_url is not None:
            return await self._requests_completion_with_retry(
                f"{self.base_url}/chat/completions",
                headers=headers,
                payload=request_payload,
            )
        client = self._client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            # NVIDIA's official ``requests`` example uses HTTP/1.1.  Keeping
            # that protocol avoids HTTP/2 framing failures observed through
            # some local CONNECT proxies while retaining HTTPS to the
            # allowlisted NVIDIA endpoint.
            http2=False,
            # Do not allow a host HTTP(S)_PROXY setting to divert the only
            # credentialed egress path away from the allowlisted NVIDIA host.
            trust_env=False,
            # A trusted CONNECT proxy may be configured explicitly for the
            # Gateway container only. Generic inherited proxy variables stay
            # disabled above.
            proxy=self.proxy_url,
        )
        try:
            if stream:
                return await self._stream_completion_with_retry(
                    client,
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    payload=request_payload,
                )
            response = await self._request_with_retry(
                client,
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=request_payload,
                follow_redirects=False,
            )
            if response.status_code == 202:
                request_id = _pending_request_id(response)
                response = await self._poll(client, request_id, headers=headers)
            return _response_mapping(response)
        finally:
            if owned:
                await client.aclose()

    async def _requests_completion_with_retry(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Use NVIDIA's documented requests-style path for an explicit proxy."""
        deadline = time.monotonic() + self.timeout_seconds
        for attempt in range(self.max_retries + 1):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise ProviderRunError(
                    "nvidia_nim_network_failure",
                    "NVIDIA NIM request could not be completed",
                    retryable=True,
                )
            response: requests.Response | None = None
            try:
                response = await asyncio.to_thread(
                    _requests_request,
                    method="POST",
                    url=url,
                    headers=headers,
                    payload=payload,
                    proxy_url=self.proxy_url,
                    timeout_seconds=remaining_seconds,
                )
                if response.is_redirect:
                    raise ProviderRunError(
                        "nvidia_nim_redirect_rejected",
                        "NVIDIA NIM returned an unsafe redirect",
                        retryable=False,
                    )
                if response.status_code == 202:
                    request_id = _pending_request_id(response)
                    return await self._requests_poll(
                        request_id,
                        headers=headers,
                        deadline=deadline,
                    )
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return _response_mapping(response)
                if attempt >= self.max_retries or time.monotonic() >= deadline:
                    _raise_response_error(response)
            except requests.RequestException as exc:
                if attempt >= self.max_retries or time.monotonic() >= deadline:
                    raise _requests_transport_error(exc) from exc
            finally:
                if response is not None:
                    response.close()
            await asyncio.sleep(
                min(2**attempt, 4, max(0, deadline - time.monotonic()))
            )
        raise AssertionError("bounded requests retry loop did not return")

    async def _requests_poll(
        self,
        request_id: str,
        *,
        headers: Mapping[str, str],
        deadline: float,
    ) -> Mapping[str, Any]:
        for _ in range(self.max_poll_attempts):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            await asyncio.sleep(min(self.poll_interval_seconds, remaining_seconds))
            response: requests.Response | None = None
            try:
                response = await asyncio.to_thread(
                    _requests_request,
                    method="GET",
                    url=f"{self.base_url}/status/{request_id}",
                    headers=headers,
                    payload=None,
                    proxy_url=self.proxy_url,
                    timeout_seconds=remaining_seconds,
                )
                if response.status_code == 200:
                    return _response_mapping(response)
                if response.status_code != 202:
                    _raise_response_error(response)
            except requests.RequestException as exc:
                raise _requests_transport_error(exc) from exc
            finally:
                if response is not None:
                    response.close()
        raise ProviderRunError(
            "nvidia_nim_poll_timeout",
            "NVIDIA NIM result polling exceeded its bounded deadline",
            retryable=True,
        )

    async def _stream_completion_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        for attempt in range(self.max_retries + 1):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise ProviderRunError(
                    "nvidia_nim_network_failure",
                    "NVIDIA NIM request could not be completed",
                    retryable=True,
                )
            response: httpx.Response | None = None
            try:
                request = client.build_request(
                    "POST",
                    url,
                    headers=headers,
                    json=dict(payload),
                    timeout=remaining_seconds,
                )
                response = await client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
                if response.is_redirect:
                    raise ProviderRunError(
                        "nvidia_nim_redirect_rejected",
                        "NVIDIA NIM returned an unsafe redirect",
                        retryable=False,
                    )
                if response.status_code == 202:
                    await response.aread()
                    request_id = _pending_request_id(response)
                    return _response_mapping(
                        await self._poll(client, request_id, headers=headers)
                    )
                if response.status_code == 200:
                    try:
                        # httpx timeouts are inactivity limits. An SSE response
                        # can therefore run forever by emitting small deltas.
                        # Enforce the documented deadline over the complete
                        # streamed request, including aggregation.
                        async with asyncio.timeout(remaining_seconds):
                            return await _stream_response_mapping(response)
                    except TimeoutError as exc:
                        raise ProviderRunError(
                            "nvidia_nim_network_timeout",
                            "NVIDIA NIM streaming response exceeded its deadline",
                            retryable=True,
                        ) from exc
                if response.status_code not in {429, 500, 502, 503, 504}:
                    _raise_response_error(response)
                if attempt >= self.max_retries or time.monotonic() >= deadline:
                    _raise_response_error(response)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries or time.monotonic() >= deadline:
                    raise _transport_error(exc) from exc
            finally:
                if response is not None:
                    await response.aclose()
            await asyncio.sleep(
                min(2**attempt, 4, max(0, deadline - time.monotonic()))
            )
        raise AssertionError("bounded streaming retry loop did not return")

    async def _poll(
        self,
        client: httpx.AsyncClient,
        request_id: str,
        *,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        for _ in range(self.max_poll_attempts):
            await asyncio.sleep(self.poll_interval_seconds)
            response = await self._request_with_retry(
                client,
                "GET",
                f"{self.base_url}/status/{request_id}",
                headers=headers,
                follow_redirects=False,
            )
            if response.status_code == 200:
                return response
            if response.status_code != 202:
                _raise_response_error(response)
        raise ProviderRunError(
            "nvidia_nim_poll_timeout",
            "NVIDIA NIM result polling exceeded its bounded deadline",
            retryable=True,
        )

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        deadline = time.monotonic() + self.timeout_seconds
        for attempt in range(self.max_retries + 1):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise ProviderRunError(
                    "nvidia_nim_network_failure",
                    "NVIDIA NIM request could not be completed",
                    retryable=True,
                )
            try:
                response = await client.request(
                    method,
                    url,
                    **kwargs,
                    timeout=remaining_seconds,
                )
            # NVIDIA Build can close a keep-alive connection before it has
            # produced response headers.  httpx exposes that as a
            # RemoteProtocolError (a TransportError), not a NetworkError.
            # Treat every transport-layer failure alike so a transient close
            # receives the same bounded retry and never escapes as a raw 500
            # from the internal gateway.
            except httpx.TransportError as exc:
                if attempt >= self.max_retries or time.monotonic() >= deadline:
                    raise _transport_error(exc) from exc
                await asyncio.sleep(
                    min(2**attempt, 4, max(0, deadline - time.monotonic()))
                )
                continue
            if response.is_redirect:
                raise ProviderRunError(
                    "nvidia_nim_redirect_rejected",
                    "NVIDIA NIM returned an unsafe redirect",
                    retryable=False,
                )
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            if attempt >= self.max_retries or time.monotonic() >= deadline:
                _raise_response_error(response)
            delay = min(
                _retry_after(response) or min(2**attempt, 4),
                max(0, deadline - time.monotonic()),
            )
            await asyncio.sleep(delay)
        raise AssertionError("bounded retry loop did not return")


class NvidiaNimGatewayRunner:
    """Run a structured provider turn through the internal credential gateway."""

    def __init__(
        self,
        *,
        broker: GatewayTaskCredentialBroker | None,
        parameters: NvidiaNimParameters | None = None,
        timeout_seconds: float = MODEL_GATEWAY_REQUEST_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.broker = broker
        self.parameters = parameters or NvidiaNimParameters()
        self.timeout_seconds = timeout_seconds
        self._client = client

    async def run(self, invocation: CodexExecInvocation) -> CodexExecResult:
        completion = await self.complete(invocation)
        events = (
            {"type": "thread.started", "thread_id": invocation.request_id},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": completion.content,
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": completion.input_tokens,
                    "cached_input_tokens": completion.cached_input_tokens,
                    "output_tokens": completion.output_tokens,
                },
            },
        )
        return CodexExecResult(
            stdout="\n".join(canonical_json(event) for event in events),
            return_code=0,
            duration_ms=completion.duration_ms,
        )

    async def complete(
        self,
        invocation: CodexExecInvocation,
    ) -> NvidiaNimCompletion:
        if self.broker is None:
            raise ProviderRunError(
                "model_gateway_not_configured",
                "NVIDIA NIM model gateway is not configured in this process",
                retryable=False,
            )
        credential = self.broker.issue(invocation)
        nemotron_structured_mode = (
            invocation.model == NEMOTRON_3_5_LIGHTNING_MODEL
        )
        analysis_contract = invocation.stage in {ProviderStage.INSPECT, ProviderStage.ANALYZE}
        required_fields = invocation.output_schema.get("required", [])
        required_field_instruction = (
            "The top-level object must contain exactly these keys, with no "
            "wrapper and no omissions: "
            + canonical_json(required_fields)
            + ". Include every required array; use [] when the schema permits "
            "an empty result. Every nested object must likewise include all of "
            "its required keys. "
            if nemotron_structured_mode and isinstance(required_fields, list)
            else ""
        )
        output_instruction = (
            "Call the required function exactly once with an arguments object "
            "matching its JSON Schema. Do not return reasoning, prose, or Markdown. "
            + required_field_instruction
            + "Also obey these cross-field invariants: arrays declared unique must "
            "contain no duplicates; estimated_effort hours_min and hours_max must "
            "both be null or both be integers with hours_min <= hours_max; "
            "bounty_basis.amount_usd must be null when has_bounty is false; every "
            "citation_map array must have exactly one non-empty citation row per "
            "corresponding value, including fit_reasons, next_steps, and "
            "maintainer_questions; when a corresponding value array is empty, "
            "its citation_map array must also be []; every scalar citation_map "
            "field must be a non-empty array; each citation array must contain "
            "only unique exact literals from the final allowed_evidence_ids; "
            "specifically, acceptance_criteria, missing_information, "
            "similar_issue_pr_evidence, risks, fit_reasons, next_steps, and "
            "maintainer_questions use an array of citation arrays with exactly "
            "the same length as their value arrays; problem_summary, "
            "project_summary, requirement_summary, current_behavior, "
            "expected_behavior, competition, estimated_effort, "
            "bounty_basis, confidence, recommendation, and "
            "recommendation_summary each use one non-empty citation-ID array; "
            "project_summary must describe concrete repository evidence; "
            "requirement_summary must describe concrete Issue evidence; all later "
            "judgments must connect those two summaries. All narrative values must "
            "set citation_map.project_summary to include repository, "
            "citation_map.requirement_summary to include issue, and include both "
            "issue and repository in citation_map.recommendation_summary and at "
            "least one citation_map.fit_reasons row; all narrative values must "
            "be substantive descriptions grounded in frozen evidence and the "
            "inspection: never use a bare evidence ID, JSON property name, "
            "or placeholder such as issue as the text of a summary, criterion, "
            "reason, step, question, signal, rationale, basis, title, or "
            "relationship; "
            "competition means only evidence that another contributor is working "
            "on this exact Issue; never infer it from similar products, market "
            "position, popularity, or domain, and use unknown when exact-Issue "
            "evidence is absent; in that unknown case citation_map.competition must "
            "still contain issue because frozen assignee and comment fields support "
            "the absence, and citation arrays must never be empty; ground effort in "
            "concrete requirement work items "
            "and project characteristics; translate ordinary English prose into "
            "Chinese rather than preserving phrases such as future or "
            "per-jurisdiction; "
            "for this screening response, emit exactly one substantive item in "
            "each of acceptance_criteria, missing_information, risks, "
            "fit_reasons, next_steps, and maintainer_questions, and therefore "
            "exactly one citation row in each matching citation_map field; set "
            "similar_issue_pr_evidence and its citation_map field to [] unless "
            "the frozen inspection contains an actual cited GitHub Issue or PR "
            "URL—never invent or substitute an example URL; "
            "the acceptance_criteria item must be a complete, testable behavior "
            "sentence, never only an Issue number or observation code; "
            "cited_evidence_ids itself must be unique and exactly equal the union "
            "of all citation_map IDs; risk codes must be unique. "
            if nemotron_structured_mode and analysis_contract
            else (
                "Call the required function exactly once with an object "
                "matching its JSON Schema. Do not return prose or Markdown. "
            )
        )
        citation_instruction = (
            "For every evidence citation, copy an opaque ID verbatim "
            "from allowed_evidence_ids in the user input; never invent, "
            "shorten, translate, or use a label as an ID. The top-level "
            "cited_evidence_ids must exactly equal the union of every "
            "citation_map entry. The final user input line repeats the "
            "only permitted citation IDs; use those exact literals."
            if analysis_contract else
            "Use only the fields in the supplied stage-specific schema. "
            "Do not add fields from other workflow stages."
        )
        payload: dict[str, Any] = {
            "model": invocation.model,
            "messages": [
                {
                    "role": "system",
                    "content": output_instruction + citation_instruction,
                },
                {
                    "role": "user",
                    "content": (
                        invocation.prompt
                        + "\nCONTRIBOS_REQUIRED_OUTPUT_SCHEMA="
                        + canonical_json(invocation.output_schema)
                    ),
                },
            ],
            "temperature": self.parameters.temperature,
            "max_tokens": self.parameters.max_tokens,
            "stream": self.parameters.stream,
        }
        if nemotron_structured_mode:
            # Keep reasoning out of the arguments channel. NVIDIA Build does
            # not expose self-hosted ``nvext.guided_json``, while Nemotron's
            # native tool parser does accept the same constrained function
            # schema used successfully by the Inspect turn.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": STRUCTURED_OUTPUT_TOOL_NAME,
                    "description": "Record the validated ContribOS output.",
                    "parameters": _nvidia_tool_schema(
                        invocation.output_schema,
                        single_item_analysis=nemotron_structured_mode,
                    ),
                },
            }
        ]
        payload["tool_choice"] = {
            "type": "function",
            "function": {"name": STRUCTURED_OUTPUT_TOOL_NAME},
        }
        if self.parameters.seed is not None:
            payload["seed"] = self.parameters.seed
        if self.parameters.reasoning_effort is not None:
            payload["reasoning_effort"] = self.parameters.reasoning_effort
        if self.parameters.top_p is not None:
            payload["top_p"] = self.parameters.top_p
        started = time.monotonic()
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            # Provider Worker must contact only its internal Gateway endpoint;
            # inherited proxy variables are neither needed nor trusted.
            trust_env=False,
        )
        try:
            response = await client.post(
                f"{credential.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {credential.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                follow_redirects=False,
            )
        except httpx.TransportError as exc:
            raise ProviderRunError(
                "model_gateway_network_failure",
                "Internal model gateway request could not be completed",
                retryable=True,
            ) from exc
        finally:
            if owned:
                await client.aclose()
        if response.status_code != 200:
            raise ProviderRunError(
                "model_gateway_upstream_failure",
                "Internal model gateway rejected the NVIDIA NIM request",
                retryable=response.status_code >= 500,
            )
        data = _response_mapping(response)
        content, usage = _completion(data)
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        return NvidiaNimCompletion(
            content=content,
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            output_tokens=usage["output_tokens"],
            duration_ms=duration_ms,
        )


def create_nvidia_analysis_provider(
    *,
    model: str,
    broker: GatewayTaskCredentialBroker | None,
) -> CodexCLIAdapter:
    return CodexCLIAdapter(
        NvidiaNimGatewayRunner(
            broker=broker,
            parameters=analysis_parameters_for_model(model),
            timeout_seconds=MODEL_GATEWAY_REQUEST_TIMEOUT_SECONDS,
        ),
        identity=ProviderIdentity(
            provider=NVIDIA_NIM_PROVIDER,
            adapter_version=NVIDIA_NIM_ADAPTER_VERSION,
            model=model,
            model_version=NVIDIA_NIM_MODEL_VERSION,
        ),
    )


def analysis_parameters_for_model(model: str) -> NvidiaNimParameters:
    """Return the fixed, reviewed provider parameters for an analysis model."""

    if model == NEMOTRON_3_5_LIGHTNING_MODEL:
        return NEMOTRON_3_5_LIGHTNING_PARAMETERS
    if model == "minimaxai/minimax-m3":
        return MINIMAX_M3_PARAMETERS
    return NvidiaNimParameters()


def _pending_request_id(response: httpx.Response) -> str:
    data = _response_mapping(response)
    value = data.get("requestId") or data.get("request_id")
    try:
        normalized = str(UUID(value)) if isinstance(value, str) else ""
    except (ValueError, AttributeError, TypeError):
        normalized = ""
    if not normalized or value.lower() != normalized:
        raise ProviderRunError(
            "nvidia_nim_pending_response_invalid",
            "NVIDIA NIM returned an invalid pending response",
            retryable=True,
        )
    return normalized


def _nvidia_tool_schema(
    output_schema: Mapping[str, Any],
    *,
    single_item_analysis: bool = False,
) -> dict[str, Any]:
    """Project a contract schema onto NVIDIA Build's hosted tool subset.

    NVIDIA's hosted endpoint rejects the JSON Schema annotation ``$schema`` and
    ``uniqueItems`` inside function parameters.  They are transport metadata,
    not an authorization or persistence decision: the original schema remains
    in the prompt and ContribOS still performs its full local validation before
    an output can become an AnalysisVersion.  In particular, evidence enums,
    required fields and ``additionalProperties: false`` stay intact.
    """

    unsupported = frozenset({"$schema", "uniqueItems"})

    def project(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: project(item)
                for key, item in value.items()
                if isinstance(key, str) and key not in unsupported
            }
        if isinstance(value, list):
            return [project(item) for item in value]
        return value

    projected = project(output_schema)
    if not isinstance(projected, dict):
        raise ProviderRunError(
            "nvidia_nim_tool_schema_invalid",
            "NVIDIA NIM tool schema is invalid",
            retryable=False,
        )
    properties = projected.get("properties")
    if (
        single_item_analysis
        and isinstance(properties, dict)
        and "fit_reasons" in properties
    ):
        aligned_fields = (
            "acceptance_criteria",
            "missing_information",
            "risks",
            "fit_reasons",
            "next_steps",
            "maintainer_questions",
        )
        citation_map = properties.get("citation_map")
        citation_properties = (
            citation_map.get("properties")
            if isinstance(citation_map, dict)
            else None
        )
        for field in aligned_fields:
            field_schema = properties.get(field)
            if isinstance(field_schema, dict):
                field_schema["minItems"] = 1
                field_schema["maxItems"] = 1
                item_schema = field_schema.get("items")
                if isinstance(item_schema, dict) and item_schema.get("type") == "string":
                    item_schema["minLength"] = max(
                        int(item_schema.get("minLength", 0)),
                        12,
                    )
            citation_schema = (
                citation_properties.get(field)
                if isinstance(citation_properties, dict)
                else None
            )
            if isinstance(citation_schema, dict):
                citation_schema["minItems"] = 1
                citation_schema["maxItems"] = 1
    return projected


def _requests_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any] | None,
    proxy_url: str | None,
    timeout_seconds: float,
) -> requests.Response:
    """Make one proxy-isolated requests call without inherited host settings."""
    with requests.Session() as session:
        session.trust_env = False
        if proxy_url is not None:
            session.proxies = {"https": proxy_url}
        return session.request(
            method,
            url,
            headers=dict(headers),
            json=dict(payload) if payload is not None else None,
            timeout=timeout_seconds,
            allow_redirects=False,
        )


def _response_mapping(response: httpx.Response | requests.Response) -> Mapping[str, Any]:
    if len(response.content) > MAX_NIM_RESPONSE_BYTES:
        raise ProviderRunError(
            "nvidia_nim_response_limit",
            "NVIDIA NIM response exceeded its byte limit",
            retryable=False,
        )
    if response.status_code != 200 and response.status_code != 202:
        _raise_response_error(response)
    try:
        value = response.json()
    except (ValueError, UnicodeError) as exc:
        raise ProviderRunError(
            "nvidia_nim_response_invalid",
            "NVIDIA NIM returned an invalid response",
            retryable=True,
        ) from exc
    if not isinstance(value, Mapping):
        raise ProviderRunError(
            "nvidia_nim_response_invalid",
            "NVIDIA NIM returned an invalid response",
            retryable=True,
        )
    ensure_no_sensitive_data(value, context="NVIDIA NIM response")
    return value


async def _stream_response_mapping(response: httpx.Response) -> Mapping[str, Any]:
    """Aggregate NVIDIA's SSE deltas into the normal completion shape."""

    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: Mapping[str, Any] = {}
    finish_reason: str | None = None
    received_bytes = 0
    completed = False
    try:
        async for line in response.aiter_lines():
            received_bytes += len(line.encode("utf-8"))
            if received_bytes > MAX_NIM_RESPONSE_BYTES:
                raise ProviderRunError(
                    "nvidia_nim_response_limit",
                    "NVIDIA NIM response exceeded its byte limit",
                    retryable=False,
                )
            if not line.startswith("data:"):
                continue
            event = line[5:].strip()
            if event == "[DONE]":
                completed = True
                break
            try:
                value = json.loads(event)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ProviderRunError(
                    "nvidia_nim_response_invalid",
                    "NVIDIA NIM returned an invalid streaming response",
                    retryable=True,
                ) from exc
            if not isinstance(value, Mapping):
                raise ProviderRunError(
                    "nvidia_nim_response_invalid",
                    "NVIDIA NIM returned an invalid streaming response",
                    retryable=True,
                )
            ensure_no_sensitive_data(value, context="NVIDIA NIM streaming response")
            raw_usage = value.get("usage")
            if isinstance(raw_usage, Mapping):
                usage = raw_usage
            choices = value.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                raw_finish_reason = (
                    choice.get("finish_reason")
                    if isinstance(choice, Mapping)
                    else None
                )
                if isinstance(raw_finish_reason, str):
                    finish_reason = raw_finish_reason
                delta = choice.get("delta") if isinstance(choice, Mapping) else None
                chunk = delta.get("content") if isinstance(delta, Mapping) else None
                if isinstance(chunk, str):
                    content_parts.append(chunk)
                raw_calls = (
                    delta.get("tool_calls") if isinstance(delta, Mapping) else None
                )
                if not isinstance(raw_calls, list):
                    continue
                for raw_call in raw_calls:
                    if not isinstance(raw_call, Mapping):
                        continue
                    index = raw_call.get("index", 0)
                    if not isinstance(index, int) or isinstance(index, bool):
                        continue
                    current = tool_calls.setdefault(
                        index,
                        {"function": {"arguments_parts": []}},
                    )
                    for field in ("id", "type"):
                        item = raw_call.get(field)
                        if isinstance(item, str):
                            current[field] = item
                    function = raw_call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    target = current["function"]
                    name = function.get("name")
                    if isinstance(name, str):
                        target["name"] = name
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        target["arguments_parts"].append(arguments)
                    elif isinstance(arguments, Mapping):
                        # NVIDIA may emit one decoded arguments object instead
                        # of OpenAI-style JSON string fragments in an SSE
                        # tool-call delta. Canonicalize it before applying the
                        # unchanged local schema and evidence validation.
                        target["arguments_parts"].append(
                            canonical_json(arguments)
                        )
    except httpx.TransportError as exc:
        raise ProviderRunError(
            "nvidia_nim_network_failure",
            "NVIDIA NIM streaming response could not be completed",
            retryable=True,
        ) from exc
    if not completed:
        raise ProviderRunError(
            "nvidia_nim_response_invalid",
            "NVIDIA NIM streaming response ended without completion",
            retryable=True,
        )
    content = "".join(content_parts)
    message: dict[str, Any] = {"role": "assistant", "content": content or None}
    if tool_calls:
        message["tool_calls"] = [
            {
                **{field: value for field, value in call.items() if field != "function"},
                "function": {
                    "name": call["function"].get("name"),
                    "arguments": "".join(call["function"]["arguments_parts"]),
                },
            }
            for _, call in sorted(tool_calls.items())
        ]
    result_choice: dict[str, Any] = {"message": message}
    if finish_reason is not None:
        result_choice["finish_reason"] = finish_reason
    result: Mapping[str, Any] = {
        "choices": [result_choice],
        "usage": dict(usage),
    }
    ensure_no_sensitive_data(result, context="NVIDIA NIM response")
    return result


def _completion(
    value: Mapping[str, Any],
    *,
    error_prefix: str = "nvidia_nim",
    provider_label: str = "NVIDIA NIM",
) -> tuple[str, dict[str, int]]:
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProviderRunError(
            f"{error_prefix}_completion_invalid",
            f"{provider_label} completion response is invalid",
            retryable=False,
        )
    choice = choices[0]
    if isinstance(choice, Mapping) and choice.get("finish_reason") == "length":
        raise ProviderRunError(
            f"{error_prefix}_completion_truncated",
            f"{provider_label} completion exhausted its output-token budget",
            retryable=False,
        )
    message = choice.get("message") if isinstance(choice, Mapping) else None
    # Hosted models can include a natural-language explanation alongside the
    # required structured-output tool call.  The tool arguments are the
    # authoritative output: using the explanation would send non-JSON text to
    # the strict local schema and citation validators.
    content = _tool_call_arguments(message)
    if not isinstance(content, str) or not content.strip():
        content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, str) or not content.strip():
        raise ProviderRunError(
            f"{error_prefix}_completion_invalid",
            f"{provider_label} completion response has no final content",
            retryable=False,
        )
    raw_usage = value.get("usage")
    usage = raw_usage if isinstance(raw_usage, Mapping) else {}
    return content, {
        "input_tokens": _non_negative_int(
            usage.get("prompt_tokens", usage.get("input_tokens", 0))
        ),
        "cached_input_tokens": _non_negative_int(
            usage.get("cached_input_tokens", 0)
        ),
        "output_tokens": _non_negative_int(
            usage.get("completion_tokens", usage.get("output_tokens", 0))
        ),
    }


def _tool_call_arguments(message: Mapping[str, Any] | None) -> str | None:
    if not isinstance(message, Mapping):
        return None
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        return None
    call = tool_calls[0]
    function = call.get("function") if isinstance(call, Mapping) else None
    if not isinstance(function, Mapping):
        return None
    if function.get("name") != STRUCTURED_OUTPUT_TOOL_NAME:
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        return arguments
    # NVIDIA-hosted tool calls may return already-decoded arguments instead of
    # OpenAI's JSON string. Canonicalize that bounded object and send it through
    # the exact same schema, citation and sensitive-data checks as text output.
    if isinstance(arguments, Mapping):
        return canonical_json(arguments)
    return None


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _retry_after(response: httpx.Response | requests.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0), 10)
    except ValueError:
        return None


def _transport_error(exc: httpx.TransportError) -> ProviderRunError:
    if isinstance(exc, httpx.TimeoutException):
        code = "nvidia_nim_network_timeout"
    elif isinstance(exc, httpx.RemoteProtocolError):
        code = "nvidia_nim_network_protocol"
    elif isinstance(exc, httpx.ConnectError):
        code = "nvidia_nim_network_connect"
    else:
        code = "nvidia_nim_network_failure"
    return ProviderRunError(
        code,
        "NVIDIA NIM request could not be completed",
        retryable=True,
    )


def _requests_transport_error(exc: requests.RequestException) -> ProviderRunError:
    code = (
        "nvidia_nim_network_timeout"
        if isinstance(exc, requests.Timeout)
        else "nvidia_nim_network_failure"
    )
    return ProviderRunError(
        code,
        "NVIDIA NIM request could not be completed",
        retryable=True,
    )


def _validated_proxy_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    parsed = urlparse(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("NVIDIA HTTPS proxy is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port is not None and not 1 <= port <= 65_535
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("NVIDIA HTTPS proxy is invalid")
    return normalized.rstrip("/")


def _raise_response_error(response: httpx.Response) -> None:
    retryable = response.status_code in {429, 500, 502, 503, 504}
    raise ProviderRunError(
        "nvidia_nim_rate_limited" if response.status_code == 429 else "nvidia_nim_upstream_failure",
        "NVIDIA NIM request was rate limited" if response.status_code == 429 else "NVIDIA NIM request failed",
        retryable=retryable,
    )
