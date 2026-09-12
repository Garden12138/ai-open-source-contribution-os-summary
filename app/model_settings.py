"""Versioned, non-secret model configuration shared by API and workers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4
import ipaddress
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import AuditService
from app.models import ModelConfigVersion, JobModelBinding, Job, ExecutionAttempt
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

STAGES = ("analysis", "planning", "implementation", "review")
MINIMAX_PROVIDER = "minimax"
MINIMAX_BASE_URL = "https://api.minimax.cn/v1"
MINIMAX_MODEL = "MiniMax-M3"


def model_presets() -> list[dict[str, object]]:
    """Return public, credential-free presets for the native settings UI."""
    return [
        {
            "id": "minimax-m3-cn",
            "name": "MiniMax M3（国内官方）",
            "provider": MINIMAX_PROVIDER,
            "base_url": MINIMAX_BASE_URL,
            "model": MINIMAX_MODEL,
            "profile": {
                "max_tokens": 16_384,
                "timeout_seconds": 300,
                "temperature": None,
                "reasoning_effort": None,
                "structured_output": "tools",
            },
        }
    ]


class ModelSettingsError(ValueError):
    pass


def validate_base_url(value: str) -> str:
    try:
        url = urlsplit(value)
        port = url.port
        host = (url.hostname or "").lower()
        if (
            url.scheme != "https"
            or not host
            or url.username
            or url.password
            or url.query
            or url.fragment
            or port not in (None, 443)
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or host.endswith((".localhost", ".local", ".internal"))
            or host == "localhost"
            or "." not in host
            or ".." in url.path
            or "\\" in value
            or "%" in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            raise ValueError()  # Use DNS names with verified TLS, never literal IPs.
    except ValueError:
        raise ModelSettingsError(
            "模型地址必须是公共 HTTPS 域名，不能包含凭证、查询参数或非标准端口"
        ) from None
    return value.rstrip("/")


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    provider: Literal["openai_compatible", "nvidia_nim", "minimax"] = (
        "openai_compatible"
    )
    base_url: str = Field(min_length=1, max_length=500)
    credential_ref: str | None = Field(
        default=None, pattern=r"^(?:cred-[a-f0-9]{32}|env-nvidia)$"
    )

    @model_validator(mode="after")
    def validate_connection(self):
        self.base_url = validate_base_url(self.base_url)
        if (
            self.provider == "nvidia_nim"
            and self.base_url != "https://integrate.api.nvidia.com/v1"
        ):
            raise ValueError("NVIDIA 连接必须使用官方地址")
        if self.provider == MINIMAX_PROVIDER and self.base_url != MINIMAX_BASE_URL:
            raise ValueError("MiniMax 国内连接必须使用官方地址")
        if self.provider == MINIMAX_PROVIDER and (
            self.credential_ref is None or self.credential_ref == "env-nvidia"
        ):
            raise ValueError("MiniMax 国内连接必须使用独立保存的 MiniMax API Key")
        ensure_no_sensitive_data(self.model_dump(), context="model connection")
        return self


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    connection_id: str
    model: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:@/-]+$")
    max_tokens: int = Field(default=8192, ge=256, le=16384)
    timeout_seconds: int = Field(default=180, ge=10, le=300)
    temperature: float | None = Field(default=None, ge=0, le=1)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    structured_output: Literal["tools", "json_schema"] = "tools"

    @model_validator(mode="after")
    def safe(self):
        ensure_no_sensitive_data(self.model_dump(), context="model profile")
        return self


class DefaultsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: str | None = None
    analysis: str | None = None
    planning: str | None = None
    implementation: str | None = None
    review: str | None = None


class ModelSettingsService:
    def __init__(self, session: Session):
        self.session = session

    def latest(self, scope: str) -> ModelConfigVersion | None:
        row = self.session.scalar(
            select(ModelConfigVersion)
            .where(ModelConfigVersion.scope == scope)
            .order_by(ModelConfigVersion.sequence.desc())
            .limit(1)
        )
        return self.verify(row) if row else None

    def get(self, identifier: str, kind: str | None = None) -> ModelConfigVersion:
        row = self.session.get(ModelConfigVersion, identifier)
        if row is None or (kind and not row.scope.startswith(kind + ":")):
            raise ModelSettingsError("模型配置不存在")
        return self.verify(row)

    @staticmethod
    def verify(row: ModelConfigVersion) -> ModelConfigVersion:
        expected = content_hash(
            {
                "scope": row.scope,
                "sequence": row.sequence,
                "payload": row.payload,
                "previous_hash": row.previous_hash,
            }
        )
        if expected != row.record_hash:
            raise ModelSettingsError("模型配置校验失败")
        return row

    def append(
        self,
        scope: str,
        payload: dict,
        expected_hash: str | None,
        *,
        commit: bool = True,
    ) -> ModelConfigVersion:
        ensure_no_sensitive_data(payload, context="model configuration")
        previous = self.latest(scope)
        if (previous.record_hash if previous else None) != expected_hash:
            raise ModelSettingsError("设置已在其他页面更新，请重新载入")
        sequence = previous.sequence + 1 if previous else 1
        record_hash = content_hash(
            {
                "scope": scope,
                "sequence": sequence,
                "payload": payload,
                "previous_hash": expected_hash,
            }
        )
        row = ModelConfigVersion(
            id=str(uuid4()),
            scope=scope,
            sequence=sequence,
            payload=payload,
            previous_hash=expected_hash,
            record_hash=record_hash,
            created_at=datetime.now(timezone.utc),
        )
        self.session.add(row)
        self.session.flush()
        AuditService(self.session).prepare(
            event_type="model_configuration_saved",
            actor_type="local_user",
            actor_id="local-user",
            correlation_id=row.id,
            payload={"config_id": row.id, "scope": scope, "record_hash": record_hash},
        )
        if commit:
            self.session.commit()
        return row

    def list_kind(self, kind: str) -> list[ModelConfigVersion]:
        rows = self.session.scalars(
            select(ModelConfigVersion)
            .where(ModelConfigVersion.scope.like(kind + ":%"))
            .order_by(ModelConfigVersion.sequence)
        )
        return list({row.scope: self.verify(row) for row in rows}.values())

    def is_redundant_task_selection(self, task_id: str, payload: dict) -> bool:
        """Prove a historical model_changed event only repeated the exact profile IDs."""
        scope = "task:" + task_id
        row = self.session.scalar(select(ModelConfigVersion).where(
            ModelConfigVersion.scope == scope,
            ModelConfigVersion.record_hash == payload.get("binding_hash"),
        ))
        if row is None or set(row.payload) != set(STAGES):
            return False
        self.verify(row)
        if payload.get("profiles") != row.payload:
            return False
        previous = self.session.scalar(select(ModelConfigVersion).where(
            ModelConfigVersion.scope == scope,
            ModelConfigVersion.sequence == row.sequence - 1,
        ))
        if previous is None:
            return False
        self.verify(previous)
        return row.previous_hash == previous.record_hash and row.payload == previous.payload

    def defaults(self) -> dict[str, str | None]:
        row = self.latest("defaults")
        value = row.payload if row else {}
        return {stage: value.get(stage) or value.get("default") for stage in STAGES}

    def task_profiles(
        self, task_id: str, *, bind: bool = False
    ) -> dict[str, str | None]:
        row = self.latest("task:" + task_id)
        if row:
            return row.payload
        values = self.defaults()
        if bind and any(values.values()):
            self.append("task:" + task_id, values, None, commit=False)
        return values

    def profile(self, identifier: str) -> dict:
        row = self.get(identifier, "profile")
        connection = self.get(row.payload["connection_id"], "connection")
        return {
            **row.payload,
            "id": row.id,
            "record_hash": row.record_hash,
            "connection": {
                **connection.payload,
                "id": connection.id,
                "record_hash": connection.record_hash,
            },
        }

    def identity(self, identifier: str):
        from app.providers.contracts import ProviderIdentity

        profile = self.profile(identifier)
        return ProviderIdentity(
            provider=profile["connection"]["provider"],
            adapter_version="studio-model-v1",
            model=profile["model"],
            model_version=profile["record_hash"],
        )

    def bootstrap(self, settings) -> None:
        if self.latest("defaults") is not None:
            return
        real = [
            stage
            for stage in STAGES
            if getattr(
                settings,
                ("implementation" if stage == "planning" else stage) + "_provider",
            )
            == "nvidia_nim"
        ]
        if not real:
            return
        connection = self.append(
            "connection:environment-nvidia",
            ConnectionInput(
                name="NVIDIA · 部署配置",
                provider="nvidia_nim",
                base_url="https://integrate.api.nvidia.com/v1",
                credential_ref="env-nvidia",
            ).model_dump(),
            None,
            commit=False,
        )
        values = {stage: None for stage in STAGES}
        for stage in real:
            model = getattr(
                settings,
                ("implementation" if stage == "planning" else stage) + "_model",
            )
            profile = self.append(
                "profile:environment-" + stage,
                ProfileInput(
                    name=model,
                    connection_id=connection.id,
                    model=model,
                    max_tokens=16384 if stage != "analysis" else 8192,
                    timeout_seconds=300,
                ).model_dump(),
                None,
                commit=False,
            )
            values[stage] = profile.id
        self.append("defaults", {"default": values.get("planning"), **values}, None)


def bind_job_model(session: Session, job: Job) -> None:
    """Freeze at enqueue, inside the same transaction as the Job."""
    stage = {
        "provider_analysis": "analysis",
        "planning_turn": "planning",
        "coding_turn": "implementation",
        "change_set_proposal": "implementation",
        "provider_review": "review",
        "model_connection_test": "analysis",
        "model_list": "analysis",
    }.get(job.kind)
    if not stage:
        return
    service = ModelSettingsService(session)
    profile_id = job.payload.get("model_profile_id")
    expected = job.payload.get("expected_provider", {})
    if not profile_id and expected.get("adapter_version") == "studio-model-v1":
        row = session.scalar(
            select(ModelConfigVersion).where(
                ModelConfigVersion.record_hash == expected.get("model_version")
            )
        )
        profile_id = row.id if row else None
    task_id = job.payload.get("task_id")
    if not task_id and job.payload.get("execution_attempt_id"):
        attempt = session.get(ExecutionAttempt, job.payload["execution_attempt_id"])
        task_id = attempt.task_id if attempt else None
    if not profile_id and task_id:
        binding = service.latest("task:" + task_id)
        profile_id = binding.payload.get(stage) if binding else None
    if profile_id:
        profile = service.get(profile_id, "profile")
        session.add(
            JobModelBinding(
                job_id=job.id, profile_id=profile.id, profile_hash=profile.record_hash
            )
        )


def job_profile(session: Session, job_id: str) -> dict | None:
    binding = session.get(JobModelBinding, job_id)
    if not binding:
        return None
    profile = ModelSettingsService(session).profile(binding.profile_id)
    if profile["record_hash"] != binding.profile_hash:
        raise ModelSettingsError("任务模型绑定校验失败")
    return profile


def analysis_runtime(request, session):
    from app.providers.codex_cli import CodexCLIAdapter
    from app.providers.budgets import AnalysisBudget
    from app.providers.studio import runner_for_profile, profile_identity

    service = ModelSettingsService(session)
    identifier = service.defaults().get("analysis")
    if not identifier:
        return request.app.state.analysis_provider, request.app.state.analysis_budget
    profile = service.profile(identifier)
    return (
        CodexCLIAdapter(
            runner_for_profile(request.app.state.settings, profile),
            identity=profile_identity(profile),
        ),
        AnalysisBudget(max_duration_ms=840 * 5000),
    )


def task_identity(session, settings, task_id: str, stage: str):
    service = ModelSettingsService(session)
    binding = service.latest("task:" + task_id)
    identifier = binding.payload.get(stage) if binding else None
    if identifier:
        return service.identity(identifier)
    from app.contribution_workflow import model_identity

    return model_identity(getattr(settings, stage + "_model"))


def execution_model_configured(
    session, settings, execution_id: str, stage: str
) -> bool:
    attempt = session.get(ExecutionAttempt, execution_id)
    if attempt:
        binding = ModelSettingsService(session).latest("task:" + attempt.task_id)
        if binding and binding.payload.get(stage):
            return True
    return getattr(settings, stage + "_provider") == "nvidia_nim"


def configure_bound_worker(
    worker, job_id: str, settings, *, analysis=False, planning=False
) -> None:
    from app.providers.studio import runner_for_profile, profile_identity
    from app.providers.codex_cli import CodexCLIAdapter

    if not hasattr(worker, "_legacy_model_runtime"):
        worker._legacy_model_runtime = (
            (worker.provider,)
            if analysis
            else (
                (worker.provider, worker.identity)
                if planning
                else (worker.runner, worker.identity)
            )
        )
    if analysis:
        worker.provider = worker._legacy_model_runtime[0]
    elif planning:
        worker.provider, worker.identity = worker._legacy_model_runtime
    else:
        worker.runner, worker.identity = worker._legacy_model_runtime
    with worker.database.session() as session:
        profile = job_profile(session, job_id)
    if not profile:
        return
    runner = runner_for_profile(settings, profile)
    identity = profile_identity(profile)
    if analysis:
        worker.provider = CodexCLIAdapter(runner, identity=identity)
    elif planning:
        worker.provider, worker.identity = runner, identity
    else:
        worker.runner, worker.identity = runner, identity
