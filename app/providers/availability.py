from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.providers.budgets import AnalysisBudget
from app.providers.contracts import AnalysisProvider


class AnalysisAvailabilityMode(StrEnum):
    RULE_ONLY_FALLBACK = "rule_only_fallback"
    PROVIDER_READY = "provider_ready"


class AnalysisFallbackReason(StrEnum):
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    BUDGET_NOT_CONFIGURED = "budget_not_configured"


@dataclass(frozen=True, slots=True)
class AnalysisAvailability:
    mode: AnalysisAvailabilityMode
    automatic_model_invocation_enabled: bool
    rule_leaderboard_preserved: bool
    fallback_reasons: tuple[AnalysisFallbackReason, ...]

    def __post_init__(self) -> None:
        expected_enabled = self.mode is AnalysisAvailabilityMode.PROVIDER_READY
        if self.automatic_model_invocation_enabled is not expected_enabled:
            raise ValueError("Analysis mode and invocation state are inconsistent")
        if not self.rule_leaderboard_preserved:
            raise ValueError("Analysis availability must preserve the rule leaderboard")
        if expected_enabled and self.fallback_reasons:
            raise ValueError("Provider-ready analysis cannot have fallback reasons")
        if not expected_enabled and not self.fallback_reasons:
            raise ValueError("Rule-only fallback requires a reason")


def resolve_analysis_availability(
    *,
    provider: AnalysisProvider | None,
    budget: AnalysisBudget | None,
) -> AnalysisAvailability:
    """Return the deterministic gate used before any automatic model work.

    Missing runtime dependencies are an expected rule-only mode, not an error.
    The stable reason order is part of the API contract.
    """

    fallback_reasons: list[AnalysisFallbackReason] = []
    if provider is None:
        fallback_reasons.append(AnalysisFallbackReason.PROVIDER_NOT_CONFIGURED)
    if budget is None:
        fallback_reasons.append(AnalysisFallbackReason.BUDGET_NOT_CONFIGURED)
    if fallback_reasons:
        return AnalysisAvailability(
            mode=AnalysisAvailabilityMode.RULE_ONLY_FALLBACK,
            automatic_model_invocation_enabled=False,
            rule_leaderboard_preserved=True,
            fallback_reasons=tuple(fallback_reasons),
        )
    return AnalysisAvailability(
        mode=AnalysisAvailabilityMode.PROVIDER_READY,
        automatic_model_invocation_enabled=True,
        rule_leaderboard_preserved=True,
        fallback_reasons=(),
    )
