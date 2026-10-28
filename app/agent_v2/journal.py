"""One task journal, short transactions and CAS checkpoints; no SDK state store."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from app.agent_v2.contracts import AgentCheckpoint, TaskSpec
from app.provenance import content_hash
from app.workbench import Workbench, WorkbenchError

PREFIX = "agent_v2_"
TERMINAL = {"blocked", "finished", "cancelled"}
EDGES = {
    "ready": {"model", "paused", "blocked", "cancelled"},
    "model": {"ready", "tools", "finished", "blocked", "paused", "cancelled"},
    "tools": {"tools", "ready", "paused", "blocked", "cancelled"},
    "paused": {"ready", "tools", "blocked", "cancelled"},
    "blocked": set(), "finished": set(), "cancelled": set(),
}


class AgentJournal:
    def __init__(self, wb: Workbench, task_id: str, session_id: str) -> None:
        self.wb, self.task_id, self.session_id = wb, task_id, session_id

    def lock(self) -> None:
        # Serializes both the observation and append, including separate processes.
        from app.cla import _lock
        _lock(self.wb)

    def events(self) -> list[Any]:
        return [e for e in self.wb.history(self.task_id)
                if e.kind.startswith(PREFIX) and e.payload.get("session_id") == self.session_id]

    def root(self) -> Any:
        root = next((e for e in self.events() if e.kind == PREFIX + "session"), None)
        if root is None or root.actor_id != "orchestrator":
            raise WorkbenchError("运行会话不存在或来源无效。", code="agent_session_invalid")
        return root

    def spec(self) -> TaskSpec:
        return TaskSpec.model_validate(self.wb.read(self.root().payload["spec_artifact"]))

    def checkpoint(self) -> AgentCheckpoint:
        event = next((e for e in reversed(self.events()) if e.kind == PREFIX + "checkpoint"), None)
        if event is None or event.actor_id != "orchestrator":
            raise WorkbenchError("运行检查点不存在。", code="agent_checkpoint_invalid")
        result = AgentCheckpoint.model_validate(event.payload["checkpoint"])
        return result.model_copy(update={"revision": event.record_hash})

    def append(self, kind: str, payload: dict, *, key: str, job_id: str | None = None) -> Any:
        return self.wb.append(self.task_id, PREFIX + kind,
            {"version": "agent-runtime-v2", "session_id": self.session_id, **payload},
            key="arv2:" + key, actor="orchestrator", job_id=job_id, commit=False)

    def transition(self, expected: AgentCheckpoint, *, state: str,
                   key: str, job_id: str | None = None, **updates) -> AgentCheckpoint:
        self.lock()
        current = self.checkpoint()
        if current.revision != expected.revision:
            raise WorkbenchError("运行事实已变化。", code="agent_checkpoint_stale")
        if state not in EDGES[current.state]:
            raise WorkbenchError("运行状态不能这样继续。", code="agent_transition_invalid")
        values = {**current.model_dump(), **updates, "state": state}
        next_checkpoint = AgentCheckpoint.model_validate(values)
        self.append("checkpoint", {"checkpoint": next_checkpoint.model_dump()}, key=key, job_id=job_id)
        return self.checkpoint()

    def remaining(self, checkpoint: AgentCheckpoint | None = None) -> dict[str, int]:
        cp = checkpoint or self.checkpoint()
        values = {k: max(0, v - cp.usage.get(k, 0)) for k, v in self.spec().limits.model_dump().items()}
        values["seconds"] = max(0, values["seconds"] - int(self.elapsed_seconds()))
        return values

    def elapsed_seconds(self) -> float:
        root = self.root()
        start = root.payload.get("budget_started_at")
        initial = datetime.fromisoformat(start) if start else root.created_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - initial).total_seconds()

    def charge(self, cp: AgentCheckpoint, charges: dict[str, int]) -> dict[str, int]:
        limits = self.spec().limits.model_dump()
        usage = dict(cp.usage)
        for key, count in charges.items():
            if key not in limits or count < 0 or usage.get(key, 0) + count > limits[key]:
                raise WorkbenchError("运行预算已用尽；已用额度保留。", code="agent_budget_exceeded")
            usage[key] = usage.get(key, 0) + count
        if self.elapsed_seconds() > limits["seconds"]:
            raise WorkbenchError("运行时间预算已用尽。", code="agent_time_budget_exceeded")
        return usage

    def control(self, action: str, expected_revision: str) -> AgentCheckpoint:
        self.lock()
        cp = self.checkpoint()
        if cp.revision != expected_revision:
            raise WorkbenchError("运行事实已变化。", code="agent_checkpoint_stale")
        # A continue is a deterministic no-op for active work. Never requeue a
        # tool/model whose completion is ambiguous and never reset its usage.
        if action == "continue" and cp.state != "paused":
            return cp
        if action == "pause" and cp.state in {"model", "tools"}:
            self.append("pause_requested", {"checkpoint_hash": cp.revision}, key="pause-request:" + cp.revision)
            return cp
        target = {"pause": "paused", "continue": "tools" if cp.pending_calls else "ready", "cancel": "cancelled"}.get(action)
        if target is None:
            raise WorkbenchError("不支持此运行操作。", code="agent_control_invalid")
        if action == "continue":
            self.append("resumed", {"checkpoint_hash": cp.revision}, key="resumed:" + cp.revision)
        return self.transition(cp, state=target, key="control:" + content_hash([cp.revision, action]))
