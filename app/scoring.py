from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Callable, Iterable

from app.config import Settings
from app.domain import (
    FilterDecision,
    IssueFacts,
    ScoreResult,
    Selection,
    SelectionCandidate,
)


WEIGHTS = {
    "reward_reliability": 0.22,
    "acceptance_probability": 0.20,
    "tech_match": 0.18,
    "project_impact": 0.15,
    "issue_clarity": 0.10,
    "competition": 0.10,
    "learning_value": 0.05,
}


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _contains_keyword(text: str, keyword: str) -> bool:
    pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def filter_issue(
    issue: IssueFacts, settings: Settings, now: datetime | None = None
) -> FilterDecision:
    now = _aware(now or datetime.now(timezone.utc))
    reasons: list[str] = []
    repository = issue.repository
    license_is_recognized = bool(repository.license_spdx) and (
        repository.license_spdx.upper() not in {"NOASSERTION", "NONE"}
    )

    if issue.state.lower() != "open":
        reasons.append("issue_not_open")
    if repository.sync_error and not license_is_recognized:
        reasons.append("repository_metadata_unavailable")
    if repository.archived or repository.disabled:
        reasons.append("repository_inactive")
    if not license_is_recognized:
        reasons.append("repository_has_no_recognized_license")
    if issue.assignees_count:
        reasons.append("issue_already_assigned")
    if len(issue.body.strip()) < settings.minimum_issue_body_length:
        reasons.append("issue_description_too_short")

    pushed_at = _aware(repository.pushed_at)
    if pushed_at and now and (now - pushed_at).days > settings.repository_inactive_days:
        reasons.append("repository_activity_is_stale")

    return FilterDecision(eligible=not reasons, reasons=tuple(reasons))


def score_issue(
    issue: IssueFacts, settings: Settings, now: datetime | None = None
) -> ScoreResult:
    now = _aware(now or datetime.now(timezone.utc))
    repository = issue.repository
    text = issue.searchable_text
    body = issue.body.lower()
    labels = {label.lower() for label in issue.labels}
    preferred = {language.lower() for language in settings.preferred_languages}
    strategic_keywords = {keyword.lower() for keyword in settings.strategic_keywords}
    language = (repository.language or "").lower()

    bounty_amount = issue.bounty_amount_usd
    has_bounty = issue.has_bounty_signal
    if bounty_amount:
        reward = 72 + min(28, math.log10(max(10, bounty_amount)) * 9)
    elif has_bounty:
        reward = 60
    elif {"help wanted", "good first issue"} & labels:
        reward = 22
    else:
        reward = 8

    clarity = 20.0
    body_length = len(issue.body.strip())
    clarity += min(35, body_length / 20)
    clarity += (
        12 if any(token in body for token in ("expected", "actual", "预期")) else 0
    )
    clarity += (
        12 if any(token in body for token in ("reproduce", "steps", "复现")) else 0
    )
    clarity += (
        10
        if any(token in body for token in ("acceptance", "checklist", "- [ ]"))
        else 0
    )
    clarity += 8 if labels else 0

    acceptance = 42.0
    acceptance += 13 if repository.has_contributing_guide else 0
    acceptance += min(12, (repository.health_percentage or 0) / 8)
    acceptance += 10 if body_length >= 160 else 0
    acceptance += (
        8 if issue.author_association in {"OWNER", "MEMBER", "COLLABORATOR"} else 0
    )
    acceptance += 8 if issue.assignees_count == 0 else -25
    acceptance -= min(20, issue.comments_count * 1.5)
    pushed_at = _aware(repository.pushed_at)
    if pushed_at and now:
        age_days = max(0, (now - pushed_at).days)
        acceptance += 10 if age_days <= 30 else 4 if age_days <= 120 else -8

    if language in preferred:
        tech_match = 100.0
    elif not language:
        tech_match = 42.0
    else:
        tech_match = 25.0
    topic_text = " ".join(repository.topics).lower()
    if any(_contains_keyword(topic_text, keyword) for keyword in strategic_keywords):
        tech_match = min(100, tech_match + 8)

    stars = max(0, repository.stars)
    project_impact = 10 + min(86, math.log10(stars + 1) * 22)
    if pushed_at and now and (now - pushed_at).days <= 30:
        project_impact += 4

    competition = 100 - issue.comments_count * 4 - issue.assignees_count * 50
    in_progress_signals = ("i'm working", "i am working", "working on this", "claim")
    if any(signal in text for signal in in_progress_signals):
        competition -= 25

    strategic_text = " ".join(
        (issue.title, repository.description, *repository.topics)
    ).lower()
    is_strategic = any(
        _contains_keyword(strategic_text, keyword) for keyword in strategic_keywords
    )
    is_tech_match = language in preferred
    learning_value = 38.0
    learning_value += 28 if is_tech_match else 8
    learning_value += 20 if is_strategic else 0
    learning_value += min(14, math.log10(stars + 1) * 4)

    components = {
        "reward_reliability": round(_clamp(reward), 1),
        "acceptance_probability": round(_clamp(acceptance), 1),
        "tech_match": round(_clamp(tech_match), 1),
        "project_impact": round(_clamp(project_impact), 1),
        "issue_clarity": round(_clamp(clarity), 1),
        "competition": round(_clamp(competition), 1),
        "learning_value": round(_clamp(learning_value), 1),
    }

    risk_penalty = 0.0
    risk_reasons: list[str] = []
    large_scope_tokens = (
        "epic",
        "rewrite",
        "redesign",
        "refactor all",
        "breaking change",
    )
    if any(token in text for token in large_scope_tokens):
        risk_penalty += 12
        risk_reasons.append("scope_may_be_too_large")
    if issue.comments_count >= 15:
        risk_penalty += 8
        risk_reasons.append("high_discussion_or_competition")
    issue_created_at = _aware(issue.created_at)
    if issue_created_at and now and (now - issue_created_at).days > 730:
        risk_penalty += 7
        risk_reasons.append("issue_is_very_old")
    if not repository.language:
        risk_penalty += 4
        risk_reasons.append("repository_language_unknown")
    if has_bounty and bounty_amount is None:
        risk_penalty += 3
        risk_reasons.append("bounty_amount_or_terms_unclear")

    weighted = sum(components[name] * weight for name, weight in WEIGHTS.items())
    total = round(_clamp(weighted - risk_penalty), 1)
    return ScoreResult(
        total=total,
        components=components,
        risk_penalty=risk_penalty,
        risk_reasons=tuple(risk_reasons),
        has_bounty=has_bounty,
        bounty_amount_usd=bounty_amount,
        is_strategic=is_strategic,
        is_tech_match=is_tech_match,
    )


def select_daily_opportunities(
    candidates: Iterable[SelectionCandidate], top_n: int = 10
) -> list[Selection]:
    ranked = sorted(
        candidates,
        key=lambda item: (-item.total_score, -item.impact_score, item.opportunity_id),
    )
    selected: dict[int, Selection] = {}

    quota_specs: tuple[tuple[str, int, Callable[[SelectionCandidate], bool]], ...] = (
        ("bounty", 3, lambda item: item.has_bounty),
        ("strategic", 2, lambda item: item.is_strategic),
        ("tech_match", 2, lambda item: item.is_tech_match),
        ("high_impact", 3, lambda item: True),
    )

    for reason, quota, predicate in quota_specs:
        matches = [
            item
            for item in ranked
            if item.opportunity_id not in selected and predicate(item)
        ]
        key = (
            (lambda item: (-item.impact_score, -item.total_score, item.opportunity_id))
            if reason == "high_impact"
            else (
                lambda item: (
                    -item.total_score,
                    -item.impact_score,
                    item.opportunity_id,
                )
            )
        )
        for item in sorted(matches, key=key)[:quota]:
            if len(selected) >= top_n:
                break
            selected[item.opportunity_id] = Selection(
                opportunity_id=item.opportunity_id,
                selection_reason=reason,
                total_score=item.total_score,
            )

    if len(selected) < top_n:
        for item in ranked:
            if item.opportunity_id in selected:
                continue
            selected[item.opportunity_id] = Selection(
                opportunity_id=item.opportunity_id,
                selection_reason="best_available",
                total_score=item.total_score,
            )
            if len(selected) >= top_n:
                break

    return sorted(
        selected.values(), key=lambda item: (-item.total_score, item.opportunity_id)
    )
