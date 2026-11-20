"""A task's execution environment owns all access to its workspace."""

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
    async def exec(self, argv: list[str], cwd: str | None = None, timeout: float = 120) -> ExecResult: ...
    async def spawn_agent(self, profile: AgentProfile) -> AgentProcess: ...
    async def export_file(self, path: str) -> Path: ...
    async def stop(self) -> None: ...
    async def destroy(self) -> None: ...
