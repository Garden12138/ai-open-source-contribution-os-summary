import asyncio
import base64
import os
import secrets
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from contribos.agent.profiles import PROFILES
from contribos.cli import _sandbox_checks
from contribos.config import Settings
from contribos.sandbox import probes
from contribos.sandbox.base import ExecResult, SandboxError
from contribos.sandbox.sbx import SbxSandbox
from test_sandbox import FakeSbx, sbx_task


def test_fresh_vm_checks_cleanup_and_report(tmp_path, monkeypatch):
    settings = Settings(home=tmp_path)
    destroyed = []
    checks = []

    async def create(vm, task):
        assert task.isolation == 'sbx' and not task.repo_full_name
        assert vm.profile is PROFILES['codex']

    async def run(vm, argv, **kwargs):
        checks.append(argv)
        return ExecResult(0, '', '')

    async def canary(vm, address, *, host_alias):
        return host_alias

    async def credentials(vm):
        checks.append('credentials')
        return 'ok'

    async def destroy(vm):
        destroyed.append(vm)

    monkeypatch.setattr(SbxSandbox, 'create_empty', create)
    monkeypatch.setattr(SbxSandbox, 'exec', run)
    monkeypatch.setattr(SbxSandbox, 'probe_credentials', credentials)
    monkeypatch.setattr(SbxSandbox, 'destroy', destroy)
    monkeypatch.setattr(probes, '_lan_address', lambda: '192.168.1.2')
    monkeypatch.setattr(probes, '_canary', canary)
    assert _sandbox_checks(settings)[0].status == 'not_run'
    report = _sandbox_checks(settings, run=True)
    assert report == _sandbox_checks(settings)
    assert {row.name: row.status for row in report} == {**dict.fromkeys(probes.CHECKS, 'ok'), 'sandbox_lan': 'error'}
    assert len(destroyed) == 1 and checks[-1] == 'credentials'
    assert len(checks) == 3


def test_setup_failure_reports_unrun_checks_and_destroys_vm():
    calls = []
    async def fail(task):
        raise SandboxError('sbx_create_failed')
    async def destroy():
        calls.append(True)
    results = asyncio.run(probes.probe_empty_vm(SimpleNamespace(create_empty=fail, destroy=destroy)))
    assert results.pop('sandbox_policy') == 'error'
    assert set(results.values()) == {'not_run'} and calls == [True]


@pytest.mark.parametrize('source', ['clean', 'raw_env', 'other_env', 'near_hostname', 'gh_file', 'git_file', 'npm_file',
                                     'netrc_file', 'codex_file', 'ssh_file', 'agent_env'])
def test_raw_and_agent_credential_probes_use_exit_status_only(tmp_path, source):
    config = tmp_path / 'codex'
    config.mkdir()
    (config / 'config.toml').write_text('[model_providers.sandboxd]\nexperimental_bearer_token="oai-oat01-proxy-managed"\n')
    (config / 'auth.json').write_text('{"OPENAI_API_KEY":"proxy-managed"}')
    marker = b'-----BEGIN ' + b'PRIVATE KEY-----'
    (config / 'format-notes').write_bytes(b'Key files start with ' + marker)
    gh = tmp_path / 'gh-check'
    gh.mkdir()
    (gh / 'hosts.yml').write_text('github.com:\n  git_protocol: https\n  user: test\n')
    (tmp_path / '.npmrc').write_text('registry=https://registry.npmjs.org/\n//registry.npmjs.org/:_authToken=${NODE_AUTH_TOKEN}\n')
    sentinel = 'gho_' + 'sbxproxymanaged' + '0' * 21
    env = {'PATH': str(Path(sys.executable).parent) + os.pathsep + os.defpath, 'HOME': str(tmp_path),
           'CODEX_HOME': str(config), 'GH_CONFIG_DIR': str(gh), 'GH_TOKEN': sentinel}
    canary = secrets.token_urlsafe(28)
    if source == 'raw_env':
        env['GH_TOKEN'] = canary
    if source == 'other_env':
        env['UNRELATED'] = 'ghp_' + secrets.token_hex(20)
    if source == 'gh_file':
        (gh / 'hosts.yml').write_text('github.com:\n  oauth_token: ' + canary)
    if source == 'git_file':
        (tmp_path / '.git-credentials').write_text('https://test:' + canary + '@example.invalid')
    if source == 'npm_file':
        (tmp_path / '.npmrc').write_text('//registry.npmjs.org/:_authToken=' + canary)
    if source == 'netrc_file':
        (tmp_path / '.netrc').write_text('machine example.invalid login user password ' + canary)
    if source == 'codex_file':
        (config / 'session.jsonl').write_text('{"data":"github_pat_' + secrets.token_hex(20) + '"}')
    if source == 'ssh_file':
        (tmp_path / '.ssh').mkdir()
        (tmp_path / '.ssh/id_ed25519').write_bytes(
            marker + b'\n' + base64.b64encode(secrets.token_bytes(48)) + b'\n' + marker.replace(b'BEGIN', b'END'))

    modes = []
    class ProbeVM(FakeSbx):
        async def _spawn(self, *args, stdout, stderr):
            assert stdout == stderr == asyncio.subprocess.DEVNULL
            command = args[args.index('--') + 1:]
            mode, hostname = command[-2:]
            modes.append(mode)
            env['HOSTNAME'] = hostname
            if source == 'near_hostname':
                env['UNRELATED'] = hostname + ' sk-' + secrets.token_hex(20)
            return await asyncio.create_subprocess_exec(*command, env=env, stdout=stdout, stderr=stderr)

    async def check():
        vm = ProbeVM(tmp_path)
        if source == 'agent_env':
            vm.profile = replace(vm.profile, sbx=replace(vm.profile.sbx, environment={'GITHUB_TOKEN': sentinel}))
        task = sbx_task()
        task.task_id = 'probe-' + uuid.uuid4().hex
        await vm.create_empty(task)
        try:
            assert await vm.probe_credentials() == ('ok' if source == 'clean' else 'error')
        finally:
            await vm.destroy()
    asyncio.run(check())
    assert modes == (['raw', 'agent'] if source in {'clean', 'agent_env'} else ['raw'])


@pytest.mark.sandbox
@pytest.mark.skipif(os.getenv('CONTRIBOS_IT_SBX') != '1', reason='opt-in mountless sbx probe')
def test_real_empty_vm_isolation(tmp_path):
    results = asyncio.run(probes.probe_empty_vm(SbxSandbox(Settings(home=tmp_path), PROFILES['codex'])))
    assert results == dict.fromkeys(probes.CHECKS, 'ok')
