from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class RepositorySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    full_name: str
    description: str | None
    html_url: str | None
    language: str | None
    license_spdx: str | None
    stars: int
    forks: int
    archived: bool
    pushed_at: datetime | None
    has_contributing_guide: bool


class OpportunityCard(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    issue_number: int
    title: str
    html_url: str
    labels: list[str]
    comments_count: int
    score_total: float
    score_components: dict[str, float]
    risk_penalty: float
    risk_reasons: list[str]
    has_bounty: bool
    bounty_amount_usd: float | None
    is_strategic: bool
    is_tech_match: bool
    repository: RepositorySummary


class OpportunityDetail(OpportunityCard):
    body: str
    source_queries: list[str]
    issue_created_at: datetime
    issue_updated_at: datetime
    eligible: bool
    filter_reasons: list[str]


class DailyPickItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rank: int
    selection_reason: str
    score_snapshot: float
    opportunity: OpportunityCard


class DailyLeaderboardResponse(BaseModel):
    selection_date: date
    generated_at: datetime | None
    total_candidates: int
    total_eligible: int
    picks: list[DailyPickItem]


class ScanRequest(BaseModel):
    queries: list[str] | None = Field(default=None, max_length=20)
    top_n: int | None = Field(default=None, ge=1, le=50)


class ScanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    queries: list[str]
    candidate_count: int
    eligible_count: int
    selected_count: int
    repository_count: int
    error_message: str | None
    rate_limit_remaining: int | None
    rate_limit_reset_at: datetime | None
    started_at: datetime
    completed_at: datetime | None


class MetaResponse(BaseModel):
    app_name: str
    token_configured: bool
    preferred_languages: list[str]
    queries: list[str]
    daily_pick_count: int
    timezone: str


class HealthResponse(BaseModel):
    status: str
    database: str
