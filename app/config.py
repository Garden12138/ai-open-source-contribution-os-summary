from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_GITHUB_QUERIES = (
    'is:issue is:open no:assignee label:"help wanted" language:Python',
    'is:issue is:open no:assignee label:"help wanted" language:TypeScript',
    'is:issue is:open no:assignee label:"good first issue" language:Python',
    "is:issue is:open no:assignee bounty in:title,body",
)

DEFAULT_STRATEGIC_KEYWORDS = (
    "agent",
    "ai",
    "cli",
    "codex",
    "dify",
    "github",
    "llm",
    "mcp",
    "openclaw",
    "vllm",
)


def _split_env(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(item.strip() for item in value.split(";") if item.strip())


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "AI Open Source Contribution OS"
    database_url: str = "sqlite:///./data/contribos.db"
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    github_queries: tuple[str, ...] = DEFAULT_GITHUB_QUERIES
    preferred_languages: tuple[str, ...] = ("Python", "TypeScript")
    strategic_keywords: tuple[str, ...] = DEFAULT_STRATEGIC_KEYWORDS
    candidates_per_query: int = 25
    daily_pick_count: int = 10
    repository_cache_hours: int = 24
    repository_inactive_days: int = 365
    minimum_issue_body_length: int = 40
    github_concurrency: int = 5
    request_timeout_seconds: int = 20
    timezone: str = "Asia/Shanghai"

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or None
        preferred = _split_env(
            os.getenv("PREFERRED_LANGUAGES"), defaults.preferred_languages
        )
        strategic = _split_env(
            os.getenv("STRATEGIC_KEYWORDS"), defaults.strategic_keywords
        )
        return cls(
            app_name=os.getenv("APP_NAME", defaults.app_name),
            database_url=os.getenv("DATABASE_URL", defaults.database_url),
            github_token=token,
            github_api_url=os.getenv("GITHUB_API_URL", defaults.github_api_url).rstrip(
                "/"
            ),
            github_queries=_split_env(
                os.getenv("GITHUB_QUERIES"), DEFAULT_GITHUB_QUERIES
            ),
            preferred_languages=preferred,
            strategic_keywords=tuple(item.lower() for item in strategic),
            candidates_per_query=_int_env("CANDIDATES_PER_QUERY", 25),
            daily_pick_count=_int_env("DAILY_PICK_COUNT", 10),
            repository_cache_hours=_int_env("REPOSITORY_CACHE_HOURS", 24),
            repository_inactive_days=_int_env("REPOSITORY_INACTIVE_DAYS", 365),
            minimum_issue_body_length=_int_env("MINIMUM_ISSUE_BODY_LENGTH", 40),
            github_concurrency=_int_env("GITHUB_CONCURRENCY", 5),
            request_timeout_seconds=_int_env("REQUEST_TIMEOUT_SECONDS", 20),
            timezone=os.getenv("APP_TIMEZONE", defaults.timezone),
        )
