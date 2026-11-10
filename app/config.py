from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
import sys


DEFAULT_GITHUB_QUERIES = (
    'is:issue is:open no:assignee label:"help wanted" language:Python',
    'is:issue is:open no:assignee label:"help wanted" language:TypeScript',
    'is:issue is:open no:assignee label:"good first issue" language:Python',
    "is:issue is:open no:assignee bounty in:title,body",
    'is:issue is:open no:assignee typo in:title language:Python',
    'is:issue is:open no:assignee typo in:title language:TypeScript',
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


def dependency_builder_image() -> str | None:
    """Non-secret, explicitly configured capability shared by API and Workers."""
    value = os.getenv("DEPENDENCY_BUILDER_IMAGE", "").strip()
    if value and not re.fullmatch(r"sha256:[0-9a-f]{64}",value):
        raise ValueError("DEPENDENCY_BUILDER_IMAGE must be digest-pinned")
    return value or None


def environment_engine_configuration() -> dict | None:
    """Opt in only newly frozen plans; existing authorizations never migrate."""
    if not _bool_env("UPEVE_ENABLED"):
        return None
    target = os.getenv("UPEVE_TARGET_PLATFORM", "")
    if target not in {"linux/arm64", "linux/amd64"}:
        raise ValueError("UPEVE_TARGET_PLATFORM must explicitly select linux/arm64 or linux/amd64")
    return {"version": "upeve-v1", "target_platform": target}


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


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _secret_text(name: str, file_name: str) -> str | None:
    path_value = os.getenv(file_name)
    if path_value:
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{file_name} is invalid")
        return path.read_text(encoding="utf-8")
    return os.getenv(name)


def _signing_key_env() -> bytes | None:
    raw = _secret_text(
        "SANDBOX_JOB_SPEC_SIGNING_KEY",
        "SANDBOX_JOB_SPEC_SIGNING_KEY_FILE",
    )
    if raw is None or not raw.strip():
        return None
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise ValueError(
            "SANDBOX_JOB_SPEC_SIGNING_KEY must be hexadecimal"
        ) from exc
    if len(key) < 32:
        raise ValueError(
            "SANDBOX_JOB_SPEC_SIGNING_KEY must contain at least 32 bytes"
        )
    return key


def _hex_key_env(name: str) -> bytes | None:
    if name in {"MODEL_GATEWAY_SIGNING_KEY", "MODEL_GATEWAY_MANAGEMENT_KEY", "MODEL_GATEWAY_READINESS_KEY"} and os.getenv(name + "_FILE"):
        # Host LaunchAgent configuration points to private files, never embeds
        # credentials in a plist or silently falls back to inherited variables.
        from app.providers.gateway_storage import _read
        if os.getenv(name):
            raise ValueError("Gateway key must have one explicit source")
        try:
            raw = _read(Path(os.environ[name + "_FILE"])).decode()
        except (OSError, UnicodeError):
            raise ValueError("Gateway key file is unavailable") from None
    else:
        raw = _secret_text(name, f"{name}_FILE")
    if raw is None or not raw.strip():
        return None
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be hexadecimal") from exc
    if len(key) < 32:
        raise ValueError(f"{name} must contain at least 32 bytes")
    return key


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "AI Open Source Contribution OS"
    database_url: str = "sqlite:///./data/contribos.db"
    artifact_root: str = "./data/artifacts"
    local_access_token: str | None = None
    github_token: str | None = None
    github_discovery_token_configured: bool = False
    github_read_proxy_url: str | None = None
    github_read_proxy_key: bytes | None = None
    github_api_url: str = "https://api.github.com"
    github_allowed_hosts: tuple[str, ...] = ("api.github.com",)
    github_archive_hosts: tuple[str, ...] = ("codeload.github.com",)
    github_max_retries: int = 3
    github_retry_base_seconds: float = 0.5
    github_retry_max_seconds: float = 60
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
    analysis_provider: str = "none"
    analysis_model: str = "nvidia/nemotron-3.5-lightning-30b-a3b"
    implementation_provider: str = "none"
    implementation_model: str = "deepseek-ai/deepseek-v4-pro-0813"
    review_provider: str = "none"
    review_model: str = "minimaxai/minimax-m3"
    model_gateway_base_url: str = "http://contribos-model-gateway:8001/v1"
    model_gateway_network: str = "contribos-model-gateway-local"
    model_gateway_service_name: str = "contribos-model-gateway"
    model_gateway_signing_key: bytes | None = None
    model_gateway_management_key: bytes | None = None
    model_gateway_readiness_key: bytes | None = None
    model_gateway_secret_root: str = field(default_factory=lambda: str(Path.home() / "Library/Application Support/ContribOS/model-gateway") if sys.platform == "darwin" else "/data/model-secrets")
    model_gateway_store_id: str | None = None
    nvidia_https_proxy: str | None = None
    sandbox_job_spec_key_id: str = "local-v1"
    sandbox_job_spec_signing_key: bytes | None = None
    sandbox_stage_runtime: str = "none"
    agent_runtime_v2_enabled: bool = False
    workbench_runner_image: str = ""
    publisher_mode: str = "none"
    publisher_management_key: bytes | None = None
    publisher_management_url: str = "http://publisher:8002"
    publisher_secret_root: str = "/publisher-private"

    def __post_init__(self) -> None:
        if self.github_read_proxy_key is not None:
            if len(self.github_read_proxy_key) < 32 or self.github_read_proxy_key == self.publisher_management_key:
                raise ValueError("GITHUB_READ_PROXY_KEY must be independent and at least 32 bytes")
        if self.github_read_proxy_url:
            from urllib.parse import urlsplit
            target = urlsplit(self.github_read_proxy_url)
            if (target.scheme not in {"http", "https"} or target.hostname not in {"publisher", "localhost", "127.0.0.1", "::1"}
                    or target.username or target.password or target.query or target.fragment or target.path not in {"", "/"}
                    or not self.github_read_proxy_key or self.github_api_url != "https://api.github.com"):
                raise ValueError("GitHub read proxy requires an authenticated local Publisher and the official GitHub API")
        if self.publisher_management_key is not None and len(self.publisher_management_key) < 32:
            raise ValueError("PUBLISHER_MANAGEMENT_KEY must contain at least 32 bytes")
        if self.publisher_mode not in {"none", "fake", "gh"}:
            raise ValueError("PUBLISHER_MODE must be none, fake or gh")
        if self.workbench_runner_image and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.workbench_runner_image):
            raise ValueError("WORKBENCH_RUNNER_IMAGE must be a digest")
        if not 0 <= self.github_max_retries <= 3:
            raise ValueError("GITHUB_MAX_RETRIES must be between 0 and 3")
        if self.github_retry_base_seconds < 0:
            raise ValueError("GITHUB_RETRY_BASE_SECONDS must be non-negative")
        if self.github_retry_max_seconds < 0:
            raise ValueError("GITHUB_RETRY_MAX_SECONDS must be non-negative")
        if self.analysis_provider not in {
            "none", "fake", "nvidia_nim", "openai_compatible", "minimax", "deepseek"
        }:
            raise ValueError(
                "ANALYSIS_PROVIDER must be 'none', 'fake', 'nvidia_nim', "
                "'openai_compatible', 'minimax', or 'deepseek'"
            )
        for value, name in (
            (self.analysis_model, "ANALYSIS_MODEL"),
            (self.implementation_model, "IMPLEMENTATION_MODEL"),
            (self.review_model, "REVIEW_MODEL"),
        ):
            if not value.strip() or len(value) > 120:
                raise ValueError(f"{name} is invalid")
        for value, name in (
            (self.implementation_provider, "IMPLEMENTATION_PROVIDER"),
            (self.review_provider, "REVIEW_PROVIDER"),
        ):
            if value not in {
                "none", "fake", "nvidia_nim", "openai_compatible", "minimax", "deepseek"
            }:
                raise ValueError(
                    f"{name} must be 'none', 'fake', 'nvidia_nim', "
                    "'openai_compatible', 'minimax', or 'deepseek'"
                )
        if self.sandbox_stage_runtime not in {"none", "fake", "docker"}:
            raise ValueError(
                "SANDBOX_STAGE_RUNTIME must be 'none', 'fake', or 'docker'"
            )
        if (
            self.sandbox_job_spec_signing_key is not None
            and len(self.sandbox_job_spec_signing_key) < 32
        ):
            raise ValueError(
                "SANDBOX_JOB_SPEC_SIGNING_KEY must contain at least 32 bytes"
            )

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
        allowed_hosts = _split_env(
            os.getenv("GITHUB_ALLOWED_HOSTS"),
            defaults.github_allowed_hosts,
        )
        archive_hosts = _split_env(
            os.getenv("GITHUB_ARCHIVE_HOSTS"),
            defaults.github_archive_hosts,
        )
        return cls(
            agent_runtime_v2_enabled=_bool_env("AGENT_RUNTIME_V2_ENABLED"),
            workbench_runner_image=os.getenv("WORKBENCH_RUNNER_IMAGE", ""),
            publisher_mode=os.getenv("PUBLISHER_MODE", "none"),
            publisher_management_key=(bytes.fromhex(os.environ["PUBLISHER_MANAGEMENT_KEY"])
                                      if os.getenv("PUBLISHER_MANAGEMENT_KEY") else None),
            publisher_management_url=os.getenv("PUBLISHER_MANAGEMENT_URL", defaults.publisher_management_url),
            publisher_secret_root=os.getenv("PUBLISHER_SECRET_ROOT", defaults.publisher_secret_root),
            app_name=os.getenv("APP_NAME", defaults.app_name),
            database_url=os.getenv("DATABASE_URL", defaults.database_url),
            artifact_root=os.getenv("ARTIFACT_ROOT", defaults.artifact_root),
            local_access_token=os.getenv("LOCAL_ACCESS_TOKEN") or None,
            github_token=token,
            github_read_proxy_url=os.getenv("GITHUB_READ_PROXY_URL") or None,
            github_read_proxy_key=_hex_key_env("GITHUB_READ_PROXY_KEY"),
            github_discovery_token_configured=_bool_env(
                "GITHUB_DISCOVERY_TOKEN_CONFIGURED"
            ),
            github_api_url=os.getenv("GITHUB_API_URL", defaults.github_api_url).rstrip(
                "/"
            ),
            github_allowed_hosts=tuple(host.lower() for host in allowed_hosts),
            github_archive_hosts=tuple(host.lower() for host in archive_hosts),
            github_max_retries=_int_env("GITHUB_MAX_RETRIES", 3),
            github_retry_base_seconds=_float_env(
                "GITHUB_RETRY_BASE_SECONDS",
                defaults.github_retry_base_seconds,
            ),
            github_retry_max_seconds=_float_env(
                "GITHUB_RETRY_MAX_SECONDS",
                defaults.github_retry_max_seconds,
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
            analysis_provider=(
                os.getenv("ANALYSIS_PROVIDER", defaults.analysis_provider).strip()
                or defaults.analysis_provider
            ),
            analysis_model=(
                os.getenv("ANALYSIS_MODEL", defaults.analysis_model).strip()
                or defaults.analysis_model
            ),
            implementation_provider=(
                os.getenv(
                    "IMPLEMENTATION_PROVIDER",
                    defaults.implementation_provider,
                ).strip()
                or defaults.implementation_provider
            ),
            implementation_model=(
                os.getenv(
                    "IMPLEMENTATION_MODEL",
                    defaults.implementation_model,
                ).strip()
                or defaults.implementation_model
            ),
            review_provider=(
                os.getenv("REVIEW_PROVIDER", defaults.review_provider).strip()
                or defaults.review_provider
            ),
            review_model=(
                os.getenv("REVIEW_MODEL", defaults.review_model).strip()
                or defaults.review_model
            ),
            model_gateway_base_url=(
                os.getenv(
                    "MODEL_GATEWAY_BASE_URL",
                    defaults.model_gateway_base_url,
                ).strip()
                or defaults.model_gateway_base_url
            ).rstrip("/"),
            model_gateway_network=(
                os.getenv(
                    "MODEL_GATEWAY_NETWORK",
                    defaults.model_gateway_network,
                ).strip()
                or defaults.model_gateway_network
            ),
            model_gateway_service_name=(
                os.getenv(
                    "MODEL_GATEWAY_SERVICE_NAME",
                    defaults.model_gateway_service_name,
                ).strip()
                or defaults.model_gateway_service_name
            ),
            model_gateway_signing_key=_hex_key_env(
                "MODEL_GATEWAY_SIGNING_KEY"
            ),
            model_gateway_management_key=_hex_key_env("MODEL_GATEWAY_MANAGEMENT_KEY"),
            model_gateway_readiness_key=_hex_key_env("MODEL_GATEWAY_READINESS_KEY"),
            model_gateway_secret_root=os.getenv("MODEL_GATEWAY_SECRET_ROOT", defaults.model_gateway_secret_root),
            model_gateway_store_id=os.getenv("MODEL_GATEWAY_STORE_ID", "").strip() or None,
            nvidia_https_proxy=(
                os.getenv("NVIDIA_HTTPS_PROXY", "").strip() or None
            ),
            sandbox_job_spec_key_id=(
                os.getenv(
                    "SANDBOX_JOB_SPEC_KEY_ID",
                    defaults.sandbox_job_spec_key_id,
                ).strip()
                or defaults.sandbox_job_spec_key_id
            ),
            sandbox_job_spec_signing_key=_signing_key_env(),
            sandbox_stage_runtime=(
                os.getenv(
                    "SANDBOX_STAGE_RUNTIME",
                    defaults.sandbox_stage_runtime,
                ).strip()
                or defaults.sandbox_stage_runtime
            ),
        )
