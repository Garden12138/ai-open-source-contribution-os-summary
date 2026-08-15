from __future__ import annotations

import pytest

from app.providers import (
    AnalysisAvailability,
    AnalysisAvailabilityMode,
    AnalysisBudget,
    AnalysisFallbackReason,
    FakeProvider,
    resolve_analysis_availability,
)


@pytest.mark.parametrize(
    ("provider_present", "budget_present", "expected_reasons"),
    (
        (
            False,
            False,
            (
                AnalysisFallbackReason.PROVIDER_NOT_CONFIGURED,
                AnalysisFallbackReason.BUDGET_NOT_CONFIGURED,
            ),
        ),
        (
            False,
            True,
            (AnalysisFallbackReason.PROVIDER_NOT_CONFIGURED,),
        ),
        (
            True,
            False,
            (AnalysisFallbackReason.BUDGET_NOT_CONFIGURED,),
        ),
    ),
)
def test_missing_provider_or_budget_is_a_deterministic_rule_only_fallback(
    provider_present: bool,
    budget_present: bool,
    expected_reasons: tuple[AnalysisFallbackReason, ...],
) -> None:
    provider = FakeProvider() if provider_present else None
    availability = resolve_analysis_availability(
        provider=provider,
        budget=AnalysisBudget() if budget_present else None,
    )

    assert availability == AnalysisAvailability(
        mode=AnalysisAvailabilityMode.RULE_ONLY_FALLBACK,
        automatic_model_invocation_enabled=False,
        rule_leaderboard_preserved=True,
        fallback_reasons=expected_reasons,
    )
    if provider is not None:
        assert provider.inspect_requests == ()
        assert provider.analyze_requests == ()


def test_provider_and_budget_make_analysis_ready_without_invoking_the_provider() -> None:
    provider = FakeProvider()

    availability = resolve_analysis_availability(
        provider=provider,
        budget=AnalysisBudget(),
    )

    assert availability == AnalysisAvailability(
        mode=AnalysisAvailabilityMode.PROVIDER_READY,
        automatic_model_invocation_enabled=True,
        rule_leaderboard_preserved=True,
        fallback_reasons=(),
    )
    assert provider.inspect_requests == ()
    assert provider.analyze_requests == ()


def test_inconsistent_availability_states_are_rejected() -> None:
    with pytest.raises(ValueError, match="inconsistent"):
        AnalysisAvailability(
            mode=AnalysisAvailabilityMode.RULE_ONLY_FALLBACK,
            automatic_model_invocation_enabled=True,
            rule_leaderboard_preserved=True,
            fallback_reasons=(AnalysisFallbackReason.PROVIDER_NOT_CONFIGURED,),
        )
    with pytest.raises(ValueError, match="requires a reason"):
        AnalysisAvailability(
            mode=AnalysisAvailabilityMode.RULE_ONLY_FALLBACK,
            automatic_model_invocation_enabled=False,
            rule_leaderboard_preserved=True,
            fallback_reasons=(),
        )
