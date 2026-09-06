from __future__ import annotations

import asyncio
import os
import random
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx

from app.config import Settings
from app.provenance import canonical_json


MAX_REPOSITORY_ARCHIVE_BYTES = 512 * 1024 * 1024
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_BASE_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ARCHIVE_CREDENTIAL_QUERY = frozenset(
    {"access_token", "token", "authorization"}
)
_GZIP_MAGIC = b"\x1f\x8b"


class GitHubAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class ETagCache(Protocol):
    def get(self, key: str) -> tuple[str, dict[str, Any]] | None: ...

    def put(self, key: str, etag: str, payload: dict[str, Any]) -> None: ...


class MemoryETagCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[str, dict[str, Any]]] = {}

    def get(self, key: str) -> tuple[str, dict[str, Any]] | None:
        return self._items.get(key)

    def put(self, key: str, etag: str, payload: dict[str, Any]) -> None:
        self._items[key] = (etag, payload)


Sleep = Callable[[float], Awaitable[None]]
RandomValue = Callable[[], float]


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        etag_cache: ETagCache | None = None,
        sleep: Sleep = asyncio.sleep,
        random_value: RandomValue = random.random,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._etag_cache = etag_cache or MemoryETagCache()
        self._sleep = sleep
        self._random_value = random_value
        self._client: httpx.AsyncClient | None = None
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: datetime | None = None
        self._base_origin = self._validate_api_url(settings.github_api_url)

    async def __aenter__(self) -> "GitHubClient":
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "contribos/0.2",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        self._client = httpx.AsyncClient(
            base_url=self.settings.github_api_url,
            headers=headers,
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("GitHubClient must be used as an async context manager")
        return self._client

    def _validate_api_url(self, value: str) -> tuple[str, str, int]:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        allowed = {item.lower() for item in self.settings.github_allowed_hosts}
        if parsed.scheme.lower() != "https":
            raise ValueError("GITHUB_API_URL must use HTTPS")
        if not host or host not in allowed:
            raise ValueError(
                "GITHUB_API_URL host is not allowed by GITHUB_ALLOWED_HOSTS"
            )
        if parsed.username or parsed.password:
            raise ValueError("GITHUB_API_URL must not contain credentials")
        port = parsed.port or 443
        return "https", host, port

    def _capture_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining and remaining.isdigit():
            self.rate_limit_remaining = int(remaining)
        if reset and reset.isdigit():
            self.rate_limit_reset_at = datetime.fromtimestamp(
                int(reset), tz=timezone.utc
            )

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any]:
        cache_key = self._cache_key(path, params)
        cached = self._etag_cache.get(cache_key)
        request_headers = {"If-None-Match": cached[0]} if cached else {}
        current_url = path
        current_params = params

        for attempt in range(self.settings.github_max_retries + 1):
            try:
                response = await self.client.get(
                    current_url,
                    params=current_params,
                    headers=request_headers,
                )
            except httpx.HTTPError as exc:
                if attempt >= self.settings.github_max_retries:
                    raise GitHubAPIError(
                        "GitHub request failed after bounded retries"
                    ) from exc
                await self._sleep(self._backoff_seconds(attempt))
                continue

            self._capture_rate_limit(response)
            if response.status_code == 304:
                if cached is None:
                    raise GitHubAPIError(
                        "GitHub API returned 304 without cached content",
                        304,
                    )
                return cached[1]
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise GitHubAPIError(
                        "GitHub API returned a redirect without a location",
                        response.status_code,
                    )
                redirected = urljoin(str(response.request.url), location)
                if self._origin(redirected) != self._base_origin:
                    raise GitHubAPIError(
                        "GitHub API cross-origin redirect rejected",
                        response.status_code,
                    )
                current_url = redirected
                current_params = None
                continue

            if allow_not_found and response.status_code == 404:
                return {}

            retry_after = self._retry_delay(response, attempt)
            if retry_after is not None:
                if attempt >= self.settings.github_max_retries:
                    raise GitHubAPIError(
                        f"GitHub API retry limit reached for HTTP "
                        f"{response.status_code}",
                        response.status_code,
                        retry_after_seconds=retry_after,
                    )
                await self._sleep(retry_after)
                continue

            if response.is_error:
                raise GitHubAPIError(
                    f"GitHub API returned HTTP {response.status_code}",
                    response.status_code,
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise GitHubAPIError(
                    "GitHub API returned invalid JSON",
                    response.status_code,
                ) from exc
            if not isinstance(payload, dict):
                raise GitHubAPIError("GitHub API returned an unexpected payload")
            etag = response.headers.get("etag")
            if etag:
                self._etag_cache.put(cache_key, etag, payload)
            return payload

        raise GitHubAPIError("GitHub request retry loop ended unexpectedly")

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        collected: list[dict[str, Any]] = []
        page = 1
        while len(collected) < limit:
            page_size = min(100, limit - len(collected))
            payload = await self._get(
                "/search/issues",
                params={
                    "q": query,
                    "sort": "updated",
                    "order": "desc",
                    "per_page": page_size,
                    "page": page,
                },
            )
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise GitHubAPIError(
                    "GitHub search response did not contain an item list"
                )
            valid_items = [item for item in items if isinstance(item, dict)]
            collected.extend(valid_items[: limit - len(collected)])
            if len(items) < page_size or not items:
                break
            page += 1
        return collected

    async def download_repository_archive(
        self,
        full_name: str,
        commit_sha: str,
        destination: Path,
        *,
        max_bytes: int = MAX_REPOSITORY_ARCHIVE_BYTES,
    ) -> int:
        if not _REPOSITORY.fullmatch(full_name):
            raise ValueError("Repository full name is invalid")
        if not _BASE_SHA.fullmatch(commit_sha):
            raise ValueError("Repository commit SHA is invalid")
        if max_bytes < 1 or max_bytes > MAX_REPOSITORY_ARCHIVE_BYTES:
            raise ValueError("Repository archive size limit is invalid")
        encoded = quote(full_name, safe="/")
        path = f"/repos/{encoded}/tarball/{commit_sha}"
        last_error: GitHubAPIError | None = None
        for attempt in range(self.settings.github_max_retries + 1):
            try:
                return await self._download_archive_once(
                    path,
                    Path(destination),
                    max_bytes,
                )
            except httpx.HTTPError as exc:
                last_error = GitHubAPIError(
                    "GitHub archive request failed after bounded retries"
                )
                last_error.__cause__ = exc
                if attempt >= self.settings.github_max_retries:
                    raise last_error from exc
                await self._sleep(self._backoff_seconds(attempt))
            except GitHubAPIError as exc:
                if (
                    exc.status_code is not None
                    and 500 <= exc.status_code <= 599
                    and attempt < self.settings.github_max_retries
                ):
                    last_error = exc
                    await self._sleep(self._backoff_seconds(attempt))
                    continue
                if (
                    exc.status_code is not None
                    and 500 <= exc.status_code <= 599
                ):
                    raise GitHubAPIError(
                        f"GitHub API retry limit reached for HTTP "
                        f"{exc.status_code}",
                        exc.status_code,
                    ) from exc
                raise
        if last_error is not None:
            raise last_error
        raise GitHubAPIError("GitHub archive retry loop ended unexpectedly")

    async def _download_archive_once(
        self,
        path: str,
        destination: Path,
        max_bytes: int,
    ) -> int:
        redirected: str | None = None
        async with self.client.stream("GET", path) as response:
            self._capture_rate_limit(response)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise GitHubAPIError(
                        "GitHub API returned a redirect without a location",
                        response.status_code,
                    )
                redirected = self._validate_archive_redirect(
                    urljoin(str(response.request.url), location)
                )
            elif response.is_error:
                raise GitHubAPIError(
                    f"GitHub API returned HTTP {response.status_code}",
                    response.status_code,
                )
            else:
                return await self._write_archive_stream(
                    response,
                    destination,
                    max_bytes,
                )
        if redirected is None:
            raise GitHubAPIError("GitHub archive redirect was missing")
        return await self._download_unauthenticated(
            redirected,
            destination,
            max_bytes,
        )

    async def _download_unauthenticated(
        self,
        url: str,
        destination: Path,
        max_bytes: int,
    ) -> int:
        timeout = max(self.settings.request_timeout_seconds, 120)
        async with httpx.AsyncClient(
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "contribos/0.2",
            },
            timeout=timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise GitHubAPIError(
                        "GitHub archive download returned an unexpected redirect",
                        response.status_code,
                    )
                if response.is_error:
                    raise GitHubAPIError(
                        f"GitHub API returned HTTP {response.status_code}",
                        response.status_code,
                    )
                return await self._write_archive_stream(
                    response,
                    destination,
                    max_bytes,
                )

    def _validate_archive_redirect(self, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        allowed = {item.lower() for item in self.settings.github_archive_hosts}
        if parsed.scheme.lower() != "https":
            raise GitHubAPIError("GitHub archive redirect must use HTTPS")
        if parsed.username or parsed.password:
            raise GitHubAPIError(
                "GitHub archive redirect must not contain credentials"
            )
        if not host or host not in allowed:
            raise GitHubAPIError("GitHub archive redirect host is not allowed")
        query_names = {name.lower() for name in parse_qs(parsed.query)}
        if query_names & _ARCHIVE_CREDENTIAL_QUERY:
            raise GitHubAPIError(
                "GitHub archive redirect must not contain credentials"
            )
        return value

    @staticmethod
    async def _write_archive_stream(
        response: httpx.Response,
        destination: Path,
        max_bytes: int,
    ) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        size = 0
        first_chunk = True
        try:
            with temporary.open("wb") as stream:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    if first_chunk:
                        if not chunk.startswith(_GZIP_MAGIC):
                            raise GitHubAPIError(
                                "GitHub archive response is not a gzip stream"
                            )
                        first_chunk = False
                    size += len(chunk)
                    if size > max_bytes:
                        raise GitHubAPIError(
                            "GitHub archive exceeds the configured size limit"
                        )
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size < 1:
                raise GitHubAPIError("GitHub archive response was empty")
            os.replace(temporary, destination)
            return size
        finally:
            temporary.unlink(missing_ok=True)

    async def get_repository_bundle(self, full_name: str) -> dict[str, Any]:
        encoded = quote(full_name, safe="/")
        repository_task = self._get(f"/repos/{encoded}")
        community_task = self._get(
            f"/repos/{encoded}/community/profile", allow_not_found=True
        )
        repository_result, community_result = await asyncio.gather(
            repository_task, community_task, return_exceptions=True
        )

        if isinstance(repository_result, BaseException):
            raise repository_result
        if isinstance(community_result, BaseException):
            community_result = {}

        return {
            "repository": repository_result,
            "community": community_result,
        }

    def _retry_delay(
        self, response: httpx.Response, attempt: int
    ) -> float | None:
        remaining = response.headers.get("x-ratelimit-remaining")
        primary_rate_limited = (
            response.status_code in {403, 429} and remaining == "0"
        )
        secondary_rate_limited = self._is_secondary_rate_limit(response)
        rate_limited = (
            response.status_code == 429
            or primary_rate_limited
            or secondary_rate_limited
        )
        transient = 500 <= response.status_code <= 599
        if not rate_limited and not transient:
            return None

        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return self._bounded_delay(float(retry_after))
            except ValueError:
                pass
        reset = response.headers.get("x-ratelimit-reset")
        if primary_rate_limited and reset and reset.isdigit():
            seconds = max(
                0.0,
                int(reset) - datetime.now(timezone.utc).timestamp(),
            )
            return self._bounded_delay(seconds)
        if secondary_rate_limited or response.status_code == 429:
            # GitHub requires at least a one-minute pause when a secondary
            # limit response provides neither Retry-After nor an exhausted
            # primary-limit reset. Keep the wait bounded by local policy.
            return self._bounded_delay(
                max(60.0, self._backoff_seconds(attempt))
            )
        return self._backoff_seconds(attempt)

    @staticmethod
    def _is_secondary_rate_limit(response: httpx.Response) -> bool:
        if response.status_code not in {403, 429}:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        message = payload.get("message")
        return (
            isinstance(message, str)
            and "secondary rate limit" in message.lower()
        )

    def _backoff_seconds(self, attempt: int) -> float:
        base = max(0.0, self.settings.github_retry_base_seconds)
        jitter = base * self._random_value()
        return self._bounded_delay(base * (2**attempt) + jitter)

    def _bounded_delay(self, seconds: float) -> float:
        return max(
            0.0,
            min(seconds, max(0.0, self.settings.github_retry_max_seconds)),
        )

    @staticmethod
    def _cache_key(path: str, params: dict[str, str | int] | None) -> str:
        return canonical_json({"path": path, "params": params or {}})

    @staticmethod
    def _origin(value: str) -> tuple[str, str, int]:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, (parsed.hostname or "").lower(), port
