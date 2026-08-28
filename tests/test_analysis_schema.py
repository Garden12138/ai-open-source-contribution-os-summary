from __future__ import annotations

import copy

import pytest

from app.providers import ProviderContractError, validate_structured_analysis


def analysis_payload() -> dict[str, object]:
    return {
        "problem_summary": "Add structured analysis persistence.",
        "current_behavior": "Analysis output is an unconstrained object.",
        "expected_behavior": "Every required analysis field is machine-readable.",
        "acceptance_criteria": [
            "All required fields validate before persistence.",
            "Unknown fields fail closed.",
        ],
        "missing_information": ["A real effort estimate needs repository inspection."],
        "similar_issue_pr_evidence": [
            {
                "kind": "issue",
                "title": "Related schema request",
                "url": "https://github.com/fixture/repository/issues/2",
                "relationship": "Uses the same structured output boundary.",
                "evidence_ids": ["issue"],
            }
        ],
        "competition": {
            "level": "medium",
            "summary": "The frozen Issue has active discussion.",
            "signals": ["Two recent comments are visible in frozen evidence."],
        },
        "estimated_effort": {
            "size": "m",
            "hours_min": 4,
            "hours_max": 8,
            "rationale": "Schema, migration, and tests are required.",
        },
        "bounty_basis": {
            "has_bounty": True,
            "amount_usd": 250.0,
            "basis": "The frozen Issue title contains a $250 bounty.",
        },
        "risks": [
            {
                "code": "schema_drift",
                "summary": "Provider output may omit a required field.",
                "severity": "high",
            }
        ],
        "confidence": 0.85,
        "cited_evidence_ids": ["issue", "repository", "rule_score"],
    }


def test_complete_structured_analysis_schema_is_accepted() -> None:
    validate_structured_analysis(
        analysis_payload(),
        allowed_evidence_ids=("issue", "repository", "rule_score"),
        schema_version="analysis-schema-v1",
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda value: value.pop("current_behavior"), "fields"),
        (
            lambda value: value.__setitem__("unknown", True),
            "fields",
        ),
        (
            lambda value: value.__setitem__("acceptance_criteria", []),
            "at least",
        ),
        (
            lambda value: value["estimated_effort"].__setitem__(
                "hours_min",
                9,
            ),
            "cannot exceed",
        ),
        (
            lambda value: value["bounty_basis"].__setitem__(
                "has_bounty",
                False,
            ),
            "must be null",
        ),
        (
            lambda value: value["risks"].append(
                {
                    "code": "schema_drift",
                    "summary": "Duplicate code",
                    "severity": "low",
                }
            ),
            "unique",
        ),
        (
            lambda value: value.__setitem__(
                "cited_evidence_ids",
                ["not-frozen"],
            ),
            "outside",
        ),
        (
            lambda value: value.__setitem__(
                "problem_summary",
                "Bearer github_pat_ANALYSIS_SCHEMA_CANARY_123456",
            ),
            "Credential-like",
        ),
    ),
)
def test_structured_analysis_schema_rejects_malformed_or_unsafe_values(
    mutation,  # type: ignore[no-untyped-def]
    message: str,
) -> None:
    value = copy.deepcopy(analysis_payload())
    mutation(value)

    with pytest.raises((ProviderContractError, ValueError), match=message):
        validate_structured_analysis(
            value,
            allowed_evidence_ids=("issue", "repository", "rule_score"),
            schema_version="analysis-schema-v1",
        )


def cited_analysis_payload() -> dict[str, object]:
    value = analysis_payload()
    value.update(
        {
            "recommendation": "consider",
            "recommendation_summary": "Confirm scope before starting.",
            "fit_reasons": ["The task matches the preferred stack."],
            "next_steps": ["Ask the maintainer whether the task is available."],
            "maintainer_questions": ["Is this Issue still available?"],
        }
    )
    value["citation_map"] = {
        "problem_summary": ["issue"],
        "current_behavior": ["issue"],
        "expected_behavior": ["issue"],
        "acceptance_criteria": [["issue"], ["repository"]],
        "missing_information": [["rule_score"]],
        "similar_issue_pr_evidence": [["issue"]],
        "competition": ["issue"],
        "estimated_effort": ["repository"],
        "bounty_basis": ["issue", "rule_score"],
        "risks": [["repository"]],
        "confidence": ["issue", "repository", "rule_score"],
        "recommendation": ["issue", "rule_score"],
        "recommendation_summary": ["issue"],
        "fit_reasons": [["repository", "rule_score"]],
        "next_steps": [["issue"]],
        "maintainer_questions": [["issue"]],
    }
    return value


def test_every_material_analysis_value_has_an_exact_frozen_citation() -> None:
    validate_structured_analysis(
        cited_analysis_payload(),
        allowed_evidence_ids=("issue", "repository", "rule_score"),
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda value: value["citation_map"].__setitem__(
                "problem_summary",
                [],
            ),
            "at least",
        ),
        (
            lambda value: value["citation_map"].__setitem__(
                "acceptance_criteria",
                [["issue"]],
            ),
            "align",
        ),
        (
            lambda value: value["citation_map"].__setitem__(
                "similar_issue_pr_evidence",
                [["repository"]],
            ),
            "does not match",
        ),
        (
            lambda value: value["citation_map"].__setitem__(
                "risks",
                [],
            ),
            "align",
        ),
        (
            lambda value: value["cited_evidence_ids"].append("unused"),
            "exactly match",
        ),
        (
            lambda value: value["citation_map"].__setitem__(
                "confidence",
                ["not-frozen"],
            ),
            "exactly match",
        ),
    ),
)
def test_statement_level_citations_fail_closed_on_gaps_or_drift(
    mutation,  # type: ignore[no-untyped-def]
    message: str,
) -> None:
    value = copy.deepcopy(cited_analysis_payload())
    mutation(value)

    with pytest.raises(ProviderContractError, match=message):
        validate_structured_analysis(
            value,
            allowed_evidence_ids=(
                "issue",
                "repository",
                "rule_score",
                "unused",
            ),
        )
