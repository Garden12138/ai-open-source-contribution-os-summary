"""Public GitHub issue snapshots read by the host backend."""

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from contribos.config import Settings
from contribos.discovery.domain import has_bounty_signal
from contribos.github import GitHubClient, GitHubError, GitHubReader


class IssueError(GitHubError):
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


async def fetch_issue(ref: IssueRef, client: httpx.AsyncClient | None = None, *,
                      github: GitHubClient | None = None) -> dict[str, Any]:
    try:
        if client is not None:
            return await _snapshot(ref, GitHubReader(client))
        async with (github or GitHubClient(Settings())).read() as reader:
            return await _snapshot(ref, reader)
    except IssueError:
        raise
    except GitHubError as error:
        raise IssueError(error.code, error.message) from None


def linked_pull_requests(timeline: list[dict]) -> list[dict]:
    linked = []
    for event in timeline:
        source = (event.get('source') or {}).get('issue') or {}
        if event.get('event') == 'cross-referenced' and 'pull_request' in source:
            linked.append({key: source.get(key) for key in ('number', 'title', 'html_url', 'state', 'pull_request')})
    return linked


async def _snapshot(ref: IssueRef, reader: GitHubReader) -> dict[str, Any]:
    async def pages(path: str) -> list[dict[str, Any]]:
        items = []
        page = 1
        while True:
            batch = await reader.get(f'{path}?per_page=100&page={page}')
            if not isinstance(batch, list):
                raise IssueError('github_invalid_response')
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1
    prefix = f'/repos/{ref.repo}'
    try:
        issue = await reader.get(f'{prefix}/issues/{ref.number}')
        if not isinstance(issue, dict) or 'pull_request' in issue:
            raise IssueError('url_is_not_an_issue')
        repo = await reader.get(prefix)
        comments = await pages(f'{prefix}/issues/{ref.number}/comments')
        timeline = await pages(f'{prefix}/issues/{ref.number}/timeline')
        linked = linked_pull_requests(timeline)
        return {'title': issue.get('title', ''), 'body': issue.get('body') or '', 'state': issue.get('state'),
                'language': repo.get('language'), 'has_bounty': has_bounty_signal(' '.join([
                    issue.get('title', ''), issue.get('body') or '', *(l['name'] for l in issue.get('labels', []))])),
                'assignees': [a.get('login') for a in issue.get('assignees', [])],
                'comments': [{'body': c.get('body') or ''} for c in comments],
                'linked_prs': linked, 'default_branch': repo['default_branch']}
    except (KeyError, TypeError, AttributeError):
        raise IssueError('github_invalid_response') from None
