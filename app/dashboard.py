from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import func, or_, select
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
    Job,
)
from app.task_states import ContributionTaskState, ContributionTaskStateService
from app.task_visibility import current_visibility, visibility_expression


class ContributionDashboardService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def snapshot(self) -> dict[str, object]:
        scanned = self.session.scalar(select(func.count(Opportunity.id))) or 0
        analyzed = (
            self.session.scalar(select(func.count(AnalysisVersion.id))) or 0
        )
        planned = self.session.scalar(select(func.count(PlanVersion.id)).where(visibility_expression(PlanVersion.task_id) != "deleted")) or 0
        executed = (
            self.session.scalar(select(func.count(ExecutionAttempt.id)).where(visibility_expression(ExecutionAttempt.task_id) != "deleted")) or 0
        )
        reviewed = self.session.scalar(select(func.count(ReviewRun.id)).where(visibility_expression(ReviewRun.task_id) != "deleted")) or 0
        submitted = (
            self.session.scalar(select(func.count(DraftPullRequest.id)).where(visibility_expression(DraftPullRequest.task_id) != "deleted")) or 0
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
                    self.session.scalar(select(func.count(ContributionTask.id)).where(visibility_expression() != "deleted"))
                    or 0
                ),
                "publish_intent_count": (
                    self.session.scalar(select(func.count(PublishIntent.id)).where(visibility_expression(PublishIntent.task_id) != "deleted"))
                    or 0
                ),
                "pull_request_event_count": (
                    self.session.scalar(select(func.count(PullRequestEvent.id)).where(visibility_expression(PullRequestEvent.task_id) != "deleted"))
                    or 0
                ),
            },
        }

    def task_summaries(
        self,
        *,
        state: str | None = None,
        archived: bool = False,
    ) -> list[dict[str, object]]:
        erasure = select(Job.id).where(Job.kind == "task_erasure",
            Job.idempotency_key == "erase:" + ContributionTask.id).exists()
        visible = visibility_expression()
        tasks = list(
            self.session.scalars(
                select(ContributionTask).where(
                    or_(visible == "archived", (visible == "deleted") & erasure) if archived else visible == "active"
                ).order_by(
                    ContributionTask.created_at,
                    ContributionTask.id,
                )
            )
        )
        states = ContributionTaskStateService(self.session)
        summaries: list[dict[str, object]] = []
        for task in tasks:
            visibility = current_visibility(self.session, task.id)
            erasure_job = self.session.scalar(select(Job).where(
                Job.kind == "task_erasure", Job.idempotency_key == "erase:" + task.id))
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
            opportunity = self.session.get(Opportunity, task.opportunity_id)
            repository = (
                opportunity.repository if opportunity is not None else None
            )
            friendly, progress, next_action = _friendly_task_state(visible_state)
            summaries.append(
                {
                    "id": task.id,
                    "visibility": visibility.state if visibility else "active",
                    "visibility_sequence": visibility.sequence if visibility else 0,
                    "erasure_state": erasure_job.state if erasure_job else None,
                    "opportunity_id": task.opportunity_id,
                    "analysis_version_id": task.analysis_version_id,
                    "current_state": visible_state,
                    "reason_code": reason,
                    "state_record_hash": current.record_hash,
                    "opportunity_title": (
                        opportunity.title if opportunity is not None else None
                    ),
                    "repository_full_name": (
                        repository.full_name if repository is not None else None
                    ),
                    "friendly_state": friendly,
                    "progress_percent": progress,
                    "next_action": next_action,
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
        task_ids = list(self.session.scalars(select(ContributionTask.id).where(visibility_expression() != "deleted")))
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
            select(ContributionTask.created_at).where(visibility_expression() != "deleted")
        ):
            buckets[_as_date(created_at)]["contributions"] += 1
        for created_at in self.session.scalars(
            select(DraftPullRequest.created_at).where(visibility_expression(DraftPullRequest.task_id) != "deleted")
        ):
            buckets[_as_date(created_at)]["pull_requests"] += 1
        versions = list(
            self.session.scalars(
                select(ContributionTaskStateVersion).where(
                    visibility_expression(ContributionTaskStateVersion.task_id) != "deleted",
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


def _friendly_task_state(state: str) -> tuple[str, int, str | None]:
    return {
        "planning": ("准备贡献计划", 15, "完善并批准计划"),
        "plan_approved": ("计划已确认", 30, "开始执行"),
        "executing": ("正在实现与验证", 55, "查看执行进度"),
        "reviewing": ("正在评审", 70, "处理评审结果"),
        "ready": ("可以准备提交", 82, "创建发布意图"),
        "draft_pr": ("Draft PR 已创建", 90, "关注维护者反馈"),
        "changes_requested": ("需要修改", 72, "根据反馈修订"),
        "merged": ("已合并", 100, "记录贡献成果"),
        "rewarded": ("已获奖", 100, None),
        "failed": ("执行失败", 50, "检查失败原因"),
        "rejected": ("贡献未被接收", 100, None),
        "abandoned": ("已放弃", 100, None),
    }.get(state, (state, 0, None))
