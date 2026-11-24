"""Offline browser demonstration using the regular task runtime."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from contribos.agent.engine import ContentBlock, EngineEvent, TaskContext, TurnResult
from contribos.agent.fake import FakeEngine
from contribos.agent.profiles import PROFILES, AgentProfile, AgentStatus
from contribos.config import Settings
from contribos.sandbox.base import AgentProcess, ExecResult, Sandbox, SandboxError, Workspace
from contribos.tasks.issue import IssueRef
from contribos.tasks.store import TaskStore
from contribos.tasks.supervisor import TaskSupervisor

SAMPLE = IssueRef('contribos-demo/example', 42)
PATCH = """diff --git a/validate.py b/validate.py
--- a/validate.py
+++ b/validate.py
@@ -1,3 +1,3 @@
 def parse_count(value):
     if not value.isdecimal():
-        raise ValueError("Invalid value")
+        raise ValueError("Count must be a non-negative integer")
"""


@dataclass
class DemoSandbox:
    kind: Literal['host', 'sbx', 'container']
    changed: bool = False

    async def prepare(self, task: TaskContext) -> Workspace:
        return Workspace('/demo/repo', '/demo/context', 'main', 'demo-base', 'demo-work')

    async def restore(self, task: TaskContext) -> None:
        task.repo_dir, task.context_dir = '/demo/repo', '/demo/context'

    async def write_task_context(self, task: TaskContext) -> None:
        pass

    async def exec(self, argv: list[str], cwd: str | None = None, timeout: float = 120) -> ExecResult:
        raise SandboxError('demo_command_unavailable')

    async def spawn_agent(self, profile: AgentProfile) -> AgentProcess:
        raise SandboxError('demo_agent_unavailable')

    async def export_file(self, path: str) -> Path:
        raise SandboxError('demo_export_unavailable')

    async def diff(self) -> str:
        return PATCH if self.changed else ''

    async def stop(self) -> None:
        pass

    async def destroy(self) -> None:
        self.changed = False


async def demo_issue(ref: IssueRef) -> dict[str, Any]:
    return {
        'title': '为无效计数提供清晰的错误提示',
        'body': '当计数无效时，提示应说明它必须是非负整数。请保留现有的校验逻辑。',
        'state': 'open', 'assignees': [], 'comments': [{'body': '复现示例：输入 `-1`。'}],
        'linked_prs': [], 'default_branch': 'main',
    }


def demo_statuses() -> list[AgentStatus]:
    return [AgentStatus(profile.id, 'ok', 'demo', '', 'logged_in', '', '离线模拟 Agent', '')
            for profile in PROFILES.values()]


def _event(kind: str, **payload: Any) -> EngineEvent:
    return EngineEvent(kind, payload)


class DemoEngine(FakeEngine):
    def __init__(self, sandbox: DemoSandbox, delay: float) -> None:
        super().__init__(event_delay=delay)
        self.sandbox = sandbox

    async def prompt(self, session_id: str, blocks: list[ContentBlock]) -> TurnResult:
        first = len(self.prompts) == 0
        second = len(self.prompts) == 1
        if first:
            script = [
                _event('reasoning.delta', text='正在检查示例 Issue。'),
                _event('reasoning.summary', text='现有校验逻辑可保留。'),
                _event('message.delta', text='只需要调整错误提示。\n'),
                _event('plan.updated', entries=[{'content': '更新错误提示并运行相关测试', 'status': 'pending'}]),
                _event('message.agent', text='**方案**\n- 更新计数的错误提示。\n- 运行相关测试。\n\n确认后我会开始实现。'),
            ]
        else:
            script = [
                _event('reasoning.summary', text='改动只涉及输入校验的错误提示。'),
                _event('message.delta', text='正在更新错误提示。\n'),
            ]
            if second and self.sandbox.kind == 'host':
                script.append(_event('permission.requested', tool_call={
                    'kind': 'execute', 'title': '运行相关测试',
                    'rawInput': {'command': 'pytest -q'}, 'locations': [{'path': 'validate.py'}],
                }, options=[
                    {'optionId': 'allow', 'kind': 'allow_once'}, {'optionId': 'reject', 'kind': 'reject_once'},
                    {'optionId': 'always', 'kind': 'allow_always'},
                ]))
            if second:
                script.append(_event('question.asked', message='要覆盖哪些测试？', schema={
                    'required': ['scope'], 'properties': {
                        'scope': {'type': 'string', 'title': '测试范围', 'enum': ['输入校验', '全部测试']},
                        'note': {'type': 'string', 'title': '补充要求'},
                    },
                }))
            script += [
                _event('message.delta', text='正在验证改动。\n'),
                _event('command.started', tool_call_id='tests', title='运行测试', command='pytest -q', status='in_progress'),
                _event('command.completed', tool_call_id='tests', status='completed', exit_code=0, output_tail='3 passed'),
                _event('files.changed', tool_call_id='edit', status='completed', paths=['validate.py']),
                _event('usage.updated', tokens_input=260, tokens_output=94),
                _event('message.agent', text='已更新 `validate.py` 中的错误提示。右侧「改动」可查看差异。'),
            ]
        self.scripts = [script]
        result = await super().prompt(session_id, blocks)
        if not first and result.stop_reason == 'end_turn':
            self.sandbox.changed = True
        return result


def demo_supervisor(settings: Settings, store: TaskStore, *, delay: float = 0.45) -> TaskSupervisor:
    def engine(sandbox: Sandbox, profile: AgentProfile) -> FakeEngine:
        return DemoEngine(cast(DemoSandbox, sandbox), delay)

    supervisor = TaskSupervisor(settings, store, issue_loader=demo_issue, engine_factory=engine, sandbox_factories={
        'host': lambda: DemoSandbox('host'),
        'sbx': lambda: DemoSandbox('sbx'),
        'container': lambda: DemoSandbox('container'),
    })
    if not store.list_tasks():
        supervisor.create(SAMPLE, agent='codex', isolation='sbx')
    return supervisor
