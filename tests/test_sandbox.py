import asyncio
import json
import sys
from pathlib import Path

import pytest

from contribos.agent.engine import TaskContext
from contribos.agent.profiles import PROFILES
from contribos.config import Settings
from contribos.sandbox.base import Sandbox, SandboxError
from contribos.sandbox.sbx import CONTEXT, REPO, ROOT, SbxSandbox


class FakeSbx(SbxSandbox):
    """Model the external sbx CLI behind the production Sandbox interface."""

    def __init__(self, home: Path):
        super().__init__(Settings(home=home), PROFILES['codex'])
        self.exists = False
        self.state = 'stopped'
        self.context: dict[str, str] = {}
        self.received: list[tuple[str, ...]] = []
        self.extra_rules: list[dict] = []
        self.configured_service = 'openai'
        self.mounts: list[str] = []

    async def _call(self, *args, timeout=60, data=None):
        self.received.append(args)
        match args:
            case ('secret', 'ls', *rest):
                result = {'secrets': [{'name': self.configured_service, 'type': 'service',
                                       'secret': '(oauth configured)'}], 'custom_secrets': []}
            case ('settings', 'get', 'ssh.agentSocketPath'):
                return 0, b'\n', b''
            case ('policy', 'ls', *rest):
                result = {'rules': [{'id': 'default-deny-all', 'status': 'active', 'decision': 'deny'}]
                          + self.extra_rules}
            case ('policy', 'allow', *rest):
                assert args[3:5] == ('--sandbox', self.name)
                return 0, b'', b''
            case ('create', *rest):
                assert args[-1] == self.profile.sbx.workload
                assert '--mount' not in args and '--static-mcp' in args and '--skills' in args
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
        sandbox.extra_rules.append({'status': 'active', 'resource_type': 'network',
                                    'decision': 'allow', 'resources': ['**']})
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
