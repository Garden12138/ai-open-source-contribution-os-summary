from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AnalysisVersion,
    ContributionTask,
    OpportunitySnapshot,
    ScoreVersion,
)
from app.provenance import canonical_json, content_hash
from app.security import contains_sensitive_text, ensure_no_sensitive_data
from app.task_states import create_initial_task_state


CONTRIBUTION_TASK_SCHEMA_VERSION = "1"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class ContributionTaskError(RuntimeError):
    pass


class ContributionTaskNotFoundError(ContributionTaskError):
    pass


class ContributionTaskConflictError(ContributionTaskError):
    pass


class ContributionTaskService:
    """Create one immutable task root from one verified AnalysisVersion."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        analysis_version_id: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ContributionTask:
        key = _idempotency_key(idempotency_key)
        analysis = self.session.get(AnalysisVersion, analysis_version_id)
        if analysis is None:
            raise ContributionTaskNotFoundError(
                "AnalysisVersion was not found"
            )
        snapshot, _ = _validate_analysis(self.session, analysis)

        replay = self.session.scalar(
            select(ContributionTask).where(
                ContributionTask.idempotency_key == key
            )
        )
        if replay is not None:
            if replay.analysis_version_id != analysis.id:
                raise ContributionTaskConflictError(
                    "Idempotency key already belongs to a different analysis"
                )
            return self.get_verified(replay.id)

        existing = self.session.scalar(
            select(ContributionTask).where(
                ContributionTask.analysis_version_id == analysis.id
            )
        )
        if existing is not None:
            return self.get_verified(existing.id)

        record_payload = contribution_task_record_payload(
            analysis_version_id=analysis.id,
            snapshot_id=snapshot.id,
            opportunity_id=snapshot.opportunity_id,
            analysis_record_hash=analysis.record_hash,
            analysis_output_hash=analysis.analysis_output_hash,
            snapshot_inputs_hash=analysis.snapshot_inputs_hash,
        )
        ensure_no_sensitive_data(
            {
                "idempotency_key": key,
                "record": record_payload,
            },
            context="ContributionTask",
        )
        task = ContributionTask(
            id=str(uuid4()),
            analysis_version_id=analysis.id,
            snapshot_id=snapshot.id,
            opportunity_id=snapshot.opportunity_id,
            schema_version=CONTRIBUTION_TASK_SCHEMA_VERSION,
            idempotency_key=key,
            analysis_record_hash=analysis.record_hash,
            analysis_output_hash=analysis.analysis_output_hash,
            snapshot_inputs_hash=analysis.snapshot_inputs_hash,
            record_hash=content_hash(record_payload),
            created_at=_aware(now),
        )
        self.session.add(task)
        create_initial_task_state(
            self.session,
            task,
            now=task.created_at,
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            replay = self.session.scalar(
                select(ContributionTask).where(
                    ContributionTask.idempotency_key == key
                )
            )
            if (
                replay is not None
                and replay.analysis_version_id == analysis.id
                and replay.record_hash == task.record_hash
            ):
                return self.get_verified(replay.id)
            existing = self.session.scalar(
                select(ContributionTask).where(
                    ContributionTask.analysis_version_id == analysis.id
                )
            )
            if existing is not None and existing.record_hash == task.record_hash:
                return self.get_verified(existing.id)
            raise ContributionTaskConflictError(
                "ContributionTask provenance constraints rejected the record"
            ) from exc
        return task

    def get_verified(self, task_id: str) -> ContributionTask:
        task = self.session.get(ContributionTask, task_id)
        if task is None:
            raise ContributionTaskNotFoundError(
                "ContributionTask was not found"
            )
        analysis = self.session.get(
            AnalysisVersion,
            task.analysis_version_id,
        )
        if analysis is None:
            raise ContributionTaskConflictError(
                "ContributionTask AnalysisVersion was not found"
            )
        snapshot, _ = _validate_analysis(self.session, analysis)
        expected_hash = content_hash(
            contribution_task_record_payload(
                analysis_version_id=analysis.id,
                snapshot_id=snapshot.id,
                opportunity_id=snapshot.opportunity_id,
                analysis_record_hash=analysis.record_hash,
                analysis_output_hash=analysis.analysis_output_hash,
                snapshot_inputs_hash=analysis.snapshot_inputs_hash,
            )
        )
        if (
            task.schema_version != CONTRIBUTION_TASK_SCHEMA_VERSION
            or task.snapshot_id != snapshot.id
            or task.opportunity_id != snapshot.opportunity_id
            or task.analysis_record_hash != analysis.record_hash
            or task.analysis_output_hash != analysis.analysis_output_hash
            or task.snapshot_inputs_hash != analysis.snapshot_inputs_hash
            or task.record_hash != expected_hash
        ):
            raise ContributionTaskConflictError(
                "ContributionTask record does not match its immutable analysis"
            )
        return task


def contribution_task_record_payload(
    *,
    analysis_version_id: str,
    snapshot_id: str,
    opportunity_id: int,
    analysis_record_hash: str,
    analysis_output_hash: str,
    snapshot_inputs_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": CONTRIBUTION_TASK_SCHEMA_VERSION,
        "analysis_version_id": analysis_version_id,
        "snapshot_id": snapshot_id,
        "opportunity_id": opportunity_id,
        "analysis_record_hash": analysis_record_hash,
        "analysis_output_hash": analysis_output_hash,
        "snapshot_inputs_hash": snapshot_inputs_hash,
    }


def _validate_analysis(
    session: Session,
    analysis: AnalysisVersion,
) -> tuple[OpportunitySnapshot, ScoreVersion]:
    snapshot = session.get(
        OpportunitySnapshot,
        analysis.snapshot_id,
    )
    score = session.get(
        ScoreVersion,
        analysis.score_version_id,
    )
    if snapshot is None or score is None:
        raise ContributionTaskConflictError(
            "AnalysisVersion provenance was not found"
        )
    if (
        snapshot.inputs_hash != analysis.snapshot_inputs_hash
        or score.snapshot_id != snapshot.id
        or score.output_hash != analysis.score_output_hash
    ):
        raise ContributionTaskConflictError(
            "AnalysisVersion provenance does not match its frozen inputs"
        )
    if content_hash(_analysis_record_payload(analysis)) != analysis.record_hash:
        raise ContributionTaskConflictError(
            "AnalysisVersion record hash does not match its immutable content"
        )
    return snapshot, score


def _analysis_record_payload(
    analysis: AnalysisVersion,
) -> dict[str, object]:
    return {
        "schema_version": analysis.schema_version,
        "job_id": analysis.job_id,
        "snapshot_id": analysis.snapshot_id,
        "score_version_id": analysis.score_version_id,
        "inspect_invocation_id": analysis.inspect_invocation_id,
        "analyze_invocation_id": analysis.analyze_invocation_id,
        "frozen_input_hash": analysis.frozen_input_hash,
        "snapshot_inputs_hash": analysis.snapshot_inputs_hash,
        "score_output_hash": analysis.score_output_hash,
        "inspect_input_hash": analysis.inspect_input_hash,
        "inspect_output_hash": analysis.inspect_output_hash,
        "analysis_input_hash": analysis.analysis_input_hash,
        "analysis_output_hash": analysis.analysis_output_hash,
        "provider": {
            "provider": analysis.provider_name,
            "adapter_version": analysis.adapter_version,
            "model": analysis.model_name,
            "model_version": analysis.model_version,
        },
        "versions": {
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
        },
        "inspection_structured_output": _json_copy(
            analysis.inspection_structured_output
        ),
        "inspection_cited_evidence_ids": list(
            analysis.inspection_cited_evidence_ids
        ),
        "structured_output": _json_copy(analysis.structured_output),
        "cited_evidence_ids": list(analysis.cited_evidence_ids),
        "usage": {
            "input_tokens": analysis.input_tokens,
            "cached_input_tokens": analysis.cached_input_tokens,
            "output_tokens": analysis.output_tokens,
            "estimated_cost_microusd": analysis.estimated_cost_microusd,
            "duration_ms": analysis.duration_ms,
        },
    }


def _json_copy(value: dict[str, object]) -> dict[str, object]:
    return json.loads(canonical_json(value))


def _idempotency_key(value: str) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if (
        not _IDEMPOTENCY_KEY.fullmatch(key)
        or contains_sensitive_text(key)
    ):
        raise ValueError("ContributionTask idempotency key is invalid")
    return key


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
