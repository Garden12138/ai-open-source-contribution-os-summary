"""Local Docker Sandboxes backend. Workspace paths are always guest paths."""

import asyncio
import fcntl
import json
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from contribos.agent.engine import TaskContext
from contribos.agent.profiles import AgentProfile
from contribos.config import Settings
from contribos.sandbox.base import AgentProcess, ExecResult, SandboxError, Workspace, context_files, workspace_diff
from contribos.sandbox.host import GIT_OPTIONS, clean_environment, safe_text

ROOT = PurePosixPath('/home/agent/workspace')
REPO, CONTEXT = str(ROOT / 'repo'), str(ROOT / 'context')
PACKAGE_DOMAINS = (
    'github.com:443', 'api.github.com:443', 'codeload.github.com:443', 'raw.githubusercontent.com:443',
    'release-assets.githubusercontent.com:443', 'pypi.org:443', 'files.pythonhosted.org:443',
    'registry.npmjs.org:443', 'crates.io:443', 'static.crates.io:443', 'index.crates.io:443',
    'deb.debian.org:80', 'deb.debian.org:443', 'security.debian.org:80',
)
PRIVATE_NETWORKS = (
    'localhost', 'host.docker.internal', 'gateway.docker.internal',
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
    '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10',
)
CREDENTIAL_VARS = (
    'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN',
    'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'MCP_GATEWAY_URL', 'MCP_SENTINEL_TOKEN_NAME',
)
GUEST_ENV = [v for key in CREDENTIAL_VARS for v in ('-u', key)] + [
    f'{k}={v}' for k, v in clean_environment({}, Path(str(ROOT / 'runtime'))).items()
    if k.startswith(('GH_', 'GIT_', 'SSH_'))
]
WRITE_CONTEXT = '''
import json, pathlib, sys
directory = pathlib.Path(sys.argv[1])
directory.mkdir(mode=0o700, exist_ok=True)
for name, content in json.load(sys.stdin).items():
    (directory / name).write_text(content, encoding="utf-8")
'''
EXPORT = '''
import os, pathlib, shutil, stat, sys
path = pathlib.Path(sys.argv[1]).resolve(strict=True)
path.relative_to(sys.argv[2])
with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
        raise ValueError("Expected regular file")
    shutil.copyfileobj(f, sys.stdout.buffer)
'''


class SbxSandbox:
    kind = 'sbx'

    def __init__(self, settings: Settings, profile: AgentProfile) -> None:
        self.settings = settings
        self.profile = profile
        self.name = ''
        self.vm_id = ''
        self.lock: BinaryIO | None = None
        self.agent: AgentProcess | None = None

    def _attach(self, task: TaskContext) -> None:
        if task.isolation != self.kind or not self.profile.sbx:
            raise SandboxError('sandbox_agent_unavailable')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,55}', task.task_id) or task.task_id.endswith('-'):
            raise SandboxError('invalid_task_id')
        name = f'ctask-{task.task_id}'
        if self.name and self.name != name:
            raise SandboxError('sandbox_task_mismatch')
        self.name = name
        if self.lock is not None:
            return
        runtime = self.settings.home / 'runtime'
        if runtime.is_symlink():
            raise SandboxError('unsafe_runtime_path')
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(runtime / f'{name}.lock', os.O_NOFOLLOW | os.O_CREAT | os.O_RDWR, 0o600)
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

    async def _spawn(self, *args: str, interactive: bool = False, stderr: int = asyncio.subprocess.PIPE) -> AgentProcess:
        executable = self.settings.tool('sbx')
        if not executable:
            raise SandboxError('sbx_unavailable')
        # sbx can start its daemon. Let it use the OS proxy and native credential store.
        environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR',
                       'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR') if k in os.environ}
        try:
            return await asyncio.create_subprocess_exec(executable, '--no-input', *args, env=environment,
                stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=stderr, start_new_session=True, limit=50 * 1024 * 1024)
        except OSError:
            raise SandboxError('sbx_unavailable') from None

    @staticmethod
    async def _kill(process: AgentProcess) -> None:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.communicate()

    async def _call(self, *args: str, timeout: float = 60, data: bytes | None = None) -> tuple[int, bytes, bytes]:
        process = await self._spawn(*args, interactive=data is not None)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout)
        except (TimeoutError, asyncio.CancelledError) as error:
            await self._kill(process)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise SandboxError('command_timeout') from None
        return process.returncode, stdout, stderr

    async def _checked(self, *args: str, error: str, timeout: float = 60) -> bytes:
        code, stdout, _ = await self._call(*args, timeout=timeout)
        if code:
            raise SandboxError(error)
        return stdout

    async def _json(self, *args: str, error: str) -> dict[str, Any]:
        try:
            value = json.loads(await self._checked(*args, error=error))
            if isinstance(value, dict):
                return value
        except (ValueError, UnicodeError):
            pass
        raise SandboxError(error)

    async def _check_secrets(self) -> None:
        profile = self.profile.sbx
        assert profile is not None
        for scope in (('--global',), ('--sandbox', self.name)):
            secrets = await self._json('secret', 'ls', *scope, '--json', error='sbx_credentials_unavailable')
            if secrets.get('custom_secrets') or any(
                s.get('type') != 'service' or s.get('name') != profile.service or s.get('secret') != '(oauth configured)'
                for s in (secrets.get('secrets') or [])
            ):
                raise SandboxError('sbx_credential_policy_unsafe')
        socket_path = await self._checked('settings', 'get', 'ssh.agentSocketPath', error='sbx_credentials_unavailable')
        if socket_path.strip():
            raise SandboxError('sbx_credential_policy_unsafe')

    async def _check_policy(self, *, scoped: bool) -> None:
        profile = self.profile.sbx
        assert profile is not None
        scope = (self.name,) if scoped else ()
        policy = await self._json('policy', 'ls', *scope, '--json', error='sbx_network_policy_unavailable')
        approved = set(PACKAGE_DOMAINS + profile.domains)
        active = [rule for rule in policy.get('rules', []) if rule.get('status') == 'active'
                  and (not rule.get('scope', '').startswith('sandbox:')
                       or scoped and rule['scope'] == f'sandbox:{self.name}')]
        if not any(r.get('id') == 'default-deny-all' for r in active):
            raise SandboxError('sbx_deny_all_required')
        for rule in active:
            kind = rule.get('resource_type', '')
            if kind.startswith(('network', 'http')) and rule.get('decision') == 'allow':
                if not set(rule.get('resources', [])) <= approved or 'net:connect:udp' in rule.get('actions', []):
                    raise SandboxError('sbx_network_policy_unsafe')

    async def _inspect(self) -> dict[str, Any]:
        return await self._json('inspect', self.name, '--json', error='sbx_workspace_missing')

    async def _verify_workspace(self) -> None:
        profile = self.profile.sbx
        assert profile is not None
        info = await self._inspect()
        if (info.get('id') != self.vm_id or info.get('agent') != self.profile.sbx_agent
                or info.get('kits') != [profile.workload, profile.kit]
                or info.get('effective_working_dir') != str(ROOT)
                or info.get('host_mounts') != [] or info.get('runtime_mounts') != []
                or info.get('mcp_mode') != 'static' or info.get('mcp_servers') != []
                or info.get('published_ports')):
            raise SandboxError('sbx_workspace_unsafe')
        if info.get('auth_mode') != profile.auth_mode:
            raise SandboxError('sbx_oauth_required')
        await self._check_policy(scoped=True)

    async def prepare(self, task: TaskContext) -> Workspace:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', task.repo_full_name) or any(
                part in {'.', '..'} for part in task.repo_full_name.split('/')):
            raise SandboxError('invalid_repository')
        self._attach(task)
        profile = self.profile.sbx
        assert profile is not None
        try:
            await self._check_secrets()
            await self._check_policy(scoped=False)
            await self._checked('create', '--name', self.name, '--skills', 'off', '--static-mcp', 'none',
                '--deny-network', ','.join(PRIVATE_NETWORKS), '--no-progress', '--kit', profile.kit,
                profile.workload, error='sbx_create_failed', timeout=600)
            self.vm_id = (await self._inspect()).get('id') or ''
            if not self.vm_id:
                raise SandboxError('sbx_workspace_unsafe')
            await self._checked('policy', 'allow', 'network', '--sandbox', self.name, ','.join(PACKAGE_DOMAINS),
                error='sbx_network_policy_unavailable')
            await self._verify_workspace()
            task.repo_dir, task.context_dir = REPO, CONTEXT
            source = f'https://github.com/{task.repo_full_name}.git'
            await self._git('clone', '--filter=blob:none', '--', source, REPO, cwd=str(ROOT), timeout=300)
            task.default_branch = (await self._git('symbolic-ref', '--short', 'HEAD')).strip()
            task.base_sha = (await self._git('rev-parse', 'HEAD')).strip()
            if not task.default_branch or not re.fullmatch(r'[0-9a-f]{40,64}', task.base_sha):
                raise SandboxError('base_commit_unavailable')
            task.work_branch = f'contribos/{task.task_id}'
            await self._git('switch', '-c', task.work_branch)
            await self.write_task_context(task)
            task.sandbox_ref = self.vm_id
            return Workspace(REPO, CONTEXT, task.default_branch, task.base_sha, task.work_branch, self.vm_id)
        except BaseException:
            try:
                with suppress(SandboxError, OSError):
                    await self.destroy()
            finally:
                self.release_runtime()
            raise

    async def restore(self, task: TaskContext) -> None:
        self._attach(task)
        self.vm_id = task.sandbox_ref or ''
        try:
            await self._check_secrets()
            await self._verify_workspace()
            await self.stop()
            task.repo_dir, task.context_dir = REPO, CONTEXT
        except BaseException:
            self.release_runtime()
            raise

    def _exec_args(self, argv: list[str], cwd: str, *, interactive: bool = False) -> list[str]:
        target = PurePosixPath(cwd)
        if not self.vm_id or not self.lock:
            raise SandboxError('workspace_not_prepared')
        if not target.is_relative_to(ROOT) or '..' in target.parts:
            raise SandboxError('path_outside_workspace')
        return ['exec', *(['-i'] if interactive else []), '-w', str(target), self.name, '--', 'env', *GUEST_ENV, *argv]

    async def exec(self, argv: list[str], cwd: str | None = None, timeout: float = 120) -> ExecResult:
        if not argv:
            raise SandboxError('empty_command')
        if PurePosixPath(argv[0]).name == 'git':
            argv = [argv[0], *GIT_OPTIONS, *argv[1:]]
        command = ['timeout', '--kill-after=2', str(timeout), *argv]
        code, stdout, stderr = await self._call(*self._exec_args(command, cwd or REPO), timeout=timeout + 15)
        return ExecResult(code, safe_text(stdout.decode(errors='replace')), safe_text(stderr.decode(errors='replace')))

    async def _git(self, *args: str, cwd: str | None = None, timeout: float = 120) -> str:
        result = await self.exec(['git', *args], cwd=cwd, timeout=timeout)
        if result.returncode:
            raise SandboxError('clone_failed' if args[0] == 'clone' else 'workspace_git_failed')
        return result.stdout

    async def write_task_context(self, task: TaskContext) -> None:
        args = self._exec_args(['python3', '-c', WRITE_CONTEXT, CONTEXT], str(ROOT), interactive=True)
        data = {name: safe_text(contents) for name, contents in context_files(task).items()}
        code, _, _ = await self._call(*args, data=json.dumps(data).encode())
        if code:
            raise SandboxError('context_write_failed')

    async def diff(self) -> str:
        return await workspace_diff(self)

    async def spawn_agent(self, profile: AgentProfile) -> AgentProcess:
        if profile != self.profile or not profile.sbx:
            raise SandboxError('sandbox_agent_unavailable')
        await self._check_secrets()
        await self._verify_workspace()
        args = self._exec_args([*(f'{k}={v}' for k, v in profile.sbx.environment.items()), *profile.command], REPO,
                               interactive=True)
        self.agent = await self._spawn(*args, interactive=True, stderr=asyncio.subprocess.DEVNULL)
        return self.agent

    async def export_file(self, path: str) -> Path:
        source = PurePosixPath(path)
        if not source.is_relative_to(ROOT) or '..' in source.parts or source == ROOT:
            raise SandboxError('path_outside_workspace')
        args = self._exec_args(['python3', '-c', EXPORT, str(source), str(ROOT)], str(ROOT))
        process = await self._spawn(*args, stderr=asyncio.subprocess.DEVNULL)
        destination = None
        try:
            # Receive a byte stream, so a guest cannot choose a host filename or extract an archive.
            with tempfile.NamedTemporaryFile(prefix=f'{self.name}-', suffix=f'-{source.name}', delete=False) as file:
                destination = Path(file.name)
                async with asyncio.timeout(120):
                    while chunk := await process.stdout.read(65536):
                        file.write(chunk)
                    if await process.wait():
                        raise SandboxError('export_failed')
            return destination
        except BaseException as error:
            await self._kill(process)
            if destination is not None:
                destination.unlink(missing_ok=True)
            if isinstance(error, (OSError, TimeoutError)):
                raise SandboxError('export_failed') from None
            raise

    async def stop(self) -> None:
        if self.vm_id and self.lock:
            info = await self._inspect()
            if info.get('id') != self.vm_id:
                raise SandboxError('sbx_workspace_unsafe')
            if info.get('state') != 'stopped':
                await self._checked('stop', self.name, error='sbx_stop_failed')
            if self.agent is not None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.agent.wait(), 2)
                if self.agent.returncode is None:
                    with suppress(ProcessLookupError):
                        self.agent.kill()
                    await self.agent.wait()
                self.agent = None

    async def destroy(self) -> None:
        if self.vm_id and self.lock:
            await self.stop()
            await self._checked('rm', '--force', self.name, error='sbx_destroy_failed')
            self.vm_id = ''
        self.release_runtime()
