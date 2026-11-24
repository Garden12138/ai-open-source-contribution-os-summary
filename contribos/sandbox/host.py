"""Independent host clones, available only after explicit task trust confirmation."""

import asyncio
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import tempfile
from pathlib import Path
from typing import BinaryIO, Mapping

from contribos.agent.engine import TaskContext
from contribos.agent.profiles import AgentProfile
from contribos.config import Settings
from contribos.sandbox.base import AgentProcess, ExecResult, SandboxError, Workspace, context_files, workspace_diff
from contribos.sandbox.processes import ProcessInfo, process_family, process_info
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
        self.lock: BinaryIO | None = None
        self.recorded: set[tuple[int, int]] = set()
        self.leaders: dict[int, int] = {}

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
        return Workspace(str(repo), str(context), task.default_branch, task.base_sha, task.work_branch, str(root))

    def write_context(self, task: TaskContext) -> None:
        context = self._inside(task.context_dir)
        for filename, content in context_files(task).items():
            target = context / filename
            if target.is_symlink():
                raise SandboxError('unsafe_context_path')
            target.write_text(safe_text(content), encoding='utf-8')

    async def write_task_context(self, task: TaskContext) -> None:
        self.write_context(task)

    async def restore(self, task: TaskContext) -> None:
        self.attach(task)
        self.claim_runtime()
        if not self.processes:
            self._reap_previous_group()

    def claim_runtime(self) -> None:
        if self.lock is not None:
            return
        fd = os.open(self.root / '.chat.lock', os.O_NOFOLLOW | os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(fd, 'rb')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise SandboxError('task_already_open') from None
        self.lock = handle

    def release_runtime(self) -> None:
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def _record_path(self) -> Path:
        directory = self.settings.home / 'runtime'
        if directory.is_symlink():
            raise SandboxError('unsafe_runtime_path')
        return directory / f'{self.root.name}.agent-processes.json'

    def record_processes(self) -> set[tuple[int, int]]:
        members = {(info.pid, info.started) for info in self.agent_processes()}
        members.update((pid, started) for pid, started in self.recorded if self._still_running(pid, started))
        if self.lock and self.processes and members != self.recorded:
            path = self._record_path()
            path.parent.mkdir(mode=0o700, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as record:
                record.write(json.dumps({'group': self.processes[-1].pid, 'members': sorted(members)}).encode())
            os.replace(record.name, path)
            self.recorded = members
        return members

    def _reap_previous_group(self) -> None:
        path = self._record_path()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        try:
            with os.fdopen(fd, 'rb') as record:
                info = json.loads(record.read(65536))
            group = info['group']
            members = set(map(tuple, info['members']))
            if (type(group) is not int or group < 2 or any(type(pid) is not int or pid < 2
                    or type(started) is not int or started <= 0 for pid, started in members)):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SandboxError('invalid_process_record') from None
        leader = process_info(group)
        if leader and leader.group == group and (group, leader.started) in members:
            self._signal_group(group, signal.SIGKILL, leader.started)
        self._signal_members(members, signal.SIGKILL)
        path.unlink(missing_ok=True)

    async def exec(self, argv: list[str], cwd: str | None = None, timeout: float = 120) -> ExecResult:
        if not self.task or not argv:
            raise SandboxError('workspace_not_prepared')
        workdir = self._inside(cwd or self.task.repo_dir)
        command = list(argv)
        if Path(command[0]).name == 'git':
            command[1:1] = GIT_OPTIONS
        if command[0] == 'git':
            command[0] = self.settings.tool_paths.get('git') or command[0]
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
        env = self.settings.tool_environment({
            **self.environment, **profile.env(self.settings), 'GIT_CONFIG_GLOBAL': os.devnull,
        })
        command = profile.argv(self.settings)
        executable = self.settings.tool(profile.command[0])
        if not executable:
            raise SandboxError('agent_not_installed')
        try:
            proc = await asyncio.create_subprocess_exec(executable, *command[1:], cwd=self.task.repo_dir, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True, limit=50 * 1024 * 1024)
        except OSError:
            raise SandboxError('agent_start_failed') from None
        self.processes.append(proc)
        if info := process_info(proc.pid):
            self.leaders[proc.pid] = info.started
        self.record_processes()
        return proc

    async def diff(self) -> str:
        return await workspace_diff(self)

    async def export_file(self, path: str) -> Path:
        source = self._inside(path)
        if not source.is_file():
            raise SandboxError('export_not_found')
        destination = self.settings.home / 'tmp' / self.task.task_id / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def agent_processes(self) -> set[ProcessInfo]:
        return {info for process in self.processes
                for info in self._owned_family(process.pid, self.leaders.get(process.pid)).values()}

    @staticmethod
    def _still_running(pid: int, started: int) -> bool:
        info = process_info(pid)
        return bool(info and not info.zombie and info.started == started)

    @staticmethod
    def _owned_family(group: int, started: int | None) -> dict[int, ProcessInfo]:
        leader = process_info(group)
        if leader is not None and started is not None and leader.started != started:
            return {}
        return process_family(group)

    @classmethod
    def _signal_members(cls, members: set[tuple[int, int]], sig: signal.Signals) -> None:
        for pid, started in members:
            if cls._still_running(pid, started):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, sig)

    def force_stop(self) -> None:
        members = self.recorded.copy()
        for process in self.processes:
            self._signal_group(process.pid, signal.SIGKILL, self.leaders.get(process.pid))
        self._signal_members(members, signal.SIGKILL)

    @classmethod
    def _signal_group(cls, group: int, sig: signal.Signals, started: int | None) -> None:
        family = cls._owned_family(group, started)
        for info in family.values():
            if info.group != group and process_info(info.pid) == info:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(info.pid, sig)
        if any(info.group == group for info in family.values()):
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                if any(info.group == group for info in cls._owned_family(group, started).values()):
                    raise

    async def stop(self) -> None:
        for proc in self.processes:
            # ACP adapters can own an app-server child; stop the entire owned group.
            started = self.leaders.get(proc.pid)
            self._signal_group(proc.pid, signal.SIGTERM, started)
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except TimeoutError:
                    self._signal_group(proc.pid, signal.SIGKILL, started)
                    await proc.wait()
            self._signal_group(proc.pid, signal.SIGKILL, started)
        self._signal_members(self.recorded, signal.SIGKILL)
        self.processes.clear()
        self.leaders.clear()
        self.recorded.clear()
        if self.lock:
            self._record_path().unlink(missing_ok=True)

    async def destroy(self) -> None:
        await self.stop()
        try:
            if self.root:
                shutil.rmtree(self.root)
                self.root = None
        finally:
            self.release_runtime()

    async def destroy_task(self, task: TaskContext) -> None:
        if self.root is None:
            root = self.settings.home / 'workspaces' / task.task_id
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', task.task_id) or any(
                    path.is_symlink() for path in (root, *root.parents)):
                raise SandboxError('unsafe_workspace_path')
            if not root.exists():
                return
            self.root = root
        self.claim_runtime()
        if not self.processes:
            self._reap_previous_group()
        await self.destroy()
