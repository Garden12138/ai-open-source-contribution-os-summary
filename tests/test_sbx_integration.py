"""Opt-in M2 test. Run from the host with a signed-in sbx and a user-owned Issue."""

import asyncio
import os
import uuid

import pytest
from acp.schema import TextContentBlock

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.engine import TaskContext
from contribos.agent.profiles import PROFILES
from contribos.config import Settings
from contribos.sandbox.probes import probe_vm
from contribos.sandbox.sbx import SbxSandbox
from contribos.tasks.issue import parse_issue_url

@pytest.mark.sandbox
@pytest.mark.skipif(os.getenv('CONTRIBOS_IT_SBX') != '1', reason='opt-in sbx test')
def test_real_sbx_codex_bundle_and_resume(tmp_path):
    issue_url = os.getenv('CONTRIBOS_IT_ISSUE_URL')
    if not issue_url:
        pytest.skip('Set CONTRIBOS_IT_ISSUE_URL to a user-owned test Issue')
    issue = parse_issue_url(issue_url)
    profile = PROFILES['codex']
    sandbox = SbxSandbox(Settings(home=tmp_path), profile)
    task = TaskContext(uuid.uuid4().hex, issue.repo, issue.number, '', '', isolation='sbx',
                       instructions='Use the test checkout only. Changes are disposable and stay local.')
    workspace = None

    def create_engine() -> AcpEngine:
        return AcpEngine(lambda: sandbox.spawn_agent(profile), stop_process=sandbox.stop,
                         error_mapper=profile.classify_error)

    async def check():
        nonlocal workspace
        workspace = await sandbox.prepare(task)
        assert (await sandbox.exec(['cat', f'{workspace.context_dir}/TASK.md'])).stdout == task.instructions

        engine = create_engine()
        try:
            session = await engine.ensure_session(task)
            result = await engine.prompt(session, [TextContentBlock(text=(
                'Create exactly one new text file named contribos-m2-smoke.txt in the current repository '
                'with the line "ContribOS sandbox check". Run a Python command to verify its contents. '
                'Do not modify any other files. Do not commit, push or publish.'))])
            assert result.stop_reason == 'end_turn'
            assert 'contribos-m2-smoke.txt' in await sandbox.diff()
        finally:
            await engine.close()

        assert set((await probe_vm(sandbox)).values()) == {'ok'}
        bundle = f'{workspace.repo_dir}/smoke.bundle'
        for args in (
            ['add', '-A'],
            ['-c', 'user.name=ContribOS', '-c', 'user.email=smoke@example.invalid', 'commit', '-m', 'Local smoke'],
            ['bundle', 'create', bundle, 'HEAD', f'^{workspace.base_sha}'],
        ):
            assert (await sandbox.exec(['git', *args])).returncode == 0
        exported = await sandbox.export_file(bundle)
        try:
            assert exported.read_bytes().startswith((b'# v2 git bundle\n', b'# v3 git bundle\n'))
        finally:
            exported.unlink()

        await sandbox.stop()
        await sandbox.restore(task)
        assert (await sandbox.exec(['cat', 'contribos-m2-smoke.txt'])).stdout.strip() == 'ContribOS sandbox check'
        engine = create_engine()
        try:
            assert await engine.ensure_session(task) == session
            result = await engine.prompt(session, [TextContentBlock(text='Reply with a short status. Do not edit files.')])
            assert result.stop_reason == 'end_turn'
        finally:
            await engine.close()

    async def run():
        try:
            await asyncio.wait_for(check(), 600)
        finally:
            await sandbox.destroy()

    asyncio.run(run())
