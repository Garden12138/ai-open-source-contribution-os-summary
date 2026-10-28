"""Plugin contract implementation; external executors cannot supply evidence."""
from __future__ import annotations

from app.agent_v2.contracts import AgentCheckpoint, AgentRun, TaskSpec, ToolRegistry, ToolResult
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import schedule
from app.agent_v2.orchestrator import TaskOrchestrator
from app.config import Settings
from app.database import Database
from app.jobs import JobService, TERMINAL_STATES
from app.workbench import Workbench, WorkbenchError


class JournalAgentExecutor:
    """A thin async executor facade over the persistent job-driven loop."""
    def __init__(self, database: Database, settings: Settings, task_id: str) -> None:
        self.database, self.settings, self.task_id = database, settings, task_id

    async def start(self, task_spec: TaskSpec, tools: ToolRegistry) -> AgentRun:
        if task_spec.binding.task_id != self.task_id:
            raise WorkbenchError("任务来源不匹配。", code="agent_session_invalid")
        # Tools remain platform-owned. Supplying a Protocol implementation does
        # not register host functions or alter the worker's restricted registry.
        with self.database.session() as session:
            wb = Workbench(session, self.settings.artifact_root)
            journal = TaskOrchestrator(wb, self.settings).start(task_spec.binding.attempt_id, limits=task_spec.limits)
            if journal.spec() != task_spec:
                raise WorkbenchError("执行输入未通过当前授权校验。", code="agent_authorization_stale")
            schedule(journal)
            session.commit()
            return AgentRun(session_id=journal.session_id, checkpoint=journal.checkpoint())

    async def resume(self, checkpoint: AgentCheckpoint, feedback: ToolResult | None) -> AgentRun:
        if feedback is not None:
            raise WorkbenchError("工具证据只能由已租约的隔离 Worker 写入。", code="agent_tool_receipt_invalid")
        with self.database.session() as session:
            journal = AgentJournal(Workbench(session, self.settings.artifact_root), self.task_id, checkpoint.session_id)
            TaskOrchestrator(journal.wb, self.settings).require_current(journal)
            cp = journal.control("continue", checkpoint.revision)
            schedule(journal)
            session.commit()
            return AgentRun(session_id=journal.session_id, checkpoint=cp)

    async def cancel(self, run_id: str) -> None:
        with self.database.session() as session:
            journal = AgentJournal(Workbench(session, self.settings.artifact_root), self.task_id, run_id)
            cp = journal.checkpoint()
            if cp.state != "cancelled":
                journal.control("cancel", cp.revision)
            for job in journal.wb.jobs(self.task_id):
                if job.payload.get("session_id") == run_id and job.state not in TERMINAL_STATES:
                    JobService(session).request_cancel(job.id, commit=False)
            session.commit()
