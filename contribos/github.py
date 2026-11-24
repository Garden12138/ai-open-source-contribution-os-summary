"""Host-only credentials and read access to the GitHub REST API."""

import asyncio
import os
import re
import subprocess
from collections.abc import AsyncIterator
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
    def __init__(self, client: httpx.AsyncClient, token: str | None = None) -> None:
        self._client = client
        self._token = token
        self._authenticated = bool(token)

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return safe_text(value.replace(self._token, '[REDACTED]') if self._token else value)
        if isinstance(value, dict):
            return {self._clean(key): self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value

    async def get(self, path: str) -> Any:
        if not path.startswith('/') or path.startswith('//'):
            raise GitHubError('github_invalid_path')
        try:
            headers = dict(API_HEADERS)
            if self._authenticated:
                headers['Authorization'] = f'Bearer {self._token}'
            response = await self._client.get(API + path, headers=headers, follow_redirects=False)
            if self._authenticated and response.status_code == 401:
                self._authenticated = False
                response = await self._client.get(API + path, headers=API_HEADERS, follow_redirects=False)
            rate_limited = (response.status_code == 429 or response.status_code == 403
                and (not self._authenticated or response.headers.get('x-ratelimit-remaining') == '0'
                     or 'retry-after' in response.headers))
            if rate_limited:
                message = ('GitHub API rate limit reached. Try again after it resets.' if self._authenticated else
                           'Anonymous GitHub API rate limit reached. Run gh auth login or try again after it resets.')
                raise GitHubError('github_rate_limited', message)
            response.raise_for_status()
            return self._clean(response.json())
        except (httpx.HTTPError, ValueError):
            raise GitHubError('github_read_failed') from None


class GitHubClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.credentials = GitHubCredentials(settings)
        self._transport = transport

    @asynccontextmanager
    async def read(self) -> AsyncIterator[GitHubReader]:
        token = await self.credentials.token()
        async with httpx.AsyncClient(timeout=30, transport=self._transport, follow_redirects=False) as client:
            yield GitHubReader(client, token)
