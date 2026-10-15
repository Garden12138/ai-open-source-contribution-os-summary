"""Minimal, deterministic facts for planning; never an AI recommendation or approval."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AnalysisVersion, OpportunitySnapshot, ScoreVersion
from app.provenance import content_hash
from app.providers.evidence import AnalysisInputError, snapshot_evidence
from app.security import ensure_no_sensitive_data

SNAPSHOT_ASSESSMENT_SCHEMA = "snapshot-assessment-v1"


def assessment_values(snapshot: OpportunitySnapshot, score: ScoreVersion) -> dict:
    """Derive all facts and hashes from verified, immutable inputs, with no model call."""
    try:
        evidence = snapshot_evidence(snapshot, score)
    except AnalysisInputError:
        raise ValueError("Snapshot assessment inputs could not be verified") from None
    if not snapshot.filter_eligible:
        raise ValueError("Snapshot is ineligible for contribution planning")
    frozen = {
        "schema_version": SNAPSHOT_ASSESSMENT_SCHEMA,
        "snapshot_id": snapshot.id,
        "snapshot_inputs_hash": snapshot.inputs_hash,
        "score_version_id": score.id,
        "score_output_hash": score.output_hash,
        "evidence": [item.hash_payload() for item in evidence],
    }
    inspection = {"source_kind": "snapshot_facts", "evidence": frozen["evidence"]}
    structured = {
        "schema_version": SNAPSHOT_ASSESSMENT_SCHEMA,
        "source_kind": "snapshot_facts",
        "assessment_status": "unassessed",
        "repository": {"value": snapshot.repository_data["full_name"], "evidence_ids": ["repository"]},
        "requirement": {"value": snapshot.issue_data["title"], "evidence_ids": ["issue"]},
        "unknowns": ["implementation_scope", "effort", "verification_environment", "baseline", "maintainer_acceptance"],
    }
    ensure_no_sensitive_data(frozen, context="snapshot assessment")
    frozen_hash = content_hash(frozen)
    inspection_hash = content_hash(inspection)
    return {
        "schema_version": "2",
        "job_id": None,
        "snapshot_id": snapshot.id,
        "score_version_id": score.id,
        "inspect_invocation_id": None,
        "analyze_invocation_id": None,
        "frozen_input_hash": frozen_hash,
        "snapshot_inputs_hash": snapshot.inputs_hash,
        "score_output_hash": score.output_hash,
        "inspect_input_hash": frozen_hash,
        "inspect_output_hash": inspection_hash,
        "analysis_input_hash": content_hash({"frozen_input_hash": frozen_hash, "inspection_hash": inspection_hash}),
        "analysis_output_hash": content_hash(structured),
        "provider_name": "snapshot_facts",
        "adapter_version": SNAPSHOT_ASSESSMENT_SCHEMA,
        "model_name": "none",
        "model_version": "none",
        "inspect_prompt_version": "not-applicable",
        "inspect_policy_version": SNAPSHOT_ASSESSMENT_SCHEMA,
        "inspect_output_schema_version": SNAPSHOT_ASSESSMENT_SCHEMA,
        "analyze_prompt_version": "not-applicable",
        "analyze_policy_version": SNAPSHOT_ASSESSMENT_SCHEMA,
        "analyze_output_schema_version": SNAPSHOT_ASSESSMENT_SCHEMA,
        "inspection_structured_output": inspection,
        "inspection_cited_evidence_ids": [item.evidence_id for item in evidence],
        "structured_output": structured,
        "cited_evidence_ids": ["issue", "repository"],
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_microusd": 0,
        "duration_ms": 0,
    }


def verify_snapshot_assessment(
    analysis: AnalysisVersion, snapshot: OpportunitySnapshot, score: ScoreVersion,
) -> None:
    expected = assessment_values(snapshot, score)
    if any(getattr(analysis, key) != value for key, value in expected.items()):
        raise ValueError("Snapshot assessment does not match its immutable facts")


class SnapshotAssessmentService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_or_create(self, snapshot_id: str) -> AnalysisVersion:
        from app.planning import _analysis_record_payload, _validate_analysis

        def existing() -> AnalysisVersion | None:
            return self.session.scalar(select(AnalysisVersion).where(
                AnalysisVersion.snapshot_id == snapshot_id,
                AnalysisVersion.schema_version == "2",
            ))

        previous = existing()
        if previous is not None:
            _validate_analysis(self.session, previous)
            return previous
        snapshot = self.session.get(OpportunitySnapshot, snapshot_id)
        if snapshot is None:
            from app.planning import ContributionTaskNotFoundError
            raise ContributionTaskNotFoundError("OpportunitySnapshot was not found")
        score = self.session.scalar(select(ScoreVersion).where(
            ScoreVersion.snapshot_id == snapshot.id,
        ).order_by(desc(ScoreVersion.created_at), desc(ScoreVersion.id)))
        if score is None:
            raise ValueError("Snapshot has no verified rule score")
        values = assessment_values(snapshot, score)
        analysis = AnalysisVersion(id=str(uuid4()), created_at=datetime.now(timezone.utc), **values)
        analysis.record_hash = content_hash(_analysis_record_payload(analysis))
        try:
            with self.session.begin_nested():
                self.session.add(analysis)
                self.session.flush()
        except IntegrityError:
            previous = existing()
            if previous is None:
                raise ValueError("Snapshot assessment provenance was rejected") from None
            _validate_analysis(self.session, previous)
            return previous
        return analysis
