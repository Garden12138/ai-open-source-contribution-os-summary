from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    scan_run_id: str | None
    snapshot_id: str | None
    score_version_id: str | None
    provenance_status: str
    selection_reason: str
    score_snapshot: float
    opportunity: OpportunityCard


class AnalysisAvailabilityResponse(BaseModel):
    mode: Literal["rule_only_fallback", "provider_ready"]
    automatic_model_invocation_enabled: bool
    rule_leaderboard_preserved: bool
    fallback_reasons: list[
        Literal["provider_not_configured", "budget_not_configured"]
    ]


class AnalysisCreateRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=128)


class ContributionTaskCreateRequest(BaseModel):
    analysis_version_id: str = Field(min_length=1, max_length=128)


class ContributionTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    analysis_version_id: str
    snapshot_id: str
    opportunity_id: int
    schema_version: str
    analysis_record_hash: str
    analysis_output_hash: str
    snapshot_inputs_hash: str
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class PlanCommandRequest(BaseModel):
    command_id: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=1_000)
    argv: list[str] = Field(min_length=1, max_length=50)
    working_directory: str = Field(default=".", min_length=1, max_length=500)


class PlanVersionCreateRequest(BaseModel):
    parent_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    goal: str = Field(min_length=1, max_length=4_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=100)
    files_to_inspect: list[str] = Field(min_length=1, max_length=500)
    files_likely_to_change: list[str] = Field(
        min_length=1,
        max_length=500,
    )
    implementation_steps: list[str] = Field(min_length=1, max_length=100)
    tests_to_add_or_run: list[str] = Field(min_length=1, max_length=100)
    commands_to_run: list[PlanCommandRequest] = Field(
        min_length=1,
        max_length=100,
    )
    risks: list[str] = Field(default_factory=list, max_length=100)
    questions_for_maintainer: list[str] = Field(
        default_factory=list,
        max_length=100,
    )


class PlanVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    task_state_version_id: str
    parent_version_id: str | None
    parent_record_hash: str | None
    schema_version: str
    version_number: int
    task_record_hash: str
    task_state_record_hash: str
    goal: str
    acceptance_criteria: list[str]
    files_to_inspect: list[str]
    files_likely_to_change: list[str]
    implementation_steps: list[str]
    tests_to_add_or_run: list[str]
    commands_to_run: list[dict[str, Any]]
    risks: list[str]
    questions_for_maintainer: list[str]
    content_hash: str
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class PlanApprovalRequest(BaseModel):
    base_commit_sha: str = Field(
        min_length=40,
        max_length=64,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    )
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )


class PlanApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    plan_lock_id: str
    plan_version_id: str
    task_id: str
    approved_state_version_id: str
    schema_version: str
    actor_type: str
    actor_id: str
    lock_hash: str
    plan_content_hash: str
    plan_record_hash: str
    prior_state_record_hash: str
    approved_state_record_hash: str
    approval_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ContributionTaskStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    schema_version: str
    sequence: int
    from_state: str | None
    to_state: str
    reason_code: str
    task_record_hash: str
    previous_state_hash: str | None
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class PlanLockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    plan_version_id: str
    task_id: str
    task_state_version_id: str
    analysis_version_id: str
    snapshot_id: str
    schema_version: str
    base_commit_sha: str
    task_record_hash: str
    task_state_record_hash: str
    analysis_record_hash: str
    analysis_output_hash: str
    snapshot_inputs_hash: str
    provider_name: str
    adapter_version: str
    model_name: str
    model_version: str
    inspect_prompt_version: str
    inspect_policy_version: str
    inspect_output_schema_version: str
    analyze_prompt_version: str
    analyze_policy_version: str
    analyze_output_schema_version: str
    provider_contract_hash: str
    plan_content_hash: str
    plan_record_hash: str
    lock_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class PlanConversationEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    plan_version_id: str | None
    schema_version: str
    sequence: int
    entry_type: Literal["message", "decision"]
    actor_type: str
    actor_id: str
    content: dict[str, Any]
    content_hash: str
    previous_entry_hash: str | None
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ContributionTaskDetailResponse(BaseModel):
    task: ContributionTaskResponse
    current_state: ContributionTaskStateResponse
    plan_versions: list[PlanVersionResponse]
    plan_locks: list[PlanLockResponse]
    approvals: list[PlanApprovalResponse]
    conversation: list[PlanConversationEntryResponse]
    latest_plan_version_id: str | None
    active_approval_id: str | None
    approval_status: Literal["unapproved", "approved", "revoked"]
    execution_attempt_ids: list[str]
    latest_execution_attempt_id: str | None
    review_run_ids: list[str] = []
    latest_review_run_id: str | None = None
    publish_intent_ids: list[str] = []
    latest_publish_intent_id: str | None = None
    draft_pull_request_ids: list[str] = []
    latest_draft_pull_request_id: str | None = None


class PlanConversationMessageRequest(BaseModel):
    plan_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )
    text: str = Field(min_length=1, max_length=8_000)


class PlanFieldDifferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    path: str
    left_present: bool
    right_present: bool
    left: Any
    right: Any


class PlanVersionComparisonResponse(BaseModel):
    left_version_id: str
    right_version_id: str
    task_id: str
    semantic_differences: list[PlanFieldDifferenceResponse]
    unified_diff: str


class ExecutionReadinessRequest(BaseModel):
    base_commit_sha: str = Field(
        min_length=40,
        max_length=64,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    )


class ExecutionReadinessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    approval_id: str
    approval_hash: str
    plan_version_id: str
    plan_record_hash: str
    plan_content_hash: str
    task_id: str
    task_state_version_id: str
    task_state_record_hash: str
    base_commit_sha: str
    observed_fingerprint_hash: str
    ready: Literal[True] = True
    execution_started: Literal[False] = False


class ChangeOperationRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    kind: Literal["write", "delete"]
    expected_prior_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    content: str | None = None
    executable: bool | None = None


class ChangeSetCreateRequest(BaseModel):
    source: Literal["user", "fake"] = "fake"
    operations: list[ChangeOperationRequest] | None = None


class ChangeSetResponse(BaseModel):
    change_set_id: str
    change_set_hash: str
    plan_version_id: str
    paths: list[str]


class CodingMessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=12_000)


class CodingProposalAcceptRequest(BaseModel):
    action: Literal["accept_change_set"]
    expected_change_set_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class CodingTurnResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence: int
    role: Literal["user", "assistant"]
    content: str
    content_hash: str
    provider_name: str | None
    model_name: str | None
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ChangeSetProposalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    job_id: str
    conversation_hash: str
    change_set_hash: str
    summary: str
    paths: list[str]
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class CodingSessionResponse(BaseModel):
    id: str
    execution_attempt_id: str
    plan_version_id: str
    base_commit_sha: str
    explore_result_hash: str
    context_hash: str
    record_hash: str
    created_at: datetime
    turns: list[CodingTurnResponse]
    proposals: list[ChangeSetProposalResponse]

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class RepositoryArchiveCreateRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    base_commit_sha: str = Field(
        min_length=40,
        max_length=64,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    )


class ExecutionCreateRequest(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    base_commit_sha: str = Field(
        min_length=40,
        max_length=64,
        pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$",
    )
    repository_archive_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    runner_image_digest: str = Field(
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )


class ExecutionStageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    execution_attempt_id: str
    schema_version: str
    sequence: int
    stage: Literal["explore", "implement", "verify"]
    status: Literal[
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
    ]
    reason_code: str
    job_spec_hash: str | None
    input_hashes: list[str]
    result_hash: str | None
    attempt_record_hash: str
    previous_stage_state_hash: str | None
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ExecutionAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    plan_version_id: str
    plan_approval_id: str
    approved_state_version_id: str
    executing_state_version_id: str
    schema_version: str
    attempt_number: int
    actor_type: str
    actor_id: str
    action: Literal["start_execution"]
    repository_full_name: str
    base_commit_sha: str
    repository_archive_hash: str
    runner_image_digest: str
    sandbox_policy_version: str
    sandbox_policy_hash: str
    task_record_hash: str
    analysis_version_id: str
    analysis_record_hash: str
    analysis_output_hash: str
    snapshot_id: str
    snapshot_inputs_hash: str
    provider_contract_hash: str
    plan_content_hash: str
    plan_record_hash: str
    approval_hash: str
    approved_state_record_hash: str
    executing_state_record_hash: str
    observed_fingerprint_hash: str
    record_hash: str
    created_at: datetime
    current_stage: ExecutionStageResponse

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ExecutionJobStatusResponse(BaseModel):
    state: Literal[
        "queued",
        "leased",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
    ]
    attempt_count: int
    max_attempts: int
    timeout_seconds: int
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    cancel_requested_at: datetime | None
    progress_current: int
    progress_total: int | None
    progress_message: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @field_validator(
        "lease_expires_at",
        "heartbeat_at",
        "cancel_requested_at",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
    )
    @classmethod
    def normalize_timestamps(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ExecutionStageRunResponse(BaseModel):
    id: str
    job_id: str
    pending_stage_version_id: str
    schema_version: str
    stage: Literal["explore", "implement", "verify"]
    stage_run_number: int
    max_stage_runs: int
    timeout_seconds: int
    job_spec_hash: str
    input_hashes: list[str]
    attempt_record_hash: str
    pending_stage_record_hash: str
    record_hash: str
    created_at: datetime
    job: ExecutionJobStatusResponse

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ExecutionArtifactEntryResponse(BaseModel):
    position: int
    role: Literal[
        "stage-result",
        "file-inventory",
        "unified-diff",
        "normalized-test-results",
    ]
    artifact_id: str
    algorithm: Literal["sha256"]
    size_bytes: int
    media_type: str
    created_at: datetime
    content_url: str

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ExecutionArtifactManifestResponse(BaseModel):
    id: str
    job_id: str
    execution_stage_run_id: str
    execution_attempt_id: str
    schema_version: str
    stage: Literal["explore", "implement", "verify"]
    job_spec_hash: str
    result_hash: str
    entry_count: int
    manifest_hash: str
    created_at: datetime
    entries: list[ExecutionArtifactEntryResponse]

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ExecutionArtifactListResponse(BaseModel):
    execution_attempt_id: str
    manifests: list[ExecutionArtifactManifestResponse]


class ExecutionDetailResponse(ExecutionAttemptResponse):
    stages: list[ExecutionStageResponse]
    stage_runs: list[ExecutionStageRunResponse]
    artifact_manifests: list[ExecutionArtifactManifestResponse]
    reviews: list["ReviewRunResponse"] = []


class ReviewFindingResponse(BaseModel):
    severity: str
    location: str
    evidence: str
    recommendation: str
    verdict: Literal["pass", "block"]


class ReviewCreateRequest(BaseModel):
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )
    reviewer: Literal["fake", "fake_blocking"] = "fake"


class ProviderReviewCreateRequest(BaseModel):
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )


class ReviewRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    execution_attempt_id: str
    task_id: str
    schema_version: str
    review_number: int
    actor_type: str
    actor_id: str
    reviewer_kind: str
    plan_version_id: str
    plan_content_hash: str
    plan_record_hash: str
    base_commit_sha: str
    repository_archive_hash: str
    sandbox_policy_hash: str
    attempt_record_hash: str
    diff_hash: str
    verify_result_hash: str
    test_results_hash: str
    binding_hash: str
    verdict: Literal["pass", "block"]
    status: Literal["succeeded", "failed"]
    reason_code: str
    findings: list[ReviewFindingResponse]
    findings_hash: str
    reviewer_invocation_id: str
    record_hash: str
    stale: bool = False
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class RepairCreateRequest(BaseModel):
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )


class PublishIntentCreateRequest(BaseModel):
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)


class PublishIntentConfirmRequest(BaseModel):
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )
    confirmation_nonce: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class PublishIntentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    review_run_id: str
    execution_attempt_id: str
    task_id: str
    schema_version: str
    actor_type: str
    actor_id: str
    upstream_repository: str
    base_commit_sha: str
    head_branch: str
    head_commit_sha: str
    diff_hash: str
    test_results_hash: str
    review_record_hash: str
    title: str
    body: str
    allowed_actions: list[str]
    confirmation_nonce: str
    expires_at: datetime
    status: Literal["pending", "confirmed", "expired", "invalidated"]
    record_hash: str
    created_at: datetime

    @field_validator("expires_at", "created_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class DraftPullRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    publish_intent_id: str
    task_id: str
    provider: str
    number: int
    html_url: str
    head_branch: str
    base_commit_sha: str
    head_commit_sha: str
    diff_hash: str
    review_record_hash: str
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class PublishConfirmationResponse(BaseModel):
    intent: PublishIntentResponse
    draft_pull_request: DraftPullRequestResponse


class PullRequestEventCreateRequest(BaseModel):
    remote_event_id: str = Field(min_length=1, max_length=128)
    event_type: Literal[
        "opened",
        "review",
        "check",
        "changes_requested",
        "merged",
        "closed",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )


class PullRequestEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    draft_pull_request_id: str
    task_id: str
    remote_event_id: str
    event_type: str
    payload: dict[str, Any]
    payload_hash: str
    previous_event_hash: str | None
    record_hash: str
    occurred_at: datetime
    created_at: datetime

    @field_validator("occurred_at", "created_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class TaskLifecycleRequest(BaseModel):
    action: Literal["abandon", "fail", "reject", "reward", "revise"]
    actor_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    )
    reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_.-]{0,99}$",
    )


class ContributionTaskSummaryResponse(BaseModel):
    id: str
    opportunity_id: int
    analysis_version_id: str
    current_state: str
    reason_code: str
    state_record_hash: str
    opportunity_title: str | None = None
    repository_full_name: str | None = None
    friendly_state: str | None = None
    progress_percent: int = Field(default=0, ge=0, le=100)
    next_action: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class ContributionDashboardResponse(BaseModel):
    funnel: dict[str, int]
    current_states: dict[str, int]
    heatmap: list[dict[str, Any]]
    metrics: dict[str, Any]


class AnalysisVersionSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    opportunity_id: int
    snapshot_id: str
    score_version_id: str
    provider_name: str
    model_name: str
    model_version: str
    analyze_output_schema_version: str
    record_hash: str
    created_at: datetime


class AnalysisHistoryResponse(BaseModel):
    opportunity_id: int
    versions: list[AnalysisVersionSummaryResponse]


class AnalysisVersionDetailResponse(AnalysisVersionSummaryResponse):
    content: dict[str, Any]


class AnalysisFieldDifferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    path: str
    left_present: bool
    right_present: bool
    left: Any
    right: Any


class AnalysisVersionComparisonResponse(BaseModel):
    left_version_id: str
    right_version_id: str
    opportunity_id: int
    same_snapshot: bool
    same_rule_score: bool
    same_frozen_input: bool
    left: dict[str, Any]
    right: dict[str, Any]
    differences: list[AnalysisFieldDifferenceResponse]


class DailyLeaderboardResponse(BaseModel):
    selection_date: date
    scan_run_id: str | None
    provenance_status: str
    generated_at: datetime | None
    total_candidates: int
    total_eligible: int
    analysis: AnalysisAvailabilityResponse
    picks: list[DailyPickItem]


class ScanRequest(BaseModel):
    queries: list[str] | None = Field(default=None, max_length=20)
    top_n: int | None = Field(default=None, ge=1, le=50)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    state: str
    idempotency_key: str
    attempt_count: int
    max_attempts: int
    timeout_seconds: int
    run_after: datetime
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    cancel_requested_at: datetime | None
    progress_current: int
    progress_total: int | None
    progress_message: str | None
    result_data: dict[str, object]
    scan_run_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @field_validator(
        "run_after",
        "lease_expires_at",
        "heartbeat_at",
        "cancel_requested_at",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
    )
    @classmethod
    def normalize_timestamps(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class JobProgressEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    event_id: str
    event_type: str
    occurred_at: datetime
    data: dict[str, Any]


class JobProgressFeedResponse(BaseModel):
    job_id: str
    state: str
    revision: str
    unchanged: bool
    events: list[JobProgressEventResponse]


class ScanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    provenance_status: str
    selection_date: date | None
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
    local_access_token_required: bool
    csrf_token: str
    preferred_languages: list[str]
    queries: list[str]
    daily_pick_count: int
    timezone: str
    analysis_provider: Literal["none", "fake", "nvidia_nim"]
    analysis_model: str
    implementation_provider: Literal["none", "fake", "nvidia_nim"] = "none"
    implementation_model: str
    review_provider: Literal["none", "fake", "nvidia_nim"] = "none"
    review_model: str
    sandbox_stage_runtime: Literal["none", "fake", "docker"]
    draft_pr_publisher: Literal["none", "fake", "gh"]


class PreferenceUpsertRequest(BaseModel):
    primary_goal: Literal[
        "balanced", "bounty", "impact", "quick_merge", "learning"
    ] = "balanced"
    preferred_languages: list[str] = Field(default_factory=list, max_length=20)
    weekly_hours: int = Field(default=5, ge=1, le=40)
    minimum_bounty_usd: float = Field(default=0, ge=0, le=1_000_000)
    auto_scan_enabled: bool = False
    auto_scan_local_time: str = Field(
        default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )

    @field_validator("preferred_languages")
    @classmethod
    def validate_languages(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if any(len(item) > 80 for item in normalized):
            raise ValueError("preferred language is too long")
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("preferred languages must be unique")
        return normalized


class PreferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    version: int
    primary_goal: str
    preferred_languages: list[str]
    weekly_hours: int
    minimum_bounty_usd: float
    auto_scan_enabled: bool
    auto_scan_local_time: str
    record_hash: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class PreferenceCurrentResponse(BaseModel):
    configured: bool
    preference: PreferenceResponse | None


class DispositionCreateRequest(BaseModel):
    state: Literal["shortlisted", "dismissed", "neutral"]
    reason_code: Literal[
        "too_large",
        "low_reward",
        "tech_mismatch",
        "high_competition",
        "unclear_scope",
        "not_interested",
        "other",
    ] | None = None
    reminder_at: datetime | None = None

    @field_validator("reason_code")
    @classmethod
    def validate_reason(
        cls, value: str | None, info: Any
    ) -> str | None:
        state = info.data.get("state")
        if state == "dismissed" and value is None:
            raise ValueError("dismissed opportunities require a reason_code")
        if state != "dismissed" and value is not None:
            raise ValueError("reason_code is only valid for dismissed opportunities")
        return value


class DispositionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    opportunity_id: int
    preference_version_id: str | None
    sequence: int
    state: str
    reason_code: str | None
    reminder_at: datetime | None
    record_hash: str
    created_at: datetime

    @field_validator("reminder_at", "created_at")
    @classmethod
    def normalize_timestamps(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class RecommendationResponse(BaseModel):
    scan_run_id: str
    snapshot_id: str
    score_version_id: str
    preference_version_id: str | None
    goal: str
    personalized_score: float
    decision_score: float
    ai_score_adjustment: float
    recommendation_label: str
    recommendation: str
    summary: str
    reason_codes: list[str]
    reasons: list[str]
    acceptance_level: str
    competition_level: str
    impact_level: str
    effort: dict[str, Any] | None
    next_steps: list[str]
    maintainer_questions: list[str]
    analysis_version_id: str | None
    analysis_status: Literal["not_analyzed", "analyzed"]
    analysis_recommendation: Literal[
        "pursue", "consider", "skip", "insufficient_evidence"
    ] | None
    analysis_confidence: float | None
    disposition_state: str
    disposition_reason: str | None
    reminder_at: datetime | None
    opportunity: dict[str, Any]

    @field_validator("reminder_at")
    @classmethod
    def normalize_reminder_at(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class RecommendationFeedResponse(BaseModel):
    scan_run_id: str | None
    preference_version_id: str | None
    goal: str
    total: int
    analyzed_total: int
    recommended_total: int
    pending_analysis_total: int
    items: list[RecommendationResponse]


class RecommendationAnalysisBatchRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=5)


class RecommendationAnalysisJobResponse(BaseModel):
    opportunity_id: int
    snapshot_id: str
    created: bool
    job: JobResponse


class RecommendationAnalysisBatchResponse(BaseModel):
    scan_run_id: str
    requested: int
    jobs: list[RecommendationAnalysisJobResponse]


class NotificationResponse(BaseModel):
    id: str
    kind: str
    opportunity_id: int | None
    scan_run_id: str | None
    title: str
    message: str
    is_read: bool
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class NotificationReadResponse(BaseModel):
    notification_id: str | None = None
    read_count: int = 0


class ScanChangesResponse(BaseModel):
    scan_run_id: str | None
    generated_at: datetime | None
    new_matches: int
    shortlist_updates: int

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is None:
            return None
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class HealthResponse(BaseModel):
    status: str
    database: str
