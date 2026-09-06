from __future__ import annotations

import re
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OpportunitySnapshot, ScanRun, ScoreVersion
from app.provenance import canonical_json, content_hash
from app.providers.budgets import AnalysisBudget
from app.providers.contracts import FrozenEvidence, ProviderIdentity
from app.providers.jobs import ProviderAnalysisJobSpec
from app.scoring import SCORE_ALGORITHM_VERSION, SCORE_SCHEMA_VERSION
from app.security import SensitiveDataError, ensure_no_sensitive_data


ANALYSIS_INPUT_SCHEMA_VERSION = "analysis-input-v1"
MAX_ANALYSIS_CANDIDATES = 30
MAX_ISSUE_BODY_CHARACTERS = 65_536
MAX_REPOSITORY_DESCRIPTION_CHARACTERS = 10_000
MAX_FROZEN_EVIDENCE_BYTES = 128_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AnalysisInputError(RuntimeError):
    pass


class AnalysisInputNotFoundError(AnalysisInputError):
    pass


class AnalysisInputIntegrityError(AnalysisInputError):
    pass


class AnalysisInputSafetyError(AnalysisInputError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenCandidateInput:
    schema_version: str
    rule_rank: int
    scan_run_id: str
    snapshot_id: str
    snapshot_inputs_hash: str
    score_version_id: str
    score_output_hash: str
    evidence: tuple[FrozenEvidence, ...]
    input_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != ANALYSIS_INPUT_SCHEMA_VERSION:
            raise AnalysisInputIntegrityError(
                "Frozen analysis input schema version is unsupported"
            )
        if not 1 <= self.rule_rank <= MAX_ANALYSIS_CANDIDATES:
            raise AnalysisInputIntegrityError(
                "Frozen analysis input rank is outside the automatic limit"
            )
        for value, name in (
            (self.scan_run_id, "scan run ID"),
            (self.snapshot_id, "snapshot ID"),
            (self.score_version_id, "score version ID"),
        ):
            if not value.strip() or len(value) > 128:
                raise AnalysisInputIntegrityError(
                    f"Frozen analysis {name} is invalid"
                )
        for value, name in (
            (self.snapshot_inputs_hash, "snapshot inputs hash"),
            (self.score_output_hash, "score output hash"),
            (self.input_hash, "input hash"),
        ):
            if not _SHA256.fullmatch(value):
                raise AnalysisInputIntegrityError(
                    f"Frozen analysis {name} is not a SHA-256 hash"
                )
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.evidence:
            raise AnalysisInputIntegrityError(
                "Frozen analysis input requires evidence"
            )
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise AnalysisInputIntegrityError(
                "Frozen analysis evidence IDs must be unique"
            )
        _ensure_evidence_safe(self.evidence)
        expected = self.calculate_hash(
            rule_rank=self.rule_rank,
            scan_run_id=self.scan_run_id,
            snapshot_id=self.snapshot_id,
            snapshot_inputs_hash=self.snapshot_inputs_hash,
            score_version_id=self.score_version_id,
            score_output_hash=self.score_output_hash,
            evidence=self.evidence,
        )
        if self.input_hash != expected:
            raise AnalysisInputIntegrityError(
                "Frozen analysis input hash does not match its evidence"
            )

    @classmethod
    def create(
        cls,
        *,
        rule_rank: int,
        scan_run_id: str,
        snapshot_id: str,
        snapshot_inputs_hash: str,
        score_version_id: str,
        score_output_hash: str,
        evidence: tuple[FrozenEvidence, ...],
    ) -> "FrozenCandidateInput":
        evidence = tuple(evidence)
        return cls(
            schema_version=ANALYSIS_INPUT_SCHEMA_VERSION,
            rule_rank=rule_rank,
            scan_run_id=scan_run_id,
            snapshot_id=snapshot_id,
            snapshot_inputs_hash=snapshot_inputs_hash,
            score_version_id=score_version_id,
            score_output_hash=score_output_hash,
            evidence=evidence,
            input_hash=cls.calculate_hash(
                rule_rank=rule_rank,
                scan_run_id=scan_run_id,
                snapshot_id=snapshot_id,
                snapshot_inputs_hash=snapshot_inputs_hash,
                score_version_id=score_version_id,
                score_output_hash=score_output_hash,
                evidence=evidence,
            ),
        )

    @staticmethod
    def calculate_hash(
        *,
        rule_rank: int,
        scan_run_id: str,
        snapshot_id: str,
        snapshot_inputs_hash: str,
        score_version_id: str,
        score_output_hash: str,
        evidence: tuple[FrozenEvidence, ...],
    ) -> str:
        return content_hash(
            {
                "schema_version": ANALYSIS_INPUT_SCHEMA_VERSION,
                "rule_rank": rule_rank,
                "scan_run_id": scan_run_id,
                "snapshot_id": snapshot_id,
                "snapshot_inputs_hash": snapshot_inputs_hash,
                "score_version_id": score_version_id,
                "score_output_hash": score_output_hash,
                "evidence": [item.hash_payload() for item in evidence],
            }
        )

    def to_provider_job_spec(
        self,
        *,
        correlation_id: str,
        budget: AnalysisBudget,
        expected_provider: ProviderIdentity,
        inspect_prompt_version: str,
        inspect_policy_version: str,
        inspect_output_schema_version: str,
        analyze_prompt_version: str,
        analyze_policy_version: str,
        analyze_output_schema_version: str,
    ) -> ProviderAnalysisJobSpec:
        return ProviderAnalysisJobSpec(
            correlation_id=correlation_id,
            snapshot_id=self.snapshot_id,
            score_version_id=self.score_version_id,
            evidence=self.evidence,
            inspect_prompt_version=inspect_prompt_version,
            inspect_policy_version=inspect_policy_version,
            inspect_output_schema_version=inspect_output_schema_version,
            analyze_prompt_version=analyze_prompt_version,
            analyze_policy_version=analyze_policy_version,
            analyze_output_schema_version=analyze_output_schema_version,
            budget=budget,
            expected_provider=expected_provider,
            frozen_input_hash=self.input_hash,
        )


class AnalysisInputFreezer:
    def __init__(self, session: Session) -> None:
        self.session = session

    def freeze_top_candidates(
        self,
        *,
        scan_run_id: str,
        limit: int = MAX_ANALYSIS_CANDIDATES,
    ) -> tuple[FrozenCandidateInput, ...]:
        if not 1 <= limit <= MAX_ANALYSIS_CANDIDATES:
            raise ValueError(
                f"Analysis candidate limit must be between 1 and "
                f"{MAX_ANALYSIS_CANDIDATES}"
            )
        scan_run = self.session.get(ScanRun, scan_run_id)
        if scan_run is None:
            raise AnalysisInputNotFoundError("Analysis ScanRun was not found")
        if (
            scan_run.status != "completed"
            or scan_run.provenance_status != "verified"
        ):
            raise AnalysisInputIntegrityError(
                "Analysis inputs require a completed verified ScanRun"
            )
        rows = list(
            self.session.execute(
                select(OpportunitySnapshot, ScoreVersion)
                .join(
                    ScoreVersion,
                    ScoreVersion.snapshot_id == OpportunitySnapshot.id,
                )
                .where(
                    OpportunitySnapshot.scan_run_id == scan_run_id,
                    OpportunitySnapshot.filter_eligible.is_(True),
                    ScoreVersion.algorithm_version == SCORE_ALGORITHM_VERSION,
                )
            )
        )
        verified: list[tuple[OpportunitySnapshot, ScoreVersion]] = []
        for snapshot, score in rows:
            _verify_snapshot(snapshot)
            _verify_score(snapshot, score)
            verified.append((snapshot, score))
        ranked = sorted(
            verified,
            key=lambda pair: (
                -pair[1].score_total,
                -_score_component(pair[1], "project_impact"),
                pair[0].opportunity_id,
                pair[1].id,
            ),
        )
        frozen: list[FrozenCandidateInput] = []
        for rank, (snapshot, score) in enumerate(ranked[:limit], start=1):
            try:
                frozen.append(
                    freeze_candidate_input(
                        snapshot=snapshot,
                        score=score,
                        rule_rank=rank,
                    )
                )
            except AnalysisInputSafetyError:
                # One unsafe Issue must stay excluded without preventing other
                # independently frozen candidates from being analyzed. Do not
                # backfill beyond the requested rule-ranked corpus.
                continue
        return tuple(frozen)


def freeze_candidate_input(
    *,
    snapshot: OpportunitySnapshot,
    score: ScoreVersion,
    rule_rank: int,
) -> FrozenCandidateInput:
    _verify_snapshot(snapshot)
    _verify_score(snapshot, score)
    issue = _safe_issue_evidence(snapshot.issue_data)
    repository = _safe_repository_evidence(snapshot.repository_data)
    score_data = _safe_score_evidence(snapshot, score)
    repository_name = repository["full_name"]
    issue_number = issue["number"]
    evidence = (
        FrozenEvidence.capture(
            evidence_id="issue",
            kind="github_issue_snapshot",
            source_uri=f"github://{repository_name}/issues/{issue_number}",
            content=canonical_json(issue),
        ),
        FrozenEvidence.capture(
            evidence_id="repository",
            kind="github_repository_snapshot",
            source_uri=f"github://{repository_name}",
            content=canonical_json(repository),
        ),
        FrozenEvidence.capture(
            evidence_id="rule_score",
            kind="contribos_rule_score",
            source_uri=f"contribos://score-versions/{score.id}",
            content=canonical_json(score_data),
        ),
    )
    return FrozenCandidateInput.create(
        rule_rank=rule_rank,
        scan_run_id=snapshot.scan_run_id,
        snapshot_id=snapshot.id,
        snapshot_inputs_hash=snapshot.inputs_hash,
        score_version_id=score.id,
        score_output_hash=score.output_hash,
        evidence=evidence,
    )


def _verify_snapshot(snapshot: OpportunitySnapshot) -> None:
    if snapshot.schema_version != "1":
        raise AnalysisInputIntegrityError(
            "OpportunitySnapshot schema version is unsupported"
        )
    payload = {
        "schema_version": snapshot.schema_version,
        "scan_run_id": snapshot.scan_run_id,
        "opportunity_id": snapshot.opportunity_id,
        "captured_at": _utc_iso(snapshot.captured_at),
        "issue_data": snapshot.issue_data,
        "repository_data": snapshot.repository_data,
        "source_queries": snapshot.source_queries,
        "rule_config": snapshot.rule_config,
        "filter_eligible": snapshot.filter_eligible,
        "filter_reasons": snapshot.filter_reasons,
    }
    if content_hash(payload) != snapshot.inputs_hash:
        raise AnalysisInputIntegrityError(
            "OpportunitySnapshot inputs hash does not match frozen data"
        )


def _verify_score(snapshot: OpportunitySnapshot, score: ScoreVersion) -> None:
    if (
        score.snapshot_id != snapshot.id
        or score.inputs_hash != snapshot.inputs_hash
    ):
        raise AnalysisInputIntegrityError(
            "ScoreVersion does not match its OpportunitySnapshot"
        )
    if (
        score.algorithm_version != SCORE_ALGORITHM_VERSION
        or score.schema_version != SCORE_SCHEMA_VERSION
    ):
        raise AnalysisInputIntegrityError(
            "ScoreVersion algorithm or schema version is unsupported"
        )
    for value, name in (
        (score.score_total, "score total"),
        (score.risk_penalty, "risk penalty"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise AnalysisInputIntegrityError(f"{name} is not finite")
    _score_component(score, "project_impact")
    result = {
        "score_total": score.score_total,
        "score_components": score.score_components,
        "risk_penalty": score.risk_penalty,
        "risk_reasons": score.risk_reasons,
        "has_bounty": score.has_bounty,
        "bounty_amount_usd": score.bounty_amount_usd,
        "is_strategic": score.is_strategic,
        "is_tech_match": score.is_tech_match,
    }
    expected = content_hash(
        {
            "inputs_hash": score.inputs_hash,
            "algorithm_version": score.algorithm_version,
            "schema_version": score.schema_version,
            "result": result,
        }
    )
    if score.output_hash != expected:
        raise AnalysisInputIntegrityError(
            "ScoreVersion output hash does not match frozen score data"
        )


def _safe_issue_evidence(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AnalysisInputIntegrityError("Issue snapshot data is not an object")
    body = _string(value, "body", maximum=None)
    labels = _string_list(value, "labels", maximum_items=100, item_maximum=100)
    return _checked_evidence(
        {
            "github_issue_id": _integer(value, "github_issue_id"),
            "number": _integer(value, "number"),
            "title": _string(value, "title", maximum=500),
            "body": body[:MAX_ISSUE_BODY_CHARACTERS],
            "body_truncated": len(body) > MAX_ISSUE_BODY_CHARACTERS,
            "body_content_hash": content_hash(body),
            "html_url": _string(value, "html_url", maximum=1000),
            "state": _string(value, "state", maximum=30),
            "labels": labels,
            "comments_count": _integer(value, "comments_count"),
            "assignees_count": _integer(value, "assignees_count"),
            "author_association": _optional_string(
                value,
                "author_association",
                maximum=40,
            ),
            "created_at": _string(value, "created_at", maximum=64),
            "updated_at": _string(value, "updated_at", maximum=64),
        },
        "Issue evidence",
    )


def _safe_repository_evidence(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AnalysisInputIntegrityError(
            "Repository snapshot data is not an object"
        )
    description = _string(value, "description", maximum=None)
    topics = _string_list(value, "topics", maximum_items=100, item_maximum=100)
    return _checked_evidence(
        {
            "full_name": _string(value, "full_name", maximum=255),
            "description": description[:MAX_REPOSITORY_DESCRIPTION_CHARACTERS],
            "description_truncated": (
                len(description) > MAX_REPOSITORY_DESCRIPTION_CHARACTERS
            ),
            "description_content_hash": content_hash(description),
            "html_url": _optional_string(value, "html_url", maximum=1000),
            "language": _optional_string(value, "language", maximum=80),
            "license_spdx": _optional_string(
                value,
                "license_spdx",
                maximum=80,
            ),
            "stars": _integer(value, "stars"),
            "forks": _integer(value, "forks"),
            "open_issues": _integer(value, "open_issues"),
            "archived": _boolean(value, "archived"),
            "disabled": _boolean(value, "disabled"),
            "default_branch": _optional_string(
                value,
                "default_branch",
                maximum=255,
            ),
            "topics": topics,
            "pushed_at": _optional_string(value, "pushed_at", maximum=64),
            "has_contributing_guide": _boolean(
                value,
                "has_contributing_guide",
            ),
            "health_percentage": _optional_integer(
                value,
                "health_percentage",
            ),
        },
        "repository evidence",
    )


def _safe_score_evidence(
    snapshot: OpportunitySnapshot,
    score: ScoreVersion,
) -> dict[str, object]:
    components = score.score_components
    if not isinstance(components, dict):
        raise AnalysisInputIntegrityError("Score components are not an object")
    safe_components: dict[str, float] = {}
    for name, amount in sorted(components.items()):
        if not isinstance(name, str) or isinstance(amount, bool) or not isinstance(
            amount,
            (int, float),
        ):
            raise AnalysisInputIntegrityError("Score component is malformed")
        safe_components[name] = float(amount)
    return _checked_evidence(
        {
            "algorithm_version": score.algorithm_version,
            "schema_version": score.schema_version,
            "snapshot_inputs_hash": snapshot.inputs_hash,
            "score_output_hash": score.output_hash,
            "score_total": score.score_total,
            "score_components": safe_components,
            "risk_penalty": score.risk_penalty,
            "risk_reasons": _plain_string_list(
                score.risk_reasons,
                "score risk reasons",
                maximum_items=100,
                item_maximum=100,
            ),
            "has_bounty": score.has_bounty,
            "bounty_amount_usd": score.bounty_amount_usd,
            "is_strategic": score.is_strategic,
            "is_tech_match": score.is_tech_match,
        },
        "rule score evidence",
    )


def _checked_evidence(value: dict[str, object], name: str) -> dict[str, object]:
    try:
        ensure_no_sensitive_data(value, context=name)
    except SensitiveDataError as exc:
        raise AnalysisInputSafetyError(
            f"{name} contains credential-like data"
        ) from exc
    if len(canonical_json(value).encode("utf-8")) > MAX_FROZEN_EVIDENCE_BYTES:
        raise AnalysisInputSafetyError(f"{name} exceeds the evidence size limit")
    return value


def _ensure_evidence_safe(evidence: tuple[FrozenEvidence, ...]) -> None:
    for item in evidence:
        try:
            ensure_no_sensitive_data(
                {
                    "evidence_id": item.evidence_id,
                    "kind": item.kind,
                    "source_uri": item.source_uri,
                    "content": item.content,
                },
                context="frozen analysis evidence",
            )
        except SensitiveDataError as exc:
            raise AnalysisInputSafetyError(
                "Frozen analysis evidence contains credential-like data"
            ) from exc
        if len(item.content.encode("utf-8")) > MAX_FROZEN_EVIDENCE_BYTES:
            raise AnalysisInputSafetyError(
                "Frozen analysis evidence exceeds the size limit"
            )


def _string(
    value: dict[str, object],
    key: str,
    *,
    maximum: int | None,
) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise AnalysisInputIntegrityError(f"{key} must be a string")
    if maximum is not None and len(item) > maximum:
        raise AnalysisInputSafetyError(f"{key} exceeds its evidence limit")
    return item


def _optional_string(
    value: dict[str, object],
    key: str,
    *,
    maximum: int,
) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise AnalysisInputIntegrityError(f"{key} must be a string or null")
    if len(item) > maximum:
        raise AnalysisInputSafetyError(f"{key} exceeds its evidence limit")
    return item


def _integer(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise AnalysisInputIntegrityError(f"{key} must be an integer")
    return item


def _optional_integer(value: dict[str, object], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if isinstance(item, bool) or not isinstance(item, int):
        raise AnalysisInputIntegrityError(f"{key} must be an integer or null")
    return item


def _boolean(value: dict[str, object], key: str) -> bool:
    item = value.get(key)
    if not isinstance(item, bool):
        raise AnalysisInputIntegrityError(f"{key} must be a boolean")
    return item


def _string_list(
    value: dict[str, object],
    key: str,
    *,
    maximum_items: int,
    item_maximum: int,
) -> list[str]:
    return _plain_string_list(
        value.get(key),
        key,
        maximum_items=maximum_items,
        item_maximum=item_maximum,
    )


def _plain_string_list(
    value: object,
    name: str,
    *,
    maximum_items: int,
    item_maximum: int,
) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise AnalysisInputIntegrityError(f"{name} must be a string list")
    if len(value) > maximum_items or any(
        len(item) > item_maximum for item in value
    ):
        raise AnalysisInputSafetyError(f"{name} exceeds its evidence limit")
    return list(value)


def _utc_iso(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


def _score_component(score: ScoreVersion, name: str) -> float:
    components = score.score_components
    if not isinstance(components, dict):
        raise AnalysisInputIntegrityError("Score components are not an object")
    value = components.get(name, 0)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise AnalysisInputIntegrityError(f"Score component {name} is not finite")
    return float(value)
