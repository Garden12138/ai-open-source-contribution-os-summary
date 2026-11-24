import asyncio
import hashlib
import os
import secrets
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from contribos.config import Settings
from contribos.publish.git_transfer import Author, GitTransfer, TransferError, export_candidate, snapshot_workspace
from contribos.sandbox.host import HostSandbox
from test_host import local_repo, task

AUTHOR = Author('Publisher Author', 'publisher@example.invalid', signoff=True)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], env={**os.environ, 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull}, stderr=subprocess.DEVNULL).decode().strip()


def test_guest_bundle_and_host_bare_rebuild_skip_hooks(tmp_path, local_repo):
    async def check():
        remote = tmp_path / 'upstream.git'
        git(tmp_path, 'clone', '--bare', str(local_repo), str(remote))
        settings = Settings(home=tmp_path / 'data')
        transfer = GitTransfer(settings, upstream_url=lambda _: str(remote))
        sandbox = HostSandbox(settings, clone_source=str(remote))
        ctx = task()
        await sandbox.prepare(ctx)
        repo_dir = Path(ctx.repo_dir)
        cache = transfer._cache(ctx.repo_full_name)
        cache.mkdir(parents=True)
        git(cache, 'init', '--bare')
        marker = tmp_path / 'hook-ran'
        for folder in (repo_dir / '.git/hooks', cache / 'hooks'):
            for name in ('pre-commit', 'post-checkout', 'reference-transaction'):
                hook = folder / name
                hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
                hook.chmod(0o755)
        (repo_dir / 'README.md').write_text('Updated\n')
        workflow = repo_dir / '.github/workflows/check.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('name: checks\n')
        (repo_dir / 'binary.dat').write_bytes(b'\0\xfe\xf1')
        (local_repo / 'another.txt').write_text('Upstream advanced\n')
        git(local_repo, 'add', 'another.txt')
        git(local_repo, '-c', 'user.name=Upstream', '-c', 'user.email=upstream@example.invalid', 'commit', '-m', 'Upstream advanced')
        git(local_repo, 'push', str(remote), 'main')
        source = await snapshot_workspace(sandbox, ctx, 'main')
        candidate = await export_candidate(sandbox, ctx, source)
        try:
            preview = await transfer.preview(ctx.repo_full_name, 'main', candidate, AUTHOR, 'Verified change', uuid.uuid4().hex)
            assert preview.behind == 1 and preview.commit_sha != candidate.commit
            assert {change.path for change in preview.files} == {'.github/workflows/check.yml', 'README.md', 'binary.dat'}
            assert preview.warnings == ['binary_files', 'workflow_changes']
            assert hashlib.sha256(preview.diff.encode()).hexdigest() == preview.diff_hash
            metadata = git(cache, 'show', '-s', '--format=%P%n%T%n%an%n%ae%n%B', preview.commit_sha)
            assert metadata.splitlines()[:4] == [source.base, source.tree, AUTHOR.name, AUTHOR.email]
            assert 'Signed-off-by: Publisher Author <publisher@example.invalid>' in metadata
            assert not marker.exists() and git(cache, 'rev-parse', '--is-bare-repository') == 'true'
            assert not (cache / 'README.md').exists()
            assert git(repo_dir, 'diff', '--cached') == ''

            with pytest.raises(TransferError, match='base_not_on_upstream'):
                await transfer.preview(ctx.repo_full_name, 'main', replace(candidate, source=replace(source, base=candidate.commit)),
                                       AUTHOR, 'Invalid base', uuid.uuid4().hex)
            with pytest.raises(TransferError, match='invalid_candidate_parent'):
                await transfer.preview(ctx.repo_full_name, 'main', replace(candidate, source=replace(source, base=preview.upstream_sha)),
                                       AUTHOR, 'Invalid parent', uuid.uuid4().hex)

            (repo_dir / 'README.md').write_text('++ ghp_' + secrets.token_hex(20) + '\n')
            secret_bundle = await export_candidate(sandbox, ctx, await snapshot_workspace(sandbox, ctx, 'main'))
            try:
                with pytest.raises(TransferError, match='secret_detected'):
                    await transfer.preview(ctx.repo_full_name, 'main', secret_bundle, AUTHOR, 'Unsafe change', uuid.uuid4().hex)
            finally:
                secret_bundle.bundle.unlink()
        finally:
            candidate.bundle.unlink()
            await sandbox.destroy()

    asyncio.run(check())
