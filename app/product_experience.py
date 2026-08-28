from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AnalysisVersion,
    InAppNotification,
    NotificationRead,
    Opportunity,
    OpportunityDispositionVersion,
    OpportunitySnapshot,
    Repository,
    ScanRun,
    ScoreVersion,
    UserPreferenceVersion,
    utc_now,
)
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data, redact_text


GOAL_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced": {
        "reward_reliability": 0.22,
        "acceptance_probability": 0.20,
        "tech_match": 0.18,
        "project_impact": 0.15,
        "issue_clarity": 0.10,
        "competition": 0.10,
        "learning_value": 0.05,
    },
    "bounty": {
        "reward_reliability": 0.35,
        "acceptance_probability": 0.20,
        "tech_match": 0.10,
        "project_impact": 0.05,
        "issue_clarity": 0.10,
        "competition": 0.15,
        "learning_value": 0.05,
    },
    "impact": {
        "reward_reliability": 0.05,
        "acceptance_probability": 0.20,
        "tech_match": 0.15,
        "project_impact": 0.30,
        "issue_clarity": 0.10,
        "competition": 0.05,
        "learning_value": 0.15,
    },
    "quick_merge": {
        "reward_reliability": 0.05,
        "acceptance_probability": 0.30,
        "tech_match": 0.15,
        "project_impact": 0.10,
        "issue_clarity": 0.20,
        "competition": 0.15,
        "learning_value": 0.05,
    },
    "learning": {
        "reward_reliability": 0.05,
        "acceptance_probability": 0.15,
        "tech_match": 0.20,
        "project_impact": 0.15,
        "issue_clarity": 0.10,
        "competition": 0.05,
        "learning_value": 0.30,
    },
}

REASON_LABELS = {
    "reward_reliability": "回报信号更明确",
    "acceptance_probability": "更可能获得维护者接收",
    "tech_match": "与你的偏好技术栈匹配",
    "project_impact": "项目影响力较高",
    "issue_clarity": "任务描述和边界较清楚",
    "competition": "当前竞争压力较低",
    "learning_value": "具有较好的学习价值",
}


class ProductExperienceNotFoundError(LookupError):
    pass


class ProductExperienceConflictError(RuntimeError):
    pass


class ProductExperienceService:
    def __init__(
        self,
        session: Session,
        *,
        secrets: Iterable[str | None] = (),
    ) -> None:
        self.session = session
        self.secrets = tuple(secrets)

    def current_preference(self) -> UserPreferenceVersion | None:
        return self.session.scalar(
            select(UserPreferenceVersion)
            .order_by(desc(UserPreferenceVersion.version))
            .limit(1)
        )

    def create_preference(
        self,
        *,
        primary_goal: str,
        preferred_languages: list[str],
        weekly_hours: int,
        minimum_bounty_usd: float,
        auto_scan_enabled: bool,
        auto_scan_local_time: str,
    ) -> UserPreferenceVersion:
        current = self.current_preference()
        normalized_languages = sorted(
            {item.strip() for item in preferred_languages if item.strip()},
            key=str.casefold,
        )
        values = {
            "primary_goal": primary_goal,
            "preferred_languages": normalized_languages,
            "weekly_hours": weekly_hours,
            "minimum_bounty_usd": float(minimum_bounty_usd),
            "auto_scan_enabled": auto_scan_enabled,
            "auto_scan_local_time": auto_scan_local_time,
        }
        ensure_no_sensitive_data(values, context="user preferences")
        if current is not None and all(
            getattr(current, key) == value for key, value in values.items()
        ):
            return current
        version = 1 if current is None else current.version + 1
        preference = UserPreferenceVersion(
            id=str(uuid4()),
            version=version,
            schema_version="1",
            **values,
            record_hash=content_hash({"version": version, **values}),
            created_at=utc_now(),
        )
        self.session.add(preference)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            concurrent = self.current_preference()
            if concurrent is not None and all(
                getattr(concurrent, key) == value
                for key, value in values.items()
            ):
                return concurrent
            raise ProductExperienceConflictError(
                "Preferences changed concurrently; reload and try again"
            ) from exc
        return preference

    def set_disposition(
        self,
        opportunity_id: int,
        *,
        state: str,
        reason_code: str | None,
        reminder_at: datetime | None,
    ) -> OpportunityDispositionVersion:
        if self.session.get(Opportunity, opportunity_id) is None:
            raise ProductExperienceNotFoundError("Opportunity not found")
        current = self.current_disposition(opportunity_id)
        normalized_reminder = _aware(reminder_at) if reminder_at else None
        if (
            current is not None
            and current.state == state
            and current.reason_code == reason_code
            and _same_time(current.reminder_at, normalized_reminder)
        ):
            return current
        sequence = 1 if current is None else current.sequence + 1
        preference = self.current_preference()
        values = {
            "opportunity_id": opportunity_id,
            "preference_version_id": preference.id if preference else None,
            "sequence": sequence,
            "state": state,
            "reason_code": reason_code,
            "reminder_at": normalized_reminder,
        }
        disposition = OpportunityDispositionVersion(
            id=str(uuid4()),
            **values,
            record_hash=content_hash(
                {
                    **values,
                    "reminder_at": (
                        normalized_reminder.isoformat()
                        if normalized_reminder
                        else None
                    ),
                }
            ),
            created_at=utc_now(),
        )
        self.session.add(disposition)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            concurrent = self.current_disposition(opportunity_id)
            if (
                concurrent is not None
                and concurrent.state == state
                and concurrent.reason_code == reason_code
                and _same_time(concurrent.reminder_at, normalized_reminder)
            ):
                return concurrent
            raise ProductExperienceConflictError(
                "Opportunity decision changed concurrently; reload and try again"
            ) from exc
        return disposition

    def current_disposition(
        self, opportunity_id: int
    ) -> OpportunityDispositionVersion | None:
        return self.session.scalar(
            select(OpportunityDispositionVersion)
            .where(OpportunityDispositionVersion.opportunity_id == opportunity_id)
            .order_by(desc(OpportunityDispositionVersion.sequence))
            .limit(1)
        )

    def recommendations(
        self,
        *,
        default_languages: Iterable[str],
        goal: str | None = None,
        include_dismissed: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        scan = self._latest_scan()
        preference = self.current_preference()
        active_goal = goal or (preference.primary_goal if preference else "balanced")
        if active_goal not in GOAL_WEIGHTS:
            raise ProductExperienceConflictError("Unsupported recommendation goal")
        languages = (
            preference.preferred_languages
            if preference and preference.preferred_languages
            else list(default_languages)
        )
        if scan is None:
            return {
                "scan_run_id": None,
                "preference_version_id": preference.id if preference else None,
                "goal": active_goal,
                "total": 0,
                "items": [],
            }
        candidates = self._scan_candidates(scan.id)
        dispositions = self._latest_dispositions(
            item[2].id for item in candidates
        )
        items = [
            self._recommendation_item(
                snapshot,
                score,
                opportunity,
                repository,
                goal=active_goal,
                preferred_languages=languages,
                preference=preference,
                disposition=dispositions.get(opportunity.id),
            )
            for snapshot, score, opportunity, repository in candidates
        ]
        if not include_dismissed:
            items = [
                item for item in items if item["disposition_state"] != "dismissed"
            ]
        items.sort(
            key=lambda item: (
                -float(item["personalized_score"]),
                int(item["opportunity"]["id"]),
            )
        )
        total = len(items)
        return {
            "scan_run_id": scan.id,
            "preference_version_id": preference.id if preference else None,
            "goal": active_goal,
            "total": total,
            "items": items[offset : offset + limit],
        }

    def shortlist(
        self,
        *,
        default_languages: Iterable[str],
    ) -> list[dict[str, object]]:
        preference = self.current_preference()
        goal = preference.primary_goal if preference else "balanced"
        languages = (
            preference.preferred_languages
            if preference and preference.preferred_languages
            else list(default_languages)
        )
        latest = self._latest_dispositions()
        items: list[dict[str, object]] = []
        for opportunity_id, disposition in latest.items():
            if disposition.state != "shortlisted":
                continue
            candidate = self._latest_candidate(opportunity_id)
            if candidate is None:
                continue
            snapshot, score, opportunity, repository = candidate
            items.append(
                self._recommendation_item(
                    snapshot,
                    score,
                    opportunity,
                    repository,
                    goal=goal,
                    preferred_languages=languages,
                    preference=preference,
                    disposition=disposition,
                )
            )
        items.sort(key=lambda item: -float(item["personalized_score"]))
        return items

    def compare(
        self,
        opportunity_ids: list[int],
        *,
        default_languages: Iterable[str],
    ) -> list[dict[str, object]]:
        if not 2 <= len(opportunity_ids) <= 3 or len(set(opportunity_ids)) != len(
            opportunity_ids
        ):
            raise ProductExperienceConflictError(
                "Compare requires two or three distinct opportunities"
            )
        preference = self.current_preference()
        goal = preference.primary_goal if preference else "balanced"
        languages = (
            preference.preferred_languages
            if preference and preference.preferred_languages
            else list(default_languages)
        )
        dispositions = self._latest_dispositions(opportunity_ids)
        result: list[dict[str, object]] = []
        for opportunity_id in opportunity_ids:
            candidate = self._latest_candidate(opportunity_id)
            if candidate is None:
                raise ProductExperienceNotFoundError("Opportunity not found")
            result.append(
                self._recommendation_item(
                    *candidate,
                    goal=goal,
                    preferred_languages=languages,
                    preference=preference,
                    disposition=dispositions.get(opportunity_id),
                )
            )
        return result

    def create_scan_notifications(self, scan_run_id: str) -> int:
        scan = self.session.get(ScanRun, scan_run_id)
        if scan is None:
            raise ProductExperienceNotFoundError("ScanRun not found")
        previous = self.session.scalar(
            select(ScanRun)
            .where(
                ScanRun.status == "completed",
                ScanRun.id != scan.id,
                ScanRun.completed_at < scan.completed_at,
            )
            .order_by(desc(ScanRun.completed_at))
            .limit(1)
        )
        previous_by_opportunity = (
            {
                opportunity.id: (snapshot, score)
                for snapshot, score, opportunity, _ in self._scan_candidates(
                    previous.id,
                    eligible_only=False,
                )
            }
            if previous is not None
            else {}
        )
        preference = self.current_preference()
        languages = preference.preferred_languages if preference else []
        goal = preference.primary_goal if preference else "balanced"
        dispositions = self._latest_dispositions()
        created = 0
        for snapshot, score, opportunity, repository in self._scan_candidates(
            scan.id,
            eligible_only=False,
        ):
            previous_candidate = previous_by_opportunity.get(opportunity.id)
            if previous_candidate is None and snapshot.filter_eligible:
                item = self._recommendation_item(
                    snapshot,
                    score,
                    opportunity,
                    repository,
                    goal=goal,
                    preferred_languages=languages,
                    preference=preference,
                    disposition=dispositions.get(opportunity.id),
                )
                if float(item["personalized_score"]) >= 70:
                    created += self._add_notification(
                        kind="new_match",
                        opportunity=opportunity,
                        scan=scan,
                        dedupe_key=f"new-match:{scan.id}:{opportunity.id}",
                        title="发现新的高匹配机会",
                        message=opportunity.title,
                    )
            elif (
                dispositions.get(opportunity.id) is not None
                and dispositions[opportunity.id].state == "shortlisted"
                and self._candidate_change_hash(*previous_candidate)
                != self._candidate_change_hash(snapshot, score)
            ):
                created += self._add_notification(
                    kind="shortlist_updated",
                    opportunity=opportunity,
                    scan=scan,
                    dedupe_key=f"shortlist-updated:{scan.id}:{opportunity.id}",
                    title="候选机会有新变化",
                    message=opportunity.title,
                )
        self.session.commit()
        return created

    def sync_due_reminders(self, *, now: datetime | None = None) -> int:
        current_time = _aware(now or utc_now())
        created = 0
        for opportunity_id, disposition in self._latest_dispositions().items():
            if (
                disposition.state != "shortlisted"
                or disposition.reminder_at is None
                or _aware(disposition.reminder_at) > current_time
            ):
                continue
            opportunity = self.session.get(Opportunity, opportunity_id)
            if opportunity is None:
                continue
            created += self._add_notification(
                kind="reminder_due",
                opportunity=opportunity,
                scan=None,
                dedupe_key=f"reminder:{disposition.id}",
                title="候选机会提醒",
                message=opportunity.title,
            )
        self.session.commit()
        return created

    def notifications(
        self, *, unread_only: bool = False, limit: int = 50
    ) -> list[dict[str, object]]:
        reads = {
            item.notification_id
            for item in self.session.scalars(select(NotificationRead))
        }
        rows = list(
            self.session.scalars(
                select(InAppNotification)
                .order_by(desc(InAppNotification.created_at))
                .limit(limit * 2 if unread_only else limit)
            )
        )
        result = [
            {
                "id": item.id,
                "kind": item.kind,
                "opportunity_id": item.opportunity_id,
                "scan_run_id": item.scan_run_id,
                "title": item.title,
                "message": item.message,
                "is_read": item.id in reads,
                "created_at": item.created_at,
            }
            for item in rows
            if not unread_only or item.id not in reads
        ]
        return result[:limit]

    def mark_notification_read(self, notification_id: str) -> NotificationRead:
        if self.session.get(InAppNotification, notification_id) is None:
            raise ProductExperienceNotFoundError("Notification not found")
        current = self.session.get(NotificationRead, notification_id)
        if current is not None:
            return current
        read = NotificationRead(notification_id=notification_id, read_at=utc_now())
        self.session.add(read)
        self.session.commit()
        return read

    def mark_all_notifications_read(self) -> int:
        read_ids = {
            item.notification_id
            for item in self.session.scalars(select(NotificationRead))
        }
        rows = list(self.session.scalars(select(InAppNotification.id)))
        for notification_id in rows:
            if notification_id not in read_ids:
                self.session.add(
                    NotificationRead(
                        notification_id=notification_id,
                        read_at=utc_now(),
                    )
                )
        self.session.commit()
        return len([item for item in rows if item not in read_ids])

    def latest_scan_changes(self) -> dict[str, object]:
        scan = self._latest_scan()
        if scan is None:
            return {
                "scan_run_id": None,
                "generated_at": None,
                "new_matches": 0,
                "shortlist_updates": 0,
            }
        rows = list(
            self.session.scalars(
                select(InAppNotification).where(
                    InAppNotification.scan_run_id == scan.id
                )
            )
        )
        return {
            "scan_run_id": scan.id,
            "generated_at": scan.completed_at,
            "new_matches": sum(item.kind == "new_match" for item in rows),
            "shortlist_updates": sum(
                item.kind == "shortlist_updated" for item in rows
            ),
        }

    def _recommendation_item(
        self,
        snapshot: OpportunitySnapshot,
        score: ScoreVersion,
        opportunity: Opportunity,
        repository: Repository,
        *,
        goal: str,
        preferred_languages: Iterable[str],
        preference: UserPreferenceVersion | None,
        disposition: OpportunityDispositionVersion | None,
    ) -> dict[str, object]:
        components = {key: float(value) for key, value in score.score_components.items()}
        preferred = {item.casefold() for item in preferred_languages}
        if preferred:
            language = (repository.language or "").casefold()
            components["tech_match"] = 100.0 if language in preferred else 50.0 if not language else 40.0
        weights = GOAL_WEIGHTS[goal]
        personalized = sum(
            components.get(key, 0.0) * weight for key, weight in weights.items()
        ) - float(score.risk_penalty)
        if (
            goal == "bounty"
            and preference is not None
            and preference.minimum_bounty_usd > 0
            and (
                score.bounty_amount_usd is None
                or score.bounty_amount_usd < preference.minimum_bounty_usd
            )
        ):
            personalized -= 15
        analysis = self.session.scalar(
            select(AnalysisVersion)
            .join(
                OpportunitySnapshot,
                AnalysisVersion.snapshot_id == OpportunitySnapshot.id,
            )
            .where(OpportunitySnapshot.opportunity_id == opportunity.id)
            .order_by(desc(AnalysisVersion.created_at))
            .limit(1)
        )
        structured = dict(analysis.structured_output) if analysis else {}
        effort = structured.get("estimated_effort")
        if isinstance(effort, dict) and preference is not None:
            hours_max = effort.get("hours_max")
            if isinstance(hours_max, int) and hours_max > preference.weekly_hours * 2:
                personalized -= 10
        personalized = round(max(0.0, min(100.0, personalized)), 1)
        ranked_reasons = sorted(
            weights,
            key=lambda key: (-(components.get(key, 0.0) * weights[key]), key),
        )[:3]
        recommendation = structured.get("recommendation")
        if not isinstance(recommendation, str):
            recommendation = _recommendation_from_score(personalized, structured)
        summary = structured.get("recommendation_summary") or structured.get(
            "problem_summary"
        )
        if not isinstance(summary, str) or not summary.strip():
            summary = repository.description or opportunity.title
        return {
            "scan_run_id": snapshot.scan_run_id,
            "snapshot_id": snapshot.id,
            "score_version_id": score.id,
            "preference_version_id": preference.id if preference else None,
            "goal": goal,
            "personalized_score": personalized,
            "recommendation_label": _score_label(personalized),
            "recommendation": recommendation,
            "summary": summary,
            "reason_codes": ranked_reasons,
            "reasons": [REASON_LABELS[key] for key in ranked_reasons],
            "acceptance_level": _level(components.get("acceptance_probability", 0)),
            "competition_level": _level(components.get("competition", 0)),
            "impact_level": _level(components.get("project_impact", 0)),
            "effort": effort if isinstance(effort, dict) else None,
            "next_steps": structured.get("next_steps", []),
            "maintainer_questions": structured.get("maintainer_questions", []),
            "analysis_version_id": analysis.id if analysis else None,
            "disposition_state": disposition.state if disposition else "neutral",
            "disposition_reason": disposition.reason_code if disposition else None,
            "reminder_at": disposition.reminder_at if disposition else None,
            "opportunity": {
                "id": opportunity.id,
                "issue_number": opportunity.issue_number,
                "title": opportunity.title,
                "html_url": opportunity.html_url,
                "labels": opportunity.labels,
                "comments_count": opportunity.comments_count,
                "issue_updated_at": opportunity.issue_updated_at,
                "has_bounty": score.has_bounty,
                "bounty_amount_usd": score.bounty_amount_usd,
                "risk_penalty": score.risk_penalty,
                "risk_reasons": score.risk_reasons,
                "score_components": components,
                "repository": {
                    "full_name": repository.full_name,
                    "description": repository.description,
                    "html_url": repository.html_url,
                    "language": repository.language,
                    "license_spdx": repository.license_spdx,
                    "stars": repository.stars,
                    "forks": repository.forks,
                    "pushed_at": repository.pushed_at,
                },
            },
        }

    def _latest_scan(self) -> ScanRun | None:
        return self.session.scalar(
            select(ScanRun)
            .where(ScanRun.status == "completed")
            .order_by(desc(ScanRun.completed_at))
            .limit(1)
        )

    def _scan_candidates(
        self,
        scan_run_id: str,
        *,
        eligible_only: bool = True,
    ) -> list[tuple[OpportunitySnapshot, ScoreVersion, Opportunity, Repository]]:
        statement = (
            select(OpportunitySnapshot, ScoreVersion, Opportunity, Repository)
            .join(ScoreVersion, ScoreVersion.snapshot_id == OpportunitySnapshot.id)
            .join(Opportunity, Opportunity.id == OpportunitySnapshot.opportunity_id)
            .join(Repository, Repository.id == Opportunity.repository_id)
            .where(OpportunitySnapshot.scan_run_id == scan_run_id)
            .order_by(Opportunity.id, desc(ScoreVersion.created_at))
        )
        if eligible_only:
            statement = statement.where(
                OpportunitySnapshot.filter_eligible.is_(True)
            )
        rows = self.session.execute(statement).all()
        unique: dict[int, tuple[OpportunitySnapshot, ScoreVersion, Opportunity, Repository]] = {}
        for row in rows:
            unique.setdefault(row[2].id, (row[0], row[1], row[2], row[3]))
        return list(unique.values())

    def _latest_candidate(
        self, opportunity_id: int
    ) -> tuple[OpportunitySnapshot, ScoreVersion, Opportunity, Repository] | None:
        row = self.session.execute(
            select(OpportunitySnapshot, ScoreVersion, Opportunity, Repository)
            .join(ScoreVersion, ScoreVersion.snapshot_id == OpportunitySnapshot.id)
            .join(Opportunity, Opportunity.id == OpportunitySnapshot.opportunity_id)
            .join(Repository, Repository.id == Opportunity.repository_id)
            .where(Opportunity.id == opportunity_id)
            .order_by(desc(OpportunitySnapshot.captured_at), desc(ScoreVersion.created_at))
            .limit(1)
        ).first()
        return (row[0], row[1], row[2], row[3]) if row else None

    def _latest_dispositions(
        self, opportunity_ids: Iterable[int] | None = None
    ) -> dict[int, OpportunityDispositionVersion]:
        statement = select(OpportunityDispositionVersion).order_by(
            OpportunityDispositionVersion.opportunity_id,
            OpportunityDispositionVersion.sequence,
        )
        if opportunity_ids is not None:
            ids = list(opportunity_ids)
            if not ids:
                return {}
            statement = statement.where(
                OpportunityDispositionVersion.opportunity_id.in_(ids)
            )
        result: dict[int, OpportunityDispositionVersion] = {}
        for item in self.session.scalars(statement):
            result[item.opportunity_id] = item
        return result

    def _add_notification(
        self,
        *,
        kind: str,
        opportunity: Opportunity,
        scan: ScanRun | None,
        dedupe_key: str,
        title: str,
        message: str,
    ) -> int:
        if self.session.scalar(
            select(func.count(InAppNotification.id)).where(
                InAppNotification.dedupe_key == dedupe_key
            )
        ):
            return 0
        self.session.add(
            InAppNotification(
                id=str(uuid4()),
                kind=kind,
                opportunity_id=opportunity.id,
                scan_run_id=scan.id if scan else None,
                dedupe_key=dedupe_key,
                title=redact_text(title, secrets=self.secrets),
                message=redact_text(message, secrets=self.secrets),
                created_at=utc_now(),
            )
        )
        return 1

    @staticmethod
    def _candidate_change_hash(
        snapshot: OpportunitySnapshot, score: ScoreVersion
    ) -> str:
        issue = snapshot.issue_data
        return content_hash(
            {
                "title": issue.get("title"),
                "body": issue.get("body"),
                "labels": issue.get("labels"),
                "state": issue.get("state"),
                "comments_count": issue.get("comments_count"),
                "score_output_hash": score.output_hash,
            }
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _same_time(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return _aware(left) == _aware(right)


def _level(value: float) -> str:
    if value >= 80:
        return "high"
    if value >= 55:
        return "medium"
    return "low"


def _score_label(value: float) -> str:
    if value >= 85:
        return "strong"
    if value >= 70:
        return "worth_reviewing"
    if value >= 55:
        return "cautious"
    return "low_priority"


def _recommendation_from_score(
    score: float, analysis: dict[str, Any]
) -> str:
    risks = analysis.get("risks")
    if isinstance(risks, list) and any(
        isinstance(item, dict) and item.get("severity") == "high" for item in risks
    ):
        return "consider"
    if score >= 75:
        return "pursue"
    if score >= 55:
        return "consider"
    return "skip"
