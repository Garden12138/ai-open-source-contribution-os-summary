"""Local Docker Sandboxes backend. Workspace paths are always guest paths."""

import asyncio
import fcntl
import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from contribos.agent.engine import AgentError, TaskContext
from contribos.agent.profiles import AgentProfile, provider_key
from contribos.config import OpenModel, Settings
from contribos.sandbox.base import AgentProcess, ExecResult, SandboxError, Workspace, context_files, workspace_diff
from contribos.sandbox.host import GIT_OPTIONS, clean_environment, safe_text
from contribos.sandbox.resources import ResourceLimits, writable_bytes

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
    'CONTRIBOS_GITHUB_TOKEN',
    'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'MCP_GATEWAY_URL', 'MCP_SENTINEL_TOKEN_NAME',
)
GUEST_ENV = [v for key in CREDENTIAL_VARS for v in ('-u', key)] + [
    f'{k}={v}' for k, v in clean_environment({}, Path(str(ROOT / 'runtime'))).items()
    if k.startswith(('GH_', 'GIT_', 'SSH_'))
]
WRITE_CONTEXT = '''
import json, pathlib, sys
directory = pathlib.Path(sys.argv[1])
directory.mkdir(mode=0o700, exist_ok=True, parents=True)
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


def normalize_egress_domains(domains: Sequence[str]) -> list[str]:
    """Return exact public DNS names suitable for a TCP/443 sandbox rule."""
    if not isinstance(domains, (tuple, list)) or len(domains) > 100:
        raise ValueError('Provide up to 100 additional domains.')
    result = []
    for value in domains:
        domain = value.strip().lower() if isinstance(value, str) and value.isascii() else ''
        labels = domain.split('.')
        if (len(domain) > 253 or len(labels) < 2
                or not re.fullmatch(r'[a-z]{2,63}|xn--[a-z0-9-]+', labels[-1])
                or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
                or labels[-1] in {'localhost', 'local', 'localdomain', 'internal', 'lan', 'home', 'arpa'}):
            raise ValueError('Provide an exact public DNS hostname without a scheme, wildcard, port or path.')
        if domain not in result:
            result.append(domain)
    return result


class SbxSandbox:
    kind = 'sbx'
    disk_check_interval = 15

    def __init__(self, settings: Settings, profile: AgentProfile, *, extra_domains: Sequence[str] = ()) -> None:
        self.settings = settings
        self.profile = profile
        self.model = OpenModel.model_validate(settings.open_model) if profile.open_model else None
        self.extra_domains = tuple(normalize_egress_domains(extra_domains))
        self.read_extra_domains: Callable[[], Sequence[str]] = lambda: extra_domains
        self.name = ''
        self.vm_id = ''
        self.lock: BinaryIO | None = None
        self.agent: AgentProcess | None = None
        self.resource_limits: Callable[[], ResourceLimits] = ResourceLimits
        self.on_resource_pause: Callable[[str], None] = lambda _: None
        self.disk_error: str | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._measure_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()

    @property
    def _xcli_host(self) -> str | None:
        if self.model and self.model.provider == 'xcli':
            return f'localhost:{self.model.xcli_port}'
        return None

    def _private_denies(self) -> tuple[str, ...]:
        if not self._xcli_host:
            return PRIVATE_NETWORKS
        # sbx maps host.docker.internal to localhost. Keep explicit IP denies on
        # every package/extra domain port, including DNS resolutions to 127.0.0.1.
        rest = ipaddress.ip_network('127.0.0.0/8').address_exclude(ipaddress.ip_network('127.0.0.1/32'))
        return (tuple(v for v in PRIVATE_NETWORKS if v not in {'localhost', 'host.docker.internal', '127.0.0.0/8'})
                + tuple(str(network) for network in rest) + ('127.0.0.1:80', '127.0.0.1:443'))

    def _secret_host(self) -> str | None:
        if self._xcli_host:
            return 'localhost' if os.environ.get('CONTRIBOS_XCLI_API_KEY') else None
        return 'api.deepseek.com' if self.model else None

    def _configured_domains(self) -> tuple[str, ...]:
        try:
            return tuple(normalize_egress_domains(self.read_extra_domains()))
        except ValueError:
            raise SandboxError('invalid_egress_domains') from None

    def network_policy_changed(self) -> bool:
        return set(self._configured_domains()) != set(self.extra_domains)

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

    async def _spawn(self, *args: str, interactive: bool = False, stdout: int = asyncio.subprocess.PIPE,
                     stderr: int = asyncio.subprocess.PIPE) -> AgentProcess:
        executable = self.settings.tool('sbx')
        if not executable:
            raise SandboxError('sbx_unavailable')
        # sbx can start its daemon. Let it use the OS proxy and native credential store.
        environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR',
                       'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR') if k in os.environ}
        try:
            return await asyncio.create_subprocess_exec(executable, '--no-input', *args, env=environment,
                stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=True, limit=50 * 1024 * 1024)
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
            custom = secrets.get('custom_secrets')
            # Static values are masked in the metadata. Dynamic sources include a source descriptor.
            key = provider_key(self.model) if self._secret_host() else ''
            allowed_custom = (self.vm_id and self._secret_host() and scope == ('--sandbox', self.name)
                              and isinstance(custom, list) and len(custom) == 1 and isinstance(custom[0], dict)
                              and set(custom[0]) == {'scope', 'targets', 'env', 'placeholder', 'secret'}
                              and custom[0].get('scope') == self.name and custom[0].get('targets') == [self._secret_host()]
                              and custom[0].get('env') == 'CONTRIBOS_PROVIDER_KEY'
                              and custom[0].get('secret') == f'{key[:6]}******...******{key[-4:]}'
                              and custom[0].get('placeholder') == 'contribos-model-proxy')
            if (not isinstance(secrets.get('secrets'), list) or (custom != [] and not allowed_custom)
                    or secrets.get('env_only_count', 0) or secrets.get('shadowed_services')
                    or any(not isinstance(s, dict) or s.get('type') != 'service' or s.get('name') != profile.service
                           or s.get('secret') != '(oauth configured)' for s in secrets['secrets'])):
                raise SandboxError('sbx_credential_policy_unsafe')
        socket_path = await self._checked('settings', 'get', 'ssh.agentSocketPath', error='sbx_credentials_unavailable')
        if socket_path.strip():
            raise SandboxError('sbx_credential_policy_unsafe')

    async def _check_extra_routes(self) -> None:
        if not self.extra_domains:
            return

        async def setting(name: str) -> str:
            raw = await self._checked('settings', 'get', name, error='sbx_network_policy_unavailable')
            return raw.decode(errors='replace').strip()

        # An upstream proxy that resolves a custom domain bypasses sbx's destination CIDR check.
        if (await setting('proxy.sandbox') or await setting('proxy')) == 'direct':
            return
        bypass = await setting('no_proxy.sandbox') or await setting('no_proxy')
        hosts = {host.strip().lower() for host in bypass.split(',')}
        suffixes = tuple(host[1:] for host in hosts if host.startswith('*.'))
        if '*' not in hosts and any(domain not in hosts and not domain.endswith(suffixes)
                                    for domain in self.extra_domains):
            raise SandboxError('sbx_custom_domains_require_direct_route')

    async def _check_policy(self, *, scoped: bool, reconcile: bool = False) -> None:
        profile = self.profile.sbx
        assert profile is not None
        scope = (self.name,) if scoped else ()
        policy = await self._json('policy', 'ls', *scope, '--json', error='sbx_network_policy_unavailable')
        rules = policy.get('rules')
        if not isinstance(rules, list) or not all(isinstance(r, dict) and isinstance(r.get('scope'), str)
                and r.get('status') in ('active', 'inactive') and isinstance(r.get('resource_type'), str)
                and all(isinstance(r.get(key), list) and all(isinstance(value, str) for value in r[key])
                        for key in ('actions', 'resources')) for r in rules):
            raise SandboxError('sbx_network_policy_unavailable')
        approved = set(PACKAGE_DOMAINS + profile.domains) | {f'{domain}:443' for domain in self.extra_domains}
        if self.model and self.model.provider == 'deepseek':
            approved.add('api.deepseek.com:443')
        # A scoped query already returns the effective policy for this VM, including its kit and organization.
        active = [rule for rule in rules if rule['status'] == 'active'
                  and (scoped or not rule['scope'].startswith('sandbox:'))]
        transports = {'net:connect:tcp', 'net:connect:udp'}
        if not any(r.get('id') == 'default-deny-all' and r.get('decision') == 'deny'
                   and r.get('scope') == 'global' and r.get('resource_type') == 'network'
                   and r.get('editable') is False and r.get('resources') == ['**']
                   and set(r.get('actions', [])) == transports for r in active):
            raise SandboxError('sbx_deny_all_required')
        allowed, denied = set(), set()
        obsolete = []
        model_route = False
        for rule in active:
            kind = rule['resource_type']
            if kind in {'filesystem:read', 'filesystem:write'} and all(a.startswith('fs:') for a in rule['actions']):
                continue
            resources = set(rule['resources'])
            actions = set(rule['actions'])
            if rule.get('decision') == 'deny':
                if kind == 'network' and transports <= actions:
                    denied.update(resources)
                continue
            if (kind == 'http' and rule.get('decision') == 'allow' and scoped and self._xcli_host
                    and rule['scope'] == f'sandbox:{self.name}' and rule.get('editable') is True
                    and rule.get('methods') == ['POST'] and actions == {'http:request:post'}
                    and resources == {self._xcli_host} and rule.get('http_targets') == [
                        {'host': self._xcli_host, 'path': '/v1/chat/completions'}]):
                model_route = True
                continue
            if (rule.get('decision') != 'allow' or not resources
                    or kind != 'network' or actions != {'net:connect:tcp'}):
                raise SandboxError('sbx_network_policy_unsafe')
            if resources - approved:
                # Remove retired hostname entries only from this task's editable rules.
                if not (reconcile and rule.get('editable') is True and rule.get('id')
                        and rule.get('scope') == f'sandbox:{self.name}' and len(resources) == 1):
                    raise SandboxError('sbx_network_policy_unsafe')
                host, _, port = next(iter(resources)).rpartition(':')
                try:
                    normalize_egress_domains([host])
                except ValueError:
                    raise SandboxError('sbx_network_policy_unsafe') from None
                if port != '443':
                    raise SandboxError('sbx_network_policy_unsafe')
                obsolete.append(rule['id'])
            else:
                allowed.update(resources)
        if not scoped:
            return
        if set(self._private_denies()) - denied:
            raise SandboxError('sbx_private_network_policy_missing')
        missing = approved - allowed
        missing_model = bool(self._xcli_host and not model_route)
        if reconcile and (obsolete or missing or missing_model):
            for rule_id in obsolete:
                await self._checked('policy', 'rm', 'network', '--sandbox', self.name, '--id', rule_id, '--force',
                                    error='sbx_network_policy_unavailable')
            if missing:
                await self._checked('policy', 'allow', 'network', '--sandbox', self.name, ','.join(sorted(missing)),
                                    error='sbx_network_policy_unavailable')
            if missing_model:
                await self._checked('policy', 'allow', 'network', '--sandbox', self.name, self._xcli_host,
                                    '--method', 'POST', '--path', '/v1/chat/completions',
                                    error='sbx_network_policy_unavailable')
            await self._check_policy(scoped=True)
        elif missing or missing_model:
            raise SandboxError('sbx_network_policy_incomplete')

    async def _inspect(self) -> dict[str, Any]:
        return await self._json('inspect', self.name, '--json', error='sbx_workspace_missing')

    async def _verify_workspace(self) -> None:
        profile = self.profile.sbx
        assert profile is not None
        info = await self._inspect()
        if (info.get('id') != self.vm_id or info.get('agent') != self.profile.sbx_agent
                or info.get('kits') != [item for item in (profile.workload, profile.kit) if item]
                or info.get('effective_working_dir') != str(ROOT)
                or info.get('host_mounts') != [] or info.get('runtime_mounts') != []
                or info.get('mcp_mode') != 'static' or info.get('mcp_servers') != []
                or info.get('published_ports')):
            raise SandboxError('sbx_workspace_unsafe')
        if info.get('auth_mode') != profile.auth_mode:
            raise SandboxError('sbx_oauth_required')

    async def create_empty(self, task: TaskContext) -> None:
        self.extra_domains = self._configured_domains()
        self._attach(task)
        profile = self.profile.sbx
        assert profile is not None
        try:
            if self.model:
                if not self.model.model or (self._xcli_host and self.model.xcli_port == self.settings.port):
                    raise SandboxError('model_not_configured')
                try:
                    provider_key(self.model)
                except AgentError as error:
                    raise SandboxError(error.code) from None
            await self._check_secrets()
            await self._check_extra_routes()
            await self._check_policy(scoped=False)
            limits = self.resource_limits()
            await self._checked('create', '--cpus', str(limits.cpus), '--memory', f'{limits.memory_mib}m',
                '--name', self.name, '--skills', 'off', '--static-mcp', 'none',
                '--deny-network', ','.join(self._private_denies()), '--no-progress', *(['--kit', profile.kit] if profile.kit else []),
                profile.workload, error='sbx_create_failed', timeout=600)
            self.vm_id = (await self._inspect()).get('id') or ''
            if not self.vm_id:
                raise SandboxError('sbx_workspace_unsafe')
            await self._verify_workspace()
            await self._check_policy(scoped=True, reconcile=True)
            if profile.install:
                result = await self.exec(list(profile.install), cwd=str(ROOT), timeout=600)
                if result.returncode:
                    raise SandboxError('sandbox_agent_install_failed')
        except BaseException:
            await self._discard_created()
            raise

    async def _discard_created(self) -> None:
        try:
            with suppress(SandboxError, OSError):
                await self.destroy()
        finally:
            self.release_runtime()

    async def discard_preparation(self, task: TaskContext) -> None:
        if task.sandbox_ref:
            # An interrupted initial clone has no usable baseline. Only remove the VM recorded for that attempt.
            self._attach(task)
            if self.name in (await self._checked('ls', '-q', error='sbx_workspace_unavailable')).decode().splitlines():
                self.vm_id = task.sandbox_ref
                await self.destroy()
            task.sandbox_ref = None

    async def prepare(self, task: TaskContext) -> Workspace:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', task.repo_full_name) or any(
                part in {'.', '..'} for part in task.repo_full_name.split('/')):
            raise SandboxError('invalid_repository')
        await self.create_empty(task)
        try:
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
            if self.disk_error:
                await self.stop()
            else:
                await self._discard_created()
            raise

    async def restore(self, task: TaskContext) -> None:
        self._attach(task)
        self.vm_id = task.sandbox_ref or ''
        try:
            await self._verify_workspace()
            await self.stop()
            self.extra_domains = self._configured_domains()
            await self._check_secrets()
            await self._check_extra_routes()
            await self._check_policy(scoped=True, reconcile=True)
            task.repo_dir, task.context_dir = REPO, CONTEXT
        except BaseException:
            self.release_runtime()
            raise

    async def stop_saved(self, task: TaskContext) -> None:
        self._attach(task)
        try:
            self.vm_id = task.sandbox_ref or ''
            await self.stop()
        finally:
            self.release_runtime()

    async def _check_disk(self) -> None:
        try:
            report = await self._json('df', '--verbose', '--json', error='sandbox_disk_unavailable')
            used = writable_bytes(report, self.name)
        except (SandboxError, OSError):
            raise SandboxError('sandbox_disk_unavailable') from None
        if used > self.resource_limits().disk_mib * 1024**2:
            raise SandboxError('sandbox_disk_limit')

    async def _watch_disk(self) -> None:
        while not self.disk_error:
            await asyncio.sleep(self.disk_check_interval)
            try:
                await self._check_disk()
            except SandboxError as error:
                self.disk_error = str(error)
                self.on_resource_pause(self.disk_error)
        while self.vm_id and self.lock:
            try:
                await self.stop()
                return
            except (OSError, SandboxError):
                await asyncio.sleep(5)

    async def check_resources(self) -> None:
        async with self._measure_lock:
            if self.disk_error:
                raise SandboxError(self.disk_error)
            if self._monitor is None or self._monitor.done():
                try:
                    await self._check_disk()
                except SandboxError as error:
                    self.disk_error = str(error)
                    self.on_resource_pause(self.disk_error)
                    self._monitor = asyncio.create_task(self._watch_disk())
                    raise
                self._monitor = asyncio.create_task(self._watch_disk())

    @property
    def stopping_for_disk(self) -> bool:
        return bool(self.disk_error and self._monitor and not self._monitor.done())

    async def _exec_args(self, argv: list[str], cwd: str, *, interactive: bool = False) -> list[str]:
        target = PurePosixPath(cwd)
        if not self.vm_id or not self.lock:
            raise SandboxError('workspace_not_prepared')
        if not target.is_relative_to(ROOT) or '..' in target.parts:
            raise SandboxError('path_outside_workspace')
        await self.check_resources()
        return ['exec', *(['-i'] if interactive else []), '-w', str(target), self.name, '--', 'env', *GUEST_ENV, *argv]

    async def exec(self, argv: list[str], cwd: str | None = None, timeout: float = 120) -> ExecResult:
        if not argv:
            raise SandboxError('empty_command')
        if PurePosixPath(argv[0]).name == 'git':
            argv = [argv[0], *GIT_OPTIONS, *argv[1:]]
        command = ['timeout', '--kill-after=2', str(timeout), *argv]
        args = await self._exec_args(command, cwd or REPO)
        code, stdout, stderr = await self._call(*args, timeout=timeout + 15)
        return ExecResult(code, safe_text(stdout.decode(errors='replace')), safe_text(stderr.decode(errors='replace')))

    async def _git(self, *args: str, cwd: str | None = None, timeout: float = 120) -> str:
        result = await self.exec(['git', *args], cwd=cwd, timeout=timeout)
        if result.returncode:
            raise SandboxError('clone_failed' if args[0] == 'clone' else 'workspace_git_failed')
        return result.stdout

    async def write_task_context(self, task: TaskContext) -> None:
        args = await self._exec_args(['python3', '-c', WRITE_CONTEXT, CONTEXT], str(ROOT), interactive=True)
        data = {name: safe_text(contents) for name, contents in context_files(task).items()}
        code, _, _ = await self._call(*args, data=json.dumps(data).encode())
        if code:
            raise SandboxError('context_write_failed')

    async def diff(self) -> str:
        return await workspace_diff(self)

    async def probe_credentials(self) -> str:
        profile = self.profile.sbx
        if not profile or not profile.credential_probe:
            return 'unavailable'
        if not self.vm_id or not self.lock:
            raise SandboxError('workspace_not_prepared')
        await self._verify_workspace()
        await self._check_secrets()
        await self._check_extra_routes()
        await self._check_policy(scoped=True)
        command = ['python3', '-I', '-S', '-c', profile.credential_probe]
        for mode in ('raw', 'agent'):
            if mode == 'raw':
                await self.check_resources()
                args = ['exec', '-w', str(ROOT), self.name, '--', *command, mode, self.name]
            else:
                args = await self._exec_args([*(f'{k}={v}' for k, v in profile.environment.items()),
                                              *command, mode, self.name], str(ROOT))
            process = await self._spawn(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                if await asyncio.wait_for(process.wait(), 60):
                    return 'error'
            except (TimeoutError, asyncio.CancelledError):
                await self._kill(process)
                raise
        return 'ok'

    async def spawn_agent(self, profile: AgentProfile) -> AgentProcess:
        if profile != self.profile or not profile.sbx:
            raise SandboxError('sandbox_agent_unavailable')
        await self._check_secrets()
        await self._verify_workspace()
        await self._check_extra_routes()
        await self._check_policy(scoped=True)
        environment = dict(profile.sbx.environment)
        if self.model:
            if self._secret_host():
                # sbx evaluates the host alias as localhost before substituting the placeholder.
                code, _, _ = await self._call('secret', 'set-custom', '--sandbox', self.name, '--host', self._secret_host(),
                                              '--env', 'CONTRIBOS_PROVIDER_KEY', '--placeholder', 'contribos-model-proxy',
                                              '--value-stdin', data=provider_key(self.model).encode())
                if code:
                    raise SandboxError('model_credentials_unavailable')
                await self._check_secrets()
            directory = str(ROOT / 'runtime' / profile.id)
            model_env, files = profile.model_environment(self.model, directory, guest=True)
            environment.update(profile.isolated_env(directory), **model_env)
            if files:
                write = await self._exec_args(['python3', '-I', '-c', WRITE_CONTEXT, directory], str(ROOT), interactive=True)
                code, _, _ = await self._call(*write, data=json.dumps(files).encode())
                if code:
                    raise SandboxError('model_config_write_failed')
        args = await self._exec_args([*(f'{k}={v}' for k, v in environment.items()), *(profile.sbx.command or profile.command)],
                                     REPO, interactive=True)
        self.agent = await self._spawn(*args, interactive=True, stderr=asyncio.subprocess.DEVNULL)
        return self.agent

    async def export_file(self, path: str) -> Path:
        source = PurePosixPath(path)
        if not source.is_relative_to(ROOT) or '..' in source.parts or source == ROOT:
            raise SandboxError('path_outside_workspace')
        args = await self._exec_args(['python3', '-c', EXPORT, str(source), str(ROOT)], str(ROOT))
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
        async with self._stop_lock:
            await self._stop_vm()
        if self._monitor and self._monitor is not asyncio.current_task():
            self._monitor.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor

    async def _stop_vm(self) -> None:
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
