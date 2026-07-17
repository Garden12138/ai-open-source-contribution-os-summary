from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, nullable=True
    )
    full_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    html_url: Mapped[str | None] = mapped_column(String(500))
    language: Mapped[str | None] = mapped_column(String(80), index=True)
    license_spdx: Mapped[str | None] = mapped_column(String(80))
    stars: Mapped[int] = mapped_column(Integer, default=0)
    forks: Mapped[int] = mapped_column(Integer, default=0)
    open_issues: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    topics: Mapped[list[str]] = mapped_column(JSON, default=list)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    has_contributing_guide: Mapped[bool] = mapped_column(Boolean, default=False)
    health_percentage: Mapped[int | None] = mapped_column(Integer)
    sync_error: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    opportunities: Mapped[list["Opportunity"]] = relationship(
        back_populates="repository"
    )


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_issue_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    issue_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, default="")
    html_url: Mapped[str] = mapped_column(String(500), unique=True)
    state: Mapped[str] = mapped_column(String(30), default="open")
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    comments_count: Mapped[int] = mapped_column(Integer, default=0)
    assignees_count: Mapped[int] = mapped_column(Integer, default=0)
    author_association: Mapped[str | None] = mapped_column(String(40))
    source_queries: Mapped[list[str]] = mapped_column(JSON, default=list)
    issue_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    issue_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    eligible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    filter_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    score_total: Mapped[float] = mapped_column(Float, default=0, index=True)
    score_components: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    risk_penalty: Mapped[float] = mapped_column(Float, default=0)
    risk_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    has_bounty: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    bounty_amount_usd: Mapped[float | None] = mapped_column(Float)
    is_strategic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_tech_match: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    repository: Mapped[Repository] = relationship(back_populates="opportunities")
    daily_picks: Mapped[list["DailyPick"]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("repository_id", "issue_number", name="uq_repository_issue"),
        Index("ix_opportunity_eligible_score", "eligible", "score_total"),
    )


class DailyPick(Base):
    __tablename__ = "daily_picks"

    id: Mapped[int] = mapped_column(primary_key=True)
    selection_date: Mapped[date] = mapped_column(Date, index=True)
    rank: Mapped[int] = mapped_column(Integer)
    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True
    )
    selection_reason: Mapped[str] = mapped_column(String(40))
    score_snapshot: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    opportunity: Mapped[Opportunity] = relationship(back_populates="daily_picks")

    __table_args__ = (
        UniqueConstraint("selection_date", "rank", name="uq_daily_pick_rank"),
        UniqueConstraint(
            "selection_date", "opportunity_id", name="uq_daily_pick_opportunity"
        ),
    )


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    queries: Mapped[list[str]] = mapped_column(JSON, default=list)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    repository_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
