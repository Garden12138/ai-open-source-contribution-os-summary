from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import AuditService
from app.authorizations import (
    AuthorizationActionError,
    UserAction,
    require_user_action,
)
from app.executions import ExecutionAttemptService
from app.models import (
    DraftPullRequest,
    PublishConfirmation,
    PublishIntent,
)
from app.provenance import content_hash
from app.publishers import (
    FakeGitHubPublisher,
    GitHubPublisher,
    PublishRequest,
    PublisherError,
    PublisherProhibitedError,
    validate_publish_actions,
)
from app.reviews import ReviewConflictError, ReviewNotFoundError, ReviewRunService
from app.security import ensure_no_sensitive_data
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
)


PUBLISH_INTENT_SCHEMA_VERSION = "1"
DRAFT_PULL_REQUEST_SCHEMA_VERSION = "1"
PUBLISH_CONFIRMATION_SCHEMA_VERSION = "1"
PUBLISH_INTENT_TTL = timedelta(hours=1)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_NONCE = re.compile(r"^[0-9a-f]{64}$")


class PublishIntentError(RuntimeError):
    pass


class PublishIntentNotFoundError(PublishIntentError):
    pass


class PublishIntentConflictError(PublishIntentError):
    pass


class PublishIntentStaleError(PublishIntentError):
    pass


class PublishIntentForbiddenError(PublishIntentError):
    pass


class PublishIntentService:
    def __init__(
        self,
        session: Session,
        publisher: GitHubPublisher | None = None,
    ) -> None:
        self.session = session
        self.publisher = publisher or FakeGitHubPublisher()

    def create(
        self,
        *,
        review_run_id: str,
        action: UserAction | str,
        idempotency_key: str,
        actor_type: str,
        actor_id: str,
        title: str,
        body: str,
        now: datetime | None = None,
    ) -> PublishIntent:
        try:
            require_user_action(
                action,
                expected=UserAction.CREATE_PUBLISH_INTENT,
            )
        except AuthorizationActionError as exc:
            raise PublishIntentConflictError(str(exc)) from exc
        key = _idempotency_key(idempotency_key)
        actor = _actor_id(actor_id)
        replay = self.session.scalar(
            select(PublishIntent).where(PublishIntent.idempotency_key == key)
        )
        if replay is not None:
            verified = self.get_verified(replay.id)
            if (
                verified.review_run_id != review_run_id
                or verified.actor_id != actor
            ):
                raise PublishIntentConflictError(
                    "Publish intent idempotency key belongs to different inputs"
                )
            return verified
        existing = self.session.scalar(
            select(PublishIntent).where(
                PublishIntent.review_run_id == review_run_id
            )
        )
        if existing is not None:
            return self.get_verified(existing.id)

        reviews = ReviewRunService(self.session)
        try:
            review = reviews.get_verified(review_run_id)
        except ReviewNotFoundError as exc:
            raise PublishIntentNotFoundError(str(exc)) from exc
        except ReviewConflictError as exc:
            raise PublishIntentConflictError(str(exc)) from exc
        if review.verdict != "pass":
            raise PublishIntentConflictError(
                "Publish intent requires a passing ReviewRun"
            )
        if reviews.is_stale(review.id):
            raise PublishIntentStaleError(
                "Review inputs changed; create a new ReviewRun"
            )
        attempt = ExecutionAttemptService(self.session).get_verified(
            review.execution_attempt_id
        )
        states = ContributionTaskStateService(self.session)
        current = states.current(review.task_id)
        if current.to_state != ContributionTaskState.READY.value:
            raise PublishIntentConflictError(
                "Publish intent requires the current ready task state"
            )
        cleaned_title = title.strip()
        cleaned_body = body.strip()
        if not cleaned_title or len(cleaned_title) > 200:
            raise PublishIntentConflictError("Publish title is invalid")
        if not cleaned_body or len(cleaned_body) > 8000:
            raise PublishIntentConflictError("Publish body is invalid")
        actions = validate_publish_actions(("create_draft_pr",))
        created_at = _aware(now)
        head_commit_sha = content_hash(
            {
                "diff_hash": review.diff_hash,
                "review_record_hash": review.record_hash,
                "base_commit_sha": review.base_commit_sha,
            }
        )[:40]
        issue_hint = attempt.task_id.replace("-", "")[:8]
        head_branch = f"contribos/issue-{issue_hint}-{attempt.task_id[:8]}"
        nonce = content_hash(
            {
                "review_run_id": review.id,
                "actor_id": actor,
                "created_at": created_at.isoformat(),
            }
        )
        payload = publish_intent_payload(
            review_run_id=review.id,
            execution_attempt_id=attempt.id,
            task_id=review.task_id,
            actor_type=actor_type,
            actor_id=actor,
            upstream_repository=attempt.repository_full_name,
            base_commit_sha=review.base_commit_sha,
            head_branch=head_branch,
            head_commit_sha=head_commit_sha,
            diff_hash=review.diff_hash,
            test_results_hash=review.test_results_hash,
            review_record_hash=review.record_hash,
            title=cleaned_title,
            body=cleaned_body,
            allowed_actions=list(actions),
            confirmation_nonce=nonce,
            expires_at=created_at + PUBLISH_INTENT_TTL,
        )
        ensure_no_sensitive_data(
            {"idempotency_key": key, "publish_intent": payload},
            context="PublishIntent",
        )
        intent = PublishIntent(
            id=str(uuid4()),
            review_run_id=review.id,
            execution_attempt_id=attempt.id,
            task_id=review.task_id,
            schema_version=PUBLISH_INTENT_SCHEMA_VERSION,
            idempotency_key=key,
            actor_type=actor_type,
            actor_id=actor,
            upstream_repository=attempt.repository_full_name,
            base_commit_sha=review.base_commit_sha,
            head_branch=head_branch,
            head_commit_sha=head_commit_sha,
            diff_hash=review.diff_hash,
            test_results_hash=review.test_results_hash,
            review_record_hash=review.record_hash,
            title=cleaned_title,
            body=cleaned_body,
            allowed_actions=list(actions),
            confirmation_nonce=nonce,
            expires_at=created_at + PUBLISH_INTENT_TTL,
            record_hash=content_hash(payload),
            created_at=created_at,
        )
        self.session.add(intent)
        try:
            AuditService(self.session).prepare(
                event_type="publish_intent.created",
                actor_type=actor_type,
                actor_id=actor,
                correlation_id=key,
                payload={
                    "publish_intent_id": intent.id,
                    "publish_intent_hash": intent.record_hash,
                    "review_run_id": review.id,
                    "review_record_hash": review.record_hash,
                },
                now=now,
            )
        except Exception as exc:
            self.session.rollback()
            raise PublishIntentConflictError(
                "Publish intent audit evidence could not be prepared"
            ) from exc
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(PublishIntent).where(
                    PublishIntent.idempotency_key == key
                )
            )
            if replay is not None:
                return self.get_verified(replay.id)
            raise PublishIntentConflictError(
                "Publish intent changed concurrently or failed provenance checks"
            ) from exc
        return intent

    def get_verified(self, intent_id: str) -> PublishIntent:
        intent = self.session.get(PublishIntent, intent_id)
        if intent is None:
            raise PublishIntentNotFoundError("PublishIntent was not found")
        expected = publish_intent_payload(
            review_run_id=intent.review_run_id,
            execution_attempt_id=intent.execution_attempt_id,
            task_id=intent.task_id,
            actor_type=intent.actor_type,
            actor_id=intent.actor_id,
            upstream_repository=intent.upstream_repository,
            base_commit_sha=intent.base_commit_sha,
            head_branch=intent.head_branch,
            head_commit_sha=intent.head_commit_sha,
            diff_hash=intent.diff_hash,
            test_results_hash=intent.test_results_hash,
            review_record_hash=intent.review_record_hash,
            title=intent.title,
            body=intent.body,
            allowed_actions=list(intent.allowed_actions),
            confirmation_nonce=intent.confirmation_nonce,
            expires_at=intent.expires_at,
        )
        if (
            intent.schema_version != PUBLISH_INTENT_SCHEMA_VERSION
            or intent.record_hash != content_hash(expected)
        ):
            raise PublishIntentConflictError(
                "PublishIntent content does not match its immutable inputs"
            )
        return intent

    def status(
        self,
        intent_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        intent = self.get_verified(intent_id)
        confirmation = self.session.scalar(
            select(PublishConfirmation).where(
                PublishConfirmation.publish_intent_id == intent.id
            )
        )
        if confirmation is not None:
            return "confirmed"
        if _aware(now) >= _aware(intent.expires_at):
            return "expired"
        reviews = ReviewRunService(self.session)
        if reviews.is_stale(intent.review_run_id):
            return "invalidated"
        return "pending"

    def confirm(
        self,
        *,
        intent_id: str,
        action: UserAction | str,
        actor_id: str,
        confirmation_nonce: str | None,
        now: datetime | None = None,
    ) -> tuple[PublishIntent, DraftPullRequest, PublishConfirmation]:
        try:
            require_user_action(action, expected=UserAction.PUBLISH_DRAFT_PR)
        except AuthorizationActionError as exc:
            raise PublishIntentForbiddenError(str(exc)) from exc
        if not confirmation_nonce or not _NONCE.fullmatch(confirmation_nonce):
            raise PublishIntentForbiddenError(
                "Publication requires the exact confirmation nonce"
            )
        intent = self.get_verified(intent_id)
        actor = _actor_id(actor_id)
        existing = self.session.scalar(
            select(PublishConfirmation).where(
                PublishConfirmation.publish_intent_id == intent.id
            )
        )
        if existing is not None:
            raise PublishIntentConflictError(
                "Publish intent was already confirmed"
            )
        if actor != intent.actor_id:
            raise PublishIntentConflictError(
                "Publish confirmation actor does not match the intent"
            )
        if confirmation_nonce != intent.confirmation_nonce:
            raise PublishIntentForbiddenError(
                "Publication requires the exact confirmation nonce"
            )
        current_time = _aware(now)
        if current_time >= _aware(intent.expires_at):
            raise PublishIntentConflictError("Publish intent has expired")
        reviews = ReviewRunService(self.session)
        review = reviews.get_verified(intent.review_run_id)
        if (
            reviews.is_stale(review.id)
            or review.record_hash != intent.review_record_hash
            or review.diff_hash != intent.diff_hash
            or review.test_results_hash != intent.test_results_hash
            or review.verdict != "pass"
        ):
            raise PublishIntentStaleError(
                "Bound review or hashes changed; no remote write was created"
            )
        states = ContributionTaskStateService(self.session)
        try:
            current = states.current(intent.task_id)
            draft_state = states.prepare_transition(
                intent.task_id,
                expected_sequence=current.sequence,
                expected_record_hash=current.record_hash,
                to_state=ContributionTaskState.DRAFT_PR,
                reason_code="draft_pr_published",
                now=now,
            )
        except TaskStateError as exc:
            raise PublishIntentConflictError(str(exc)) from exc
        if current.to_state != ContributionTaskState.READY.value:
            raise PublishIntentConflictError(
                "Confirmation requires the current ready task state"
            )
        try:
            published = self.publisher.create_draft_pull_request(
                PublishRequest(
                    upstream_repository=intent.upstream_repository,
                    head_branch=intent.head_branch,
                    base_commit_sha=intent.base_commit_sha,
                    head_commit_sha=intent.head_commit_sha,
                    title=intent.title,
                    body=intent.body,
                    actions=tuple(intent.allowed_actions),
                    diff_hash=intent.diff_hash,
                    review_record_hash=intent.review_record_hash,
                )
            )
        except PublisherProhibitedError as exc:
            raise PublishIntentConflictError(str(exc)) from exc
        except PublisherError as exc:
            raise PublishIntentConflictError(str(exc)) from exc
        draft_payload = draft_pull_request_payload(
            publish_intent_id=intent.id,
            task_id=intent.task_id,
            provider=published.provider,
            number=published.number,
            html_url=published.html_url,
            head_branch=intent.head_branch,
            base_commit_sha=intent.base_commit_sha,
            head_commit_sha=intent.head_commit_sha,
            diff_hash=intent.diff_hash,
            review_record_hash=intent.review_record_hash,
        )
        draft = DraftPullRequest(
            id=str(uuid4()),
            publish_intent_id=intent.id,
            task_id=intent.task_id,
            schema_version=DRAFT_PULL_REQUEST_SCHEMA_VERSION,
            provider=published.provider,
            number=published.number,
            html_url=published.html_url,
            head_branch=intent.head_branch,
            base_commit_sha=intent.base_commit_sha,
            head_commit_sha=intent.head_commit_sha,
            diff_hash=intent.diff_hash,
            review_record_hash=intent.review_record_hash,
            record_hash=content_hash(draft_payload),
            created_at=current_time,
        )
        confirmation_payload = publish_confirmation_payload(
            publish_intent_id=intent.id,
            draft_pull_request_id=draft.id,
            actor_type=intent.actor_type,
            actor_id=actor,
            confirmation_nonce=intent.confirmation_nonce,
            draft_pr_state_version_id=draft_state.id,
            draft_pr_state_record_hash=draft_state.record_hash,
        )
        confirmation = PublishConfirmation(
            id=str(uuid4()),
            publish_intent_id=intent.id,
            draft_pull_request_id=draft.id,
            schema_version=PUBLISH_CONFIRMATION_SCHEMA_VERSION,
            actor_type=intent.actor_type,
            actor_id=actor,
            confirmation_nonce=intent.confirmation_nonce,
            draft_pr_state_version_id=draft_state.id,
            draft_pr_state_record_hash=draft_state.record_hash,
            record_hash=content_hash(confirmation_payload),
            created_at=current_time,
        )
        ensure_no_sensitive_data(
            {
                "draft_pull_request": draft_payload,
                "publish_confirmation": confirmation_payload,
            },
            context="PublishConfirmation",
        )
        self.session.add_all((draft_state, draft))
        self.session.flush()
        self.session.add(confirmation)
        try:
            AuditService(self.session).prepare(
                event_type="publish_intent.confirmed",
                actor_type=intent.actor_type,
                actor_id=actor,
                correlation_id=intent.idempotency_key,
                payload={
                    "publish_intent_id": intent.id,
                    "publish_intent_hash": intent.record_hash,
                    "draft_pull_request_id": draft.id,
                    "draft_pull_request_hash": draft.record_hash,
                    "provider": draft.provider,
                    "number": draft.number,
                },
                now=now,
            )
        except Exception as exc:
            self.session.rollback()
            raise PublishIntentConflictError(
                "Publish confirmation audit evidence could not be prepared"
            ) from exc
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise PublishIntentConflictError(
                "Publish confirmation changed concurrently or failed provenance checks"
            ) from exc
        return intent, draft, confirmation

    def draft_for_intent(self, intent_id: str) -> DraftPullRequest | None:
        return self.session.scalar(
            select(DraftPullRequest).where(
                DraftPullRequest.publish_intent_id == intent_id
            )
        )


def publish_intent_payload(
    *,
    review_run_id: str,
    execution_attempt_id: str,
    task_id: str,
    actor_type: str,
    actor_id: str,
    upstream_repository: str,
    base_commit_sha: str,
    head_branch: str,
    head_commit_sha: str,
    diff_hash: str,
    test_results_hash: str,
    review_record_hash: str,
    title: str,
    body: str,
    allowed_actions: list[str],
    confirmation_nonce: str,
    expires_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": PUBLISH_INTENT_SCHEMA_VERSION,
        "review_run_id": review_run_id,
        "execution_attempt_id": execution_attempt_id,
        "task_id": task_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "upstream_repository": upstream_repository,
        "base_commit_sha": base_commit_sha,
        "head_branch": head_branch,
        "head_commit_sha": head_commit_sha,
        "diff_hash": diff_hash,
        "test_results_hash": test_results_hash,
        "review_record_hash": review_record_hash,
        "title": title,
        "body": body,
        "allowed_actions": allowed_actions,
        "confirmation_nonce": confirmation_nonce,
        "expires_at": _aware(expires_at).isoformat(),
    }


def draft_pull_request_payload(
    *,
    publish_intent_id: str,
    task_id: str,
    provider: str,
    number: int,
    html_url: str,
    head_branch: str,
    base_commit_sha: str,
    head_commit_sha: str,
    diff_hash: str,
    review_record_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": DRAFT_PULL_REQUEST_SCHEMA_VERSION,
        "publish_intent_id": publish_intent_id,
        "task_id": task_id,
        "provider": provider,
        "number": number,
        "html_url": html_url,
        "head_branch": head_branch,
        "base_commit_sha": base_commit_sha,
        "head_commit_sha": head_commit_sha,
        "diff_hash": diff_hash,
        "review_record_hash": review_record_hash,
    }


def publish_confirmation_payload(
    *,
    publish_intent_id: str,
    draft_pull_request_id: str,
    actor_type: str,
    actor_id: str,
    confirmation_nonce: str,
    draft_pr_state_version_id: str,
    draft_pr_state_record_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": PUBLISH_CONFIRMATION_SCHEMA_VERSION,
        "publish_intent_id": publish_intent_id,
        "draft_pull_request_id": draft_pull_request_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "confirmation_nonce": confirmation_nonce,
        "draft_pr_state_version_id": draft_pr_state_version_id,
        "draft_pr_state_record_hash": draft_pr_state_record_hash,
    }


def _idempotency_key(value: str) -> str:
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise PublishIntentConflictError("Publish idempotency key is invalid")
    return value


def _actor_id(value: str) -> str:
    if not _ACTOR_ID.fullmatch(value):
        raise PublishIntentConflictError("Publish actor id is invalid")
    return value


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
