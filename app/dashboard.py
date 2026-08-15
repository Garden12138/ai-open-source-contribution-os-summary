from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AnalysisVersion,
    ContributionTask,
    ContributionTaskStateVersion,
    DraftPullRequest,
    ExecutionAttempt,
    Opportunity,
    PlanVersion,
    PublishIntent,
    PullRequestEvent,
    ReviewRun,
    TaskLifecycleMark,
)
from app.task_states import ContributionTaskState, ContributionTaskStateService


class ContributionDashboardService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def snapshot(self) -> dict[str, object]:
        scanned = self.session.scalar(select(func.count(Opportunity.id))) or 0
        analyzed = (
            self.session.scalar(select(func.count(AnalysisVersion.id))) or 0
        )
        planned = self.session.scalar(select(func.count(PlanVersion.id))) or 0
        executed = (
            self.session.scalar(select(func.count(ExecutionAttempt.id))) or 0
        )
        reviewed = self.session.scalar(select(func.count(ReviewRun.id))) or 0
        submitted = (
            self.session.scalar(select(func.count(DraftPullRequest.id))) or 0
        )
        states = self._current_state_counts()
        merged = states.get(ContributionTaskState.MERGED.value, 0) + states.get(
            ContributionTaskState.REWARDED.value,
            0,
        )
        rewarded = states.get(ContributionTaskState.REWARDED.value, 0)
        merge_rate = (merged / submitted) if submitted else 0.0
        return {
            "funnel": {
                "scanned": scanned,
                "analyzed": analyzed,
                "planned": planned,
                "executed": executed,
                "reviewed": reviewed,
                "submitted": submitted,
                "merged": merged,
                "rewarded": rewarded,
            },
            "current_states": states,
            "heatmap": self._heatmap(),
            "metrics": {
                "merge_rate": round(merge_rate, 4),
                "task_count": (
                    self.session.scalar(select(func.count(ContributionTask.id)))
                    or 0
                ),
                "publish_intent_count": (
                    self.session.scalar(select(func.count(PublishIntent.id)))
                    or 0
                ),
                "pull_request_event_count": (
                    self.session.scalar(select(func.count(PullRequestEvent.id)))
                    or 0
                ),
            },
        }

    def task_summaries(
        self,
        *,
        state: str | None = None,
    ) -> list[dict[str, object]]:
        tasks = list(
            self.session.scalars(
                select(ContributionTask).order_by(
                    ContributionTask.created_at,
                    ContributionTask.id,
                )
            )
        )
        states = ContributionTaskStateService(self.session)
        summaries: list[dict[str, object]] = []
        for task in tasks:
            current = states.current(task.id)
            mark = self.session.scalar(
                select(TaskLifecycleMark).where(
                    TaskLifecycleMark.task_id == task.id
                )
            )
            visible_state = mark.mark if mark is not None else current.to_state
            reason = mark.reason_code if mark is not None else current.reason_code
            updated = mark.created_at if mark is not None else current.created_at
            if state is not None and visible_state != state:
                continue
            summaries.append(
                {
                    "id": task.id,
                    "opportunity_id": task.opportunity_id,
                    "analysis_version_id": task.analysis_version_id,
                    "current_state": visible_state,
                    "reason_code": reason,
                    "state_record_hash": current.record_hash,
                    "created_at": task.created_at,
                    "updated_at": updated,
                }
            )
        return summaries

    def _current_state_counts(self) -> dict[str, int]:
        states = ContributionTaskStateService(self.session)
        counts: dict[str, int] = {
            item.value: 0 for item in ContributionTaskState
        }
        counts.update({"failed": 0, "rejected": 0, "abandoned": 0})
        task_ids = list(self.session.scalars(select(ContributionTask.id)))
        for task_id in task_ids:
            mark = self.session.scalar(
                select(TaskLifecycleMark).where(
                    TaskLifecycleMark.task_id == task_id
                )
            )
            if mark is not None:
                counts[mark.mark] = counts.get(mark.mark, 0) + 1
                continue
            current = states.current(task_id)
            counts[current.to_state] = counts.get(current.to_state, 0) + 1
        return counts

    def _heatmap(self) -> list[dict[str, object]]:
        buckets: dict[date, dict[str, int]] = defaultdict(
            lambda: {
                "contributions": 0,
                "pull_requests": 0,
                "merges": 0,
                "rewards": 0,
            }
        )
        for created_at in self.session.scalars(
            select(ContributionTask.created_at)
        ):
            buckets[_as_date(created_at)]["contributions"] += 1
        for created_at in self.session.scalars(
            select(DraftPullRequest.created_at)
        ):
            buckets[_as_date(created_at)]["pull_requests"] += 1
        versions = list(
            self.session.scalars(
                select(ContributionTaskStateVersion).where(
                    ContributionTaskStateVersion.to_state.in_(
                        (
                            ContributionTaskState.MERGED.value,
                            ContributionTaskState.REWARDED.value,
                        )
                    )
                )
            )
        )
        for version in versions:
            day = _as_date(version.created_at)
            if version.to_state == ContributionTaskState.MERGED.value:
                buckets[day]["merges"] += 1
            elif version.to_state == ContributionTaskState.REWARDED.value:
                buckets[day]["rewards"] += 1
        return [
            {"date": day.isoformat(), **counts}
            for day, counts in sorted(buckets.items())
        ]


def _as_date(value: datetime) -> date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date()
