from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    nvidia_https_proxy: str | None = None
    sandbox_job_spec_key_id: str = "local-v1"
    sandbox_job_spec_signing_key: bytes | None = None
    sandbox_stage_runtime: str = "none"

    def __post_init__(self) -> None:
        if not 0 <= self.github_max_retries <= 3:
            raise ValueError("GITHUB_MAX_RETRIES must be between 0 and 3")
        if self.github_retry_base_seconds < 0:
            raise ValueError("GITHUB_RETRY_BASE_SECONDS must be non-negative")
        if self.github_retry_max_seconds < 0:
            raise ValueError("GITHUB_RETRY_MAX_SECONDS must be non-negative")
        if self.analysis_provider not in {"none", "fake", "nvidia_nim"}:
            raise ValueError(
                "ANALYSIS_PROVIDER must be 'none', 'fake', or 'nvidia_nim'"
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
            if value not in {"none", "fake", "nvidia_nim"}:
                raise ValueError(
                    f"{name} must be 'none', 'fake', or 'nvidia_nim'"
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
            app_name=os.getenv("APP_NAME", defaults.app_name),
            database_url=os.getenv("DATABASE_URL", defaults.database_url),
            artifact_root=os.getenv("ARTIFACT_ROOT", defaults.artifact_root),
            local_access_token=os.getenv("LOCAL_ACCESS_TOKEN") or None,
            github_token=token,
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
