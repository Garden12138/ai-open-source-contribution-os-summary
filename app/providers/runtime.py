from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.providers.budgets import AnalysisBudget
from app.providers.contracts import AnalysisProvider
from app.providers.fake import FakeProvider
from app.sandbox_worker.specs import JobSpecSigner


SUPPORTED_ANALYSIS_PROVIDERS = frozenset({"none", "fake"})
SUPPORTED_STAGE_RUNTIMES = frozenset({"none", "fake"})


@dataclass(frozen=True, slots=True)
class StageRuntimes:
    explore: Any
    implement: Any
    verify: Any


def resolve_analysis_runtime(
    settings: Settings,
) -> tuple[AnalysisProvider | None, AnalysisBudget | None]:
    """Attach a request-safe provider identity and budget, or stay rule-only."""

    if settings.analysis_provider == "none":
        return None, None
    if settings.analysis_provider == "fake":
        return FakeProvider(), AnalysisBudget()
    raise ValueError(
        "ANALYSIS_PROVIDER must be 'none' or 'fake'; "
        "codex remains unwired in the API and worker process"
    )


def resolve_stage_runtimes(settings: Settings) -> StageRuntimes | None:
    if settings.sandbox_stage_runtime == "none":
        return None
    if settings.sandbox_stage_runtime == "fake":
        from app.sandbox_worker.fake import (
            FakeExploreRuntime,
            FakeImplementRuntime,
            FakeVerifyRuntime,
        )

        return StageRuntimes(
            explore=FakeExploreRuntime(),
            implement=FakeImplementRuntime(),
            verify=FakeVerifyRuntime(),
        )
    raise ValueError(
        "SANDBOX_STAGE_RUNTIME must be 'none' or 'fake'; "
        "Docker runtimes stay in the Sandbox Worker process"
    )


def resolve_job_spec_signer(settings: Settings) -> JobSpecSigner | None:
    key = settings.sandbox_job_spec_signing_key
    if key is None:
        return None
    return JobSpecSigner(
        key_id=settings.sandbox_job_spec_key_id,
        signing_key=key,
    )
