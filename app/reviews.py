from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.artifacts import (
    ArtifactError,
    ArtifactStore,
    ExecutionArtifactManifestView,
)
from app.audit import AuditService
from app.authorizations import (
    AuthorizationActionError,
    UserAction,
    require_user_action,
)
from app.executions import (
    ExecutionAttemptConflictError,
    ExecutionAttemptNotFoundError,
    ExecutionAttemptService,
    ExecutionStageStatus,
)
from app.models import (
    AgentInvocation,
    ExecutionAttempt,
    ExecutionStageVersion,
    ReviewRun,
)
from app.provenance import content_hash
from app.security import contains_sensitive_text, ensure_no_sensitive_data
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
)


REVIEW_RUN_SCHEMA_VERSION = "1"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ACTOR_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REVIEWER_KINDS = frozenset({"fake", "fake_blocking", "nvidia_nim"})


class ReviewError(RuntimeError):
    pass


class ReviewNotFoundError(ReviewError):
    pass


class ReviewConflictError(ReviewError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    severity: str
    location: str
    evidence: str
    recommendation: str
    verdict: str

    def to_wire(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "location": self.location,
            "evidence": self.evidence,
            "recommendation": self.recommendation,
            "verdict": self.verdict,
        }


class ReviewRunService:
    """Independent review bound to exact execution and artifact hashes."""

    def __init__(
        self,
        session: Session,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self.session = session
        self.artifacts = artifacts

    def create(
        self,
        *,
        execution_attempt_id: str,
        action: UserAction | str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        reviewer_kind: str = "fake",
        findings_override: tuple[ReviewFinding, ...] | None = None,
        verdict_override: str | None = None,
        reason_code_override: str | None = None,
        reviewer_invocation_id: str | None = None,
        now: datetime | None = None,
        commit: bool = True,
    ) -> ReviewRun:
        try:
            require_user_action(action, expected=UserAction.START_REVIEW)
        except AuthorizationActionError as exc:
            raise ReviewConflictError(str(exc)) from exc
        if reviewer_kind not in _REVIEWER_KINDS:
            raise ReviewConflictError("Reviewer kind is unknown")
        key = _idempotency_key(idempotency_key)
        normalized_actor_type = _actor_type(actor_type)
        normalized_actor_id = _actor_id(actor_id)
        replay = self.session.scalar(
            select(ReviewRun).where(ReviewRun.idempotency_key == key)
        )
        if replay is not None:
            verified = self.get_verified(replay.id)
            if (
                verified.execution_attempt_id != execution_attempt_id
                or verified.reviewer_kind != reviewer_kind
                or verified.actor_id != normalized_actor_id
            ):
                raise ReviewConflictError(
                    "Review idempotency key belongs to different inputs"
                )
            return verified

        existing = self.session.scalar(
            select(ReviewRun).where(
                ReviewRun.execution_attempt_id == execution_attempt_id
            )
        )
        if existing is not None:
            return self.get_verified(existing.id)

        try:
            attempt = ExecutionAttemptService(self.session).get_verified(
                execution_attempt_id
            )
            history = ExecutionAttemptService(self.session).history(attempt.id)
        except ExecutionAttemptNotFoundError as exc:
            raise ReviewNotFoundError(str(exc)) from exc
        except ExecutionAttemptConflictError as exc:
            raise ReviewConflictError(str(exc)) from exc
        implement = _latest_stage(
            history,
            stage="implement",
            status=ExecutionStageStatus.SUCCEEDED,
        )
        verify = _latest_stage(
            history,
            stage="verify",
            status=ExecutionStageStatus.SUCCEEDED,
        )
        if implement is None or implement.result_hash is None:
            raise ReviewConflictError(
                "Review requires a succeeded Implement stage"
            )
        if verify is None or verify.result_hash is None:
            raise ReviewConflictError(
                "Review requires a succeeded Verify stage"
            )
        diff_hash = self._artifact_hash(
            attempt,
            stage="implement",
            stage_result_hash=implement.result_hash,
            role="unified-diff",
        )
        test_results_hash = self._artifact_hash(
            attempt,
            stage="verify",
            stage_result_hash=verify.result_hash,
            role="normalized-test-results",
        )
        binding = review_binding_payload(
            plan_content_hash=attempt.plan_content_hash,
            plan_record_hash=attempt.plan_record_hash,
            base_commit_sha=attempt.base_commit_sha,
            repository_archive_hash=attempt.repository_archive_hash,
            sandbox_policy_hash=attempt.sandbox_policy_hash,
            attempt_record_hash=attempt.record_hash,
            diff_hash=diff_hash,
            verify_result_hash=verify.result_hash,
            test_results_hash=test_results_hash,
        )
        if reviewer_kind == "nvidia_nim":
            findings, verdict, reason_code = _model_review_result(
                findings_override,
                verdict=verdict_override,
                reason_code=reason_code_override,
                reviewer_invocation_id=reviewer_invocation_id,
            )
        else:
            if any(
                value is not None
                for value in (
                    findings_override,
                    verdict_override,
                    reason_code_override,
                    reviewer_invocation_id,
                )
            ):
                raise ReviewConflictError(
                    "Fake Review cannot consume provider result overrides"
                )
            findings, verdict, reason_code = evaluate_fake_review(
                reviewer_kind=reviewer_kind,
                binding=binding,
            )
        findings_payload = [item.to_wire() for item in findings]
        findings_hash = content_hash(findings_payload)
        invocation_id = reviewer_invocation_id or str(uuid4())
        states = ContributionTaskStateService(self.session)
        try:
            current = states.current(attempt.task_id)
            reviewing = states.prepare_transition(
                attempt.task_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                to_state=ContributionTaskState.REVIEWING,
                reason_code="review_started",
                now=now,
            )
        except TaskStateError as exc:
            raise ReviewConflictError(str(exc)) from exc
        if current.to_state != ContributionTaskState.EXECUTING.value:
            raise ReviewConflictError(
                "Review requires the current executing task state"
            )
        self.session.add(reviewing)
        self.session.flush()
        ready = None
        if verdict == "pass":
            try:
                ready = states.prepare_transition(
                    attempt.task_id,
                    expected_sequence=reviewing.sequence,
                    expected_record_hash=reviewing.record_hash,
                    to_state=ContributionTaskState.READY,
                    reason_code="review_passed",
                    now=now,
                )
            except TaskStateError as exc:
                raise ReviewConflictError(str(exc)) from exc
            self.session.add(ready)
            self.session.flush()
        review_number = (
            self.session.scalar(
                select(func.max(ReviewRun.review_number)).where(
                    ReviewRun.task_id == attempt.task_id
                )
            )
            or 0
        ) + 1
        payload = review_run_payload(
            execution_attempt_id=attempt.id,
            task_id=attempt.task_id,
            review_number=review_number,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            reviewer_kind=reviewer_kind,
            plan_version_id=attempt.plan_version_id,
            plan_content_hash=attempt.plan_content_hash,
            plan_record_hash=attempt.plan_record_hash,
            base_commit_sha=attempt.base_commit_sha,
            repository_archive_hash=attempt.repository_archive_hash,
            sandbox_policy_hash=attempt.sandbox_policy_hash,
            attempt_record_hash=attempt.record_hash,
            implement_stage_version_id=implement.id,
            diff_hash=diff_hash,
            verify_stage_version_id=verify.id,
            verify_result_hash=verify.result_hash,
            test_results_hash=test_results_hash,
            binding_hash=content_hash(binding),
            reviewing_state_version_id=reviewing.id,
            reviewing_state_record_hash=reviewing.record_hash,
            ready_state_version_id=None if ready is None else ready.id,
            ready_state_record_hash=(
                None if ready is None else ready.record_hash
            ),
            verdict=verdict,
            status="succeeded",
            reason_code=reason_code,
            findings=findings_payload,
            findings_hash=findings_hash,
            reviewer_invocation_id=invocation_id,
        )
        ensure_no_sensitive_data(
            {"idempotency_key": key, "review_run": payload},
            context="ReviewRun",
        )
        review = ReviewRun(
            id=str(uuid4()),
            execution_attempt_id=attempt.id,
            task_id=attempt.task_id,
            schema_version=REVIEW_RUN_SCHEMA_VERSION,
            review_number=review_number,
            idempotency_key=key,
            actor_type=normalized_actor_type,
            actor_id=normalized_actor_id,
            reviewer_kind=reviewer_kind,
            plan_version_id=attempt.plan_version_id,
            plan_content_hash=attempt.plan_content_hash,
            plan_record_hash=attempt.plan_record_hash,
            base_commit_sha=attempt.base_commit_sha,
            repository_archive_hash=attempt.repository_archive_hash,
            sandbox_policy_hash=attempt.sandbox_policy_hash,
            attempt_record_hash=attempt.record_hash,
            implement_stage_version_id=implement.id,
            diff_hash=diff_hash,
            verify_stage_version_id=verify.id,
            verify_result_hash=verify.result_hash,
            test_results_hash=test_results_hash,
            binding_hash=content_hash(binding),
            reviewing_state_version_id=reviewing.id,
            reviewing_state_record_hash=reviewing.record_hash,
            ready_state_version_id=None if ready is None else ready.id,
            ready_state_record_hash=(
                None if ready is None else ready.record_hash
            ),
            verdict=verdict,
            status="succeeded",
            reason_code=reason_code,
            findings=findings_payload,
            findings_hash=findings_hash,
            reviewer_invocation_id=invocation_id,
            record_hash=content_hash(payload),
            created_at=_aware(now),
        )
        self.session.add(review)
        try:
            AuditService(self.session).prepare(
                event_type="review.completed",
                actor_type=normalized_actor_type,
                actor_id=normalized_actor_id,
                correlation_id=key,
                payload={
                    "review_run_id": review.id,
                    "review_record_hash": review.record_hash,
                    "execution_attempt_id": attempt.id,
                    "verdict": verdict,
                    "binding_hash": review.binding_hash,
                    "reviewer_invocation_id": invocation_id,
                },
                now=now,
            )
        except Exception as exc:
            self.session.rollback()
            raise ReviewConflictError(
                "Review audit evidence could not be prepared"
            ) from exc
        try:
            if commit:
                self.session.commit()
            else:
                self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(ReviewRun).where(ReviewRun.idempotency_key == key)
            )
            if replay is not None:
                return self.get_verified(replay.id)
            raise ReviewConflictError(
                "Review changed concurrently or failed provenance checks"
            ) from exc
        return review

    def get_verified(self, review_id: str) -> ReviewRun:
        review = self.session.get(ReviewRun, review_id)
        if review is None:
            raise ReviewNotFoundError("ReviewRun was not found")
        try:
            attempt = ExecutionAttemptService(self.session).get_verified(
                review.execution_attempt_id
            )
        except ExecutionAttemptNotFoundError as exc:
            raise ReviewNotFoundError(str(exc)) from exc
        except ExecutionAttemptConflictError as exc:
            raise ReviewConflictError(str(exc)) from exc
        expected = review_run_payload(
            execution_attempt_id=review.execution_attempt_id,
            task_id=review.task_id,
            review_number=review.review_number,
            actor_type=review.actor_type,
            actor_id=review.actor_id,
            reviewer_kind=review.reviewer_kind,
            plan_version_id=review.plan_version_id,
            plan_content_hash=review.plan_content_hash,
            plan_record_hash=review.plan_record_hash,
            base_commit_sha=review.base_commit_sha,
            repository_archive_hash=review.repository_archive_hash,
            sandbox_policy_hash=review.sandbox_policy_hash,
            attempt_record_hash=review.attempt_record_hash,
            implement_stage_version_id=review.implement_stage_version_id,
            diff_hash=review.diff_hash,
            verify_stage_version_id=review.verify_stage_version_id,
            verify_result_hash=review.verify_result_hash,
            test_results_hash=review.test_results_hash,
            binding_hash=review.binding_hash,
            reviewing_state_version_id=review.reviewing_state_version_id,
            reviewing_state_record_hash=review.reviewing_state_record_hash,
            ready_state_version_id=review.ready_state_version_id,
            ready_state_record_hash=review.ready_state_record_hash,
            verdict=review.verdict,
            status=review.status,
            reason_code=review.reason_code,
            findings=list(review.findings),
            findings_hash=review.findings_hash,
            reviewer_invocation_id=review.reviewer_invocation_id,
        )
        if (
            review.schema_version != REVIEW_RUN_SCHEMA_VERSION
            or review.task_id != attempt.task_id
            or review.plan_version_id != attempt.plan_version_id
            or review.attempt_record_hash != attempt.record_hash
            or review.binding_hash
            != content_hash(
                review_binding_payload(
                    plan_content_hash=review.plan_content_hash,
                    plan_record_hash=review.plan_record_hash,
                    base_commit_sha=review.base_commit_sha,
                    repository_archive_hash=review.repository_archive_hash,
                    sandbox_policy_hash=review.sandbox_policy_hash,
                    attempt_record_hash=review.attempt_record_hash,
                    diff_hash=review.diff_hash,
                    verify_result_hash=review.verify_result_hash,
                    test_results_hash=review.test_results_hash,
                )
            )
            or review.findings_hash != content_hash(list(review.findings))
            or review.record_hash != content_hash(expected)
        ):
            raise ReviewConflictError(
                "ReviewRun content does not match its immutable inputs"
            )
        if review.reviewer_kind == "nvidia_nim":
            invocation = self.session.get(
                AgentInvocation, review.reviewer_invocation_id
            )
            if (
                invocation is None
                or invocation.role != "review"
                or invocation.provider_name != "nvidia_nim"
                or invocation.output_hash
                != content_hash(
                    {
                        "verdict": review.verdict,
                        "reason_code": review.reason_code,
                        "findings": list(review.findings),
                    }
                )
                or invocation.record_hash
                != content_hash(_agent_invocation_payload(invocation))
            ):
                raise ReviewConflictError(
                    "NVIDIA Review invocation binding is invalid"
                )
        return review

    def is_stale(self, review_id: str) -> bool:
        review = self.get_verified(review_id)
        try:
            attempt = ExecutionAttemptService(self.session).get_verified(
                review.execution_attempt_id
            )
            history = ExecutionAttemptService(self.session).history(attempt.id)
        except (ExecutionAttemptNotFoundError, ExecutionAttemptConflictError):
            return True
        implement = _latest_stage(
            history,
            stage="implement",
            status=ExecutionStageStatus.SUCCEEDED,
        )
        verify = _latest_stage(
            history,
            stage="verify",
            status=ExecutionStageStatus.SUCCEEDED,
        )
        latest_attempt_id = self.session.scalar(
            select(ExecutionAttempt.id)
            .where(ExecutionAttempt.task_id == review.task_id)
            .order_by(
                ExecutionAttempt.attempt_number.desc(),
                ExecutionAttempt.id.desc(),
            )
            .limit(1)
        )
        return (
            implement is None
            or verify is None
            or implement.id != review.implement_stage_version_id
            or verify.result_hash != review.verify_result_hash
            or attempt.plan_content_hash != review.plan_content_hash
            or attempt.plan_record_hash != review.plan_record_hash
            or attempt.base_commit_sha != review.base_commit_sha
            or attempt.repository_archive_hash
            != review.repository_archive_hash
            or attempt.sandbox_policy_hash != review.sandbox_policy_hash
            or latest_attempt_id != review.execution_attempt_id
        )

    def list_for_attempt(self, execution_attempt_id: str) -> tuple[ReviewRun, ...]:
        rows = tuple(
            self.session.scalars(
                select(ReviewRun)
                .where(ReviewRun.execution_attempt_id == execution_attempt_id)
                .order_by(ReviewRun.review_number, ReviewRun.id)
            )
        )
        return tuple(self.get_verified(row.id) for row in rows)

    def _artifact_hash(
        self,
        attempt: ExecutionAttempt,
        *,
        stage: str,
        stage_result_hash: str,
        role: str,
    ) -> str:
        if self.artifacts is None:
            raise ReviewConflictError(
                "Review requires verified artifact storage"
            )
        try:
            manifests = self.artifacts.execution_manifests(attempt.id)
        except ArtifactError as exc:
            raise ReviewConflictError(
                "Review artifacts failed integrity verification"
            ) from exc
        manifest = next(
            (
                item
                for item in manifests
                if item.stage == stage
                and item.result_hash == stage_result_hash
            ),
            None,
        )
        if manifest is None:
            raise ReviewConflictError(
                f"Review requires a verified {role} artifact"
            )
        extracted = _artifact_hash_from_manifest(manifest, role=role)
        if extracted is None:
            raise ReviewConflictError(
                f"Review requires a verified {role} artifact"
            )
        return extracted


def _model_review_result(
    findings: tuple[ReviewFinding, ...] | None,
    *,
    verdict: str | None,
    reason_code: str | None,
    reviewer_invocation_id: str | None,
) -> tuple[tuple[ReviewFinding, ...], str, str]:
    values = tuple(findings or ())
    if (
        not values
        or len(values) > 100
        or verdict not in {"pass", "block"}
        or not isinstance(reason_code, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", reason_code)
        or not isinstance(reviewer_invocation_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", reviewer_invocation_id)
    ):
        raise ReviewConflictError("NVIDIA Review result is invalid")
    for finding in values:
        if (
            finding.severity
            not in {"info", "low", "medium", "high", "blocking"}
            or finding.verdict not in {"pass", "block"}
            or not finding.location.strip()
            or len(finding.location) > 500
            or not finding.evidence.strip()
            or len(finding.evidence) > 4000
            or not finding.recommendation.strip()
            or len(finding.recommendation) > 4000
        ):
            raise ReviewConflictError("NVIDIA Review finding is invalid")
    has_block = any(item.verdict == "block" for item in values)
    if (verdict == "block") != has_block:
        raise ReviewConflictError("NVIDIA Review verdict is inconsistent")
    return values, verdict, reason_code


def _agent_invocation_payload(invocation: AgentInvocation) -> dict[str, object]:
    return {
        "id": invocation.id,
        "job_id": invocation.job_id,
        "attempt_number": invocation.attempt_number,
        "role": invocation.role,
        "provider_name": invocation.provider_name,
        "adapter_version": invocation.adapter_version,
        "model_name": invocation.model_name,
        "model_version": invocation.model_version,
        "input_hash": invocation.input_hash,
        "output_hash": invocation.output_hash,
        "input_tokens": invocation.input_tokens,
        "cached_input_tokens": invocation.cached_input_tokens,
        "output_tokens": invocation.output_tokens,
        "duration_ms": invocation.duration_ms,
    }


def evaluate_fake_review(
    *,
    reviewer_kind: str,
    binding: dict[str, str],
) -> tuple[tuple[ReviewFinding, ...], str, str]:
    if any(
        contains_sensitive_text(value)
        for value in binding.values()
    ):
        finding = ReviewFinding(
            severity="blocking",
            location="binding",
            evidence="Bound review inputs contained a credential canary",
            recommendation="Rotate the secret and create a new execution",
            verdict="block",
        )
        return (finding,), "block", "secret_detected"
    if reviewer_kind == "fake_blocking":
        finding = ReviewFinding(
            severity="high",
            location="acceptance",
            evidence="Deterministic blocking reviewer rejected publication",
            recommendation="Repair the change set and rerun Verify",
            verdict="block",
        )
        return (finding,), "block", "blocking_finding"
    findings = (
        ReviewFinding(
            severity="info",
            location="verify",
            evidence=(
                "Verify artifact "
                f"{binding['verify_result_hash'][:12]} succeeded"
            ),
            recommendation="Keep the exact verified diff for publication",
            verdict="pass",
        ),
        ReviewFinding(
            severity="info",
            location="plan",
            evidence="Review bound only to plan, base, diff, tests, and policy",
            recommendation="Any hash change must start a new ReviewRun",
            verdict="pass",
        ),
    )
    return findings, "pass", "review_passed"


def review_binding_payload(
    *,
    plan_content_hash: str,
    plan_record_hash: str,
    base_commit_sha: str,
    repository_archive_hash: str,
    sandbox_policy_hash: str,
    attempt_record_hash: str,
    diff_hash: str,
    verify_result_hash: str,
    test_results_hash: str,
) -> dict[str, str]:
    for name, value in {
        "plan content hash": plan_content_hash,
        "plan record hash": plan_record_hash,
        "repository archive hash": repository_archive_hash,
        "sandbox policy hash": sandbox_policy_hash,
        "attempt record hash": attempt_record_hash,
        "diff hash": diff_hash,
        "verify result hash": verify_result_hash,
        "test results hash": test_results_hash,
    }.items():
        _hash(value, name=name)
    return {
        "plan_content_hash": plan_content_hash,
        "plan_record_hash": plan_record_hash,
        "base_commit_sha": base_commit_sha,
        "repository_archive_hash": repository_archive_hash,
        "sandbox_policy_hash": sandbox_policy_hash,
        "attempt_record_hash": attempt_record_hash,
        "diff_hash": diff_hash,
        "verify_result_hash": verify_result_hash,
        "test_results_hash": test_results_hash,
    }


def review_run_payload(
    *,
    execution_attempt_id: str,
    task_id: str,
    review_number: int,
    actor_type: str,
    actor_id: str,
    reviewer_kind: str,
    plan_version_id: str,
    plan_content_hash: str,
    plan_record_hash: str,
    base_commit_sha: str,
    repository_archive_hash: str,
    sandbox_policy_hash: str,
    attempt_record_hash: str,
    implement_stage_version_id: str,
    diff_hash: str,
    verify_stage_version_id: str,
    verify_result_hash: str,
    test_results_hash: str,
    binding_hash: str,
    reviewing_state_version_id: str,
    reviewing_state_record_hash: str,
    ready_state_version_id: str | None,
    ready_state_record_hash: str | None,
    verdict: str,
    status: str,
    reason_code: str,
    findings: list[dict[str, Any]],
    findings_hash: str,
    reviewer_invocation_id: str,
) -> dict[str, object]:
    return {
        "schema_version": REVIEW_RUN_SCHEMA_VERSION,
        "execution_attempt_id": execution_attempt_id,
        "task_id": task_id,
        "review_number": review_number,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "reviewer_kind": reviewer_kind,
        "plan_version_id": plan_version_id,
        "plan_content_hash": plan_content_hash,
        "plan_record_hash": plan_record_hash,
        "base_commit_sha": base_commit_sha,
        "repository_archive_hash": repository_archive_hash,
        "sandbox_policy_hash": sandbox_policy_hash,
        "attempt_record_hash": attempt_record_hash,
        "implement_stage_version_id": implement_stage_version_id,
        "diff_hash": diff_hash,
        "verify_stage_version_id": verify_stage_version_id,
        "verify_result_hash": verify_result_hash,
        "test_results_hash": test_results_hash,
        "binding_hash": binding_hash,
        "reviewing_state_version_id": reviewing_state_version_id,
        "reviewing_state_record_hash": reviewing_state_record_hash,
        "ready_state_version_id": ready_state_version_id,
        "ready_state_record_hash": ready_state_record_hash,
        "verdict": verdict,
        "status": status,
        "reason_code": reason_code,
        "findings": findings,
        "findings_hash": findings_hash,
        "reviewer_invocation_id": reviewer_invocation_id,
    }


def _latest_stage(
    history: tuple[ExecutionStageVersion, ...],
    *,
    stage: str,
    status: ExecutionStageStatus,
) -> ExecutionStageVersion | None:
    matches = [
        item
        for item in history
        if item.stage == stage and item.status == status.value
    ]
    return matches[-1] if matches else None


def _artifact_hash_from_manifest(
    manifest: ExecutionArtifactManifestView,
    *,
    role: str,
) -> str | None:
    matches = tuple(
        entry.artifact_id
        for entry in manifest.entries
        if entry.role == role
    )
    return matches[0] if len(matches) == 1 else None


def _idempotency_key(value: str) -> str:
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ReviewConflictError("Review idempotency key is invalid")
    return value


def _actor_type(value: str) -> str:
    if not _ACTOR_TYPE.fullmatch(value):
        raise ReviewConflictError("Review actor type is invalid")
    return value


def _actor_id(value: str) -> str:
    if not _ACTOR_ID.fullmatch(value):
        raise ReviewConflictError("Review actor id is invalid")
    return value


def _hash(value: str, *, name: str) -> str:
    if not _HASH.fullmatch(value):
        raise ReviewConflictError(f"Review {name} is invalid")
    return value


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
