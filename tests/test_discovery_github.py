import asyncio
import time

import httpx
import pytest

from contribos.github import GitHubError, GitHubReader


def test_search_paginates_stably_and_revalidates_each_page():
    calls = []

    def respond(request):
        page = int(request.url.params['page'])
        size = int(request.url.params['per_page'])
        calls.append(page)
        if request.headers.get('if-none-match') == str(page):
            return httpx.Response(304)
        start = (page - 1) * size
        return httpx.Response(200, headers={'etag': str(page)}, json={'items': [
            {'id': i} for i in range(start, start + size)]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            reader = GitHubReader(client)
            for _ in range(2):
                rows = await reader.search_issues('is:issue', 150)
                assert [item['id'] for item in rows] == list(range(150))

    asyncio.run(run())
    assert calls == [1, 2, 1, 2]


@pytest.mark.parametrize('status,headers,message,delay', [
    (429, {'retry-after': '2'}, '', 2), (502, {}, '', 0.5),
    (403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': str(int(time.time()))}, '', 0),
    (403, {'x-ratelimit-remaining': '8'}, 'secondary rate limit', 60),
    (0, {}, '', 0.5),
])
def test_search_retries_transient_failures(status, headers, message, delay):
    delays = []

    async def sleep(value):
        delays.append(value)

    def respond(request):
        if delays:
            return httpx.Response(200, json={'items': []})
        if status == 0:
            raise httpx.ReadTimeout('private diagnostics')
        return httpx.Response(status, headers=headers, json={'message': message})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await GitHubReader(client, sleep=sleep).search_issues('retry', 1) == []

    asyncio.run(run())
    assert delays == [delay]


@pytest.mark.parametrize('status,body,expected', [(403, {}, 'github_read_failed'),
    (302, {}, 'github_read_failed'), (200, {'items': 'bad'}, 'github_invalid_response'),
    (500, {}, 'github_read_failed')])
def test_search_failure_is_bounded_and_safe(status, body, expected):
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(status, headers={'location': 'https://outside.invalid'}, json=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            reader = GitHubReader(client, 'test-identity', sleep=lambda _: asyncio.sleep(0))
            with pytest.raises(GitHubError, match=expected):
                await reader.search_issues('error', 1)

    asyncio.run(run())
    assert calls == ['api.github.com'] * (3 if status == 500 else 1)
