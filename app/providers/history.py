from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import AnalysisVersion, Opportunity, OpportunitySnapshot
from app.provenance import canonical_json
from app.security import ensure_no_sensitive_data


class AnalysisHistoryError(RuntimeError):
    pass


class AnalysisHistoryNotFoundError(AnalysisHistoryError):
    pass


class AnalysisHistoryConflictError(AnalysisHistoryError):
    pass


@dataclass(frozen=True, slots=True)
class AnalysisVersionSummary:
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


@dataclass(frozen=True, slots=True)
class AnalysisFieldDifference:
    path: str
    left_present: bool
    right_present: bool
    left: Any
    right: Any


@dataclass(frozen=True, slots=True)
class AnalysisVersionDetail:
    summary: AnalysisVersionSummary
    content: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AnalysisVersionComparison:
    left_version_id: str
    right_version_id: str
    opportunity_id: int
    same_snapshot: bool
    same_rule_score: bool
    same_frozen_input: bool
    left: Mapping[str, Any]
    right: Mapping[str, Any]
    differences: tuple[AnalysisFieldDifference, ...]


class AnalysisHistoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_snapshot(
        self,
        snapshot_id: str,
        *,
        limit: int = 50,
    ) -> tuple[AnalysisVersionSummary, ...]:
        self._limit(limit)
        snapshot = self.session.get(OpportunitySnapshot, snapshot_id)
        if snapshot is None:
            raise AnalysisHistoryNotFoundError(
                "OpportunitySnapshot was not found"
            )
        versions = self.session.scalars(
            select(AnalysisVersion)
            .where(AnalysisVersion.snapshot_id == snapshot.id)
            .order_by(desc(AnalysisVersion.created_at), desc(AnalysisVersion.id))
            .limit(limit)
        )
        return tuple(
            self._summary(version, opportunity_id=snapshot.opportunity_id)
            for version in versions
        )

    def list_for_opportunity(
        self,
        opportunity_id: int,
        *,
        limit: int = 50,
    ) -> tuple[AnalysisVersionSummary, ...]:
        self._limit(limit)
        if self.session.get(Opportunity, opportunity_id) is None:
            raise AnalysisHistoryNotFoundError("Opportunity was not found")
        rows = self.session.execute(
            select(AnalysisVersion, OpportunitySnapshot.opportunity_id)
            .join(
                OpportunitySnapshot,
                OpportunitySnapshot.id == AnalysisVersion.snapshot_id,
            )
            .where(OpportunitySnapshot.opportunity_id == opportunity_id)
            .order_by(desc(AnalysisVersion.created_at), desc(AnalysisVersion.id))
            .limit(limit)
        )
        return tuple(
            self._summary(version, opportunity_id=row_opportunity_id)
            for version, row_opportunity_id in rows
        )

    def compare(
        self,
        left_version_id: str,
        right_version_id: str,
    ) -> AnalysisVersionComparison:
        left = self.session.get(AnalysisVersion, left_version_id)
        right = self.session.get(AnalysisVersion, right_version_id)
        if left is None or right is None:
            raise AnalysisHistoryNotFoundError(
                "AnalysisVersion was not found"
            )
        left_snapshot = self.session.get(
            OpportunitySnapshot,
            left.snapshot_id,
        )
        right_snapshot = self.session.get(
            OpportunitySnapshot,
            right.snapshot_id,
        )
        if left_snapshot is None or right_snapshot is None:
            raise AnalysisHistoryConflictError(
                "AnalysisVersion snapshot provenance was not found"
            )
        if left_snapshot.opportunity_id != right_snapshot.opportunity_id:
            raise AnalysisHistoryConflictError(
                "Analysis versions from different opportunities cannot be compared"
            )
        left_payload = self._comparison_payload(left)
        right_payload = self._comparison_payload(right)
        ensure_no_sensitive_data(
            {
                "left": left_payload,
                "right": right_payload,
            },
            context="analysis version comparison",
        )
        return AnalysisVersionComparison(
            left_version_id=left.id,
            right_version_id=right.id,
            opportunity_id=left_snapshot.opportunity_id,
            same_snapshot=left.snapshot_id == right.snapshot_id,
            same_rule_score=left.score_version_id == right.score_version_id,
            same_frozen_input=left.frozen_input_hash == right.frozen_input_hash,
            left=MappingProxyType(left_payload),
            right=MappingProxyType(right_payload),
            differences=tuple(
                _differences(left_payload, right_payload, path="")
            ),
        )

    def get_detail(
        self,
        version_id: str,
    ) -> AnalysisVersionDetail:
        version = self.session.get(AnalysisVersion, version_id)
        if version is None:
            raise AnalysisHistoryNotFoundError(
                "AnalysisVersion was not found"
            )
        snapshot = self.session.get(
            OpportunitySnapshot,
            version.snapshot_id,
        )
        if snapshot is None:
            raise AnalysisHistoryConflictError(
                "AnalysisVersion snapshot provenance was not found"
            )
        content = self._comparison_payload(version)
        ensure_no_sensitive_data(
            content,
            context="analysis version detail",
        )
        return AnalysisVersionDetail(
            summary=self._summary(
                version,
                opportunity_id=snapshot.opportunity_id,
            ),
            content=MappingProxyType(content),
        )

    @staticmethod
    def _summary(
        version: AnalysisVersion,
        *,
        opportunity_id: int,
    ) -> AnalysisVersionSummary:
        return AnalysisVersionSummary(
            id=version.id,
            job_id=version.job_id,
            opportunity_id=opportunity_id,
            snapshot_id=version.snapshot_id,
            score_version_id=version.score_version_id,
            provider_name=version.provider_name,
            model_name=version.model_name,
            model_version=version.model_version,
            analyze_output_schema_version=(
                version.analyze_output_schema_version
            ),
            record_hash=version.record_hash,
            created_at=version.created_at,
        )

    @staticmethod
    def _comparison_payload(version: AnalysisVersion) -> dict[str, Any]:
        return {
            "provenance": {
                "snapshot_id": version.snapshot_id,
                "score_version_id": version.score_version_id,
                "frozen_input_hash": version.frozen_input_hash,
                "snapshot_inputs_hash": version.snapshot_inputs_hash,
                "score_output_hash": version.score_output_hash,
            },
            "provider": {
                "name": version.provider_name,
                "adapter_version": version.adapter_version,
                "model": version.model_name,
                "model_version": version.model_version,
            },
            "contracts": {
                "inspect_prompt": version.inspect_prompt_version,
                "inspect_policy": version.inspect_policy_version,
                "inspect_output_schema": (
                    version.inspect_output_schema_version
                ),
                "analyze_prompt": version.analyze_prompt_version,
                "analyze_policy": version.analyze_policy_version,
                "analyze_output_schema": (
                    version.analyze_output_schema_version
                ),
            },
            "analysis": _json_copy(version.structured_output),
            "cited_evidence_ids": list(version.cited_evidence_ids),
            "usage": {
                "input_tokens": version.input_tokens,
                "cached_input_tokens": version.cached_input_tokens,
                "output_tokens": version.output_tokens,
                "estimated_cost_microusd": (
                    version.estimated_cost_microusd
                ),
                "duration_ms": version.duration_ms,
            },
            "hashes": {
                "inspect_input": version.inspect_input_hash,
                "inspect_output": version.inspect_output_hash,
                "analysis_input": version.analysis_input_hash,
                "analysis_output": version.analysis_output_hash,
                "record": version.record_hash,
            },
        }

    @staticmethod
    def _limit(value: int) -> None:
        if not 1 <= value <= 100:
            raise ValueError("Analysis version limit must be between 1 and 100")


def _json_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    copied = json.loads(canonical_json(value))
    if not isinstance(copied, dict):
        raise AnalysisHistoryConflictError(
            "AnalysisVersion structured output is invalid"
        )
    return copied


def _differences(
    left: Any,
    right: Any,
    *,
    path: str,
) -> list[AnalysisFieldDifference]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        output: list[AnalysisFieldDifference] = []
        for key in sorted(set(left) | set(right)):
            child_path = f"{path}/{_pointer_token(str(key))}"
            if key not in left:
                output.append(
                    AnalysisFieldDifference(
                        path=child_path,
                        left_present=False,
                        right_present=True,
                        left=None,
                        right=right[key],
                    )
                )
            elif key not in right:
                output.append(
                    AnalysisFieldDifference(
                        path=child_path,
                        left_present=True,
                        right_present=False,
                        left=left[key],
                        right=None,
                    )
                )
            else:
                output.extend(
                    _differences(
                        left[key],
                        right[key],
                        path=child_path,
                    )
                )
        return output
    if (
        isinstance(left, Sequence)
        and not isinstance(left, (str, bytes, bytearray))
        and isinstance(right, Sequence)
        and not isinstance(right, (str, bytes, bytearray))
    ):
        output = []
        for index in range(max(len(left), len(right))):
            child_path = f"{path}/{index}"
            if index >= len(left):
                output.append(
                    AnalysisFieldDifference(
                        path=child_path,
                        left_present=False,
                        right_present=True,
                        left=None,
                        right=right[index],
                    )
                )
            elif index >= len(right):
                output.append(
                    AnalysisFieldDifference(
                        path=child_path,
                        left_present=True,
                        right_present=False,
                        left=left[index],
                        right=None,
                    )
                )
            else:
                output.extend(
                    _differences(
                        left[index],
                        right[index],
                        path=child_path,
                    )
                )
        return output
    if left == right:
        return []
    return [
        AnalysisFieldDifference(
            path=path or "/",
            left_present=True,
            right_present=True,
            left=left,
            right=right,
        )
    ]


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")
