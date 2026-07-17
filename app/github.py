from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: datetime | None = None

    async def __aenter__(self) -> "GitHubClient":
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "contribos/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        self._client = httpx.AsyncClient(
            base_url=self.settings.github_api_url,
            headers=headers,
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
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
        try:
            response = await self.client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"GitHub request failed: {exc}") from exc

        self._capture_rate_limit(response)
        if allow_not_found and response.status_code == 404:
            return {}
        if response.is_error:
            message = f"GitHub API returned HTTP {response.status_code}"
            try:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("message"):
                    message = f"{message}: {payload['message']}"
            except ValueError:
                pass
            raise GitHubAPIError(message, response.status_code)

        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAPIError("GitHub API returned an unexpected payload")
        return payload

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        payload = await self._get(
            "/search/issues",
            params={
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": max(1, min(limit, 100)),
                "page": 1,
            },
        )
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise GitHubAPIError("GitHub search response did not contain an item list")
        return [item for item in items if isinstance(item, dict)]

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
