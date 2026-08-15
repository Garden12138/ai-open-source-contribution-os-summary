from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
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
    snapshots: Mapped[list["OpportunitySnapshot"]] = relationship(
        back_populates="opportunity"
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
    scan_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="RESTRICT"), index=True
    )
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("opportunity_snapshots.id", ondelete="RESTRICT"), index=True
    )
    score_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("score_versions.id", ondelete="RESTRICT"), index=True
    )
    provenance_status: Mapped[str] = mapped_column(
        String(32), default="legacy_unverified", index=True
    )
    selection_reason: Mapped[str] = mapped_column(String(40))
    score_snapshot: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    opportunity: Mapped[Opportunity] = relationship(back_populates="daily_picks")
    scan_run: Mapped["ScanRun | None"] = relationship(back_populates="daily_picks")
    snapshot: Mapped["OpportunitySnapshot | None"] = relationship(
        back_populates="daily_picks"
    )
    score_version: Mapped["ScoreVersion | None"] = relationship(
        back_populates="daily_picks"
    )

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
    provenance_status: Mapped[str] = mapped_column(
        String(32), default="legacy_unverified", index=True
    )
    selection_date: Mapped[date | None] = mapped_column(Date, index=True)
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

    snapshots: Mapped[list["OpportunitySnapshot"]] = relationship(
        back_populates="scan_run"
    )
    daily_picks: Mapped[list[DailyPick]] = relationship(back_populates="scan_run")
    jobs: Mapped[list["Job"]] = relationship(back_populates="scan_run")


class OpportunitySnapshot(Base):
    __tablename__ = "opportunity_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scan_run_id: Mapped[str] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="RESTRICT"), index=True
    )
    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="RESTRICT"), index=True
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    inputs_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issue_data: Mapped[dict[str, object]] = mapped_column(JSON)
    repository_data: Mapped[dict[str, object]] = mapped_column(JSON)
    source_queries: Mapped[list[str]] = mapped_column(JSON)
    rule_config: Mapped[dict[str, object]] = mapped_column(JSON)
    filter_eligible: Mapped[bool] = mapped_column(Boolean)
    filter_reasons: Mapped[list[str]] = mapped_column(JSON)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    scan_run: Mapped[ScanRun] = relationship(back_populates="snapshots")
    opportunity: Mapped[Opportunity] = relationship(back_populates="snapshots")
    score_versions: Mapped[list["ScoreVersion"]] = relationship(
        back_populates="snapshot"
    )
    daily_picks: Mapped[list[DailyPick]] = relationship(back_populates="snapshot")
    final_score_versions: Mapped[list["FinalScoreVersion"]] = relationship(
        back_populates="snapshot"
    )

    __table_args__ = (
        UniqueConstraint(
            "scan_run_id",
            "opportunity_id",
            name="uq_snapshot_scan_opportunity",
        ),
    )


class ScoreVersion(Base):
    __tablename__ = "score_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("opportunity_snapshots.id", ondelete="RESTRICT"), index=True
    )
    algorithm_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))
    inputs_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    score_total: Mapped[float] = mapped_column(Float)
    score_components: Mapped[dict[str, float]] = mapped_column(JSON)
    risk_penalty: Mapped[float] = mapped_column(Float)
    risk_reasons: Mapped[list[str]] = mapped_column(JSON)
    has_bounty: Mapped[bool] = mapped_column(Boolean)
    bounty_amount_usd: Mapped[float | None] = mapped_column(Float)
    is_strategic: Mapped[bool] = mapped_column(Boolean)
    is_tech_match: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    snapshot: Mapped[OpportunitySnapshot] = relationship(
        back_populates="score_versions"
    )
    daily_picks: Mapped[list[DailyPick]] = relationship(
        back_populates="score_version"
    )
    final_score_versions: Mapped[list["FinalScoreVersion"]] = relationship(
        back_populates="rule_score_version"
    )

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "algorithm_version",
            name="uq_score_snapshot_algorithm",
        ),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    result_data: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    scan_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("scan_runs.id", ondelete="RESTRICT"), index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=600)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    progress_message: Mapped[str | None] = mapped_column(String(500))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    scan_run: Mapped[ScanRun | None] = relationship(back_populates="jobs")
    artifact_links: Mapped[list["JobArtifact"]] = relationship(
        back_populates="job"
    )
    provider_invocations: Mapped[list["ProviderInvocation"]] = relationship(
        back_populates="job"
    )
    analysis_version: Mapped["AnalysisVersion | None"] = relationship(
        back_populates="job"
    )
    execution_stage_run: Mapped["ExecutionStageRun | None"] = relationship(
        back_populates="job"
    )
    execution_artifact_manifest: Mapped[
        "ExecutionArtifactManifest | None"
    ] = relationship(back_populates="job")

    __table_args__ = (
        UniqueConstraint("kind", "idempotency_key", name="uq_job_kind_idempotency"),
        CheckConstraint(
            "state IN ('queued', 'leased', 'running', 'succeeded', 'failed', "
            "'cancelled', 'timed_out')",
            name="ck_job_state",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 "
            "AND attempt_count <= max_attempts",
            name="ck_job_attempts",
        ),
        CheckConstraint("timeout_seconds >= 1", name="ck_job_timeout"),
        CheckConstraint(
            "progress_current >= 0 "
            "AND (progress_total IS NULL OR progress_total >= progress_current)",
            name="ck_job_progress",
        ),
        CheckConstraint(
            "("
            "state = 'queued' "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL "
            "AND completed_at IS NULL"
            ") OR ("
            "state IN ('leased', 'running') "
            "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND completed_at IS NULL"
            ") OR ("
            "state IN ('succeeded', 'failed', 'cancelled', 'timed_out') "
            "AND lease_owner IS NULL AND lease_expires_at IS NULL "
            "AND completed_at IS NOT NULL"
            ")",
            name="ck_job_state_fields",
        ),
    )


class ProviderInvocation(Base):
    __tablename__ = "provider_invocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    request_id: Mapped[str] = mapped_column(String(128))
    correlation_id: Mapped[str] = mapped_column(String(128), index=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    provider_name: Mapped[str] = mapped_column(String(80))
    adapter_version: Mapped[str] = mapped_column(String(80))
    model_name: Mapped[str] = mapped_column(String(120))
    model_version: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[str] = mapped_column(String(128))
    output_schema_version: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    estimated_cost_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    job: Mapped[Job] = relationship(back_populates="provider_invocations")
    inspection_analysis_versions: Mapped[list["AnalysisVersion"]] = relationship(
        back_populates="inspect_invocation",
        foreign_keys="AnalysisVersion.inspect_invocation_id",
    )
    analysis_analysis_versions: Mapped[list["AnalysisVersion"]] = relationship(
        back_populates="analyze_invocation",
        foreign_keys="AnalysisVersion.analyze_invocation_id",
    )

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "attempt_number",
            "stage",
            name="uq_provider_invocation_attempt_stage",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="ck_provider_invocation_attempt",
        ),
        CheckConstraint(
            "stage IN ('inspect', 'analyze')",
            name="ck_provider_invocation_stage",
        ),
        CheckConstraint(
            "status IN ('succeeded', 'failed', 'timed_out', 'cancelled')",
            name="ck_provider_invocation_status",
        ),
        CheckConstraint(
            "input_tokens >= 0 "
            "AND cached_input_tokens >= 0 "
            "AND cached_input_tokens <= input_tokens "
            "AND output_tokens >= 0 "
            "AND estimated_cost_microusd >= 0 "
            "AND duration_ms >= 0",
            name="ck_provider_invocation_usage",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND output_hash IS NOT NULL "
            "AND error_code IS NULL) "
            "OR (status != 'succeeded' AND output_hash IS NULL "
            "AND error_code IS NOT NULL)",
            name="ck_provider_invocation_outcome",
        ),
    )


class AnalysisVersion(Base):
    __tablename__ = "analysis_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("opportunity_snapshots.id", ondelete="RESTRICT"),
        index=True,
    )
    score_version_id: Mapped[str] = mapped_column(
        ForeignKey("score_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    inspect_invocation_id: Mapped[str] = mapped_column(
        ForeignKey("provider_invocations.id", ondelete="RESTRICT"),
        index=True,
    )
    analyze_invocation_id: Mapped[str] = mapped_column(
        ForeignKey("provider_invocations.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    frozen_input_hash: Mapped[str] = mapped_column(String(64))
    snapshot_inputs_hash: Mapped[str] = mapped_column(String(64))
    score_output_hash: Mapped[str] = mapped_column(String(64))
    inspect_input_hash: Mapped[str] = mapped_column(String(64))
    inspect_output_hash: Mapped[str] = mapped_column(String(64))
    analysis_input_hash: Mapped[str] = mapped_column(String(64))
    analysis_output_hash: Mapped[str] = mapped_column(String(64))
    provider_name: Mapped[str] = mapped_column(String(80))
    adapter_version: Mapped[str] = mapped_column(String(80))
    model_name: Mapped[str] = mapped_column(String(120))
    model_version: Mapped[str] = mapped_column(String(120))
    inspect_prompt_version: Mapped[str] = mapped_column(String(128))
    inspect_policy_version: Mapped[str] = mapped_column(String(128))
    inspect_output_schema_version: Mapped[str] = mapped_column(String(128))
    analyze_prompt_version: Mapped[str] = mapped_column(String(128))
    analyze_policy_version: Mapped[str] = mapped_column(String(128))
    analyze_output_schema_version: Mapped[str] = mapped_column(String(128))
    inspection_structured_output: Mapped[dict[str, object]] = mapped_column(JSON)
    inspection_cited_evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    structured_output: Mapped[dict[str, object]] = mapped_column(JSON)
    cited_evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    input_tokens: Mapped[int] = mapped_column(BigInteger)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger)
    output_tokens: Mapped[int] = mapped_column(BigInteger)
    estimated_cost_microusd: Mapped[int] = mapped_column(BigInteger)
    duration_ms: Mapped[int] = mapped_column(BigInteger)
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    job: Mapped[Job] = relationship(back_populates="analysis_version")
    snapshot: Mapped[OpportunitySnapshot] = relationship()
    score_version: Mapped[ScoreVersion] = relationship()
    inspect_invocation: Mapped[ProviderInvocation] = relationship(
        back_populates="inspection_analysis_versions",
        foreign_keys=[inspect_invocation_id],
    )
    analyze_invocation: Mapped[ProviderInvocation] = relationship(
        back_populates="analysis_analysis_versions",
        foreign_keys=[analyze_invocation_id],
    )
    final_score_versions: Mapped[list["FinalScoreVersion"]] = relationship(
        back_populates="analysis_version"
    )
    contribution_tasks: Mapped[list["ContributionTask"]] = relationship(
        back_populates="analysis_version"
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = '1'",
            name="ck_analysis_version_schema",
        ),
        CheckConstraint(
            "input_tokens >= 0 "
            "AND cached_input_tokens >= 0 "
            "AND cached_input_tokens <= input_tokens "
            "AND output_tokens >= 0 "
            "AND estimated_cost_microusd >= 0 "
            "AND duration_ms >= 0",
            name="ck_analysis_version_usage",
        ),
    )


class FinalScoreVersion(Base):
    __tablename__ = "final_score_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("opportunity_snapshots.id", ondelete="RESTRICT"),
        index=True,
    )
    rule_score_version_id: Mapped[str] = mapped_column(
        ForeignKey("score_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    analysis_version_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    algorithm_version: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    calibration_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str] = mapped_column(String(64), unique=True)
    score_total: Mapped[float] = mapped_column(Float)
    score_components: Mapped[dict[str, float]] = mapped_column(JSON)
    risk_penalty: Mapped[float] = mapped_column(Float)
    risk_reasons: Mapped[list[str]] = mapped_column(JSON)
    rationale: Mapped[str] = mapped_column(Text)
    cited_evidence_ids: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    snapshot: Mapped[OpportunitySnapshot] = relationship(
        back_populates="final_score_versions"
    )
    rule_score_version: Mapped[ScoreVersion] = relationship(
        back_populates="final_score_versions"
    )
    analysis_version: Mapped[AnalysisVersion] = relationship(
        back_populates="final_score_versions"
    )

    __table_args__ = (
        UniqueConstraint(
            "analysis_version_id",
            "algorithm_version",
            name="uq_final_score_analysis_algorithm",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_final_score_schema",
        ),
        CheckConstraint(
            "score_total >= 0 AND score_total <= 100",
            name="ck_final_score_total",
        ),
        CheckConstraint(
            "risk_penalty >= 0",
            name="ck_final_score_risk_penalty",
        ),
    )


class ContributionTask(Base):
    __tablename__ = "contribution_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_version_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("opportunity_snapshots.id", ondelete="RESTRICT"),
        index=True,
    )
    opportunity_id: Mapped[int] = mapped_column(
        ForeignKey("opportunities.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    analysis_record_hash: Mapped[str] = mapped_column(String(64))
    analysis_output_hash: Mapped[str] = mapped_column(String(64))
    snapshot_inputs_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    analysis_version: Mapped[AnalysisVersion] = relationship(
        back_populates="contribution_tasks"
    )
    snapshot: Mapped[OpportunitySnapshot] = relationship()
    opportunity: Mapped[Opportunity] = relationship()
    state_versions: Mapped[list["ContributionTaskStateVersion"]] = relationship(
        back_populates="task"
    )
    plan_versions: Mapped[list["PlanVersion"]] = relationship(
        back_populates="task"
    )
    plan_approvals: Mapped[list["PlanApproval"]] = relationship(
        back_populates="task"
    )
    plan_conversation_entries: Mapped[list["PlanConversationEntry"]] = (
        relationship(back_populates="task")
    )
    execution_attempts: Mapped[list["ExecutionAttempt"]] = relationship(
        back_populates="task"
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = '1'",
            name="ck_contribution_task_schema",
        ),
    )


class ContributionTaskStateVersion(Base):
    __tablename__ = "contribution_task_state_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(BigInteger)
    from_state: Mapped[str | None] = mapped_column(String(40))
    to_state: Mapped[str] = mapped_column(String(40), index=True)
    reason_code: Mapped[str] = mapped_column(String(100))
    task_record_hash: Mapped[str] = mapped_column(String(64))
    previous_state_hash: Mapped[str | None] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    task: Mapped[ContributionTask] = relationship(
        back_populates="state_versions"
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "sequence",
            name="uq_contribution_task_state_sequence",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_contribution_task_state_schema",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_contribution_task_state_sequence",
        ),
        CheckConstraint(
            "to_state IN ("
            "'planning', 'plan_approved', 'executing', 'reviewing', "
            "'ready', 'draft_pr', 'changes_requested', 'merged', 'rewarded'"
            ")",
            name="ck_contribution_task_state_value",
        ),
    )


class PlanVersion(Base):
    __tablename__ = "plan_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    task_state_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    parent_record_hash: Mapped[str | None] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))
    version_number: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    task_record_hash: Mapped[str] = mapped_column(String(64))
    task_state_record_hash: Mapped[str] = mapped_column(String(64))
    goal: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON)
    files_to_inspect: Mapped[list[str]] = mapped_column(JSON)
    files_likely_to_change: Mapped[list[str]] = mapped_column(JSON)
    implementation_steps: Mapped[list[str]] = mapped_column(JSON)
    tests_to_add_or_run: Mapped[list[str]] = mapped_column(JSON)
    commands_to_run: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    risks: Mapped[list[str]] = mapped_column(JSON)
    questions_for_maintainer: Mapped[list[str]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    task: Mapped[ContributionTask] = relationship(
        back_populates="plan_versions"
    )
    task_state_version: Mapped[ContributionTaskStateVersion] = relationship()
    parent_version: Mapped["PlanVersion | None"] = relationship(
        remote_side=[id],
        back_populates="child_versions",
    )
    child_versions: Mapped[list["PlanVersion"]] = relationship(
        back_populates="parent_version",
    )
    locks: Mapped[list["PlanLock"]] = relationship(
        back_populates="plan_version"
    )
    approvals: Mapped[list["PlanApproval"]] = relationship(
        back_populates="plan_version"
    )
    conversation_entries: Mapped[list["PlanConversationEntry"]] = relationship(
        back_populates="plan_version"
    )
    execution_attempts: Mapped[list["ExecutionAttempt"]] = relationship(
        back_populates="plan_version"
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "version_number",
            name="uq_plan_version_number",
        ),
        UniqueConstraint(
            "task_id",
            "content_hash",
            name="uq_plan_version_content",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_plan_version_schema",
        ),
        CheckConstraint(
            "version_number >= 1",
            name="ck_plan_version_number",
        ),
    )


class PlanLock(Base):
    __tablename__ = "plan_locks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    task_state_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    analysis_version_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("opportunity_snapshots.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    task_record_hash: Mapped[str] = mapped_column(String(64))
    task_state_record_hash: Mapped[str] = mapped_column(String(64))
    analysis_record_hash: Mapped[str] = mapped_column(String(64))
    analysis_output_hash: Mapped[str] = mapped_column(String(64))
    snapshot_inputs_hash: Mapped[str] = mapped_column(String(64))
    provider_name: Mapped[str] = mapped_column(String(80))
    adapter_version: Mapped[str] = mapped_column(String(80))
    model_name: Mapped[str] = mapped_column(String(120))
    model_version: Mapped[str] = mapped_column(String(120))
    inspect_prompt_version: Mapped[str] = mapped_column(String(128))
    inspect_policy_version: Mapped[str] = mapped_column(String(128))
    inspect_output_schema_version: Mapped[str] = mapped_column(String(128))
    analyze_prompt_version: Mapped[str] = mapped_column(String(128))
    analyze_policy_version: Mapped[str] = mapped_column(String(128))
    analyze_output_schema_version: Mapped[str] = mapped_column(String(128))
    provider_contract_hash: Mapped[str] = mapped_column(String(64))
    plan_content_hash: Mapped[str] = mapped_column(String(64))
    plan_record_hash: Mapped[str] = mapped_column(String(64))
    lock_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    plan_version: Mapped[PlanVersion] = relationship(back_populates="locks")
    task: Mapped[ContributionTask] = relationship()
    task_state_version: Mapped[ContributionTaskStateVersion] = relationship()
    analysis_version: Mapped[AnalysisVersion] = relationship()
    snapshot: Mapped[OpportunitySnapshot] = relationship()
    approval: Mapped["PlanApproval | None"] = relationship(
        back_populates="plan_lock"
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = '1'",
            name="ck_plan_lock_schema",
        ),
        CheckConstraint(
            "(length(base_commit_sha) = 40 OR length(base_commit_sha) = 64) "
            "AND base_commit_sha NOT GLOB '*[^0-9a-f]*'",
            name="ck_plan_lock_base_sha",
        ),
    )


class PlanApproval(Base):
    __tablename__ = "plan_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_lock_id: Mapped[str] = mapped_column(
        ForeignKey("plan_locks.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    approved_state_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
        unique=True,
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(128))
    lock_hash: Mapped[str] = mapped_column(String(64))
    plan_content_hash: Mapped[str] = mapped_column(String(64))
    plan_record_hash: Mapped[str] = mapped_column(String(64))
    prior_state_record_hash: Mapped[str] = mapped_column(String(64))
    approved_state_record_hash: Mapped[str] = mapped_column(String(64))
    approval_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    plan_lock: Mapped[PlanLock] = relationship(back_populates="approval")
    plan_version: Mapped[PlanVersion] = relationship(
        back_populates="approvals"
    )
    task: Mapped[ContributionTask] = relationship(
        back_populates="plan_approvals"
    )
    approved_state_version: Mapped[ContributionTaskStateVersion] = (
        relationship()
    )
    execution_attempts: Mapped[list["ExecutionAttempt"]] = relationship(
        back_populates="plan_approval"
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = '1'",
            name="ck_plan_approval_schema",
        ),
    )


class PlanConversationEntry(Base):
    __tablename__ = "plan_conversation_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(BigInteger)
    entry_type: Mapped[str] = mapped_column(String(20), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(128))
    content: Mapped[dict[str, object]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    previous_entry_hash: Mapped[str | None] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    task: Mapped[ContributionTask] = relationship(
        back_populates="plan_conversation_entries"
    )
    plan_version: Mapped[PlanVersion | None] = relationship(
        back_populates="conversation_entries"
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "sequence",
            name="uq_plan_conversation_entry_sequence",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_plan_conversation_entry_schema",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_plan_conversation_entry_sequence",
        ),
        CheckConstraint(
            "entry_type IN ('message', 'decision')",
            name="ck_plan_conversation_entry_type",
        ),
    )


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    plan_approval_id: Mapped[str] = mapped_column(
        ForeignKey("plan_approvals.id", ondelete="RESTRICT"),
        index=True,
    )
    approved_state_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    executing_state_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
        unique=True,
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    attempt_number: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(40))
    repository_full_name: Mapped[str] = mapped_column(String(255))
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    repository_archive_hash: Mapped[str] = mapped_column(String(64))
    runner_image_digest: Mapped[str] = mapped_column(String(71))
    sandbox_policy_version: Mapped[str] = mapped_column(String(128))
    sandbox_policy_hash: Mapped[str] = mapped_column(String(64))
    task_record_hash: Mapped[str] = mapped_column(String(64))
    analysis_version_id: Mapped[str] = mapped_column(String(36))
    analysis_record_hash: Mapped[str] = mapped_column(String(64))
    analysis_output_hash: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str] = mapped_column(String(36))
    snapshot_inputs_hash: Mapped[str] = mapped_column(String(64))
    provider_contract_hash: Mapped[str] = mapped_column(String(64))
    plan_content_hash: Mapped[str] = mapped_column(String(64))
    plan_record_hash: Mapped[str] = mapped_column(String(64))
    approval_hash: Mapped[str] = mapped_column(String(64))
    approved_state_record_hash: Mapped[str] = mapped_column(String(64))
    executing_state_record_hash: Mapped[str] = mapped_column(String(64))
    observed_fingerprint_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    task: Mapped[ContributionTask] = relationship(
        back_populates="execution_attempts"
    )
    plan_version: Mapped[PlanVersion] = relationship(
        back_populates="execution_attempts"
    )
    plan_approval: Mapped[PlanApproval] = relationship(
        back_populates="execution_attempts"
    )
    approved_state_version: Mapped[ContributionTaskStateVersion] = relationship(
        foreign_keys=[approved_state_version_id]
    )
    executing_state_version: Mapped[ContributionTaskStateVersion] = relationship(
        foreign_keys=[executing_state_version_id]
    )
    stage_versions: Mapped[list["ExecutionStageVersion"]] = relationship(
        back_populates="execution_attempt"
    )
    stage_runs: Mapped[list["ExecutionStageRun"]] = relationship(
        back_populates="execution_attempt"
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_execution_attempt_number",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_execution_attempt_schema",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="ck_execution_attempt_number",
        ),
        CheckConstraint(
            "action = 'start_execution'",
            name="ck_execution_attempt_action",
        ),
        CheckConstraint(
            "(length(base_commit_sha) = 40 OR length(base_commit_sha) = 64) "
            "AND base_commit_sha NOT GLOB '*[^0-9a-f]*'",
            name="ck_execution_attempt_base_sha",
        ),
        CheckConstraint(
            "length(runner_image_digest) = 71 "
            "AND substr(runner_image_digest, 1, 7) = 'sha256:' "
            "AND substr(runner_image_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="ck_execution_attempt_image_digest",
        ),
    )


class ExecutionStageVersion(Base):
    __tablename__ = "execution_stage_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(BigInteger)
    stage: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    reason_code: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    job_spec_hash: Mapped[str | None] = mapped_column(String(64))
    input_hashes: Mapped[list[str]] = mapped_column(JSON)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    workspace_id: Mapped[str | None] = mapped_column(String(128))
    workspace_ref: Mapped[str | None] = mapped_column(String(128))
    workspace_inventory_hash: Mapped[str | None] = mapped_column(String(64))
    attempt_record_hash: Mapped[str] = mapped_column(String(64))
    previous_stage_state_hash: Mapped[str | None] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    execution_attempt: Mapped[ExecutionAttempt] = relationship(
        back_populates="stage_versions"
    )
    scheduled_run: Mapped["ExecutionStageRun | None"] = relationship(
        back_populates="pending_stage_version"
    )

    __table_args__ = (
        UniqueConstraint(
            "execution_attempt_id",
            "sequence",
            name="uq_execution_stage_sequence",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_execution_stage_schema",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_execution_stage_sequence",
        ),
        CheckConstraint(
            "stage IN ('explore', 'implement', 'verify')",
            name="ck_execution_stage_name",
        ),
        CheckConstraint(
            "status IN ("
            "'pending', 'running', 'succeeded', 'failed', "
            "'cancelled', 'timed_out'"
            ")",
            name="ck_execution_stage_status",
        ),
        CheckConstraint(
            "(workspace_id IS NULL AND workspace_ref IS NULL "
            "AND workspace_inventory_hash IS NULL) OR "
            "(workspace_id IS NOT NULL AND workspace_ref IS NOT NULL)",
            name="ck_execution_stage_workspace",
        ),
        CheckConstraint(
            "(status = 'pending' AND job_spec_hash IS NULL "
            "AND result_hash IS NULL) OR "
            "(status = 'running' AND job_spec_hash IS NOT NULL "
            "AND result_hash IS NULL) OR "
            "(status = 'succeeded' AND job_spec_hash IS NOT NULL "
            "AND result_hash IS NOT NULL) OR "
            "(status IN ('failed', 'cancelled', 'timed_out'))",
            name="ck_execution_stage_evidence",
        ),
    )


class ExecutionStageRun(Base):
    __tablename__ = "execution_stage_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    execution_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="RESTRICT"),
        index=True,
    )
    pending_stage_version_id: Mapped[str] = mapped_column(
        ForeignKey("execution_stage_versions.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(20), index=True)
    stage_run_number: Mapped[int] = mapped_column(BigInteger)
    max_stage_runs: Mapped[int] = mapped_column(BigInteger)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    job_spec_hash: Mapped[str] = mapped_column(String(64))
    input_hashes: Mapped[list[str]] = mapped_column(JSON)
    attempt_record_hash: Mapped[str] = mapped_column(String(64))
    pending_stage_record_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    job: Mapped[Job] = relationship(back_populates="execution_stage_run")
    execution_attempt: Mapped[ExecutionAttempt] = relationship(
        back_populates="stage_runs"
    )
    pending_stage_version: Mapped[ExecutionStageVersion] = relationship(
        back_populates="scheduled_run"
    )
    artifact_manifest: Mapped[
        "ExecutionArtifactManifest | None"
    ] = relationship(back_populates="execution_stage_run")

    __table_args__ = (
        UniqueConstraint(
            "execution_attempt_id",
            "stage",
            "stage_run_number",
            name="uq_execution_stage_run_number",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_execution_stage_run_schema",
        ),
        CheckConstraint(
            "stage IN ('explore', 'implement', 'verify')",
            name="ck_execution_stage_run_stage",
        ),
        CheckConstraint(
            "stage_run_number >= 1 "
            "AND max_stage_runs >= 1 "
            "AND max_stage_runs <= 3 "
            "AND stage_run_number <= max_stage_runs",
            name="ck_execution_stage_run_budget",
        ),
        CheckConstraint(
            "timeout_seconds >= 1 AND timeout_seconds <= 600",
            name="ck_execution_stage_run_timeout",
        ),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    algorithm: Mapped[str] = mapped_column(String(16), default="sha256")
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    job_links: Mapped[list["JobArtifact"]] = relationship(
        back_populates="artifact"
    )
    execution_entries: Mapped[list["ExecutionArtifactEntry"]] = relationship(
        back_populates="artifact"
    )

    __table_args__ = (
        CheckConstraint("algorithm = 'sha256'", name="ck_artifact_algorithm"),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_size"),
    )


class JobArtifact(Base):
    __tablename__ = "job_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), index=True
    )
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    job: Mapped[Job] = relationship(back_populates="artifact_links")
    artifact: Mapped[Artifact] = relationship(back_populates="job_links")

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "artifact_id",
            "role",
            name="uq_job_artifact_role",
        ),
    )


class ExecutionArtifactManifest(Base):
    __tablename__ = "execution_artifact_manifests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    execution_stage_run_id: Mapped[str] = mapped_column(
        ForeignKey("execution_stage_runs.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    execution_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(20), index=True)
    job_spec_hash: Mapped[str] = mapped_column(String(64))
    result_hash: Mapped[str] = mapped_column(String(64), index=True)
    entry_count: Mapped[int] = mapped_column(Integer)
    manifest_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    job: Mapped[Job] = relationship(
        back_populates="execution_artifact_manifest"
    )
    execution_stage_run: Mapped[ExecutionStageRun] = relationship(
        back_populates="artifact_manifest"
    )
    entries: Mapped[list["ExecutionArtifactEntry"]] = relationship(
        back_populates="manifest"
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'execution-artifact-manifest-v1'",
            name="ck_execution_artifact_manifest_schema",
        ),
        CheckConstraint(
            "stage IN ('explore', 'implement', 'verify')",
            name="ck_execution_artifact_manifest_stage",
        ),
        CheckConstraint(
            "(stage = 'explore' AND entry_count = 1) OR "
            "(stage = 'implement' AND entry_count = 3) OR "
            "(stage = 'verify' AND entry_count = 2)",
            name="ck_execution_artifact_manifest_count",
        ),
    )


class ExecutionArtifactEntry(Base):
    __tablename__ = "execution_artifact_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    manifest_id: Mapped[str] = mapped_column(
        ForeignKey("execution_artifact_manifests.id", ondelete="RESTRICT"),
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(80))
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )

    manifest: Mapped[ExecutionArtifactManifest] = relationship(
        back_populates="entries"
    )
    artifact: Mapped[Artifact] = relationship(
        back_populates="execution_entries"
    )

    __table_args__ = (
        UniqueConstraint(
            "manifest_id",
            "position",
            name="uq_execution_artifact_entry_position",
        ),
        UniqueConstraint(
            "manifest_id",
            "role",
            name="uq_execution_artifact_entry_role",
        ),
        CheckConstraint(
            "position >= 0 AND position <= 2",
            name="ck_execution_artifact_entry_position",
        ),
        CheckConstraint(
            "role IN ("
            "'stage-result', 'file-inventory', 'unified-diff', "
            "'normalized-test-results'"
            ")",
            name="ck_execution_artifact_entry_role",
        ),
    )


class ExecutionWorkspaceDisposal(Base):
    __tablename__ = "execution_workspace_disposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    verify_stage_run_id: Mapped[str] = mapped_column(
        ForeignKey("execution_stage_runs.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    artifact_manifest_id: Mapped[str] = mapped_column(
        ForeignKey("execution_artifact_manifests.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[str] = mapped_column(String(128))
    workspace_ref: Mapped[str] = mapped_column(String(128))
    workspace_inventory_hash: Mapped[str] = mapped_column(String(64))
    runner_image_digest: Mapped[str] = mapped_column(String(71))
    sandbox_policy_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    versions: Mapped[list["ExecutionWorkspaceDisposalVersion"]] = relationship(
        back_populates="disposal"
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = 'execution-workspace-disposal-v1'",
            name="ck_execution_workspace_disposal_schema",
        ),
    )


class ExecutionWorkspaceDisposalVersion(Base):
    __tablename__ = "execution_workspace_disposal_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    disposal_id: Mapped[str] = mapped_column(
        ForeignKey("execution_workspace_disposals.id", ondelete="RESTRICT"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), index=True)
    reason_code: Mapped[str] = mapped_column(String(100))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    disposal_record_hash: Mapped[str] = mapped_column(String(64))
    previous_version_hash: Mapped[str | None] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    disposal: Mapped[ExecutionWorkspaceDisposal] = relationship(
        back_populates="versions"
    )

    __table_args__ = (
        UniqueConstraint(
            "disposal_id",
            "sequence",
            name="uq_execution_workspace_disposal_sequence",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_execution_workspace_disposal_sequence",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_execution_workspace_disposal_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND worker_id IS NULL) OR "
            "(status IN ('running', 'succeeded', 'failed') "
            "AND worker_id IS NOT NULL)",
            name="ck_execution_workspace_disposal_worker",
        ),
    )


class TaskLifecycleMark(Base):
    __tablename__ = "task_lifecycle_marks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str] = mapped_column(String(40))
    mark: Mapped[str] = mapped_column(String(40))
    reason_code: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(128))
    state_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
    )
    state_record_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = '1'",
            name="ck_task_lifecycle_mark_schema",
        ),
        CheckConstraint(
            "mark IN ('failed', 'rejected', 'abandoned')",
            name="ck_task_lifecycle_mark_value",
        ),
    )


class ReviewRun(Base):
    __tablename__ = "review_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    review_number: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(128))
    reviewer_kind: Mapped[str] = mapped_column(String(40))
    plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    plan_content_hash: Mapped[str] = mapped_column(String(64))
    plan_record_hash: Mapped[str] = mapped_column(String(64))
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    repository_archive_hash: Mapped[str] = mapped_column(String(64))
    sandbox_policy_hash: Mapped[str] = mapped_column(String(64))
    attempt_record_hash: Mapped[str] = mapped_column(String(64))
    implement_stage_version_id: Mapped[str] = mapped_column(
        ForeignKey("execution_stage_versions.id", ondelete="RESTRICT"),
    )
    diff_hash: Mapped[str] = mapped_column(String(64))
    verify_stage_version_id: Mapped[str] = mapped_column(
        ForeignKey("execution_stage_versions.id", ondelete="RESTRICT"),
    )
    verify_result_hash: Mapped[str] = mapped_column(String(64))
    test_results_hash: Mapped[str] = mapped_column(String(64))
    binding_hash: Mapped[str] = mapped_column(String(64))
    reviewing_state_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
        unique=True,
    )
    reviewing_state_record_hash: Mapped[str] = mapped_column(String(64))
    ready_state_version_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
    )
    ready_state_record_hash: Mapped[str | None] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20))
    reason_code: Mapped[str] = mapped_column(String(100))
    findings: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    findings_hash: Mapped[str] = mapped_column(String(64))
    reviewer_invocation_id: Mapped[str] = mapped_column(String(36))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "review_number",
            name="uq_review_run_number",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_review_run_schema",
        ),
        CheckConstraint(
            "review_number >= 1",
            name="ck_review_run_number",
        ),
        CheckConstraint(
            "reviewer_kind IN ('fake', 'fake_blocking')",
            name="ck_review_run_kind",
        ),
        CheckConstraint(
            "verdict IN ('pass', 'block')",
            name="ck_review_run_verdict",
        ),
        CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_review_run_status",
        ),
        CheckConstraint(
            "(verdict = 'block' AND ready_state_version_id IS NULL "
            "AND ready_state_record_hash IS NULL) OR "
            "(verdict = 'pass' AND ready_state_version_id IS NOT NULL "
            "AND ready_state_record_hash IS NOT NULL)",
            name="ck_review_run_ready",
        ),
    )


class PublishIntent(Base):
    __tablename__ = "publish_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_run_id: Mapped[str] = mapped_column(
        ForeignKey("review_runs.id", ondelete="RESTRICT"),
        unique=True,
    )
    execution_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="RESTRICT"),
        index=True,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(128))
    upstream_repository: Mapped[str] = mapped_column(String(255))
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    head_branch: Mapped[str] = mapped_column(String(200))
    head_commit_sha: Mapped[str] = mapped_column(String(64))
    diff_hash: Mapped[str] = mapped_column(String(64))
    test_results_hash: Mapped[str] = mapped_column(String(64))
    review_record_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(8000))
    allowed_actions: Mapped[list[str]] = mapped_column(JSON)
    confirmation_nonce: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = '1'",
            name="ck_publish_intent_schema",
        ),
    )


class DraftPullRequest(Base):
    __tablename__ = "draft_pull_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    publish_intent_id: Mapped[str] = mapped_column(
        ForeignKey("publish_intents.id", ondelete="RESTRICT"),
        unique=True,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(40))
    number: Mapped[int] = mapped_column(Integer)
    html_url: Mapped[str] = mapped_column(String(500))
    head_branch: Mapped[str] = mapped_column(String(200))
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    head_commit_sha: Mapped[str] = mapped_column(String(64))
    diff_hash: Mapped[str] = mapped_column(String(64))
    review_record_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = '1'",
            name="ck_draft_pull_request_schema",
        ),
        CheckConstraint(
            "provider = 'fake'",
            name="ck_draft_pull_request_provider",
        ),
        CheckConstraint(
            "number >= 1",
            name="ck_draft_pull_request_number",
        ),
    )


class PublishConfirmation(Base):
    __tablename__ = "publish_confirmations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    publish_intent_id: Mapped[str] = mapped_column(
        ForeignKey("publish_intents.id", ondelete="RESTRICT"),
        unique=True,
    )
    draft_pull_request_id: Mapped[str] = mapped_column(
        ForeignKey("draft_pull_requests.id", ondelete="RESTRICT"),
        unique=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(128))
    confirmation_nonce: Mapped[str] = mapped_column(String(64))
    draft_pr_state_version_id: Mapped[str] = mapped_column(
        ForeignKey(
            "contribution_task_state_versions.id",
            ondelete="RESTRICT",
        ),
        unique=True,
    )
    draft_pr_state_record_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version = '1'",
            name="ck_publish_confirmation_schema",
        ),
    )


class PullRequestEvent(Base):
    __tablename__ = "pull_request_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_pull_request_id: Mapped[str] = mapped_column(
        ForeignKey("draft_pull_requests.id", ondelete="RESTRICT"),
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("contribution_tasks.id", ondelete="RESTRICT"),
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(32))
    remote_event_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    previous_event_hash: Mapped[str | None] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "draft_pull_request_id",
            "remote_event_id",
            name="uq_pull_request_event_remote",
        ),
        CheckConstraint(
            "schema_version = '1'",
            name="ck_pull_request_event_schema",
        ),
        CheckConstraint(
            "event_type IN ("
            "'opened', 'review', 'check', 'changes_requested', "
            "'merged', 'closed'"
            ")",
            name="ck_pull_request_event_type",
        ),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(128))
    correlation_id: Mapped[str] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    previous_event_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_audit_sequence"),
    )
