from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AnalysisVersion, PlanLock, PlanVersion
from app.planning import (
    ContributionTaskError,
    ContributionTaskService,
)
from app.plans import (
    PlanVersionError,
    PlanVersionService,
)
from app.provenance import content_hash
from app.security import contains_sensitive_text, ensure_no_sensitive_data
from app.task_states import (
    ContributionTaskState,
    ContributionTaskStateService,
    TaskStateError,
)


PLAN_LOCK_SCHEMA_VERSION = "1"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_BASE_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PlanLockError(RuntimeError):
    pass


class PlanLockNotFoundError(PlanLockError):
    pass


class PlanLockConflictError(PlanLockError):
    pass


class PlanLockService:
    """Freeze exact pre-approval inputs without granting approval."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        plan_version_id: str,
        base_commit_sha: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> PlanLock:
        base_sha = _base_commit_sha(base_commit_sha)
        key = _idempotency_key(idempotency_key)
        try:
            plan = PlanVersionService(
                self.session
            ).get_verified(plan_version_id)
        except PlanVersionError as exc:
            raise PlanLockNotFoundError(str(exc)) from exc
        try:
            task = ContributionTaskService(
                self.session
            ).get_verified(plan.task_id)
        except ContributionTaskError as exc:
            raise PlanLockConflictError(str(exc)) from exc
        analysis = self.session.get(
            AnalysisVersion,
            task.analysis_version_id,
        )
        if analysis is None:
            raise PlanLockConflictError(
                "PlanLock AnalysisVersion was not found"
            )

        replay = self.session.scalar(
            select(PlanLock).where(PlanLock.idempotency_key == key)
        )
        if replay is not None:
            if (
                replay.plan_version_id != plan.id
                or replay.base_commit_sha != base_sha
            ):
                raise PlanLockConflictError(
                    "PlanLock idempotency key belongs to different inputs"
                )
            return self.get_verified(replay.id)
        existing = self.session.scalar(
            select(PlanLock).where(
                PlanLock.plan_version_id == plan.id
            )
        )
        if existing is not None:
            if existing.base_commit_sha != base_sha:
                raise PlanLockConflictError(
                    "PlanVersion is already locked to a different base commit"
                )
            return self.get_verified(existing.id)

        try:
            state = ContributionTaskStateService(
                self.session
            ).current(task.id)
        except TaskStateError as exc:
            raise PlanLockConflictError(str(exc)) from exc
        if (
            state.to_state != ContributionTaskState.PLANNING.value
            or plan.task_state_version_id != state.id
            or plan.task_state_record_hash != state.record_hash
        ):
            raise PlanLockConflictError(
                "PlanVersion is stale against the current planning state"
            )
        latest = self.session.scalar(
            select(PlanVersion)
            .where(PlanVersion.task_id == task.id)
            .order_by(
                PlanVersion.version_number.desc(),
                PlanVersion.id.desc(),
            )
            .limit(1)
        )
        if latest is None or latest.id != plan.id:
            raise PlanLockConflictError(
                "Only the latest PlanVersion can be locked"
            )

        provider_contract = provider_contract_payload(analysis)
        provider_contract_hash = content_hash(provider_contract)
        lock_payload = plan_lock_payload(
            plan_version_id=plan.id,
            task_id=task.id,
            task_state_version_id=state.id,
            analysis_version_id=analysis.id,
            snapshot_id=analysis.snapshot_id,
            base_commit_sha=base_sha,
            task_record_hash=task.record_hash,
            task_state_record_hash=state.record_hash,
            analysis_record_hash=analysis.record_hash,
            analysis_output_hash=analysis.analysis_output_hash,
            snapshot_inputs_hash=analysis.snapshot_inputs_hash,
            provider_contract_hash=provider_contract_hash,
            plan_content_hash=plan.content_hash,
            plan_record_hash=plan.record_hash,
        )
        ensure_no_sensitive_data(
            {
                "idempotency_key": key,
                "provider_contract": provider_contract,
                "lock": lock_payload,
            },
            context="PlanLock",
        )
        lock = PlanLock(
            id=str(uuid4()),
            plan_version_id=plan.id,
            task_id=task.id,
            task_state_version_id=state.id,
            analysis_version_id=analysis.id,
            snapshot_id=analysis.snapshot_id,
            schema_version=PLAN_LOCK_SCHEMA_VERSION,
            idempotency_key=key,
            base_commit_sha=base_sha,
            task_record_hash=task.record_hash,
            task_state_record_hash=state.record_hash,
            analysis_record_hash=analysis.record_hash,
            analysis_output_hash=analysis.analysis_output_hash,
            snapshot_inputs_hash=analysis.snapshot_inputs_hash,
            provider_name=analysis.provider_name,
            adapter_version=analysis.adapter_version,
            model_name=analysis.model_name,
            model_version=analysis.model_version,
            inspect_prompt_version=analysis.inspect_prompt_version,
            inspect_policy_version=analysis.inspect_policy_version,
            inspect_output_schema_version=(
                analysis.inspect_output_schema_version
            ),
            analyze_prompt_version=analysis.analyze_prompt_version,
            analyze_policy_version=analysis.analyze_policy_version,
            analyze_output_schema_version=(
                analysis.analyze_output_schema_version
            ),
            provider_contract_hash=provider_contract_hash,
            plan_content_hash=plan.content_hash,
            plan_record_hash=plan.record_hash,
            lock_hash=content_hash(lock_payload),
            created_at=_aware(now),
        )
        self.session.add(lock)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(PlanLock).where(PlanLock.idempotency_key == key)
            )
            if (
                replay is not None
                and replay.plan_version_id == plan.id
                and replay.base_commit_sha == base_sha
            ):
                return self.get_verified(replay.id)
            raise PlanLockConflictError(
                "PlanLock provenance constraints rejected the record"
            ) from exc
        return lock

    def get_verified(self, lock_id: str) -> PlanLock:
        lock = self.session.get(PlanLock, lock_id)
        if lock is None:
            raise PlanLockNotFoundError("PlanLock was not found")
        try:
            plan = PlanVersionService(
                self.session
            ).get_verified(lock.plan_version_id)
            task = ContributionTaskService(
                self.session
            ).get_verified(lock.task_id)
        except (PlanVersionError, ContributionTaskError) as exc:
            raise PlanLockConflictError(str(exc)) from exc
        analysis = self.session.get(
            AnalysisVersion,
            lock.analysis_version_id,
        )
        if analysis is None:
            raise PlanLockConflictError(
                "PlanLock AnalysisVersion was not found"
            )
        provider_contract = provider_contract_payload(analysis)
        provider_hash = content_hash(provider_contract)
        try:
            base_sha = _base_commit_sha(lock.base_commit_sha)
        except ValueError as exc:
            raise PlanLockConflictError(str(exc)) from exc
        expected_hash = content_hash(
            plan_lock_payload(
                plan_version_id=plan.id,
                task_id=task.id,
                task_state_version_id=lock.task_state_version_id,
                analysis_version_id=analysis.id,
                snapshot_id=analysis.snapshot_id,
                base_commit_sha=base_sha,
                task_record_hash=task.record_hash,
                task_state_record_hash=lock.task_state_record_hash,
                analysis_record_hash=analysis.record_hash,
                analysis_output_hash=analysis.analysis_output_hash,
                snapshot_inputs_hash=analysis.snapshot_inputs_hash,
                provider_contract_hash=provider_hash,
                plan_content_hash=plan.content_hash,
                plan_record_hash=plan.record_hash,
            )
        )
        if (
            lock.schema_version != PLAN_LOCK_SCHEMA_VERSION
            or lock.task_id != task.id
            or plan.task_id != task.id
            or task.analysis_version_id != analysis.id
            or lock.task_state_version_id != plan.task_state_version_id
            or lock.task_state_record_hash != plan.task_state_record_hash
            or lock.snapshot_id != analysis.snapshot_id
            or lock.task_record_hash != task.record_hash
            or lock.analysis_record_hash != analysis.record_hash
            or lock.analysis_output_hash != analysis.analysis_output_hash
            or lock.snapshot_inputs_hash != analysis.snapshot_inputs_hash
            or lock.provider_name != analysis.provider_name
            or lock.adapter_version != analysis.adapter_version
            or lock.model_name != analysis.model_name
            or lock.model_version != analysis.model_version
            or lock.inspect_prompt_version
            != analysis.inspect_prompt_version
            or lock.inspect_policy_version
            != analysis.inspect_policy_version
            or lock.inspect_output_schema_version
            != analysis.inspect_output_schema_version
            or lock.analyze_prompt_version
            != analysis.analyze_prompt_version
            or lock.analyze_policy_version
            != analysis.analyze_policy_version
            or lock.analyze_output_schema_version
            != analysis.analyze_output_schema_version
            or lock.provider_contract_hash != provider_hash
            or lock.plan_content_hash != plan.content_hash
            or lock.plan_record_hash != plan.record_hash
            or lock.lock_hash != expected_hash
        ):
            raise PlanLockConflictError(
                "PlanLock content does not match its immutable inputs"
            )
        return lock


def provider_contract_payload(
    analysis: AnalysisVersion,
) -> dict[str, object]:
    return {
        "provider": {
            "name": analysis.provider_name,
            "adapter_version": analysis.adapter_version,
            "model": analysis.model_name,
            "model_version": analysis.model_version,
        },
        "inspect": {
            "prompt": analysis.inspect_prompt_version,
            "policy": analysis.inspect_policy_version,
            "output_schema": analysis.inspect_output_schema_version,
        },
        "analyze": {
            "prompt": analysis.analyze_prompt_version,
            "policy": analysis.analyze_policy_version,
            "output_schema": analysis.analyze_output_schema_version,
        },
    }


def plan_lock_payload(
    *,
    plan_version_id: str,
    task_id: str,
    task_state_version_id: str,
    analysis_version_id: str,
    snapshot_id: str,
    base_commit_sha: str,
    task_record_hash: str,
    task_state_record_hash: str,
    analysis_record_hash: str,
    analysis_output_hash: str,
    snapshot_inputs_hash: str,
    provider_contract_hash: str,
    plan_content_hash: str,
    plan_record_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": PLAN_LOCK_SCHEMA_VERSION,
        "plan_version_id": plan_version_id,
        "task_id": task_id,
        "task_state_version_id": task_state_version_id,
        "analysis_version_id": analysis_version_id,
        "snapshot_id": snapshot_id,
        "base_commit_sha": base_commit_sha,
        "task_record_hash": task_record_hash,
        "task_state_record_hash": task_state_record_hash,
        "analysis_record_hash": analysis_record_hash,
        "analysis_output_hash": analysis_output_hash,
        "snapshot_inputs_hash": snapshot_inputs_hash,
        "provider_contract_hash": provider_contract_hash,
        "plan_content_hash": plan_content_hash,
        "plan_record_hash": plan_record_hash,
    }


def _base_commit_sha(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not _BASE_COMMIT_SHA.fullmatch(normalized):
        raise ValueError("PlanLock base commit SHA is invalid")
    return normalized


def _idempotency_key(value: str) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not _IDEMPOTENCY_KEY.fullmatch(key) or contains_sensitive_text(key):
        raise ValueError("PlanLock idempotency key is invalid")
    return key


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
