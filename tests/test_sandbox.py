import asyncio
import json
import secrets
import sys
from pathlib import Path

import pytest

from contribos.agent.engine import TaskContext
from contribos.agent.profiles import PROFILES
from contribos.config import Settings
from contribos.sandbox.base import Sandbox, SandboxError
from contribos.sandbox.sbx import CONTEXT, PACKAGE_DOMAINS, PRIVATE_NETWORKS, REPO, ROOT, SbxSandbox, normalize_egress_domains


class FakeSbx(SbxSandbox):
    """Model the external sbx CLI behind the production Sandbox interface."""

    def __init__(self, home: Path, **kwargs):
        super().__init__(Settings(home=home), PROFILES['codex'], **kwargs)
        self.exists = False
        self.state = 'stopped'
        self.context: dict[str, str] = {}
        self.received: list[tuple[str, ...]] = []
        self.extra_rules: list[dict] = []
        self.scoped_rules: list[dict] = []
        self.proxy = {'proxy.sandbox': 'direct'}
        self.configured_service = 'openai'
        self.mounts: list[str] = []
        self.writable_layer = 0
        self.volume_bytes = 0

    async def _call(self, *args, timeout=60, data=None):
        self.received.append(args)
        match args:
            case ('ls', '-q'):
                return 0, (self.name if self.exists else '').encode(), b''
            case ('df', '--verbose', '--json'):
                result = {'containers': [{'name': self.name, 'local_volumes': 1, 'size': self.writable_layer}],
                          'volumes': [{'name': self.name + '-docker', 'size': self.volume_bytes}]}
            case ('secret', 'ls', *rest):
                result = {'secrets': [{'name': self.configured_service, 'type': 'service',
                                       'secret': '(oauth configured)'}], 'custom_secrets': []}
            case ('settings', 'get', 'ssh.agentSocketPath'):
                return 0, b'\n', b''
            case ('settings', 'get', key):
                return 0, self.proxy.get(key, '').encode(), b''
            case ('policy', 'ls', *rest):
                result = {'rules': [{'id': 'default-deny-all', 'scope': 'global', 'status': 'active',
                                     'resource_type': 'network', 'resources': ['**'], 'decision': 'deny', 'editable': False,
                                     'actions': ['net:connect:tcp', 'net:connect:udp']}] + self.extra_rules + self.scoped_rules}
            case ('policy', 'allow', 'network', '--sandbox', name, domains):
                assert name == self.name
                self.scoped_rules.extend({'id': domain, 'status': 'active', 'scope': 'sandbox:' + name,
                    'resource_type': 'network', 'decision': 'allow', 'resources': [domain],
                    'actions': ['net:connect:tcp'], 'editable': True} for domain in domains.split(','))
                return 0, b'', b''
            case ('policy', 'rm', 'network', '--sandbox', name, '--id', rule_id, '--force'):
                assert name == self.name
                self.scoped_rules = [r for r in self.scoped_rules if r.get('id') != rule_id]
                return 0, b'', b''
            case ('create', *rest):
                assert args[-1] == self.profile.sbx.workload
                assert '--mount' not in args and '--static-mcp' in args and '--skills' in args
                self.scoped_rules.append({'scope': 'sandbox:' + self.name, 'status': 'active', 'decision': 'deny',
                    'resource_type': 'network', 'resources': args[args.index('--deny-network') + 1].split(','),
                    'actions': ['net:connect:tcp', 'net:connect:udp']})
                self.exists = True
                return 0, b'', b''
            case ('inspect', *rest):
                if not self.exists:
                    return 1, b'', b''
                result = {'id': 'fake-vm', 'agent': self.profile.sbx_agent, 'state': self.state,
                          'kits': [self.profile.sbx.workload, self.profile.sbx.kit], 'effective_working_dir': str(ROOT),
                          'host_mounts': self.mounts, 'runtime_mounts': [], 'mcp_mode': 'static', 'mcp_servers': [],
                          'auth_mode': self.profile.sbx.auth_mode}
            case ('exec', *rest):
                self.state = 'running'
                if data is not None:
                    assert '-i' in args
                    self.context.update(json.loads(data))
                if 'git' in args:
                    assert 'core.hooksPath=/dev/null' in args and 'core.fsmonitor=false' in args
                if 'clone' in args:
                    assert args[-2:] == ('https://github.com/owner/repo.git', REPO)
                for operation, response in (
                    ('symbolic-ref', b'main\n'), ('rev-parse', b'a' * 40 + b'\n'),
                    ('diff', b'diff --git a/file b/file\n'), ('cat', b'guest-data\n'),
                ):
                    if operation in args:
                        return 0, response, b''
                return 0, b'', b''
            case ('stop', *rest):
                self.state = 'stopped'
                return 0, b'', b''
            case ('rm', '--force', name):
                assert name == self.name
                self.exists = False
                return 0, b'', b''
            case _:
                raise AssertionError(args)
        return 0, json.dumps(result).encode(), b''

    async def _spawn(self, *args, interactive=False, stderr=asyncio.subprocess.PIPE):
        self.received.append(args)
        assert args[0] == 'exec'
        if interactive:
            assert '-i' in args and '-t' not in args
            assert args[args.index('GH_TOKEN') - 1] == '-u'
            assert args[args.index('SSH_AUTH_SOCK') - 1] == '-u'
            code = 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'
        else:
            code = 'import sys; sys.stdout.buffer.write(bytes([0, 255, 10, 13]))'
        return await asyncio.create_subprocess_exec(sys.executable, '-c', code,
            stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=stderr)


def sbx_task() -> TaskContext:
    return TaskContext('test-sandbox', 'owner/repo', 1, '', '', isolation='sbx',
                       issue={'title': 'Example', 'body': 'Snapshot'}, instructions='Initial instructions')


async def sandbox_contract(sandbox: Sandbox, task: TaskContext) -> None:
    workspace = await sandbox.prepare(task)
    assert workspace.reference and workspace.default_branch and workspace.base_sha and workspace.work_branch
    assert workspace.repo_dir != workspace.context_dir
    task.sandbox_ref = workspace.reference
    await sandbox.write_task_context(task)
    assert (await sandbox.exec(['cat', 'example'])).stdout == 'guest-data\n'
    agent = await sandbox.spawn_agent(PROFILES['codex'])
    assert (await agent.communicate(b'{"id":1}\n'))[0] == b'{"id":1}\n'
    assert 'diff --git' in await sandbox.diff()
    artifact = await sandbox.export_file(workspace.repo_dir + '/example.bundle')
    try:
        assert artifact.read_bytes() == bytes([0, 255, 10, 13])
    finally:
        artifact.unlink()
    await sandbox.stop()
    await sandbox.restore(task)
    assert (await sandbox.exec(['cat', 'example'])).stdout == 'guest-data\n'
    await sandbox.destroy()


def test_sandbox_contract(tmp_path):
    sandbox = FakeSbx(tmp_path)
    task = sbx_task()
    asyncio.run(sandbox_contract(sandbox, task))
    assert set(sandbox.context) == {'TASK.md', 'issue.md', 'linked_prs.json'}
    assert not sandbox.exists and sandbox.lock is None
    assert not list(tmp_path.glob('workspaces/*'))


def test_real_sbx_launch_excludes_host_github_credentials(tmp_path, monkeypatch):
    canary = secrets.token_urlsafe(32)
    for key in ('CONTRIBOS_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK'):
        monkeypatch.setenv(key, canary)

    async def spawn(*argv, env, **kwargs):
        assert argv[:3] == ('/path/to/sbx', '--no-input', 'exec')
        assert all(canary not in value for value in (*argv, *env.values()))
        assert argv[argv.index('CONTRIBOS_GITHUB_TOKEN') - 1] == '-u'

    class CheckedSbx(FakeSbx):
        _spawn = SbxSandbox._spawn

    async def check():
        monkeypatch.setattr(Settings, 'tool', lambda self, name: '/path/to/sbx')
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        sandbox = CheckedSbx(tmp_path)
        await sandbox.prepare(sbx_task())
        try:
            await sandbox.spawn_agent(PROFILES['codex'])
        finally:
            await sandbox.destroy()

    asyncio.run(check())


@pytest.mark.parametrize('unsafe, error', [
    ('vault', 'sbx_credential_policy_unsafe'),
    ('egress', 'sbx_network_policy_unsafe'),
    ('mount', 'sbx_workspace_unsafe'),
])
def test_sbx_refuses_unsafe_configuration(tmp_path, unsafe, error):
    sandbox = FakeSbx(tmp_path)
    if unsafe == 'vault':
        sandbox.configured_service = 'github'
    if unsafe == 'egress':
        sandbox.extra_rules.append({'scope': 'global', 'status': 'active', 'resource_type': 'network',
                                    'decision': 'allow', 'resources': ['**'], 'actions': ['net:connect:tcp']})
    if unsafe == 'mount':
        sandbox.mounts.append('/unexpected')
    with pytest.raises(SandboxError, match=error):
        asyncio.run(sandbox.prepare(sbx_task()))
    assert not any(call[0] == 'exec' for call in sandbox.received)
    assert not sandbox.exists and sandbox.lock is None


def test_sbx_paths_and_replaced_workspace_are_rejected(tmp_path):
    async def check():
        sandbox = FakeSbx(tmp_path)
        task = sbx_task()
        await sandbox.prepare(task)
        for path in ('/etc/hostname', CONTEXT + '/../../escape'):
            with pytest.raises(SandboxError, match='path_outside_workspace'):
                await sandbox.export_file(path)
            with pytest.raises(SandboxError, match='path_outside_workspace'):
                await sandbox.exec(['true'], cwd=path)
        task.sandbox_ref = 'another-vm'
        with pytest.raises(SandboxError, match='sbx_workspace_unsafe'):
            await sandbox.restore(task)
        assert not any(call[0] == 'rm' for call in sandbox.received)
    asyncio.run(check())


@pytest.mark.parametrize('domain', [
    '**', '*.packages.example.org', '-example.org', 'example.org:80', 'https://example.org',
    'example.org/path', 'a,b.example.org', '127.0.0.1', '169.254.169.254', '[::1]', '127.0.0.0/8',
    'localhost', 'store.localhost', 'host.docker.internal', 'packages.local', 'service.home.arpa',
])
def test_egress_accepts_only_exact_public_hostnames(domain):
    with pytest.raises(ValueError):
        normalize_egress_domains([domain])


def test_sbx_egress_scoped_to_task_and_reconciled_before_restore(tmp_path):
    async def check():
        task = sbx_task()
        sandbox = FakeSbx(tmp_path, extra_domains=['Packages.Example.org', 'packages.example.org'])
        await sandbox.create_empty(task)
        task.sandbox_ref = sandbox.vm_id
        approved = {r['resources'][0] for r in sandbox.scoped_rules if r['decision'] == 'allow'}
        assert approved == set(PACKAGE_DOMAINS + sandbox.profile.sbx.domains + ('packages.example.org:443',))
        assert any(set(r['resources']) == set(PRIVATE_NETWORKS) for r in sandbox.scoped_rules)
        assert not any(call[0] == 'exec' for call in sandbox.received)
        sandbox.release_runtime()
        resumed = FakeSbx(tmp_path, extra_domains=['downloads.example.org'])
        resumed.exists, resumed.scoped_rules = True, sandbox.scoped_rules.copy()
        await resumed.restore(task)
        approved = {r['resources'][0] for r in resumed.scoped_rules if r['decision'] == 'allow'}
        assert 'downloads.example.org:443' in approved and 'packages.example.org:443' not in approved
        assert not any(call[0] == 'exec' for call in resumed.received)
        await resumed.destroy()

    asyncio.run(check())


def test_sbx_restore_refuses_drift_in_private_network_policy(tmp_path):
    async def check():
        sandbox = FakeSbx(tmp_path)
        task = sbx_task()
        await sandbox.prepare(task)
        deny = next(r for r in sandbox.scoped_rules if r['decision'] == 'deny')
        deny['resources'].remove('169.254.0.0/16')
        sandbox.received.clear()
        with pytest.raises(SandboxError, match='sbx_private_network_policy_missing'):
            await sandbox.restore(task)
        assert not any(call[0] == 'exec' for call in sandbox.received)
        assert sandbox.state == 'stopped' and sandbox.lock is None

    asyncio.run(check())


@pytest.mark.parametrize('bypass', ['packages.example.org', '*.example.org'])
def test_custom_domains_require_private_address_checks_on_actual_route(tmp_path, bypass):
    async def check():
        sandbox = FakeSbx(tmp_path, extra_domains=['packages.example.org'])
        sandbox.proxy = {'proxy': 'system'}
        with pytest.raises(SandboxError, match='sbx_custom_domains_require_direct_route'):
            await sandbox.create_empty(sbx_task())
        assert not any(call[0] == 'create' for call in sandbox.received)
        sandbox.proxy['no_proxy.sandbox'] = bypass
        await sandbox.create_empty(sbx_task())
        await sandbox.destroy()

    asyncio.run(check())


@pytest.mark.parametrize('rule', [
    {'resource_type': 'unexpected'},
    {'actions': ['net:connect:tcp', 'net:connect:udp']},
    {'scope': 'organization', 'resources': ['unlisted.example.org:443']},
    {'scope': 'sandbox:opaque-vm-id', 'resources': ['**']},
])
def test_unsafe_effective_permissions_prevent_guest_execution(tmp_path, rule):
    sandbox = FakeSbx(tmp_path)
    sandbox.extra_rules = [{'status': 'active', 'scope': 'global', 'resource_type': 'network',
                            'decision': 'allow', 'resources': ['github.com:443'], 'actions': ['net:connect:tcp'],
                            **rule}]
    with pytest.raises(SandboxError, match='sbx_network_policy_unsafe'):
        asyncio.run(sandbox.prepare(sbx_task()))
    assert not any(call[0] == 'exec' for call in sandbox.received)


@pytest.mark.parametrize('reported', [
    {}, {'secrets': [], 'custom_secrets': [{}]},
    {'secrets': [], 'custom_secrets': [], 'env_only_count': 1},
    {'secrets': [], 'custom_secrets': [], 'shadowed_services': ['github']},
])
def test_sandbox_scoped_credential_report_fails_closed_before_creation(tmp_path, reported):
    class CredentialScope(FakeSbx):
        async def _json(self, *args, error):
            if args[:3] == ('secret', 'ls', '--sandbox'):
                return reported
            return await super()._json(*args, error=error)

    sandbox = CredentialScope(tmp_path)
    with pytest.raises(SandboxError, match='sbx_credential_policy_unsafe'):
        asyncio.run(sandbox.prepare(sbx_task()))
    assert not any(call[0] in {'create', 'exec'} for call in sandbox.received)
