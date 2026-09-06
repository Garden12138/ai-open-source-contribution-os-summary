from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.github import GitHubAPIError, GitHubClient, MemoryETagCache


API_URL = "https://api.github.test"
API_HOSTS = ("api.github.test",)
TOKEN_CANARY = "github_pat_SECRET_CANARY_123"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "github_api_url": API_URL,
        "github_allowed_hosts": API_HOSTS,
        "github_token": TOKEN_CANARY,
        "github_max_retries": 3,
        "github_retry_base_seconds": 0.5,
        "github_retry_max_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"github_max_retries": -1}, "GITHUB_MAX_RETRIES"),
        ({"github_max_retries": 4}, "GITHUB_MAX_RETRIES"),
        ({"github_retry_base_seconds": -0.1}, "GITHUB_RETRY_BASE_SECONDS"),
        ({"github_retry_max_seconds": -0.1}, "GITHUB_RETRY_MAX_SECONDS"),
    ],
)
def test_retry_configuration_rejects_unbounded_values(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        settings(**overrides)


def test_search_paginates_without_exceeding_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        per_page = int(request.url.params["per_page"])
        start = 0 if page == 1 else 100
        items = [{"id": start + index} for index in range(per_page)]
        return httpx.Response(200, json={"items": items}, request=request)

    async def run() -> list[dict[str, Any]]:
        async with GitHubClient(
            settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            return await client.search_issues("is:issue", limit=150)

    items = asyncio.run(run())

    assert len(items) == 150
    assert [int(request.url.params["page"]) for request in requests] == [1, 2]
    assert [int(request.url.params["per_page"]) for request in requests] == [
        100,
        50,
    ]


def test_etag_cache_serves_304_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"fixture-etag"'},
                json={"items": [{"id": 1}]},
                request=request,
            )
        assert request.headers["if-none-match"] == '"fixture-etag"'
        return httpx.Response(304, request=request)

    async def run() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cache = MemoryETagCache()
        async with GitHubClient(
            settings(),
            transport=httpx.MockTransport(handler),
            etag_cache=cache,
        ) as client:
            first = await client.search_issues("etag-query", limit=1)
            second = await client.search_issues("etag-query", limit=1)
            return first, second

    first, second = asyncio.run(run())

    assert first == [{"id": 1}]
    assert second == first
    assert len(requests) == 2


def test_rate_limit_and_transient_errors_use_bounded_retries() -> None:
    calls = 0
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                request=request,
            )
        if calls == 2:
            return httpx.Response(502, request=request)
        return httpx.Response(200, json={"items": []}, request=request)

    async def run() -> list[dict[str, Any]]:
        async with GitHubClient(
            settings(),
            transport=httpx.MockTransport(handler),
            sleep=record_sleep,
            random_value=lambda: 0,
        ) as client:
            return await client.search_issues("retry-query", limit=1)

    assert asyncio.run(run()) == []
    assert calls == 3
    assert delays == [2.0, 1.0]


def test_403_rate_limit_uses_reset_header() -> None:
    calls = 0
    delays: list[float] = []
    reset = int(datetime.now(timezone.utc).timestamp()) + 5

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset),
                },
                request=request,
            )
        return httpx.Response(200, json={"items": []}, request=request)

    async def run() -> None:
        async with GitHubClient(
            settings(),
            transport=httpx.MockTransport(handler),
            sleep=record_sleep,
        ) as client:
            await client.search_issues("rate-limit-query", limit=1)
            assert client.rate_limit_remaining == 0
            assert client.rate_limit_reset_at is not None

    asyncio.run(run())

    assert calls == 2
    assert len(delays) == 1
    assert 0 <= delays[0] <= 5


def test_403_secondary_rate_limit_with_remaining_quota_waits_one_minute() -> None:
    calls = 0
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "27",
                    "X-RateLimit-Reset": "4102444800",
                },
                json={
                    "message": "You have exceeded a secondary rate limit.",
                    "documentation_url": (
                        "https://docs.github.com/rest/using-the-rest-api/"
                        "rate-limits-for-the-rest-api"
                    ),
                },
                request=request,
            )
        return httpx.Response(200, json={"items": []}, request=request)

    async def run() -> None:
        async with GitHubClient(
            settings(),
            transport=httpx.MockTransport(handler),
            sleep=record_sleep,
            random_value=lambda: 0,
        ) as client:
            await client.search_issues("secondary-limit-query", limit=1)

    asyncio.run(run())

    assert calls == 2
    assert delays == [60.0]


def test_non_rate_limit_403_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "27"},
            json={"message": "Resource not accessible by personal access token"},
            request=request,
        )

    async def run() -> None:
        async with GitHubClient(
            settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(GitHubAPIError, match="HTTP 403"):
                await client.search_issues("permission-query", limit=1)

    asyncio.run(run())

    assert calls == 1


def test_network_error_retries_without_exposing_exception_details() -> None:
    calls = 0
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError(
                f"connection failed with {TOKEN_CANARY}",
                request=request,
            )
        return httpx.Response(200, json={"items": []}, request=request)

    async def run() -> None:
        async with GitHubClient(
            settings(github_max_retries=1),
            transport=httpx.MockTransport(handler),
            sleep=record_sleep,
            random_value=lambda: 0,
        ) as client:
            await client.search_issues("network-query", limit=1)

    asyncio.run(run())

    assert calls == 2
    assert delays == [0.5]


def test_timeout_is_bounded_and_retried() -> None:
    calls = 0
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("injected timeout", request=request)
        return httpx.Response(200, json={"items": []}, request=request)

    async def run() -> None:
        async with GitHubClient(
            settings(github_max_retries=1),
            transport=httpx.MockTransport(handler),
            sleep=record_sleep,
            random_value=lambda: 0,
        ) as client:
            await client.search_issues("timeout-query", limit=1)

    asyncio.run(run())

    assert calls == 2
    assert delays == [0.5]


def test_cross_origin_redirect_is_rejected_before_second_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/collect"},
            request=request,
        )

    async def run() -> None:
        async with GitHubClient(
            settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(GitHubAPIError, match="cross-origin"):
                await client.search_issues("redirect-query", limit=1)

    asyncio.run(run())

    assert len(requests) == 1
    assert requests[0].url.host == "api.github.test"
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN_CANARY}"


@pytest.mark.parametrize(
    ("url", "hosts", "message"),
    [
        ("http://api.github.test", API_HOSTS, "HTTPS"),
        ("https://evil.example", API_HOSTS, "allowed"),
        ("https://user:pass@api.github.test", API_HOSTS, "credentials"),
    ],
)
def test_api_url_is_validated_before_client_start(
    url: str, hosts: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        GitHubClient(
            settings(github_api_url=url, github_allowed_hosts=hosts)
        )


def test_error_body_and_token_canary_are_not_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": f"upstream reflected {TOKEN_CANARY}"},
            request=request,
        )

    async def run() -> str:
        async with GitHubClient(
            settings(github_max_retries=0),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(GitHubAPIError) as captured:
                await client.search_issues("secret-query", limit=1)
            return str(captured.value)

    message = asyncio.run(run())

    assert message == "GitHub API retry limit reached for HTTP 500"
    assert TOKEN_CANARY not in message


def test_malformed_search_payload_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"items": "not-a-list"},
            request=request,
        )

    async def run() -> None:
        async with GitHubClient(
            settings(),
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(GitHubAPIError, match="item list"):
                await client.search_issues("malformed-query", limit=1)

    asyncio.run(run())
