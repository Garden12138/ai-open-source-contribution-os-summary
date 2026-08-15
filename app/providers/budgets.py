from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.providers.contracts import ProviderUsage
from app.security import contains_sensitive_text


class BudgetReason(StrEnum):
    CANDIDATE_LIMIT = "candidate_limit"
    INVOCATION_LIMIT = "invocation_limit"
    INPUT_TOKEN_LIMIT = "input_token_limit"
    OUTPUT_TOKEN_LIMIT = "output_token_limit"
    COST_LIMIT = "cost_limit"
    DURATION_LIMIT = "duration_limit"
    RETRY_LIMIT = "retry_limit"


class BudgetError(RuntimeError):
    pass


class BudgetStateError(BudgetError):
    pass


class BudgetExceededError(BudgetError):
    def __init__(self, reason: BudgetReason) -> None:
        super().__init__(f"Analysis budget exceeded: {reason.value}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AnalysisBudget:
    max_candidates: int = 30
    max_model_invocations: int = 20
    max_input_tokens: int = 2_000_000
    max_output_tokens: int = 200_000
    max_estimated_cost_microusd: int = 5_000_000
    max_duration_ms: int = 1_200_000
    max_retries: int = 2

    def __post_init__(self) -> None:
        for name in (
            "max_candidates",
            "max_model_invocations",
            "max_input_tokens",
            "max_output_tokens",
            "max_estimated_cost_microusd",
            "max_duration_ms",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.max_candidates > 30:
            raise ValueError("max_candidates cannot exceed the Phase 3 hard cap of 30")
        if self.max_model_invocations > 20:
            raise ValueError(
                "max_model_invocations cannot exceed the Phase 3 hard cap of 20"
            )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    candidates_considered: int
    model_invocations: int
    retries: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    estimated_cost_microusd: int
    duration_ms: int
    exhausted_reason: BudgetReason | None


@dataclass(frozen=True, slots=True)
class InvocationAllowance:
    remaining_model_invocations: int
    remaining_retries: int
    remaining_input_tokens: int
    remaining_output_tokens: int
    remaining_estimated_cost_microusd: int
    remaining_duration_ms: int


class AnalysisBudgetLedger:
    """Idempotent in-memory ledger for one analysis run.

    Durable job integration persists equivalent counters in Phase 3 task T06.
    Actual provider usage is retained even when it crosses a limit so callers
    cannot hide an over-budget invocation by rolling back accounting.
    """

    def __init__(self, budget: AnalysisBudget) -> None:
        self.budget = budget
        self._candidates: set[str] = set()
        self._invocations: dict[str, str] = {}
        self._usage: dict[str, ProviderUsage] = {}
        self._retries: dict[str, str] = {}
        self._exhausted_reason: BudgetReason | None = None

    @property
    def snapshot(self) -> BudgetSnapshot:
        input_tokens = sum(item.input_tokens for item in self._usage.values())
        cached_input_tokens = sum(
            item.cached_input_tokens for item in self._usage.values()
        )
        output_tokens = sum(item.output_tokens for item in self._usage.values())
        estimated_cost = sum(
            item.estimated_cost_microusd for item in self._usage.values()
        )
        duration = sum(item.duration_ms for item in self._usage.values())
        return BudgetSnapshot(
            candidates_considered=len(self._candidates),
            model_invocations=len(self._invocations),
            retries=len(self._retries),
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            estimated_cost_microusd=estimated_cost,
            duration_ms=duration,
            exhausted_reason=self._exhausted_reason,
        )

    @property
    def allowance(self) -> InvocationAllowance:
        current = self.snapshot
        return InvocationAllowance(
            remaining_model_invocations=max(
                0,
                self.budget.max_model_invocations - current.model_invocations,
            ),
            remaining_retries=max(
                0,
                self.budget.max_retries - current.retries,
            ),
            remaining_input_tokens=max(
                0,
                self.budget.max_input_tokens - current.input_tokens,
            ),
            remaining_output_tokens=max(
                0,
                self.budget.max_output_tokens - current.output_tokens,
            ),
            remaining_estimated_cost_microusd=max(
                0,
                self.budget.max_estimated_cost_microusd
                - current.estimated_cost_microusd,
            ),
            remaining_duration_ms=max(
                0,
                self.budget.max_duration_ms - current.duration_ms,
            ),
        )

    def consider_candidates(self, candidate_ids: tuple[str, ...]) -> BudgetSnapshot:
        normalized = tuple(
            self._normalize_id(item, name="Candidate ID") for item in candidate_ids
        )
        proposed = self._candidates | set(normalized)
        if len(proposed) > self.budget.max_candidates:
            self._exhaust(BudgetReason.CANDIDATE_LIMIT)
        self._require_active()
        self._candidates = proposed
        return self.snapshot

    def start_invocation(
        self,
        *,
        invocation_id: str,
        candidate_id: str,
    ) -> InvocationAllowance:
        invocation_id = self._normalize_id(invocation_id, name="Invocation ID")
        candidate_id = self._normalize_id(candidate_id, name="Candidate ID")
        existing_candidate = self._invocations.get(invocation_id)
        if existing_candidate is not None:
            if existing_candidate != candidate_id:
                raise BudgetStateError(
                    "Invocation ID was reused for a different candidate"
                )
            return self.allowance
        self._require_active()
        if candidate_id not in self._candidates:
            raise BudgetStateError(
                "Model invocation candidate was not admitted by this run"
            )
        if len(self._invocations) >= self.budget.max_model_invocations:
            self._exhaust(BudgetReason.INVOCATION_LIMIT)
        self._require_active()
        self._invocations[invocation_id] = candidate_id
        return self.allowance

    def record_retry(
        self,
        *,
        retry_id: str,
        invocation_id: str,
    ) -> BudgetSnapshot:
        retry_id = self._normalize_id(retry_id, name="Retry ID")
        invocation_id = self._normalize_id(invocation_id, name="Invocation ID")
        existing_invocation = self._retries.get(retry_id)
        if existing_invocation is not None:
            if existing_invocation != invocation_id:
                raise BudgetStateError("Retry ID was reused for another invocation")
            return self.snapshot
        self._require_active()
        if invocation_id not in self._invocations:
            raise BudgetStateError("Retry references an unknown invocation")
        if len(self._retries) >= self.budget.max_retries:
            self._exhaust(BudgetReason.RETRY_LIMIT)
        self._require_active()
        self._retries[retry_id] = invocation_id
        return self.snapshot

    def record_usage(
        self,
        *,
        invocation_id: str,
        usage: ProviderUsage,
    ) -> BudgetSnapshot:
        invocation_id = self._normalize_id(invocation_id, name="Invocation ID")
        if invocation_id not in self._invocations:
            raise BudgetStateError("Usage references an unknown invocation")
        existing = self._usage.get(invocation_id)
        if existing is not None:
            if existing != usage:
                raise BudgetStateError(
                    "Invocation usage was replayed with different values"
                )
            return self.snapshot
        self._usage[invocation_id] = usage
        self._check_usage_limits()
        return self.snapshot

    @staticmethod
    def _normalize_id(value: str, *, name: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise BudgetStateError(
                f"{name} must contain between 1 and 128 characters"
            )
        if contains_sensitive_text(normalized):
            raise BudgetStateError(f"{name} must not contain credential-like data")
        return normalized

    def _check_usage_limits(self) -> None:
        current = self.snapshot
        for actual, maximum, reason in (
            (
                current.input_tokens,
                self.budget.max_input_tokens,
                BudgetReason.INPUT_TOKEN_LIMIT,
            ),
            (
                current.output_tokens,
                self.budget.max_output_tokens,
                BudgetReason.OUTPUT_TOKEN_LIMIT,
            ),
            (
                current.estimated_cost_microusd,
                self.budget.max_estimated_cost_microusd,
                BudgetReason.COST_LIMIT,
            ),
            (
                current.duration_ms,
                self.budget.max_duration_ms,
                BudgetReason.DURATION_LIMIT,
            ),
        ):
            if actual > maximum:
                self._exhaust(reason)
        self._require_active()

    def _exhaust(self, reason: BudgetReason) -> None:
        if self._exhausted_reason is None:
            self._exhausted_reason = reason
        raise BudgetExceededError(self._exhausted_reason)

    def _require_active(self) -> None:
        if self._exhausted_reason is not None:
            raise BudgetExceededError(self._exhausted_reason)
