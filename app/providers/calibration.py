from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AnalysisVersion, FinalScoreVersion
from app.provenance import content_hash
from app.scoring import WEIGHTS
from app.security import ensure_no_sensitive_data


FINAL_SCORE_ALGORITHM_VERSION = "ai-calibration-v1"
FINAL_SCORE_SCHEMA_VERSION = "1"
_REASON_CODE = re.compile(r"^[a-z0-9_.-]{1,100}$")


class FinalScoreError(RuntimeError):
    pass


class FinalScoreNotFoundError(FinalScoreError):
    pass


class FinalScoreConflictError(FinalScoreError):
    pass


@dataclass(frozen=True, slots=True)
class ScoreCalibration:
    score_total: float
    score_components: Mapping[str, float]
    risk_penalty: float
    risk_reasons: tuple[str, ...]
    rationale: str
    cited_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        components = dict(self.score_components)
        if set(components) != set(WEIGHTS):
            raise ValueError(
                "AI calibration components must match the rule component set"
            )
        normalized: dict[str, float] = {}
        for name, value in sorted(components.items()):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= value <= 100
            ):
                raise ValueError(
                    f"AI calibration component {name} must be between 0 and 100"
                )
            normalized[name] = float(value)
        object.__setattr__(
            self,
            "score_components",
            MappingProxyType(normalized),
        )
        if (
            isinstance(self.risk_penalty, bool)
            or not isinstance(self.risk_penalty, (int, float))
            or not math.isfinite(float(self.risk_penalty))
            or not 0 <= self.risk_penalty <= 100
        ):
            raise ValueError("AI calibration risk penalty must be between 0 and 100")
        expected_total = self.calculate_total(
            score_components=normalized,
            risk_penalty=float(self.risk_penalty),
        )
        if (
            isinstance(self.score_total, bool)
            or not isinstance(self.score_total, (int, float))
            or not math.isfinite(float(self.score_total))
            or float(self.score_total) != expected_total
        ):
            raise ValueError(
                "AI calibration total does not match weighted components and risk"
            )
        reasons = tuple(self.risk_reasons)
        citations = tuple(self.cited_evidence_ids)
        object.__setattr__(self, "risk_reasons", reasons)
        object.__setattr__(self, "cited_evidence_ids", citations)
        if len(reasons) > 20 or len(reasons) != len(set(reasons)):
            raise ValueError(
                "AI calibration risk reasons must be unique and at most 20"
            )
        if any(not _REASON_CODE.fullmatch(reason) for reason in reasons):
            raise ValueError("AI calibration risk reason code is invalid")
        if (
            not self.rationale.strip()
            or len(self.rationale) > 4_000
        ):
            raise ValueError(
                "AI calibration rationale must contain between 1 and 4000 characters"
            )
        if (
            not citations
            or len(citations) > 100
            or len(citations) != len(set(citations))
            or any(not item.strip() or len(item) > 128 for item in citations)
        ):
            raise ValueError(
                "AI calibration citations must be unique, bounded, and non-empty"
            )
        ensure_no_sensitive_data(
            self.hash_payload(),
            context="AI score calibration",
        )

    @staticmethod
    def calculate_total(
        *,
        score_components: Mapping[str, float],
        risk_penalty: float,
    ) -> float:
        weighted = sum(
            float(score_components[name]) * weight
            for name, weight in WEIGHTS.items()
        )
        return round(max(0.0, min(100.0, weighted - risk_penalty)), 1)

    @property
    def calibration_hash(self) -> str:
        return content_hash(self.hash_payload())

    def hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": FINAL_SCORE_SCHEMA_VERSION,
            "score_total": float(self.score_total),
            "score_components": dict(self.score_components),
            "risk_penalty": float(self.risk_penalty),
            "risk_reasons": list(self.risk_reasons),
            "rationale": self.rationale,
            "cited_evidence_ids": list(self.cited_evidence_ids),
        }


class FinalScoreVersionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        analysis_version_id: str,
        calibration: ScoreCalibration,
        now: datetime | None = None,
    ) -> FinalScoreVersion:
        analysis = self.session.get(AnalysisVersion, analysis_version_id)
        if analysis is None:
            raise FinalScoreNotFoundError("AnalysisVersion was not found")
        unknown = set(calibration.cited_evidence_ids) - set(
            analysis.cited_evidence_ids
        )
        if unknown:
            raise ValueError(
                "AI calibration cites evidence outside the AnalysisVersion"
            )
        input_hash = content_hash(
            {
                "algorithm_version": FINAL_SCORE_ALGORITHM_VERSION,
                "schema_version": FINAL_SCORE_SCHEMA_VERSION,
                "snapshot_id": analysis.snapshot_id,
                "snapshot_inputs_hash": analysis.snapshot_inputs_hash,
                "rule_score_version_id": analysis.score_version_id,
                "rule_score_output_hash": analysis.score_output_hash,
                "analysis_version_id": analysis.id,
                "analysis_record_hash": analysis.record_hash,
                "analysis_output_hash": analysis.analysis_output_hash,
                "calibration_hash": calibration.calibration_hash,
            }
        )
        existing = self.session.scalar(
            select(FinalScoreVersion).where(
                FinalScoreVersion.analysis_version_id == analysis.id,
                FinalScoreVersion.algorithm_version
                == FINAL_SCORE_ALGORITHM_VERSION,
            )
        )
        if existing is not None:
            if existing.input_hash != input_hash:
                raise FinalScoreConflictError(
                    "AnalysisVersion already has a different AI calibration"
                )
            return existing
        result = {
            "score_total": float(calibration.score_total),
            "score_components": dict(calibration.score_components),
            "risk_penalty": float(calibration.risk_penalty),
            "risk_reasons": list(calibration.risk_reasons),
            "rationale": calibration.rationale,
            "cited_evidence_ids": list(calibration.cited_evidence_ids),
        }
        output_hash = content_hash(
            {
                "input_hash": input_hash,
                "algorithm_version": FINAL_SCORE_ALGORITHM_VERSION,
                "schema_version": FINAL_SCORE_SCHEMA_VERSION,
                "result": result,
            }
        )
        ensure_no_sensitive_data(result, context="FinalScoreVersion")
        version = FinalScoreVersion(
            id=str(uuid4()),
            snapshot_id=analysis.snapshot_id,
            rule_score_version_id=analysis.score_version_id,
            analysis_version_id=analysis.id,
            algorithm_version=FINAL_SCORE_ALGORITHM_VERSION,
            schema_version=FINAL_SCORE_SCHEMA_VERSION,
            input_hash=input_hash,
            calibration_hash=calibration.calibration_hash,
            output_hash=output_hash,
            score_total=float(calibration.score_total),
            score_components=dict(calibration.score_components),
            risk_penalty=float(calibration.risk_penalty),
            risk_reasons=list(calibration.risk_reasons),
            rationale=calibration.rationale,
            cited_evidence_ids=list(calibration.cited_evidence_ids),
            created_at=_aware(now),
        )
        self.session.add(version)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self.session.scalar(
                select(FinalScoreVersion).where(
                    FinalScoreVersion.analysis_version_id == analysis.id,
                    FinalScoreVersion.algorithm_version
                    == FINAL_SCORE_ALGORITHM_VERSION,
                )
            )
            if existing is not None and existing.input_hash == input_hash:
                return existing
            raise FinalScoreConflictError(
                "FinalScoreVersion provenance constraints rejected the calibration"
            ) from exc
        return version


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
