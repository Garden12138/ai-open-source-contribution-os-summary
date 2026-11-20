import asyncio
import json
import os
import secrets
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from acp.schema import TextContentBlock

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.engine import TaskContext
from contribos.agent.profiles import PROFILES
from contribos.config import Settings
from contribos.sandbox.base import SandboxError
from contribos.sandbox.host import GIT_OPTIONS, HostSandbox, clean_environment

SCRIPT = Path(__file__).with_name('fake_acp_agent.py').resolve()


@pytest.fixture
def local_repo(tmp_path):
    repo = tmp_path / 'upstream'
    repo.mkdir()
    env = {k: os.environ[k] for k in ('PATH', 'TMPDIR') if k in os.environ}
    env.update(HOME=str(tmp_path), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    def git(*args):
        return subprocess.run(['git', *GIT_OPTIONS, *args], cwd=repo, env=env, check=True, capture_output=True, text=True)
    git('init', '-b', 'main')
    (repo / 'README.md').write_text('Initial\n')
    git('add', '.')
    git('-c', 'user.email=test@example.invalid', '-c', 'user.name=Test', 'commit', '-m', 'Initial')
    return repo


def task():
    return TaskContext('task-one', 'owner/repo', 1, '', '', trust_confirmed=True,
        issue={'title': 'Fix it', 'body': 'Details', 'comments': [{'body': 'A comment'}]}, instructions='Task rules')


def test_clean_environment_removes_all_inherited_credential_overrides(tmp_path):
    canary = secrets.token_urlsafe(32)
    env = clean_environment({key: canary for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'SSH_AUTH_SOCK', 'CONTRIBOS_GITHUB_TOKEN',
        'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_VALUE_100', 'GIT_ASKPASS', 'SSH_ASKPASS', 'NODE_OPTIONS')}, tmp_path)
    assert canary not in json.dumps(env)
    assert env['GH_CONFIG_DIR'] == str(tmp_path / 'gh')
    assert env['GIT_TERMINAL_PROMPT'] == '0'
    assert env['GIT_CONFIG_VALUE_0'] == ''
    assert env['HOME'] == str(tmp_path / 'home')


def test_independent_clone_branch_context_diff_and_disabled_hooks(tmp_path, local_repo):
    async def check():
        sandbox = HostSandbox(Settings(home=tmp_path / 'data'), clone_source=str(local_repo))
        ctx = task()
        try:
            workspace = await sandbox.prepare(ctx)
            assert workspace.default_branch == 'main'
            assert workspace.work_branch == 'contribos/task-one'
            assert Path(workspace.repo_dir) != local_repo
            assert Path(ctx.context_dir, 'TASK.md').read_text() == 'Task rules'
            assert 'A comment' in Path(ctx.context_dir, 'issue.md').read_text()
            marker = tmp_path / 'hook-ran'
            hook = Path(ctx.repo_dir, '.git/hooks/post-checkout')
            hook.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
            hook.chmod(0o755)
            assert (await sandbox.exec(['git', 'switch', 'main'])).returncode == 0
            assert not marker.exists()
            assert (await sandbox.exec(['git', 'config', '--get-all', 'credential.helper'])).stdout.strip() == ''
            Path(ctx.repo_dir, 'README.md').write_text('Changed\n')
            Path(ctx.repo_dir, 'new.txt').write_text('New\n')
            diff = await sandbox.diff()
            assert '+Changed' in diff and '+New' in diff
            assert 'TASK.md' not in diff
            assert (local_repo / 'README.md').read_text() == 'Initial\n'
            with pytest.raises(SandboxError):
                await sandbox.exec(['git', 'status'], cwd=str(local_repo))
            link = Path(ctx.repo_dir, 'outside')
            link.symlink_to(local_repo / 'README.md')
            with pytest.raises(SandboxError):
                await sandbox.export_file(str(link))
            exported = await sandbox.export_file(str(Path(ctx.repo_dir, 'new.txt')))
            assert exported.read_text() == 'New\n'
            with pytest.raises(SandboxError, match='workspace_exists'):
                await sandbox.prepare(ctx)
        finally:
            await sandbox.destroy()
        assert not Path(workspace.repo_dir).exists()
    asyncio.run(asyncio.wait_for(check(), 10))


def test_spawned_agent_really_has_no_github_credentials(tmp_path, local_repo, monkeypatch):
    canary = secrets.token_urlsafe(32)
    for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'SSH_AUTH_SOCK', 'CONTRIBOS_GITHUB_TOKEN'):
        monkeypatch.setenv(key, canary)
    monkeypatch.setenv('FAKE_ACP_SCENARIO', 'environment')
    async def check():
        sandbox = HostSandbox(Settings(home=tmp_path / 'data'), clone_source=str(local_repo))
        ctx = task()
        await sandbox.prepare(ctx)
        profile = replace(PROFILES['codex'], command=(sys.executable, str(SCRIPT)), host_args=())
        agent = AcpEngine(lambda: sandbox.spawn_agent(profile))
        try:
            sid = await agent.ensure_session(ctx)
            await agent.prompt(sid, [TextContentBlock(text='Inspect')])
            async for event in agent.events(sid):
                assert canary not in json.dumps(event.payload)
                if event.type == 'message.agent':
                    env = json.loads(event.payload['text'])
                    assert all(env[k] is None for k in ('GITHUB_TOKEN', 'GH_TOKEN', 'SSH_AUTH_SOCK'))
                    assert Path(env['GH_CONFIG_DIR']).is_dir() and not list(Path(env['GH_CONFIG_DIR']).iterdir())
                    assert env['GIT_CONFIG_GLOBAL'] == os.devnull
                    assert env['GIT_TERMINAL_PROMPT'] == '0'
                if event.type == 'turn.completed':
                    break
        finally:
            await agent.close()
            await sandbox.stop()
    asyncio.run(asyncio.wait_for(check(), 10))


@pytest.mark.parametrize('changes,code', [({'trust_confirmed': False}, 'host_trust_required'),
    ({'isolation': 'container'}, 'host_trust_required'), ({'task_id': '../escape'}, 'invalid_task_id'),
    ({'repo_full_name': '-c/invalid option'}, 'invalid_repository')])
def test_invalid_host_request_never_creates_workspace(tmp_path, changes, code):
    async def check():
        sandbox = HostSandbox(Settings(home=tmp_path / 'data'))
        ctx = replace(task(), **changes)
        with pytest.raises(SandboxError, match=code):
            await sandbox.prepare(ctx)
        assert not (tmp_path / 'data').exists()
    asyncio.run(check())
