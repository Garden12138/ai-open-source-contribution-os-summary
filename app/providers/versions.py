from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AnalysisVersion,
    Job,
    OpportunitySnapshot,
    ProviderInvocation,
    ScoreVersion,
)
from app.provenance import canonical_json, content_hash
from app.providers.contracts import (
    AnalysisResult,
    AnalyzeRequest,
    InspectionResult,
    InspectRequest,
    ProviderIdentity,
    ProviderStage,
    ProviderUsage,
)
from app.providers.analysis_schema import validate_structured_analysis
from app.providers.evidence import AnalysisInputFreezer
from app.providers.evidence import AnalysisInputError
from app.providers.jobs import (
    PROVIDER_ANALYSIS_JOB_KIND,
    ProviderAnalysisJobSpec,
)
from app.security import ensure_no_sensitive_data


ANALYSIS_VERSION_SCHEMA_VERSION = "1"


class AnalysisVersionError(RuntimeError):
    pass


class AnalysisVersionNotFoundError(AnalysisVersionError):
    pass


class AnalysisVersionIntegrityError(AnalysisVersionError):
    pass


class AnalysisVersionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_from_succeeded_job(
        self,
        *,
        job_id: str,
        now: datetime | None = None,
    ) -> AnalysisVersion:
        job = self.session.get(Job, job_id)
        if job is None:
            raise AnalysisVersionNotFoundError("Provider analysis Job was not found")
        if job.kind != PROVIDER_ANALYSIS_JOB_KIND or job.state != "succeeded":
            raise AnalysisVersionIntegrityError(
                "AnalysisVersion requires a succeeded Provider analysis Job"
            )
        if content_hash(job.payload) != job.payload_hash:
            raise AnalysisVersionIntegrityError(
                "Provider analysis Job payload hash does not match"
            )
        try:
            spec = ProviderAnalysisJobSpec.from_payload(job.payload)
        except (TypeError, ValueError) as exc:
            raise AnalysisVersionIntegrityError(
                "Provider analysis Job payload is invalid"
            ) from exc
        if spec.frozen_input_hash is None:
            raise AnalysisVersionIntegrityError(
                "Provider analysis Job has no frozen input reference"
            )
        snapshot = self.session.get(OpportunitySnapshot, spec.snapshot_id)
        score = self.session.get(ScoreVersion, spec.score_version_id)
        if snapshot is None or score is None:
            raise AnalysisVersionIntegrityError(
                "Provider analysis Job provenance was not found"
            )
        try:
            frozen = next(
                (
                    item
                    for item in AnalysisInputFreezer(
                        self.session
                    ).freeze_top_candidates(
                        scan_run_id=snapshot.scan_run_id,
                    )
                    if item.snapshot_id == snapshot.id
                    and item.score_version_id == score.id
                ),
                None,
            )
        except AnalysisInputError as exc:
            raise AnalysisVersionIntegrityError(
                "Provider Job frozen provenance is invalid"
            ) from exc
        if (
            frozen is None
            or frozen.input_hash != spec.frozen_input_hash
            or frozen.evidence != spec.evidence
        ):
            raise AnalysisVersionIntegrityError(
                "Provider Job frozen input does not match verified provenance"
            )

        try:
            ensure_no_sensitive_data(
                job.result_data,
                context="Provider analysis Job result",
            )
        except ValueError as exc:
            raise AnalysisVersionIntegrityError(
                "Provider analysis Job result contains unsafe data"
            ) from exc
        try:
            inspection, analysis, provider_run = self._validate_results(
                job=job,
                spec=spec,
            )
        except AnalysisVersionIntegrityError:
            raise
        except (TypeError, ValueError) as exc:
            raise AnalysisVersionIntegrityError(
                "Provider analysis Job results are invalid"
            ) from exc
        inspect_invocation, analyze_invocation = self._final_invocations(
            job=job,
            inspection=inspection,
            analysis=analysis,
        )
        usage = _sum_usage(inspection.usage, analysis.usage)
        self._validate_provider_run(
            provider_run,
            spec=spec,
            usage=usage,
        )
        inspection_structured_output = json.loads(
            canonical_json(inspection.structured_output)
        )
        inspection_cited_evidence_ids = list(
            inspection.cited_evidence_ids
        )
        structured_output = json.loads(canonical_json(analysis.structured_output))
        cited_evidence_ids = list(analysis.cited_evidence_ids)
        payload: dict[str, object] = {
            "schema_version": ANALYSIS_VERSION_SCHEMA_VERSION,
            "job_id": job.id,
            "snapshot_id": snapshot.id,
            "score_version_id": score.id,
            "inspect_invocation_id": inspect_invocation.id,
            "analyze_invocation_id": analyze_invocation.id,
            "frozen_input_hash": frozen.input_hash,
            "snapshot_inputs_hash": snapshot.inputs_hash,
            "score_output_hash": score.output_hash,
            "inspect_input_hash": inspection.input_hash,
            "inspect_output_hash": inspection.output_hash,
            "analysis_input_hash": analysis.input_hash,
            "analysis_output_hash": analysis.output_hash,
            "provider": spec.expected_provider.hash_payload(),
            "versions": {
                "inspect": {
                    "prompt": spec.inspect_prompt_version,
                    "policy": spec.inspect_policy_version,
                    "output_schema": spec.inspect_output_schema_version,
                },
                "analyze": {
                    "prompt": spec.analyze_prompt_version,
                    "policy": spec.analyze_policy_version,
                    "output_schema": spec.analyze_output_schema_version,
                },
            },
            "inspection_structured_output": inspection_structured_output,
            "inspection_cited_evidence_ids": inspection_cited_evidence_ids,
            "structured_output": structured_output,
            "cited_evidence_ids": cited_evidence_ids,
            "usage": usage.hash_payload(),
        }
        ensure_no_sensitive_data(payload, context="AnalysisVersion")
        version = AnalysisVersion(
            id=str(uuid4()),
            job_id=job.id,
            snapshot_id=snapshot.id,
            score_version_id=score.id,
            inspect_invocation_id=inspect_invocation.id,
            analyze_invocation_id=analyze_invocation.id,
            schema_version=ANALYSIS_VERSION_SCHEMA_VERSION,
            frozen_input_hash=frozen.input_hash,
            snapshot_inputs_hash=snapshot.inputs_hash,
            score_output_hash=score.output_hash,
            inspect_input_hash=inspection.input_hash,
            inspect_output_hash=inspection.output_hash,
            analysis_input_hash=analysis.input_hash,
            analysis_output_hash=analysis.output_hash,
            provider_name=spec.expected_provider.provider,
            adapter_version=spec.expected_provider.adapter_version,
            model_name=spec.expected_provider.model,
            model_version=spec.expected_provider.model_version,
            inspect_prompt_version=spec.inspect_prompt_version,
            inspect_policy_version=spec.inspect_policy_version,
            inspect_output_schema_version=(
                spec.inspect_output_schema_version
            ),
            analyze_prompt_version=spec.analyze_prompt_version,
            analyze_policy_version=spec.analyze_policy_version,
            analyze_output_schema_version=(
                spec.analyze_output_schema_version
            ),
            inspection_structured_output=inspection_structured_output,
            inspection_cited_evidence_ids=inspection_cited_evidence_ids,
            structured_output=structured_output,
            cited_evidence_ids=cited_evidence_ids,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_microusd=usage.estimated_cost_microusd,
            duration_ms=usage.duration_ms,
            record_hash=content_hash(payload),
            created_at=_aware(now),
        )
        self.session.add(version)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self.session.scalar(
                select(AnalysisVersion).where(
                    AnalysisVersion.job_id == job_id
                )
            )
            if existing is not None and existing.record_hash == version.record_hash:
                return existing
            raise AnalysisVersionIntegrityError(
                "AnalysisVersion database provenance constraints rejected the record"
            ) from exc
        return version

    def _validate_results(
        self,
        *,
        job: Job,
        spec: ProviderAnalysisJobSpec,
    ) -> tuple[InspectionResult, AnalysisResult, Mapping[str, Any]]:
        result_data = _mapping(job.result_data, "Provider Job result")
        _exact_keys(
            result_data,
            {"provider_run", "inspection", "analysis"},
            "Provider Job result",
        )
        provider_run = _mapping(
            result_data["provider_run"],
            "Provider run metadata",
        )
        inspection_data = _result_mapping(
            result_data["inspection"],
            "inspection result",
        )
        analysis_data = _result_mapping(
            result_data["analysis"],
            "analysis result",
        )
        inspect_request = InspectRequest.create(
            request_id=_string(
                inspection_data["request_id"],
                "inspection request ID",
            ),
            correlation_id=spec.correlation_id,
            snapshot_id=spec.snapshot_id,
            score_version_id=spec.score_version_id,
            evidence=spec.evidence,
            prompt_version=spec.inspect_prompt_version,
            policy_version=spec.inspect_policy_version,
            output_schema_version=spec.inspect_output_schema_version,
        )
        if inspect_request.input_hash != inspection_data["input_hash"]:
            raise AnalysisVersionIntegrityError(
                "Inspection input hash does not match the frozen request"
            )
        inspection_citations = _string_list(
            inspection_data["cited_evidence_ids"],
            "inspection citations",
        )
        allowed_evidence = {item.evidence_id for item in spec.evidence}
        if not set(inspection_citations).issubset(allowed_evidence):
            raise AnalysisVersionIntegrityError(
                "Inspection cites evidence outside the frozen input"
            )
        inspection = InspectionResult(
            request_id=inspect_request.request_id,
            input_hash=inspect_request.input_hash,
            provider=spec.expected_provider,
            structured_output=_mapping(
                inspection_data["structured_output"],
                "inspection structured output",
            ),
            cited_evidence_ids=inspection_citations,
            usage=_usage(inspection_data["usage"]),
            output_hash=_string(
                inspection_data["output_hash"],
                "inspection output hash",
            ),
        )
        analyze_request = AnalyzeRequest.create(
            request_id=_string(
                analysis_data["request_id"],
                "analysis request ID",
            ),
            correlation_id=spec.correlation_id,
            snapshot_id=spec.snapshot_id,
            score_version_id=spec.score_version_id,
            inspection=inspection,
            prompt_version=spec.analyze_prompt_version,
            policy_version=spec.analyze_policy_version,
            output_schema_version=spec.analyze_output_schema_version,
        )
        if analyze_request.input_hash != analysis_data["input_hash"]:
            raise AnalysisVersionIntegrityError(
                "Analysis input hash does not match the inspection result"
            )
        analysis_citations = _string_list(
            analysis_data["cited_evidence_ids"],
            "analysis citations",
        )
        if not set(analysis_citations).issubset(set(inspection_citations)):
            raise AnalysisVersionIntegrityError(
                "Analysis cites evidence outside the inspection result"
            )
        analysis = AnalysisResult(
            request_id=analyze_request.request_id,
            input_hash=analyze_request.input_hash,
            provider=spec.expected_provider,
            structured_output=_mapping(
                analysis_data["structured_output"],
                "analysis structured output",
            ),
            cited_evidence_ids=analysis_citations,
            usage=_usage(analysis_data["usage"]),
            output_hash=_string(
                analysis_data["output_hash"],
                "analysis output hash",
            ),
        )
        validate_structured_analysis(
            analysis.structured_output,
            allowed_evidence_ids=inspection_citations,
            schema_version=spec.analyze_output_schema_version,
        )
        return inspection, analysis, provider_run

    def _final_invocations(
        self,
        *,
        job: Job,
        inspection: InspectionResult,
        analysis: AnalysisResult,
    ) -> tuple[ProviderInvocation, ProviderInvocation]:
        invocations = list(
            self.session.scalars(
                select(ProviderInvocation).where(
                    ProviderInvocation.job_id == job.id,
                    ProviderInvocation.attempt_number == job.attempt_count,
                )
            )
        )
        by_stage = {item.stage: item for item in invocations}
        if set(by_stage) != {
            ProviderStage.INSPECT.value,
            ProviderStage.ANALYZE.value,
        }:
            raise AnalysisVersionIntegrityError(
                "Final Provider Job attempt has incomplete invocation accounting"
            )
        inspect_invocation = by_stage[ProviderStage.INSPECT.value]
        analyze_invocation = by_stage[ProviderStage.ANALYZE.value]
        for invocation, result in (
            (inspect_invocation, inspection),
            (analyze_invocation, analysis),
        ):
            if (
                invocation.status != "succeeded"
                or invocation.request_id != result.request_id
                or invocation.input_hash != result.input_hash
                or invocation.output_hash != result.output_hash
                or _invocation_usage(invocation) != result.usage
                or _invocation_provider(invocation) != result.provider
            ):
                raise AnalysisVersionIntegrityError(
                    "Provider invocation accounting does not match Job output"
                )
        return inspect_invocation, analyze_invocation

    @staticmethod
    def _validate_provider_run(
        value: Mapping[str, Any],
        *,
        spec: ProviderAnalysisJobSpec,
        usage: ProviderUsage,
    ) -> None:
        _exact_keys(
            value,
            {
                "snapshot_id",
                "score_version_id",
                "correlation_id",
                "frozen_input_hash",
                "provider",
                "versions",
                "usage",
                "budget",
            },
            "Provider run metadata",
        )
        if (
            value["snapshot_id"] != spec.snapshot_id
            or value["score_version_id"] != spec.score_version_id
            or value["correlation_id"] != spec.correlation_id
            or value["frozen_input_hash"] != spec.frozen_input_hash
            or value["provider"] != spec.expected_provider.hash_payload()
            or _usage(value["usage"]) != usage
        ):
            raise AnalysisVersionIntegrityError(
                "Provider run metadata does not match the frozen Job"
            )
        versions = _mapping(value["versions"], "Provider run versions")
        expected_versions = {
            "inspect": {
                "prompt": spec.inspect_prompt_version,
                "policy": spec.inspect_policy_version,
                "output_schema": spec.inspect_output_schema_version,
            },
            "analyze": {
                "prompt": spec.analyze_prompt_version,
                "policy": spec.analyze_policy_version,
                "output_schema": spec.analyze_output_schema_version,
            },
        }
        if versions != expected_versions:
            raise AnalysisVersionIntegrityError(
                "Provider run versions do not match the queued Job"
            )
        budget = _mapping(value["budget"], "Provider run budget")
        if budget.get("exhausted_reason") is not None:
            raise AnalysisVersionIntegrityError(
                "Successful Provider Job reports an exhausted budget"
            )


def _result_mapping(value: Any, name: str) -> Mapping[str, Any]:
    result = _mapping(value, name)
    _exact_keys(
        result,
        {
            "request_id",
            "input_hash",
            "output_hash",
            "cited_evidence_ids",
            "structured_output",
            "usage",
        },
        name,
    )
    return result


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise AnalysisVersionIntegrityError(f"{name} is not an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise AnalysisVersionIntegrityError(
            f"{name} has missing or unknown fields"
        )


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AnalysisVersionIntegrityError(f"{name} is not a string")
    return value


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise AnalysisVersionIntegrityError(f"{name} is not a string list")
    if len(value) != len(set(value)):
        raise AnalysisVersionIntegrityError(f"{name} contains duplicates")
    return tuple(value)


def _usage(value: Any) -> ProviderUsage:
    item = _mapping(value, "Provider usage")
    _exact_keys(
        item,
        {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "estimated_cost_microusd",
            "duration_ms",
        },
        "Provider usage",
    )
    values: dict[str, int] = {}
    for name in item:
        amount = item[name]
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise AnalysisVersionIntegrityError(
                "Provider usage values must be integers"
            )
        values[name] = amount
    return ProviderUsage(**values)


def _sum_usage(*values: ProviderUsage) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=sum(value.input_tokens for value in values),
        cached_input_tokens=sum(value.cached_input_tokens for value in values),
        output_tokens=sum(value.output_tokens for value in values),
        estimated_cost_microusd=sum(
            value.estimated_cost_microusd for value in values
        ),
        duration_ms=sum(value.duration_ms for value in values),
    )


def _invocation_usage(value: ProviderInvocation) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=value.input_tokens,
        cached_input_tokens=value.cached_input_tokens,
        output_tokens=value.output_tokens,
        estimated_cost_microusd=value.estimated_cost_microusd,
        duration_ms=value.duration_ms,
    )


def _invocation_provider(value: ProviderInvocation) -> ProviderIdentity:
    return ProviderIdentity(
        provider=value.provider_name,
        adapter_version=value.adapter_version,
        model=value.model_name,
        model_version=value.model_version,
    )


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
