"""Host-only credentials and read access to the GitHub REST API."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx

from contribos.config import Settings
from contribos.sandbox.host import safe_text
from contribos.security import without_github_credentials

API = 'https://api.github.com'
API_HEADERS = {'Accept': 'application/vnd.github+json', 'User-Agent': 'ContribOS/2'}


class GitHubError(RuntimeError):
    def __init__(self, code: str, message: str = 'Could not read from GitHub.') -> None:
        super().__init__(code)
        self.code, self.message = code, message


class GitHubCredentials:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def token(self) -> str | None:
        configured = os.environ.get('CONTRIBOS_GITHUB_TOKEN', '').strip()
        if self._valid(configured):
            return configured
        executable = self.settings.tool('gh')
        return await asyncio.to_thread(self._from_gh, executable) if executable else None

    @staticmethod
    def _valid(value: str) -> bool:
        return bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,1024}', value))

    @classmethod
    def _from_gh(cls, executable: str) -> str | None:
        env = without_github_credentials(os.environ)
        env.pop('GH_DEBUG', None)
        try:
            result = subprocess.run([executable, 'auth', 'token', '--hostname', 'github.com'],
                env={**env, 'GH_PROMPT_DISABLED': '1'}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, encoding='ascii', timeout=5, check=False)
            value = result.stdout.strip()
            if result.returncode == 0 and cls._valid(value):
                return value
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            pass
        return None


class GitHubReader:
    def __init__(self, client: httpx.AsyncClient, token: str | None = None, *,
                 cache: dict[str, tuple[str, Any]] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._client = client
        self._token = token
        self._authenticated = bool(token)
        self._scope = hashlib.sha256(token.encode()).hexdigest() if token else ''
        self._cache = cache if cache is not None else {}
        self._sleep = sleep

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return safe_text(value.replace(self._token, '[REDACTED]') if self._token else value)
        if isinstance(value, dict):
            return {self._clean(key): self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value

    @staticmethod
    def _retry_delay(response: httpx.Response, limited: bool, attempt: int) -> float:
        try:
            if 'retry-after' in response.headers:
                return float(response.headers['retry-after'])
            if response.headers.get('x-ratelimit-remaining') == '0':
                return float(response.headers['x-ratelimit-reset']) - time.time()
        except (ValueError, KeyError):
            pass
        return 60.0 if limited else 0.5 * 2**attempt

    async def get(self, path: str, *, params: dict[str, str | int] | None = None,
                  cached: bool = False) -> Any:
        if not path.startswith('/') or path.startswith('//'):
            raise GitHubError('github_invalid_path')
        attempt = 0
        while True:
            key = json.dumps([self._scope if self._authenticated else '', path, params],
                             sort_keys=True, separators=(',', ':'))
            entry = self._cache.get(key) if cached else None
            headers = dict(API_HEADERS)
            if self._authenticated:
                headers['Authorization'] = f'Bearer {self._token}'
            if entry:
                headers['If-None-Match'] = entry[0]
            try:
                response = await self._client.get(API + path, params=params, headers=headers, follow_redirects=False)
            except httpx.TransportError:
                if cached and attempt < 2:
                    await self._sleep(0.5 * 2**attempt)
                    attempt += 1
                    continue
                raise GitHubError('github_read_failed') from None
            status = response.status_code
            if status == 401 and self._authenticated:
                self._authenticated = False
                continue
            if status == 304 and entry:
                return entry[1]
            limited = status == 429 or status == 403 and (
                'retry-after' in response.headers or response.headers.get('x-ratelimit-remaining') == '0'
                or 'secondary rate limit' in response.text.lower())
            if cached and attempt < 2 and (limited or 500 <= status < 600):
                delay = self._retry_delay(response, limited, attempt)
                if delay <= 60:
                    await self._sleep(max(0, delay))
                    attempt += 1
                    continue
            if limited or status == 403 and not self._authenticated:
                message = ('GitHub API rate limit reached. Try again after it resets.' if self._authenticated else
                           'Anonymous GitHub API rate limit reached. Run gh auth login or try again after it resets.')
                raise GitHubError('github_rate_limited', message)
            if status != 200:
                raise GitHubError('github_read_failed')
            try:
                # Discovery pages can contain hundreds of nested objects per PR.
                if cached:
                    payload = await asyncio.to_thread(lambda: self._clean(response.json()))
                else:
                    payload = self._clean(response.json())
            except ValueError:
                raise GitHubError('github_invalid_response') from None
            if cached and (etag := response.headers.get('etag')):
                if key not in self._cache and len(self._cache) >= 128:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = (etag, payload)
            elif entry:
                self._cache.pop(key, None)
            return payload

    async def pages(self, path: str, key: str | None = None) -> AsyncIterator[dict[str, Any]]:
        page = 1
        while True:
            result = await self.get(path, params={'page': page, 'per_page': 100}, cached=True)
            items = result.get(key) if key and isinstance(result, dict) else result
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise GitHubError('github_invalid_response')
            for item in items:
                yield item
            if len(items) < 100:
                return
            page += 1

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        results = []
        size = min(100, limit)
        for page in range(1, (max(0, limit) + 99) // 100 + 1):
            data = await self.get('/search/issues', cached=True, params={
                'q': query, 'sort': 'updated', 'order': 'desc', 'per_page': size, 'page': page})
            if not isinstance(data, dict) or not isinstance(items := data.get('items'), list):
                raise GitHubError('github_invalid_response')
            if not all(isinstance(item, dict) for item in items) or data.get('incomplete_results'):
                raise GitHubError('github_incomplete_search')
            results.extend(items[:limit - len(results)])
            if len(items) < size:
                break
        return results

    async def pull_summary(self, repo: str, number: int) -> dict[str, Any] | None:
        """Read GitHub's effective review decision and CI rollup when authenticated."""
        if not self._authenticated:
            return None
        owner, name = repo.split('/')
        query = '''query($owner:String!, $name:String!, $number:Int!) {
          repository(owner:$owner, name:$name) {
            pullRequest(number:$number) {
              title state isDraft mergedAt createdAt headRefOid reviewDecision totalCommentsCount
              statusCheckRollup { state } repository { primaryLanguage { name } }
            }
          }
        }'''
        try:
            response = await self._client.post(API + '/graphql', follow_redirects=False,
                headers={**API_HEADERS, 'Authorization': 'Bearer ' + self._token},
                json={'query': query, 'variables': {'owner': owner, 'name': name, 'number': number}})
            if response.status_code == 401:
                self._authenticated = False
                return None
            if response.status_code in {403, 429}:
                raise GitHubError('github_rate_limited', 'GitHub refused the status query. Check access and rate limits.')
            response.raise_for_status()
            data = response.json()
            if data.get('errors'):
                if all(isinstance(error, dict) and error.get('type') == 'FORBIDDEN' for error in data['errors']):
                    return None
                raise GitHubError('github_read_failed')
            return self._clean(data['data']['repository']['pullRequest'])
        except (httpx.HTTPError, ValueError, AttributeError, KeyError, TypeError):
            raise GitHubError('github_read_failed') from None


class GitHubClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.credentials = GitHubCredentials(settings)
        self._transport = transport
        self._cache: dict[str, tuple[str, Any]] = {}

    @asynccontextmanager
    async def read(self) -> AsyncIterator[GitHubReader]:
        token = await self.credentials.token()
        async with httpx.AsyncClient(timeout=30, transport=self._transport, follow_redirects=False) as client:
            yield GitHubReader(client, token, cache=self._cache)
