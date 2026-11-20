"""Independent host clones, available only after explicit task trust confirmation."""

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Mapping

from contribos.agent.engine import TaskContext
from contribos.agent.profiles import AgentProfile
from contribos.config import Settings
from contribos.sandbox.base import AgentProcess, ExecResult, SandboxError, Workspace
from contribos.security import redact

GIT_OPTIONS = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
               '-c', 'credential.helper=', '-c', 'core.sshCommand=/usr/bin/false']
MINIMAL_GIT_CONFIG = '[credential]\n\thelper =\n\tinteractive = false\n[core]\n\thooksPath = /dev/null\n\tfsmonitor = false\n'


def clean_environment(source: Mapping[str, str], runtime_dir: Path) -> dict[str, str]:
    """Remove inherited credential/config overrides and isolate default credential files."""
    blocked = {'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'BASH_ENV', 'ENV',
               'ZDOTDIR', 'NODE_OPTIONS', 'PYTHONPATH', 'PYTHONHOME', 'CODEX_HOME', 'CODEX_CONFIG', 'CODEX_PATH',
               'DEFAULT_AUTH_REQUEST', 'MODEL_PROVIDER', 'INITIAL_AGENT_MODE', 'APP_SERVER_LOGS',
               'CURSOR_CONFIG_DIR', 'CODEX_REMOTE_AUTH_TOKEN', 'NETRC', 'CURL_HOME'}
    env = {k: v for k, v in source.items() if k not in blocked and not k.startswith(
        ('GIT_', 'GH_', 'GITHUB_', 'CONTRIBOS_', 'LD_', 'DYLD_'))}
    env.update(HOME=str(runtime_dir / 'home'), USERPROFILE=str(runtime_dir / 'home'),
        XDG_CONFIG_HOME=str(runtime_dir / 'home' / '.config'),
        GH_CONFIG_DIR=str(runtime_dir / 'gh'), GIT_CONFIG_GLOBAL=str(runtime_dir / 'gitconfig'),
        GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
        GIT_ASKPASS='/usr/bin/false', SSH_ASKPASS='/usr/bin/false', GIT_SSH_COMMAND='/usr/bin/false',
        GIT_CONFIG_COUNT='5', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='',
        GIT_CONFIG_KEY_1='core.hooksPath', GIT_CONFIG_VALUE_1=os.devnull,
        GIT_CONFIG_KEY_2='core.fsmonitor', GIT_CONFIG_VALUE_2='false',
        GIT_CONFIG_KEY_3='http.extraHeader', GIT_CONFIG_VALUE_3='',
        GIT_CONFIG_KEY_4='credential.interactive', GIT_CONFIG_VALUE_4='false')
    return env


def safe_text(text: str, source: Mapping[str, str] | None = None) -> str:
    for key, value in (source or os.environ).items():
        if value and any(part in key.upper() for part in ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD')):
            text = text.replace(value, '[REDACTED]')
    return redact(text)


class HostSandbox:
    kind = 'host'

    def __init__(self, settings: Settings, *, clone_source: str | None = None) -> None:
        self.settings = settings
        self.clone_source = clone_source
        self.root: Path | None = None
        self.task: TaskContext | None = None
        self.processes: list[AgentProcess] = []
        self.environment: dict[str, str] = {}

    def attach(self, task: TaskContext) -> None:
        if not task.trust_confirmed or task.isolation != 'host':
            raise SandboxError('host_trust_required')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', task.task_id):
            raise SandboxError('invalid_task_id')
        root = self.settings.home / 'workspaces' / task.task_id
        for path in [root, *root.parents, root / 'repo', root / 'repo' / '.git', root / 'context', root / 'runtime']:
            if path.is_symlink():
                raise SandboxError('unsafe_workspace_path')
        if not (root / 'repo' / '.git').is_dir() or not (root / 'context').is_dir():
            raise SandboxError('workspace_missing')
        self.root, self.task = root, task
        task.repo_dir, task.context_dir = str(root / 'repo'), str(root / 'context')
        self.environment = clean_environment(os.environ, root / 'runtime')

    def _inside(self, path: str) -> Path:
        if not self.root:
            raise SandboxError('workspace_not_prepared')
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise SandboxError('path_outside_workspace')
        return resolved

    async def prepare(self, task: TaskContext) -> Workspace:
        if task.isolation != 'host' or not task.trust_confirmed:
            raise SandboxError('host_trust_required')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', task.task_id):
            raise SandboxError('invalid_task_id')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', task.repo_full_name):
            raise SandboxError('invalid_repository')
        root = self.settings.home / 'workspaces' / task.task_id
        for path in [root, *root.parents]:
            if path.is_symlink():
                raise SandboxError('unsafe_workspace_path')
        if root.exists():
            raise SandboxError('workspace_exists')
        root.mkdir(parents=True, mode=0o700)
        self.root, self.task = root, task
        repo, context, runtime = root / 'repo', root / 'context', root / 'runtime'
        context.mkdir(mode=0o700)
        (runtime / 'home').mkdir(parents=True, mode=0o700)
        (runtime / 'gh').mkdir(mode=0o700)
        (runtime / 'gitconfig').write_text(MINIMAL_GIT_CONFIG)
        self.environment = clean_environment(os.environ, runtime)
        task.repo_dir, task.context_dir = str(repo), str(context)
        source = self.clone_source or f'https://github.com/{task.repo_full_name}.git'
        result = await self.exec(['git', 'clone', '--filter=blob:none', '--', source, str(repo)], cwd=str(root), timeout=300)
        if result.returncode:
            raise SandboxError('clone_failed')
        branch = await self.exec(['git', 'symbolic-ref', '--short', 'HEAD'])
        base = await self.exec(['git', 'rev-parse', 'HEAD'])
        if branch.returncode or base.returncode:
            raise SandboxError('base_commit_unavailable')
        task.default_branch, task.base_sha = branch.stdout.strip(), base.stdout.strip()
        task.work_branch = f'contribos/{task.task_id}'
        result = await self.exec(['git', 'switch', '-c', task.work_branch])
        if result.returncode:
            raise SandboxError('branch_creation_failed')
        self.write_context(task)
        return Workspace(str(repo), str(context), task.default_branch, task.base_sha, task.work_branch)

    def write_context(self, task: TaskContext) -> None:
        context = self._inside(task.context_dir)
        for filename, content in {
            'TASK.md': task.instructions,
            'issue.md': f"# {task.issue.get('title', '')}\n\n{task.issue.get('body', '')}\n\nComments:\n" +
                        '\n\n'.join(str(c.get('body', '')) for c in task.issue.get('comments', [])),
            'linked_prs.json': json.dumps(task.linked_prs, ensure_ascii=False, indent=2),
        }.items():
            target = context / filename
            if target.is_symlink():
                raise SandboxError('unsafe_context_path')
            target.write_text(safe_text(content), encoding='utf-8')

    async def exec(self, argv: list[str], cwd: str | None = None, timeout: float = 120) -> ExecResult:
        if not self.task or not argv:
            raise SandboxError('workspace_not_prepared')
        workdir = self._inside(cwd or self.task.repo_dir)
        command = list(argv)
        if Path(command[0]).name == 'git':
            command[1:1] = GIT_OPTIONS
        try:
            proc = await asyncio.create_subprocess_exec(*command, cwd=workdir, env=self.environment,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError:
            raise SandboxError('command_start_failed') from None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            proc.kill()
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SandboxError('command_timeout') from None
        return ExecResult(proc.returncode, safe_text(stdout.decode(errors='replace')), safe_text(stderr.decode(errors='replace')))

    async def spawn_agent(self, profile: AgentProfile) -> AgentProcess:
        if not self.task or not self.root:
            raise SandboxError('workspace_not_prepared')
        config = profile.config_dir(self.settings)
        for path in [config, *config.parents]:
            if path.is_symlink():
                raise SandboxError('unsafe_agent_config')
        config.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Shell sandboxes can deny reads of the platform's runtime directory.
        env = {**self.environment, **profile.env(self.settings), 'GIT_CONFIG_GLOBAL': os.devnull}
        command = profile.argv(self.settings)
        executable = shutil.which(command[0])
        if not executable:
            raise SandboxError('agent_not_installed')
        try:
            proc = await asyncio.create_subprocess_exec(executable, *command[1:], cwd=self.task.repo_dir, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True, limit=50 * 1024 * 1024)
        except OSError:
            raise SandboxError('agent_start_failed') from None
        self.processes.append(proc)
        return proc

    async def diff(self) -> str:
        result = await self.exec(['git', 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--'])
        if result.returncode:
            raise SandboxError('diff_failed')
        status = await self.exec(['git', 'ls-files', '--others', '--exclude-standard', '-z'])
        if status.returncode:
            raise SandboxError('diff_failed')
        chunks = [result.stdout]
        for filename in status.stdout.split('\0'):
            if not filename:
                continue
            # no-index diff displays new files without mutating the index.
            extra = await self.exec(['git', 'diff', '--no-index', '--no-ext-diff', '--no-textconv', '--binary', '--', '/dev/null', filename])
            if extra.returncode not in {0, 1}:
                raise SandboxError('diff_failed')
            chunks.append(extra.stdout)
        return ''.join(chunks)

    async def export_file(self, path: str) -> Path:
        source = self._inside(path)
        if not source.is_file():
            raise SandboxError('export_not_found')
        destination = self.settings.home / 'tmp' / self.task.task_id / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    async def stop(self) -> None:
        for proc in self.processes:
            # ACP adapters can own an app-server child; stop the entire owned group.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        self.processes.clear()

    async def destroy(self) -> None:
        await self.stop()
        if self.root:
            shutil.rmtree(self.root)
            self.root = None
