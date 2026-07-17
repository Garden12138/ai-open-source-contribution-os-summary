from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.domain import IssueFacts, RepositoryFacts, SelectionCandidate
from app.scoring import filter_issue, score_issue, select_daily_opportunities


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def repository(**overrides: object) -> RepositoryFacts:
    values: dict[str, object] = {
        "full_name": "example/project",
        "description": "An AI developer tool",
        "language": "Python",
        "license_spdx": "MIT",
        "stars": 5_000,
        "forks": 400,
        "archived": False,
        "disabled": False,
        "topics": ("ai", "mcp"),
        "pushed_at": datetime(2026, 7, 10, tzinfo=timezone.utc),
        "has_contributing_guide": True,
        "health_percentage": 88,
        "sync_error": None,
    }
    values.update(overrides)
    return RepositoryFacts(**values)  # type: ignore[arg-type]


def issue(**overrides: object) -> IssueFacts:
    values: dict[str, object] = {
        "github_issue_id": 101,
        "number": 7,
        "title": "Improve the agent integration",
        "body": (
            "Steps to reproduce are documented here. Expected behavior is clear, "
            "and the acceptance checklist contains concrete implementation details."
        ),
        "html_url": "https://github.com/example/project/issues/7",
        "state": "open",
        "labels": ("help wanted",),
        "comments_count": 1,
        "assignees_count": 0,
        "author_association": "MEMBER",
        "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "source_queries": ("query-a",),
        "repository": repository(),
    }
    values.update(overrides)
    return IssueFacts(**values)  # type: ignore[arg-type]


def test_filter_issue_accepts_a_healthy_unassigned_issue() -> None:
    decision = filter_issue(issue(), Settings(), now=NOW)

    assert decision.eligible is True
    assert decision.reasons == ()


def test_filter_issue_can_use_valid_cached_metadata_after_refresh_error() -> None:
    candidate = issue(repository=repository(sync_error="temporary refresh timeout"))

    decision = filter_issue(candidate, Settings(), now=NOW)

    assert decision.eligible is True
    assert decision.reasons == ()


def test_filter_issue_reports_every_hard_filter_reason() -> None:
    unhealthy_repository = repository(
        license_spdx="NOASSERTION",
        archived=True,
        disabled=True,
        pushed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        sync_error="metadata timeout",
    )
    candidate = issue(
        state="closed",
        body="too short",
        assignees_count=1,
        repository=unhealthy_repository,
    )

    decision = filter_issue(candidate, Settings(), now=NOW)

    assert decision.eligible is False
    assert decision.reasons == (
        "issue_not_open",
        "repository_metadata_unavailable",
        "repository_inactive",
        "repository_has_no_recognized_license",
        "issue_already_assigned",
        "issue_description_too_short",
        "repository_activity_is_stale",
    )


def test_score_issue_extracts_bounty_and_applies_risk_penalties() -> None:
    risky = issue(
        title="$500 bounty: rewrite the AI agent integration",
        body=(
            "Steps to reproduce: run the existing integration. Expected: a stable "
            "result. Acceptance checklist: - [ ] preserve compatibility. "
            "This redesign has a deliberately detailed scope for contributors."
        ),
        labels=("bounty", "help wanted"),
        comments_count=16,
        created_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
    )

    result = score_issue(risky, Settings(), now=NOW)

    assert result.has_bounty is True
    assert result.bounty_amount_usd == 500.0
    assert result.is_strategic is True
    assert result.is_tech_match is True
    assert result.risk_penalty == 27.0
    assert result.risk_reasons == (
        "scope_may_be_too_large",
        "high_discussion_or_competition",
        "issue_is_very_old",
    )
    assert set(result.components) == {
        "reward_reliability",
        "acceptance_probability",
        "tech_match",
        "project_impact",
        "issue_clarity",
        "competition",
        "learning_value",
    }
    assert result.components["reward_reliability"] > 90
    assert result.components["tech_match"] == 100
    assert result.components["competition"] == 36
    assert 0 <= result.total <= 100


def test_strategic_keywords_match_complete_tokens() -> None:
    ordinary = issue(
        title="Improve maintainer documentation",
        repository=repository(description="Maintenance utilities", topics=()),
    )

    result = score_issue(ordinary, Settings(strategic_keywords=("ai",)), now=NOW)

    assert result.is_strategic is False


def candidate(
    opportunity_id: int,
    score: float,
    impact: float,
    *,
    bounty: bool = False,
    strategic: bool = False,
    tech: bool = False,
) -> SelectionCandidate:
    return SelectionCandidate(
        opportunity_id=opportunity_id,
        total_score=score,
        impact_score=impact,
        has_bounty=bounty,
        is_strategic=strategic,
        is_tech_match=tech,
    )


def test_select_daily_opportunities_honors_category_quotas() -> None:
    candidates = [
        candidate(1, 100, 10, bounty=True),
        candidate(2, 99, 10, bounty=True),
        candidate(3, 98, 10, bounty=True),
        candidate(4, 97, 10, strategic=True),
        candidate(5, 96, 10, strategic=True),
        candidate(6, 95, 10, tech=True),
        candidate(7, 94, 10, tech=True),
        candidate(8, 60, 100),
        candidate(9, 70, 90),
        candidate(10, 80, 80),
        candidate(11, 90, 70),
    ]

    selections = select_daily_opportunities(candidates, top_n=10)
    reasons = {
        selection.opportunity_id: selection.selection_reason for selection in selections
    }

    assert len(selections) == 10
    assert {item_id for item_id, reason in reasons.items() if reason == "bounty"} == {
        1,
        2,
        3,
    }
    assert {
        item_id for item_id, reason in reasons.items() if reason == "strategic"
    } == {4, 5}
    assert {
        item_id for item_id, reason in reasons.items() if reason == "tech_match"
    } == {6, 7}
    assert {
        item_id for item_id, reason in reasons.items() if reason == "high_impact"
    } == {8, 9, 10}
    assert 11 not in reasons
    assert [selection.total_score for selection in selections] == sorted(
        (selection.total_score for selection in selections), reverse=True
    )


def test_select_daily_opportunities_backfills_when_quotas_are_unavailable() -> None:
    candidates = [
        candidate(1, 90, 1, bounty=True),
        candidate(2, 89, 5),
        candidate(3, 88, 4),
        candidate(4, 87, 3),
        candidate(5, 86, 2),
    ]

    selections = select_daily_opportunities(candidates, top_n=5)
    reasons = {
        selection.opportunity_id: selection.selection_reason for selection in selections
    }

    assert reasons == {
        1: "bounty",
        2: "high_impact",
        3: "high_impact",
        4: "high_impact",
        5: "best_available",
    }
