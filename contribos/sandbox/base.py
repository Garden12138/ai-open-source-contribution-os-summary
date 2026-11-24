"""A task's execution environment owns all access to its workspace."""

import json
from asyncio.subprocess import Process
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from contribos.agent.engine import TaskContext
from contribos.agent.profiles import AgentProfile

AgentProcess = Process


@dataclass(frozen=True)
class Workspace:
    repo_dir: str
    context_dir: str
    default_branch: str
    base_sha: str
    work_branch: str
    reference: str | None = None


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str = ''


class SandboxError(RuntimeError):
    """A public error code, with no subprocess diagnostics attached."""


class Sandbox(Protocol):
    kind: Literal['host', 'sbx', 'container']

    async def prepare(self, task: TaskContext) -> Workspace: ...
    async def restore(self, task: TaskContext) -> None: ...
    async def write_task_context(self, task: TaskContext) -> None: ...
    async def exec(self, argv: list[str], cwd: str | None = None, timeout: float = 120) -> ExecResult: ...
    async def diff(self) -> str: ...
    async def spawn_agent(self, profile: AgentProfile) -> AgentProcess: ...
    async def export_file(self, path: str) -> Path: ...
    async def stop(self) -> None: ...
    async def destroy(self) -> None: ...


def context_files(task: TaskContext) -> dict[str, str]:
    return {
        'TASK.md': task.instructions,
        'issue.md': f"# {task.issue.get('title', '')}\n\n{task.issue.get('body', '')}\n\nComments:\n" +
                    '\n\n'.join(str(c.get('body', '')) for c in task.issue.get('comments', [])),
        'linked_prs.json': json.dumps(task.linked_prs, ensure_ascii=False, indent=2),
    }


async def workspace_diff(sandbox: Sandbox) -> str:
    result = await sandbox.exec(['git', 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--'])
    if result.returncode:
        raise SandboxError('diff_failed')
    status = await sandbox.exec(['git', 'ls-files', '--others', '--exclude-standard', '-z'])
    if status.returncode:
        raise SandboxError('diff_failed')
    chunks = [result.stdout]
    for filename in status.stdout.split('\0'):
        if not filename:
            continue
        # no-index diff displays new files without mutating the index.
        extra = await sandbox.exec(['git', 'diff', '--no-index', '--no-ext-diff', '--no-textconv', '--binary', '--', '/dev/null', filename])
        if extra.returncode not in {0, 1}:
            raise SandboxError('diff_failed')
        chunks.append(extra.stdout)
    return ''.join(chunks)
