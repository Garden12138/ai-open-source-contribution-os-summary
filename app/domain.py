from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


_BOUNTY_AMOUNT_PATTERNS = (
    re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE),
    re.compile(r"\bUSD\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.IGNORECASE),
    re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*USD\b", re.IGNORECASE),
)
_BOUNTY_WORD_PATTERN = re.compile(r"\b(?:bounty|reward|paid)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    full_name: str
    description: str
    language: str | None
    license_spdx: str | None
    stars: int
    forks: int
    archived: bool
    disabled: bool
    topics: tuple[str, ...]
    pushed_at: datetime | None
    has_contributing_guide: bool
    health_percentage: int | None
    sync_error: str | None = None


@dataclass(frozen=True, slots=True)
class IssueFacts:
    github_issue_id: int
    number: int
    title: str
    body: str
    html_url: str
    state: str
    labels: tuple[str, ...]
    comments_count: int
    assignees_count: int
    author_association: str | None
    created_at: datetime
    updated_at: datetime
    source_queries: tuple[str, ...]
    repository: RepositoryFacts

    @property
    def searchable_text(self) -> str:
        return " ".join((self.title, self.body, *self.labels)).lower()

    @property
    def bounty_amount_usd(self) -> float | None:
        for pattern in _BOUNTY_AMOUNT_PATTERNS:
            match = pattern.search(self.searchable_text)
            if match:
                return float(match.group(1).replace(",", ""))
        return None

    @property
    def has_bounty_signal(self) -> bool:
        text = self.searchable_text
        return bool(_BOUNTY_WORD_PATTERN.search(text)) or any(
            word in text for word in ("赏金", "奖金")
        )


@dataclass(frozen=True, slots=True)
class FilterDecision:
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoreResult:
    total: float
    components: dict[str, float]
    risk_penalty: float
    risk_reasons: tuple[str, ...]
    has_bounty: bool
    bounty_amount_usd: float | None
    is_strategic: bool
    is_tech_match: bool


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    opportunity_id: int
    total_score: float
    impact_score: float
    has_bounty: bool
    is_strategic: bool
    is_tech_match: bool


@dataclass(frozen=True, slots=True)
class Selection:
    opportunity_id: int
    selection_reason: str
    total_score: float
