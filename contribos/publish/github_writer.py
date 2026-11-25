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
from contribos.publish.git_transfer import OID, GitTransfer, github_url
from contribos.sandbox.host import safe_text


class WriterError(RuntimeError):
    """A public error code without GitHub diagnostics."""


@dataclass(frozen=True)
class Account:
    login: str
    id: int


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    state: str
    draft: bool
    node_id: str | None = None
    upstream_id: int | None = None
    source_id: int | None = None


@dataclass(frozen=True)
class PublishedPull:
    node_id: str
    upstream_id: int
    source_id: int
    head: str
    upstream_sha: str
    draft: bool
    title: str
    body: str


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

    async def identity(self) -> Account:
        identity = await self._request('GET', '/user')
        login = identity.get('login') if isinstance(identity, dict) else None
        if (not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', login)
                or identity.get('type') != 'User' or type(identity.get('id')) is not int or identity['id'] <= 0):
            raise WriterError('github_identity_unavailable')
        return Account(login, identity['id'])

    @staticmethod
    def destination(repo: str, account: Account) -> str:
        github_url(repo)
        _, name = repo.split('/')
        return f'{account.login}/{name}'

    async def owned_repository(self, repo: str, account: Account, branch: str, default: str,
                               *, commit: str | None = None, upstream: str | None = None, existing_branch: bool = False) -> int:
        value = await self._request('GET', f'/repos/{repo}')
        if not isinstance(value, dict):
            raise WriterError('owned_destination_conflict')
        owner, permissions = value.get('owner'), value.get('permissions')
        if (repo.split('/')[0].casefold() != account.login.casefold()
                or str(value.get('full_name')).casefold() != repo.casefold()
                or not isinstance(owner, dict) or owner.get('type') != 'User'
                or str(owner.get('login')).casefold() != account.login.casefold()
                or type(owner.get('id')) is not int or owner['id'] != account.id
                or type(value.get('id')) is not int or value['id'] <= 0
                or not isinstance(permissions, dict) or permissions.get('push') is not True
                or value.get('archived') or value.get('disabled')):
            raise WriterError('owned_destination_conflict')
        if value.get('default_branch') != default:
            raise WriterError('default_branch_changed')
        if upstream and (value.get('fork') is not True or not isinstance(value.get('parent'), dict)
                         or str(value['parent'].get('full_name')).casefold() != upstream.casefold()):
            raise WriterError('fork_destination_conflict')
        if branch == default or not re.fullmatch(r'contribos/issue-[1-9][0-9]*-[0-9a-f]{12}', branch):
            raise WriterError('unsafe_publish_branch')
        encoded = quote(branch, safe='')
        if await self._request('GET', f'/repos/{repo}/rules/branches/{encoded}') != []:
            raise WriterError('unsafe_publish_branch')
        existing = await self._request('GET', f'/repos/{repo}/branches/{encoded}', missing=True)
        if existing is None and existing_branch:
            raise WriterError('remote_branch_conflict')
        if existing is not None:
            if not isinstance(existing, dict) or existing.get('protected') is not False:
                raise WriterError('unsafe_publish_branch')
            tip = existing.get('commit')
            if commit is None or not isinstance(tip, dict) or tip.get('sha') != commit:
                raise WriterError('remote_branch_conflict')
        return value['id']

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
        if upstream.casefold() == fork.casefold():
            raise WriterError('fork_destination_conflict')
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

    async def inspect_pull(self, data: dict[str, Any], account: Account, heads: set[str] | None) -> PublishedPull:
        repo, fork, branch = (data[key] for key in ('repo', 'fork', 'branch'))
        if self.destination(repo, account).casefold() != fork.casefold() or account.id != data['publisher_id']:
            raise WriterError('github_account_changed')
        value = await self._request('GET', f'/repos/{repo}/pulls/{data["number"]}')
        pull = self._pull(repo, value)
        head, base = value.get('head'), value.get('base')
        if (pull.number != data['number'] or pull.state != 'open' or not isinstance(head, dict)
                or not isinstance(base, dict) or not isinstance(head.get('repo'), dict)
                or not isinstance(base.get('repo'), dict) or head.get('ref') != branch
                or base.get('ref') != data['default_branch'] or head['repo'].get('full_name', '').casefold() != fork.casefold()
                or base['repo'].get('full_name', '').casefold() != repo.casefold()
                or base['repo'].get('default_branch') != data['default_branch']):
            raise WriterError('pull_request_conflict')
        if not OID.fullmatch(head.get('sha') or '') or heads is not None and head['sha'] not in heads:
            raise WriterError('remote_branch_conflict')
        if (not isinstance(value.get('node_id'), str) or not value['node_id']
                or not OID.fullmatch(base.get('sha') or '') or type(base['repo'].get('id')) is not int
                or not isinstance(value.get('title'), str) or not isinstance(value.get('body') or '', str)):
            raise WriterError('github_invalid_pull_request')
        owned = repo.casefold() == fork.casefold()
        source_id = await self.owned_repository(fork, account, branch, head['repo']['default_branch'],
            commit=head['sha'], upstream=None if owned else repo, existing_branch=True)
        if source_id != head['repo'].get('id') or owned and source_id != data['owned_repository_id']:
            raise WriterError('owned_destination_conflict')
        def clean(text: str) -> str:
            return safe_text(text.replace(self._token, '[REDACTED]'))

        return PublishedPull(value['node_id'], base['repo']['id'], source_id, head['sha'], base['sha'],
                             pull.draft, clean(value['title']), clean(value.get('body') or ''))

    async def push_followup(self, data: dict[str, Any]) -> str:
        return await self.transfer.push_followup(data['repo'], data['fork'], data['branch'], data['commit_sha'],
                                                 data['parent_sha'], self._token)

    async def update_text(self, repo: str, number: int, title: str | None, body: str | None) -> None:
        changes = {k: v for k, v in (('title', title), ('body', body)) if v is not None}
        await self._request('PATCH', f'/repos/{repo}/pulls/{number}', body=changes)

    async def mark_ready(self, node_id: str) -> None:
        query = ('mutation Ready($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) '
                 '{ pullRequest { id isDraft } } }')
        value = await self._request('POST', '/graphql', body={'query': query, 'variables': {'id': node_id}})
        if not isinstance(value, dict) or value.get('errors') or not value.get('data'):
            raise WriterError('github_publish_request_failed')

    @staticmethod
    def _pull(repo: str, value: Any) -> PullRequest:
        number = value.get('number') if isinstance(value, dict) else None
        if type(number) is not int or not 0 < number < 2**63 or value.get('state') not in {'open', 'closed'}:
            raise WriterError('github_invalid_pull_request')
        return PullRequest(number, f'https://github.com/{repo}/pull/{number}', value['state'], value.get('draft') is True,
                           value.get('node_id'), ((value.get('base') or {}).get('repo') or {}).get('id'),
                           ((value.get('head') or {}).get('repo') or {}).get('id'))

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
                body={'title': title, 'body': body, 'head': branch if repo.casefold() == fork.casefold() else head,
                      'base': base, 'draft': True})
        pull = self._pull(repo, value)
        target = value.get('base')
        source = value.get('head')
        if (not isinstance(target, dict) or target.get('ref') != base or not isinstance(source, dict)
                or source.get('ref') != branch or source.get('sha') != commit
                or not isinstance(target.get('repo'), dict) or str(target['repo'].get('full_name')).casefold() != repo.casefold()
                or not isinstance(source.get('repo'), dict) or str(source['repo'].get('full_name')).casefold() != fork.casefold()
                or not existing and not pull.draft):
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
