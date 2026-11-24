"""GitHub mutations available only to the Publisher after confirmation."""

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from contribos.github import API, API_HEADERS, GitHubCredentials
from contribos.publish.git_transfer import GitTransfer, github_url


class WriterError(RuntimeError):
    """A public error code without GitHub diagnostics."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str
    draft: bool


class WriterSession:
    def __init__(self, http: httpx.AsyncClient, transfer: GitTransfer, token: str, fork_timeout: float) -> None:
        self.http, self.transfer, self._token, self.fork_timeout = http, transfer, token, fork_timeout

    async def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
                       params: dict[str, str] | None = None, missing: bool = False) -> Any:
        try:
            response = await self.http.request(method, API + path, json=body, params=params,
                headers={**API_HEADERS, 'Authorization': 'Bearer ' + self._token}, follow_redirects=False)
            if missing and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise WriterError('github_publish_request_failed') from None

    async def identity(self) -> str:
        identity = await self._request('GET', '/user')
        login = identity.get('login') if isinstance(identity, dict) else None
        if not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', login):
            raise WriterError('github_identity_unavailable')
        return login

    @staticmethod
    def destination(repo: str, login: str) -> str:
        github_url(repo)
        owner, name = repo.split('/')
        if owner.casefold() == login.casefold():
            raise WriterError('fork_owner_is_upstream')
        fork = f'{login}/{name}'
        github_url(fork)
        return fork

    async def _fork_ready(self, upstream: str, fork: str) -> bool:
        value = await self._request('GET', f'/repos/{fork}', missing=True)
        if value is None:
            return False
        parent = value.get('parent') if isinstance(value, dict) else None
        permissions = value.get('permissions') if isinstance(value, dict) else None
        if (not isinstance(parent, dict) or not isinstance(permissions, dict) or value.get('fork') is not True
                or str(parent.get('full_name')).casefold() != upstream.casefold()
                or str(value.get('full_name')).casefold() != fork.casefold()
                or permissions.get('push') is not True):
            raise WriterError('fork_destination_conflict')
        branch = value.get('default_branch')
        return isinstance(branch, str) and bool(branch) and await self._request(
            'GET', f'/repos/{fork}/git/ref/heads/{quote(branch, safe="")}', missing=True) is not None

    async def ensure_fork(self, upstream: str, fork: str) -> str:
        github_url(upstream)
        github_url(fork)
        try:
            async with asyncio.timeout(self.fork_timeout):
                value = await self._request('GET', f'/repos/{fork}', missing=True)
                if value is None:
                    await self._request('POST', f'/repos/{upstream}/forks', body={'default_branch_only': True})
                while not await self._fork_ready(upstream, fork):
                    await asyncio.sleep(1)
        except TimeoutError:
            raise WriterError('fork_not_ready') from None
        return fork

    async def push(self, upstream: str, fork: str, branch: str, commit: str) -> str:
        return await self.transfer.push_new(upstream, fork, branch, commit, self._token)

    @staticmethod
    def _pull(repo: str, value: Any) -> PullRequest:
        number = value.get('number') if isinstance(value, dict) else None
        if type(number) is not int or not 0 < number < 2**63 or value.get('state') not in {'open', 'closed'}:
            raise WriterError('github_invalid_pull_request')
        return PullRequest(number, f'https://github.com/{repo}/pull/{number}', value['state'], value.get('draft') is True)

    async def ensure_pull(self, repo: str, fork: str, branch: str, base: str, title: str, body: str,
                          commit: str) -> PullRequest:
        github_url(repo)
        github_url(fork)
        head = f'{fork.split("/", 1)[0]}:{branch}'
        existing = await self._request('GET', f'/repos/{repo}/pulls', params={'state': 'all', 'head': head})
        if not isinstance(existing, list):
            raise WriterError('github_invalid_pull_request')
        if existing:
            value = existing[0]
        else:
            value = await self._request('POST', f'/repos/{repo}/pulls',
                body={'title': title, 'body': body, 'head': head, 'base': base, 'draft': True})
        pull = self._pull(repo, value)
        target = value.get('base')
        source = value.get('head')
        if (not isinstance(target, dict) or target.get('ref') != base or not isinstance(source, dict)
                or source.get('sha') != commit or not existing and not pull.draft):
            raise WriterError('pull_request_conflict')
        return pull


class GitHubWriter:
    def __init__(self, credentials: GitHubCredentials, transfer: GitTransfer, *,
                 transport: httpx.AsyncBaseTransport | None = None, fork_timeout: float = 60) -> None:
        self.credentials, self.transfer, self.transport, self.fork_timeout = credentials, transfer, transport, fork_timeout

    @asynccontextmanager
    async def open(self) -> AsyncIterator[WriterSession]:
        token = await self.credentials.token()
        if not token:
            raise WriterError('github_login_required')
        async with httpx.AsyncClient(timeout=20, transport=self.transport, follow_redirects=False) as http:
            yield WriterSession(http, self.transfer, token, self.fork_timeout)
