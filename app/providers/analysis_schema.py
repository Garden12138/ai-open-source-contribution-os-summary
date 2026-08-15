from __future__ import annotations

import math
import re
from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import Any

from app.providers.contracts import ProviderContractError
from app.security import ensure_no_sensitive_data


ANALYSIS_SCHEMA_V1 = "analysis-schema-v1"
ANALYSIS_SCHEMA_VERSION = "analysis-schema-v2"

_STATEMENT = {"type": "string", "minLength": 1, "maxLength": 4_000}
_SHORT_STATEMENT = {"type": "string", "minLength": 1, "maxLength": 1_000}
_EVIDENCE_IDS = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 128},
    "uniqueItems": True,
}

ANALYSIS_OUTPUT_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "problem_summary": _STATEMENT,
        "current_behavior": _STATEMENT,
        "expected_behavior": _STATEMENT,
        "acceptance_criteria": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": _SHORT_STATEMENT,
        },
        "missing_information": {
            "type": "array",
            "maxItems": 20,
            "items": _SHORT_STATEMENT,
        },
        "similar_issue_pr_evidence": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["issue", "pull_request"],
                    },
                    "title": _SHORT_STATEMENT,
                    "url": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1_000,
                    },
                    "relationship": _SHORT_STATEMENT,
                    "evidence_ids": _EVIDENCE_IDS,
                },
                "required": [
                    "kind",
                    "title",
                    "url",
                    "relationship",
                    "evidence_ids",
                ],
                "additionalProperties": False,
            },
        },
        "competition": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "unknown"],
                },
                "summary": _SHORT_STATEMENT,
                "signals": {
                    "type": "array",
                    "maxItems": 20,
                    "items": _SHORT_STATEMENT,
                },
            },
            "required": ["level", "summary", "signals"],
            "additionalProperties": False,
        },
        "estimated_effort": {
            "type": "object",
            "properties": {
                "size": {
                    "type": "string",
                    "enum": ["xs", "s", "m", "l", "xl", "unknown"],
                },
                "hours_min": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "maximum": 10_000,
                },
                "hours_max": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "maximum": 10_000,
                },
                "rationale": _SHORT_STATEMENT,
            },
            "required": ["size", "hours_min", "hours_max", "rationale"],
            "additionalProperties": False,
        },
        "bounty_basis": {
            "type": "object",
            "properties": {
                "has_bounty": {"type": "boolean"},
                "amount_usd": {
                    "type": ["number", "null"],
                    "minimum": 0,
                },
                "basis": _SHORT_STATEMENT,
            },
            "required": ["has_bounty", "amount_usd", "basis"],
            "additionalProperties": False,
        },
        "risks": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "pattern": "^[a-z0-9_.-]{1,80}$",
                    },
                    "summary": _SHORT_STATEMENT,
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
                "required": ["code", "summary", "severity"],
                "additionalProperties": False,
            },
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "cited_evidence_ids": {
            **_EVIDENCE_IDS,
            "minItems": 1,
        },
    },
    "required": [
        "problem_summary",
        "current_behavior",
        "expected_behavior",
        "acceptance_criteria",
        "missing_information",
        "similar_issue_pr_evidence",
        "competition",
        "estimated_effort",
        "bounty_basis",
        "risks",
        "confidence",
        "cited_evidence_ids",
    ],
    "additionalProperties": False,
}

_CITATION_IDS = {
    **_EVIDENCE_IDS,
    "minItems": 1,
}
_CITATION_MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "problem_summary": _CITATION_IDS,
        "current_behavior": _CITATION_IDS,
        "expected_behavior": _CITATION_IDS,
        "acceptance_criteria": {
            "type": "array",
            "maxItems": 20,
            "items": _CITATION_IDS,
        },
        "missing_information": {
            "type": "array",
            "maxItems": 20,
            "items": _CITATION_IDS,
        },
        "similar_issue_pr_evidence": {
            "type": "array",
            "maxItems": 20,
            "items": _CITATION_IDS,
        },
        "competition": _CITATION_IDS,
        "estimated_effort": _CITATION_IDS,
        "bounty_basis": _CITATION_IDS,
        "risks": {
            "type": "array",
            "maxItems": 20,
            "items": _CITATION_IDS,
        },
        "confidence": _CITATION_IDS,
    },
    "required": [
        "problem_summary",
        "current_behavior",
        "expected_behavior",
        "acceptance_criteria",
        "missing_information",
        "similar_issue_pr_evidence",
        "competition",
        "estimated_effort",
        "bounty_basis",
        "risks",
        "confidence",
    ],
    "additionalProperties": False,
}
ANALYSIS_OUTPUT_SCHEMA: dict[str, Any] = deepcopy(ANALYSIS_OUTPUT_SCHEMA_V1)
ANALYSIS_OUTPUT_SCHEMA["properties"]["citation_map"] = _CITATION_MAP_SCHEMA
ANALYSIS_OUTPUT_SCHEMA["required"] = [
    *ANALYSIS_OUTPUT_SCHEMA_V1["required"],
    "citation_map",
]

_REQUIRED_FIELDS_V1 = frozenset(ANALYSIS_OUTPUT_SCHEMA_V1["required"])
_REQUIRED_FIELDS_V2 = frozenset(ANALYSIS_OUTPUT_SCHEMA["required"])
_RISK_CODE = re.compile(r"^[a-z0-9_.-]{1,80}$")


def validate_structured_analysis(
    value: Mapping[str, Any],
    *,
    allowed_evidence_ids: Sequence[str] | None = None,
    schema_version: str = ANALYSIS_SCHEMA_VERSION,
) -> None:
    if schema_version == ANALYSIS_SCHEMA_V1:
        _validate_v1(
            value,
            allowed_evidence_ids=allowed_evidence_ids,
        )
        return
    if schema_version != ANALYSIS_SCHEMA_VERSION:
        raise ProviderContractError(
            "Unsupported structured analysis schema version"
        )
    if set(value) != _REQUIRED_FIELDS_V2:
        raise ProviderContractError(
            "Structured analysis fields do not match analysis-schema-v2"
        )
    v1_value = {
        key: item for key, item in value.items() if key != "citation_map"
    }
    citations = _validate_v1(
        v1_value,
        allowed_evidence_ids=allowed_evidence_ids,
    )
    _validate_citation_map(value, citations=citations)
    ensure_no_sensitive_data(value, context="structured analysis")


def _validate_v1(
    value: Mapping[str, Any],
    *,
    allowed_evidence_ids: Sequence[str] | None,
) -> tuple[str, ...]:
    if set(value) != _REQUIRED_FIELDS_V1:
        raise ProviderContractError(
            "Structured analysis fields do not match analysis-schema-v1"
        )
    for name in ("problem_summary", "current_behavior", "expected_behavior"):
        _text(value[name], name, maximum=4_000)
    _text_list(
        value["acceptance_criteria"],
        "acceptance criteria",
        minimum=1,
    )
    _text_list(value["missing_information"], "missing information")
    citations = _evidence_ids(
        value["cited_evidence_ids"],
        "analysis citations",
        minimum=1,
    )
    if allowed_evidence_ids is not None:
        unknown = set(citations) - set(allowed_evidence_ids)
        if unknown:
            raise ProviderContractError(
                "Structured analysis cites evidence outside the frozen input"
            )

    similar = _list(
        value["similar_issue_pr_evidence"],
        "similar Issue/PR evidence",
        maximum=20,
    )
    for item in similar:
        record = _object(item, "similar Issue/PR evidence item")
        _exact(
            record,
            {"kind", "title", "url", "relationship", "evidence_ids"},
            "similar Issue/PR evidence item",
        )
        if record["kind"] not in {"issue", "pull_request"}:
            raise ProviderContractError(
                "Similar evidence kind must be issue or pull_request"
            )
        _text(record["title"], "similar evidence title", maximum=1_000)
        url = _text(record["url"], "similar evidence URL", maximum=1_000)
        if not url.startswith("https://github.com/"):
            raise ProviderContractError(
                "Similar evidence URL must use https://github.com/"
            )
        _text(
            record["relationship"],
            "similar evidence relationship",
            maximum=1_000,
        )
        nested = _evidence_ids(
            record["evidence_ids"],
            "similar evidence citations",
        )
        if set(nested) - set(citations):
            raise ProviderContractError(
                "Similar evidence contains undeclared citations"
            )

    competition = _object(value["competition"], "competition")
    _exact(competition, {"level", "summary", "signals"}, "competition")
    if competition["level"] not in {"low", "medium", "high", "unknown"}:
        raise ProviderContractError("Competition level is invalid")
    _text(competition["summary"], "competition summary", maximum=1_000)
    _text_list(competition["signals"], "competition signals")

    effort = _object(value["estimated_effort"], "estimated effort")
    _exact(
        effort,
        {"size", "hours_min", "hours_max", "rationale"},
        "estimated effort",
    )
    if effort["size"] not in {"xs", "s", "m", "l", "xl", "unknown"}:
        raise ProviderContractError("Estimated effort size is invalid")
    hours_min = _nullable_integer(
        effort["hours_min"],
        "minimum effort hours",
    )
    hours_max = _nullable_integer(
        effort["hours_max"],
        "maximum effort hours",
    )
    if (hours_min is None) != (hours_max is None):
        raise ProviderContractError(
            "Estimated effort hours must both be set or both be null"
        )
    if hours_min is not None and hours_max is not None and hours_min > hours_max:
        raise ProviderContractError(
            "Minimum effort hours cannot exceed maximum"
        )
    _text(effort["rationale"], "effort rationale", maximum=1_000)

    bounty = _object(value["bounty_basis"], "bounty basis")
    _exact(bounty, {"has_bounty", "amount_usd", "basis"}, "bounty basis")
    if not isinstance(bounty["has_bounty"], bool):
        raise ProviderContractError("Bounty flag must be a boolean")
    amount = bounty["amount_usd"]
    if amount is not None:
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(float(amount))
            or amount < 0
        ):
            raise ProviderContractError(
                "Bounty amount must be a non-negative finite number or null"
            )
    if not bounty["has_bounty"] and amount is not None:
        raise ProviderContractError(
            "Bounty amount must be null when no bounty is present"
        )
    _text(bounty["basis"], "bounty basis explanation", maximum=1_000)

    risks = _list(value["risks"], "risks", maximum=20)
    codes: list[str] = []
    for item in risks:
        risk = _object(item, "risk")
        _exact(risk, {"code", "summary", "severity"}, "risk")
        code = _text(risk["code"], "risk code", maximum=80)
        if not _RISK_CODE.fullmatch(code):
            raise ProviderContractError("Risk code is invalid")
        codes.append(code)
        _text(risk["summary"], "risk summary", maximum=1_000)
        if risk["severity"] not in {"low", "medium", "high"}:
            raise ProviderContractError("Risk severity is invalid")
    if len(codes) != len(set(codes)):
        raise ProviderContractError("Risk codes must be unique")

    confidence = value["confidence"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= confidence <= 1
    ):
        raise ProviderContractError("Analysis confidence must be between 0 and 1")
    ensure_no_sensitive_data(value, context="structured analysis")
    return citations


def _validate_citation_map(
    value: Mapping[str, Any],
    *,
    citations: tuple[str, ...],
) -> None:
    citation_map = _object(value["citation_map"], "citation map")
    expected = set(_CITATION_MAP_SCHEMA["required"])
    _exact(citation_map, expected, "citation map")
    used: set[str] = set()
    for field in (
        "problem_summary",
        "current_behavior",
        "expected_behavior",
        "competition",
        "estimated_effort",
        "bounty_basis",
        "confidence",
    ):
        used.update(
            _evidence_ids(
                citation_map[field],
                f"{field} citations",
                minimum=1,
            )
        )
    for field in (
        "acceptance_criteria",
        "missing_information",
        "similar_issue_pr_evidence",
        "risks",
    ):
        rows = _list(
            citation_map[field],
            f"{field} citation rows",
            maximum=20,
        )
        material_values = _list(
            value[field],
            field,
            maximum=20,
        )
        if len(rows) != len(material_values):
            raise ProviderContractError(
                f"{field} citations must align with material values"
            )
        for index, row in enumerate(rows):
            nested = _evidence_ids(
                row,
                f"{field} citations",
                minimum=1,
            )
            if field == "similar_issue_pr_evidence":
                similar = _object(
                    material_values[index],
                    "similar Issue/PR evidence item",
                )
                declared = _evidence_ids(
                    similar["evidence_ids"],
                    "similar evidence citations",
                    minimum=1,
                )
                if set(nested) != set(declared):
                    raise ProviderContractError(
                        "Similar evidence citation map does not match the item"
                    )
            used.update(nested)
    if used != set(citations):
        raise ProviderContractError(
            "Top-level citations must exactly match statement-level citations"
        )


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ProviderContractError(f"{name} must be an object")
    return value


def _exact(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ProviderContractError(f"{name} fields are invalid")


def _list(value: Any, name: str, *, maximum: int) -> list[Any]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > maximum
    ):
        raise ProviderContractError(f"{name} must be an array of at most {maximum}")
    return list(value)


def _text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ProviderContractError(
            f"{name} must contain between 1 and {maximum} characters"
        )
    return value


def _text_list(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
) -> tuple[str, ...]:
    items = _list(value, name, maximum=20)
    if len(items) < minimum:
        raise ProviderContractError(
            f"{name} must contain at least {minimum} item"
        )
    result = tuple(_text(item, name, maximum=1_000) for item in items)
    if len(result) != len(set(result)):
        raise ProviderContractError(f"{name} must not contain duplicates")
    return result


def _evidence_ids(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
) -> tuple[str, ...]:
    items = _list(value, name, maximum=100)
    if len(items) < minimum:
        raise ProviderContractError(
            f"{name} must contain at least {minimum} item"
        )
    result = tuple(_text(item, name, maximum=128) for item in items)
    if len(result) != len(set(result)):
        raise ProviderContractError(f"{name} must not contain duplicates")
    return result


def _nullable_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 10_000
    ):
        raise ProviderContractError(
            f"{name} must be an integer between 0 and 10000 or null"
        )
    return value
