"""Durable, hash-bound journal for the contribution workbench.

Only authenticated API services create authorization events. Provider output is
stored as proposals; it never grants an execution or publication capability.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.artifacts import ArtifactStore
from app.jobs import JobService, TERMINAL_STATES
from app.models import WorkbenchEvent, Job, PlanVersion
from app.planning import ContributionTaskService
from app.provenance import canonical_json, content_hash
from app.security import ensure_no_sensitive_data
from app.task_states import ContributionTaskStateService


class WorkbenchError(RuntimeError):
    pass


def event_payload(event: WorkbenchEvent) -> dict:
    return {
        name: getattr(event, name)
        for name in (
            "id",
            "task_id",
            "sequence",
            "kind",
            "actor_id",
            "job_id",
            "plan_version_id",
            "payload_hash",
            "previous_hash",
        )
    }


class Workbench:
    def __init__(self, session: Session, artifact_root: str) -> None:
        self.session = session
        self.artifacts = ArtifactStore(session, artifact_root)

    def models_changed_after(self, task_id: str, sequence: int) -> bool:
        from app.model_settings import ModelSettingsService

        models = ModelSettingsService(self.session)
        # Examine every change: A→B→A or a real switch followed by a duplicate
        # must not be hidden by only checking the newest event.
        return any(
            event.kind == "model_changed" and event.sequence > sequence
            and not models.is_redundant_task_selection(task_id, event.payload)
            for event in self.history(task_id)
        )

    def history(self, task_id: str) -> list[WorkbenchEvent]:
        ContributionTaskService(self.session).get_verified(task_id)
        rows = list(
            self.session.scalars(
                select(WorkbenchEvent)
                .where(WorkbenchEvent.task_id == task_id)
                .order_by(WorkbenchEvent.sequence)
            )
        )
        previous = None
        for sequence, row in enumerate(rows, 1):
            if (
                row.sequence != sequence
                or row.previous_hash != previous
                or row.payload_hash != content_hash(row.payload)
                or row.record_hash != content_hash(event_payload(row))
            ):
                raise WorkbenchError("工作台记录校验失败")
            previous = row.record_hash
        return rows

    def append(
        self,
        task_id: str,
        kind: str,
        payload: dict,
        *,
        key: str,
        actor: str = "system",
        job_id: str | None = None,
        plan_id: str | None = None,
        commit: bool = True,
    ) -> WorkbenchEvent:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", key):
            raise WorkbenchError("幂等标识无效")
        ensure_no_sensitive_data(
            {"actor": actor, "payload": payload, "key": key}, context="workbench"
        )
        rows = self.history(task_id)
        old = self.session.scalar(
            select(WorkbenchEvent).where(WorkbenchEvent.idempotency_key == key)
        )
        if old:
            if (
                old.task_id,
                old.kind,
                old.payload_hash,
                old.actor_id,
                old.job_id,
                old.plan_version_id,
            ) != (task_id, kind, content_hash(payload), actor, job_id, plan_id):
                raise WorkbenchError("重复请求的内容已变化")
            return old
        row = WorkbenchEvent(
            id=str(uuid4()),
            task_id=task_id,
            sequence=len(rows) + 1,
            kind=kind,
            actor_id=actor,
            idempotency_key=key,
            job_id=job_id,
            plan_version_id=plan_id,
            payload=payload,
            payload_hash=content_hash(payload),
            previous_hash=rows[-1].record_hash if rows else None,
            created_at=datetime.now(timezone.utc),
        )
        row.record_hash = content_hash(event_payload(row))
        self.session.add(row)
        self.session.flush()
        if commit:
            self.session.commit()
        return row

    def latest(self, task_id: str, kind: str) -> WorkbenchEvent | None:
        return next(
            (r for r in reversed(self.history(task_id)) if r.kind == kind), None
        )

    def artifact(self, data: dict | list) -> str:
        return self.artifacts.store_bytes(
            canonical_json(data).encode(), media_type="application/json", commit=False
        ).id

    def read(self, artifact_id: str) -> dict:
        import json

        return json.loads(self.artifacts.read_bytes(artifact_id))

    def latest_plan(self, task_id: str) -> PlanVersion | None:
        from app.plans import PlanVersionService

        row = self.session.scalar(
            select(PlanVersion)
            .where(PlanVersion.task_id == task_id)
            .order_by(PlanVersion.version_number.desc())
            .limit(1)
        )
        return PlanVersionService(self.session).get_verified(row.id) if row else None

    def require_planning(self, task_id: str) -> str:
        state = ContributionTaskStateService(self.session).current(task_id)
        if state.to_state != "planning":
            raise WorkbenchError("请先停止执行并返回方案编辑")
        return state.record_hash

    def jobs(self, task_id: str) -> list[Job]:
        from sqlalchemy import or_
        from app.models import ExecutionAttempt, ExecutionStageRun

        ids = {r.job_id for r in self.history(task_id) if r.job_id}
        attempts = list(
            self.session.scalars(
                select(ExecutionAttempt.id).where(ExecutionAttempt.task_id == task_id)
            )
        )
        if attempts:
            ids.update(
                self.session.scalars(
                    select(ExecutionStageRun.job_id).where(
                        ExecutionStageRun.execution_attempt_id.in_(attempts)
                    )
                )
            )
        return list(
            self.session.scalars(
                select(Job)
                .where(
                    or_(
                        Job.id.in_(ids),
                        Job.payload["execution_attempt_id"].as_string().in_(attempts),
                    )
                )
                .order_by(Job.created_at, Job.id)
            )
        )

    def enqueue(
        self,
        task_id: str,
        kind: str,
        payload: dict,
        *,
        key: str,
        actor: str = "system",
        commit: bool = True,
    ) -> Job:
        job, _ = JobService(self.session).enqueue(
            kind=kind,
            idempotency_key=key,
            payload={"task_id": task_id, **payload},
            max_attempts=1,
            timeout_seconds=600,
            commit=False,
        )
        self.append(
            task_id,
            "job_queued",
            {"kind": kind},
            key="event:" + job.id,
            actor=actor,
            job_id=job.id,
            commit=False,
        )
        if commit:
            self.session.commit()
        return job

    def assert_idle(self, task_id: str) -> None:
        if any(j.state not in TERMINAL_STATES for j in self.jobs(task_id)):
            raise WorkbenchError("任务正在运行，请等待完成或先停止")

    def binding(self, task_id: str, plan_id: str) -> WorkbenchEvent:
        binding = next(
            (
                e
                for e in reversed(self.history(task_id))
                if e.kind == "plan_bound" and e.plan_version_id == plan_id
            ),
            None,
        )
        if binding is None:
            raise WorkbenchError("这份历史方案没有代码阅读依据，请先让 AI 重新规划")
        context = self.latest(task_id, "context_ready")
        if context is None or binding.payload["context_hash"] != context.record_hash:
            raise WorkbenchError("方案的代码依据已过期，请重新规划")
        return binding
