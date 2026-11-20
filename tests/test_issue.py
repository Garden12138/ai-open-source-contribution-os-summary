import asyncio

import httpx
import pytest

from contribos.tasks.issue import IssueError, fetch_issue, parse_issue_url


@pytest.mark.parametrize('url', ['http://github.com/o/r/issues/1', 'https://evil.example/o/r/issues/1',
    'https://user@github.com/o/r/issues/1', 'https://github.com/o/r/pull/1',
    'https://github.com/o/r/issues/0', 'https://github.com/../r/issues/1', 'https://github.com/o/r/issues/1?token=x'])
def test_issue_url_validation(url):
    with pytest.raises(IssueError, match='invalid_issue_url'):
        parse_issue_url(url)


def test_public_snapshot_pagination_and_linked_prs():
    seen = []
    def respond(request):
        assert 'authorization' not in request.headers
        seen.append(str(request.url))
        path = request.url.path
        if path.endswith('/comments'):
            return httpx.Response(200, json=[{'body': 'Comment'}] * (100 if request.url.params['page'] == '1' else 1))
        if path.endswith('/timeline'):
            return httpx.Response(200, json=[{'event': 'cross-referenced', 'source': {'issue': {
                'number': 2, 'title': 'Existing PR', 'state': 'open', 'pull_request': {}}}}])
        if path.endswith('/issues/1'):
            return httpx.Response(200, json={'title': 'Issue', 'body': 'Details', 'state': 'open'})
        return httpx.Response(200, json={'default_branch': 'main'})
    async def check():
        async with httpx.AsyncClient(base_url='https://api.github.com', transport=httpx.MockTransport(respond)) as client:
            result = await fetch_issue(parse_issue_url('https://github.com/owner/repo/issues/1'), client)
            assert len(result['comments']) == 101
            assert result['linked_prs'][0]['number'] == 2
            assert result['default_branch'] == 'main'
    asyncio.run(check())
    assert len(seen) == 5


@pytest.mark.parametrize('status,code', [(403, 'github_rate_limited'), (429, 'github_rate_limited'), (404, 'github_read_failed')])
def test_github_read_error_is_a_safe_code(status, code):
    async def check():
        async with httpx.AsyncClient(base_url='https://api.github.com', transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text='private diagnostics'))) as client:
            with pytest.raises(IssueError, match=code):
                await fetch_issue(parse_issue_url('https://github.com/owner/repo/issues/1'), client)
    asyncio.run(check())
