from __future__ import annotations

import pytest

from app.providers import (
    AnalysisBudget,
    AnalysisBudgetLedger,
    BudgetExceededError,
    BudgetReason,
    BudgetStateError,
    ProviderUsage,
)


def _ledger(**overrides: int) -> AnalysisBudgetLedger:
    values = {
        "max_candidates": 30,
        "max_model_invocations": 20,
        "max_input_tokens": 100,
        "max_output_tokens": 50,
        "max_estimated_cost_microusd": 1_000,
        "max_duration_ms": 2_000,
        "max_retries": 2,
    }
    values.update(overrides)
    return AnalysisBudgetLedger(AnalysisBudget(**values))


def _started_ledger(**overrides: int) -> AnalysisBudgetLedger:
    ledger = _ledger(**overrides)
    ledger.consider_candidates(("candidate-1",))
    ledger.start_invocation(
        invocation_id="invocation-1",
        candidate_id="candidate-1",
    )
    return ledger


def test_default_budget_enforces_phase_three_candidate_and_invocation_caps() -> None:
    budget = AnalysisBudget()

    assert budget.max_candidates == 30
    assert budget.max_model_invocations == 20

    with pytest.raises(ValueError, match="hard cap of 30"):
        AnalysisBudget(max_candidates=31)
    with pytest.raises(ValueError, match="hard cap of 20"):
        AnalysisBudget(max_model_invocations=21)
    with pytest.raises(ValueError, match="max_retries"):
        AnalysisBudget(max_retries=-1)


def test_candidate_admission_is_idempotent_and_exhausts_at_the_hard_limit() -> None:
    ledger = _ledger(max_candidates=2)

    first = ledger.consider_candidates(("candidate-1", "candidate-2"))
    replay = ledger.consider_candidates(("candidate-2", "candidate-1"))

    assert first.candidates_considered == 2
    assert replay == first

    with pytest.raises(BudgetExceededError) as exc_info:
        ledger.consider_candidates(("candidate-3",))

    assert exc_info.value.reason is BudgetReason.CANDIDATE_LIMIT
    assert ledger.snapshot.candidates_considered == 2
    assert ledger.snapshot.exhausted_reason is BudgetReason.CANDIDATE_LIMIT

    with pytest.raises(BudgetExceededError):
        ledger.start_invocation(
            invocation_id="invocation-after-exhaustion",
            candidate_id="candidate-1",
        )


def test_model_invocations_are_admission_bound_idempotent_and_bounded() -> None:
    ledger = _ledger(max_model_invocations=2)
    ledger.consider_candidates(("candidate-1",))

    with pytest.raises(BudgetStateError, match="not admitted"):
        ledger.start_invocation(
            invocation_id="unknown",
            candidate_id="candidate-2",
        )

    first = ledger.start_invocation(
        invocation_id="invocation-1",
        candidate_id="candidate-1",
    )
    replay = ledger.start_invocation(
        invocation_id="invocation-1",
        candidate_id="candidate-1",
    )
    ledger.start_invocation(
        invocation_id="invocation-2",
        candidate_id="candidate-1",
    )

    assert first == replay
    assert ledger.snapshot.model_invocations == 2

    with pytest.raises(BudgetStateError, match="different candidate"):
        ledger.start_invocation(
            invocation_id="invocation-1",
            candidate_id="candidate-2",
        )

    with pytest.raises(BudgetExceededError) as exc_info:
        ledger.start_invocation(
            invocation_id="invocation-3",
            candidate_id="candidate-1",
        )

    assert exc_info.value.reason is BudgetReason.INVOCATION_LIMIT
    assert ledger.snapshot.model_invocations == 2


@pytest.mark.parametrize(
    ("budget_field", "usage", "reason"),
    (
        (
            "max_input_tokens",
            ProviderUsage(input_tokens=101),
            BudgetReason.INPUT_TOKEN_LIMIT,
        ),
        (
            "max_output_tokens",
            ProviderUsage(output_tokens=51),
            BudgetReason.OUTPUT_TOKEN_LIMIT,
        ),
        (
            "max_estimated_cost_microusd",
            ProviderUsage(estimated_cost_microusd=1_001),
            BudgetReason.COST_LIMIT,
        ),
        (
            "max_duration_ms",
            ProviderUsage(duration_ms=2_001),
            BudgetReason.DURATION_LIMIT,
        ),
    ),
)
def test_actual_usage_is_retained_and_fails_closed_when_a_limit_is_crossed(
    budget_field: str,
    usage: ProviderUsage,
    reason: BudgetReason,
) -> None:
    ledger = _started_ledger(**{budget_field: getattr(usage, budget_field[4:]) - 1})

    with pytest.raises(BudgetExceededError) as exc_info:
        ledger.record_usage(invocation_id="invocation-1", usage=usage)

    assert exc_info.value.reason is reason
    assert ledger.snapshot.exhausted_reason is reason
    assert ledger.record_usage(
        invocation_id="invocation-1",
        usage=usage,
    ) == ledger.snapshot

    with pytest.raises(BudgetStateError, match="different values"):
        ledger.record_usage(
            invocation_id="invocation-1",
            usage=ProviderUsage(),
        )


def test_exact_limits_succeed_and_allowance_decreases_from_recorded_usage() -> None:
    ledger = _started_ledger()
    usage = ProviderUsage(
        input_tokens=100,
        cached_input_tokens=25,
        output_tokens=50,
        estimated_cost_microusd=1_000,
        duration_ms=2_000,
    )

    snapshot = ledger.record_usage(invocation_id="invocation-1", usage=usage)

    assert snapshot.exhausted_reason is None
    assert snapshot.cached_input_tokens == 25
    assert ledger.allowance.remaining_input_tokens == 0
    assert ledger.allowance.remaining_output_tokens == 0
    assert ledger.allowance.remaining_estimated_cost_microusd == 0
    assert ledger.allowance.remaining_duration_ms == 0


def test_retries_are_idempotent_bound_to_an_invocation_and_bounded() -> None:
    ledger = _started_ledger(max_retries=2)

    with pytest.raises(BudgetStateError, match="unknown invocation"):
        ledger.record_retry(retry_id="retry-unknown", invocation_id="unknown")

    first = ledger.record_retry(
        retry_id="retry-1",
        invocation_id="invocation-1",
    )
    replay = ledger.record_retry(
        retry_id="retry-1",
        invocation_id="invocation-1",
    )
    ledger.record_retry(retry_id="retry-2", invocation_id="invocation-1")

    assert first == replay
    assert ledger.snapshot.retries == 2
    assert ledger.allowance.remaining_retries == 0

    with pytest.raises(BudgetStateError, match="another invocation"):
        ledger.record_retry(retry_id="retry-1", invocation_id="invocation-2")

    with pytest.raises(BudgetExceededError) as exc_info:
        ledger.record_retry(retry_id="retry-3", invocation_id="invocation-1")

    assert exc_info.value.reason is BudgetReason.RETRY_LIMIT
    assert ledger.snapshot.retries == 2


@pytest.mark.parametrize(
    "unsafe_id",
    (
        "",
        " ",
        "x" * 129,
        "github_pat_1234567890abcdef",
        "Bearer abcdefghijklmnop",
    ),
)
def test_budget_identifiers_are_bounded_and_reject_credential_like_values(
    unsafe_id: str,
) -> None:
    ledger = _ledger()

    with pytest.raises(BudgetStateError):
        ledger.consider_candidates((unsafe_id,))


def test_usage_requires_a_known_invocation() -> None:
    ledger = _ledger()

    with pytest.raises(BudgetStateError, match="unknown invocation"):
        ledger.record_usage(
            invocation_id="invocation-1",
            usage=ProviderUsage(),
        )
