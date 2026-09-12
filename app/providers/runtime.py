from __future__ import annotations

from app.config import Settings
from app.providers.budgets import AnalysisBudget
from app.providers.contracts import AnalysisProvider
from app.providers.fake import FakeProvider
from app.providers.gateway import (
    GatewayTaskCredentialBroker,
    GatewayTaskTokenCodec,
)
from app.providers.nvidia_nim import (
    NVIDIA_ANALYSIS_JOB_TIMEOUT_SECONDS,
    create_nvidia_analysis_provider,
)
from app.sandbox_worker.specs import JobSpecSigner


SUPPORTED_ANALYSIS_PROVIDERS = frozenset(
    {"none", "fake", "nvidia_nim", "openai_compatible", "minimax"}
)


def resolve_analysis_runtime(
    settings: Settings,
    *,
    execution_enabled: bool = False,
) -> tuple[AnalysisProvider | None, AnalysisBudget | None]:
    """Attach a request-safe provider identity and budget, or stay rule-only."""

    if settings.analysis_provider in {"none", "openai_compatible", "minimax"}:
        # Compatible profiles are resolved from immutable database configuration.
        return None, None
    if settings.analysis_provider == "fake":
        return FakeProvider(), AnalysisBudget()
    if settings.analysis_provider == "nvidia_nim":
        broker = (
            resolve_model_gateway_broker(settings)
            if execution_enabled
            else None
        )
        return (
            create_nvidia_analysis_provider(
                model=settings.analysis_model,
                broker=broker,
            ),
            # A screening Job makes two bounded model calls (inspect and
            # analyze). The recommendation endpoint divides this five-way, so
            # preserve an 840-second per-candidate deadline rather than
            # allowing its 360-second upstream request to outlive the Job.
            AnalysisBudget(
                max_duration_ms=NVIDIA_ANALYSIS_JOB_TIMEOUT_SECONDS * 5_000,
            ),
        )
    raise ValueError(
        "ANALYSIS_PROVIDER must be 'none', 'fake', 'nvidia_nim', "
        "'openai_compatible', or 'minimax'"
    )


def resolve_model_gateway_broker(
    settings: Settings,
) -> GatewayTaskCredentialBroker:
    if settings.model_gateway_signing_key is None:
        raise ValueError(
            "MODEL_GATEWAY_SIGNING_KEY is required by provider-worker"
        )
    return GatewayTaskCredentialBroker(
        codec=GatewayTaskTokenCodec(settings.model_gateway_signing_key),
        base_url=settings.model_gateway_base_url,
        network=settings.model_gateway_network,
        service_name=settings.model_gateway_service_name,
        provider_name="nvidia_nim",
        ttl_seconds=300,
    )


def resolve_job_spec_signer(settings: Settings) -> JobSpecSigner | None:
    key = settings.sandbox_job_spec_signing_key
    if key is None:
        return None
    return JobSpecSigner(
        key_id=settings.sandbox_job_spec_key_id,
        signing_key=key,
    )
