"""Keep each model-generated statement beside its citations on the wire.

The durable analysis contract remains unchanged and is validated after decoding.
No statement or citation is invented, repeated, dropped, or reassigned.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from app.providers.contracts import ProviderRunError

PAIRED_ANALYSIS_ADAPTER_VERSION = "studio-analysis-paired-v1"
PAIRED_FIELDS = (
    "acceptance_criteria", "missing_information", "similar_issue_pr_evidence",
    "risks", "fit_reasons", "next_steps", "maintainer_questions",
)
PAIRED_ANALYSIS_INSTRUCTION = (
    "For this analysis use the paired-statement transport schema REQUIRED_SCHEMA. "
    "Every item in acceptance_criteria, missing_information, similar_issue_pr_evidence, "
    "risks, fit_reasons, next_steps, and maintainer_questions is an object with exactly "
    "value and evidence_ids. Put the actual statement or original item object in value "
    "and its non-empty citation array in evidence_ids. Do not also put these seven "
    "fields in citation_map: citation_map contains only scalar statement citations. "
    "The application will split each pair into the durable value and citation row. "
    "Any instructions about corresponding citation rows refer to the evidence_ids "
    "on that same item. Use [] for optional lists with no supported items. "
    "At least one fit_reasons item must cite both issue and repository; project_summary "
    "must cite repository, requirement_summary must cite issue, and recommendation_summary "
    "must cite both. Competition means another contributor on this exact Issue; when "
    "unknown, cite issue for the absence of assignee/discussion signals. "
    "cited_evidence_ids must equal the union of all scalar and paired citations. "
    "Never invent citations or GitHub Issue/PR URLs. Include exactly every required "
    "top-level key; keep array items distinct. Both effort hours must be null or both "
    "integers with minimum <= maximum; bounty amount must be null when no bounty exists. "
)


def paired_analysis_schema(output_schema: Mapping[str, Any]) -> dict[str, Any]:
    """Project the bound analysis schema without mutating its frozen source."""
    schema = deepcopy(dict(output_schema))
    properties = schema["properties"]
    citation_map = properties["citation_map"]
    for field in PAIRED_FIELDS:
        item = properties[field]
        citations = citation_map["properties"].pop(field)
        item["items"] = {
            "type": "object",
            "properties": {"value": item["items"], "evidence_ids": citations["items"]},
            "required": ["value", "evidence_ids"],
            "additionalProperties": False,
        }
        citation_map["required"].remove(field)
    return schema


def decode_paired_analysis(value: Any) -> dict[str, Any]:
    """Losslessly split pairs; reject missing/ambiguous citations, never pad them."""
    def invalid(field: str = "envelope", reason: str = "invalid") -> ProviderRunError:
        return ProviderRunError(
            f"studio_analysis_pairs_{field}_{reason}",
            "模型报告中的结论与引用配对格式无效，请重新发起深入评估。",
            retryable=False,
        )

    if not isinstance(value, dict) or not isinstance(value.get("citation_map"), dict):
        raise invalid()
    result = deepcopy(value)
    citation_map = result["citation_map"]
    for field in PAIRED_FIELDS:
        items = result.get(field)
        if not isinstance(items, list):
            raise invalid(field, "not_array")
        statements, rows = [], []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"value", "evidence_ids"}:
                raise invalid(field, "unpaired_item")
            ids = item["evidence_ids"]
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids):
                raise invalid(field, "missing_citations")
            statements.append(item["value"])
            rows.append(ids)
        # Some models redundantly emit both representations. Accept only an
        # exact duplicate; any conflicting row stays an explicit failure.
        if field in citation_map and citation_map[field] != rows:
            raise invalid(field, "conflicting_rows")
        result[field] = statements
        citation_map[field] = rows
    return result
