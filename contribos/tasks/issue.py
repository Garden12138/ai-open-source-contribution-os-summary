"""Public GitHub issue snapshots; no GitHub credentials are requested or forwarded."""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from contribos.sandbox.host import safe_text


class IssueError(RuntimeError):
    """Public, machine-readable GitHub read error."""


@dataclass(frozen=True)
class IssueRef:
    repo: str
    number: int


def parse_issue_url(url: str) -> IssueRef:
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r'/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?', parsed.path)
        if (parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com' or not match
                or parsed.query or any(p in {'.', '..'} for p in match[1].split('/'))):
            raise ValueError()
        return IssueRef(match[1], int(match[2]))
    except ValueError:
        raise IssueError('invalid_issue_url') from None


async def fetch_issue(ref: IssueRef, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    owned = client is None
    client = client or httpx.AsyncClient(base_url='https://api.github.com', timeout=30, follow_redirects=False)
    async def get(path: str) -> Any:
        try:
            response = await client.get(path, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ContribOS/2'})
            if response.status_code in {403, 429}:
                raise IssueError('github_rate_limited')
            response.raise_for_status()
            return json.loads(safe_text(response.text))
        except (httpx.HTTPError, ValueError):
            raise IssueError('github_read_failed') from None
    async def pages(path: str) -> list[dict[str, Any]]:
        items = []
        page = 1
        while True:
            batch = await get(f'{path}?per_page=100&page={page}')
            if not isinstance(batch, list):
                raise IssueError('github_invalid_response')
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1
    prefix = f'/repos/{ref.repo}'
    try:
        issue = await get(f'{prefix}/issues/{ref.number}')
        if not isinstance(issue, dict) or 'pull_request' in issue:
            raise IssueError('url_is_not_an_issue')
        repo = await get(prefix)
        comments = await pages(f'{prefix}/issues/{ref.number}/comments')
        timeline = await pages(f'{prefix}/issues/{ref.number}/timeline')
        linked = []
        for event in timeline:
            source = event.get('source', {}).get('issue', {})
            if event.get('event') == 'cross-referenced' and 'pull_request' in source:
                linked.append({key: source.get(key) for key in ('number', 'title', 'html_url', 'state', 'pull_request')})
        return {'title': issue.get('title', ''), 'body': issue.get('body') or '', 'state': issue.get('state'),
                'assignees': [a.get('login') for a in issue.get('assignees', [])],
                'comments': [{'body': c.get('body') or ''} for c in comments],
                'linked_prs': linked, 'default_branch': repo['default_branch']}
    except (KeyError, TypeError, AttributeError):
        raise IssueError('github_invalid_response') from None
    finally:
        if owned:
            await client.aclose()
