"""Export a guest worktree as Git objects and validate it in a host bare cache."""

import asyncio
import base64
import hashlib
import os
import re
import signal
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from contribos.agent.engine import TaskContext
from contribos.config import Settings
from contribos.sandbox.base import Sandbox
from contribos.sandbox.host import GIT_OPTIONS, safe_text

OID = re.compile(r'[0-9a-f]{40}')
REPOSITORY = re.compile(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+')
DIFF = ['--no-ext-diff', '--no-textconv', '--no-renames', '--full-index', '--src-prefix=a/', '--dst-prefix=b/']
SECRET = re.compile(rb'gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|'
                    rb'-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:AKIA|ASIA)[A-Z0-9]{16}|'
                    rb'https?://[^\s/?#<>"\x27]+:[^\s/?#<>"\x27]+@|'
                    rb'(?i:authorization\s*[:=]\s*(?:bearer|basic)\s+)[A-Za-z0-9+=/_.-]{12,}')


class TransferError(RuntimeError):
    """A public error code without Git diagnostics."""


def github_url(repo: str) -> str:
    if not REPOSITORY.fullmatch(repo) or repo.split('/')[1] in {'.', '..'}:
        raise TransferError('invalid_repository')
    return f'https://github.com/{repo}.git'


def _oid(output: str) -> str:
    sha = output.strip()
    if not OID.fullmatch(sha):
        raise TransferError('invalid_git_object')
    return sha


@dataclass(frozen=True)
class Snapshot:
    base: str
    head: str
    tree: str


@dataclass(frozen=True)
class Author:
    name: str
    email: str
    signoff: bool = False

    def environment(self) -> dict[str, str]:
        if (not self.name.strip() or len(self.name) > 128 or re.search(r'[\x00-\x1f<>]', self.name)
                or not re.fullmatch(r'[^\s<>@\x00-\x1f]+@[^\s<>@\x00-\x1f]+', self.email)
                or len(self.email) > 320):
            raise TransferError('git_author_required')
        stamp = datetime.now(timezone.utc).isoformat()
        return {f'GIT_{role}_{key}': value for role in ('AUTHOR', 'COMMITTER')
                for key, value in (('NAME', self.name), ('EMAIL', self.email), ('DATE', stamp))}


@dataclass(frozen=True)
class Candidate:
    source: Snapshot
    bundle: Path
    ref: str
    commit: str


@dataclass(frozen=True)
class Change:
    path: str
    additions: int
    deletions: int
    binary: bool


@dataclass(frozen=True)
class GitPreview:
    commit_sha: str
    upstream_sha: str
    behind: int
    diff_hash: str
    diff: str
    files: list[Change]
    warnings: list[str]


async def _guest_git(sandbox: Sandbox, *argv: str, index: str | None = None) -> str:
    command = ['git', *argv] if index is None else ['env', f'GIT_INDEX_FILE={index}', 'git', *GIT_OPTIONS, *argv]
    result = await sandbox.exec(command, timeout=240)
    if result.returncode:
        raise TransferError('workspace_git_failed')
    return result.stdout


async def snapshot_workspace(sandbox: Sandbox, task: TaskContext, branch: str, *, fetch: bool = True,
                             require_resolved: bool = True) -> Snapshot:
    if not branch:
        raise TransferError('default_branch_unavailable')
    upstream = f'refs/heads/{branch}'
    tracking = f'refs/remotes/origin/{branch}'
    await _guest_git(sandbox, 'check-ref-format', upstream)
    if fetch:
        await _guest_git(sandbox, 'fetch', '--no-tags', '--no-recurse-submodules', 'origin', f'+{upstream}:{tracking}')
    if require_resolved and await _guest_git(sandbox, 'ls-files', '--unmerged'):
        raise TransferError('workspace_has_conflicts')
    head = _oid(await _guest_git(sandbox, 'rev-parse', '--verify', 'HEAD^{commit}'))
    base = _oid(await _guest_git(sandbox, 'merge-base', head, tracking))
    index = str(PurePosixPath(task.context_dir) / f'.publish-{uuid.uuid4().hex}.index')
    try:
        await _guest_git(sandbox, 'read-tree', head, index=index)
        await _guest_git(sandbox, 'add', '-A', '--', '.', index=index)
        tree = _oid(await _guest_git(sandbox, 'write-tree', index=index))
    finally:
        with suppress(OSError, RuntimeError):
            await sandbox.exec(['rm', '-f', '--', index, index + '.lock'])
    return Snapshot(base, head, tree)


async def workspace_patch(sandbox: Sandbox, source: Snapshot) -> str:
    return await _guest_git(sandbox, '-c', 'core.quotePath=false', 'diff', *DIFF, '--binary', source.base, source.tree, '--')


async def export_candidate(sandbox: Sandbox, task: TaskContext, source: Snapshot) -> Candidate:
    artifact = uuid.uuid4().hex
    ref = f'refs/contribos/export/{artifact}'
    path = str(PurePosixPath(task.context_dir) / f'{artifact}.bundle')
    commit = _oid(await _guest_git(sandbox, '-c', 'user.name=ContribOS', '-c', 'user.email=bundle@contribos.invalid',
                                  '-c', 'commit.gpgSign=false', 'commit-tree', source.tree, '-p', source.base,
                                  '-m', 'ContribOS candidate'))
    await _guest_git(sandbox, 'update-ref', ref, commit)
    try:
        await _guest_git(sandbox, 'bundle', 'create', path, ref, '^' + source.base)
        return Candidate(source, await sandbox.export_file(path), ref, commit)
    finally:
        with suppress(OSError, RuntimeError):
            await _guest_git(sandbox, 'update-ref', '-d', ref, commit)
        with suppress(OSError, RuntimeError):
            await sandbox.exec(['rm', '-f', '--', path])


class GitTransfer:
    def __init__(self, settings: Settings, *, upstream_url: Callable[[str], str] = github_url) -> None:
        self.settings, self.upstream_url = settings, upstream_url
        self.locks: dict[str, asyncio.Lock] = {}

    def _cache(self, repo: str) -> Path:
        github_url(repo)
        return self.settings.home / 'git' / (repo.lower().replace('/', '__') + '.git')

    async def _git(self, cache: Path, *args: str, data: bytes | None = None, token: str | None = None,
                   settings: dict[str, str] | None = None, allow_file: bool = False,
                   error: str = 'host_git_failed') -> bytes:
        env = {key: value for key, value in os.environ.items() if key.upper() in {
            'PATH', 'TMPDIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
        }}
        env.update(HOME=str(cache.parent), XDG_CONFIG_HOME=str(cache.parent), LC_ALL='C', GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/usr/bin/false',
            GIT_NO_REPLACE_OBJECTS='1', **(settings or {}))
        if token:
            basic = base64.b64encode(f'x-access-token:{token}'.encode()).decode('ascii')
            env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader',
                       GIT_CONFIG_VALUE_0='Authorization: Basic ' + basic)
        options = [*GIT_OPTIONS, '-c', 'core.bare=true', '-c', 'core.attributesFile=/dev/null', '-c',
                   'core.quotePath=false', '-c', 'protocol.ext.allow=never', '-c', 'http.followRedirects=false',
                   '-c', 'fetch.fsckObjects=true', '-c', 'transfer.fsckObjects=true', '-c', 'credential.useHttpPath=true',
                   '-c', f'protocol.file.allow={"always" if allow_file else "never"}', '-c', 'commit.gpgSign=false']
        process = None
        try:
            process = await asyncio.create_subprocess_exec(self.settings.tool('git') or 'git', *options,
                '--literal-pathspecs', *args, cwd=cache, env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            stdout, _ = await asyncio.wait_for(process.communicate(data), 180)
            if process.returncode == 0:
                return stdout
        except (OSError, TimeoutError, asyncio.CancelledError) as exc:
            if process is not None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
        raise TransferError(error)

    async def _init(self, cache: Path, url: str) -> None:
        for path in (cache, cache.parent, cache / 'config', cache / 'objects', cache / 'refs'):
            if path.is_symlink():
                raise TransferError('unsafe_git_cache')
        cache.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not (cache / 'HEAD').is_file():
            await self._git(cache.parent, 'init', '--bare', '--template=', '--object-format=sha1', str(cache))
        await self._git(cache, 'config', '--local', 'remote.origin.url', url)
        await self._git(cache, 'config', '--local', 'remote.origin.promisor', 'true')
        await self._git(cache, 'config', '--local', 'remote.origin.partialclonefilter', 'blob:none')

    async def preview(self, repo: str, branch: str, candidate: Candidate, author: Author,
                      message: str, preview_id: str, *, parent_sha: str | None = None) -> GitPreview:
        cache = self._cache(repo)
        author_env = author.environment()
        if not re.fullmatch(r'[0-9a-f]{32}', preview_id):
            raise TransferError('invalid_preview_id')
        async with self.locks.setdefault(str(cache), asyncio.Lock()):
            url = self.upstream_url(repo)
            await self._init(cache, url)
            upstream, tracking = f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'
            await self._git(cache, 'check-ref-format', upstream)
            await self._git(cache, 'fetch', '--no-auto-maintenance', '--no-tags', '--no-recurse-submodules',
                '--no-write-fetch-head', '--filter=blob:none', 'origin', f'+{upstream}:{tracking}',
                allow_file=url != github_url(repo), error='upstream_fetch_failed')
            base = _oid(candidate.source.base)
            await self._git(cache, 'merge-base', '--is-ancestor', base, tracking,
                            error='followup_rebase_required' if parent_sha else 'base_not_on_upstream')
            upstream_sha = _oid((await self._git(cache, 'rev-parse', tracking)).decode())
            ref = f'refs/contribos/import/{preview_id}'
            try:
                return await self._build(cache, candidate, base, upstream_sha, author, author_env, message,
                                         ref, preview_id, _oid(parent_sha) if parent_sha else base)
            finally:
                with suppress(TransferError):
                    await self._git(cache, 'update-ref', '-d', ref)

    async def _build(self, cache: Path, candidate: Candidate, base: str, upstream: str, author: Author,
                     author_env: dict[str, str], message: str, ref: str, preview_id: str, parent: str) -> GitPreview:
        path = str(candidate.bundle.resolve(strict=True))
        listed = (await self._git(cache, 'bundle', 'list-heads', path)).decode(errors='replace').splitlines()
        if listed != [f'{_oid(candidate.commit)} {candidate.ref}'] or not re.fullmatch(
                r'refs/contribos/export/[0-9a-f]{32}', candidate.ref):
            raise TransferError('invalid_bundle')
        await self._git(cache, 'fetch', '--no-auto-maintenance', '--no-tags', '--no-recurse-submodules',
            '--no-write-fetch-head', path, f'{candidate.ref}:{ref}', allow_file=True, error='invalid_bundle')
        parents = (await self._git(cache, 'rev-list', '--parents', '-n', '1', ref)).decode().split()
        if parents != [candidate.commit, base]:
            raise TransferError('invalid_candidate_parent')
        tree = _oid((await self._git(cache, 'rev-parse', f'{ref}^{{tree}}')).decode())
        if tree != candidate.source.tree:
            raise TransferError('invalid_candidate_tree')
        known_secrets = tuple(os.fsencode(value) for key, value in os.environ.items() if len(value) >= 16
                              and any(marker in key.upper() for marker in ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD')))

        def check_secret(data: bytes) -> None:
            if SECRET.search(data) or any(value in data for value in known_secrets):
                raise TransferError('secret_detected')

        check_secret(message.encode())
        if author.signoff:
            message = message.rstrip() + f'\n\nSigned-off-by: {author.name} <{author.email}>'
        commit = _oid((await self._git(cache, 'commit-tree', tree, '-p', parent, '-F', '-',
                                      data=(message.rstrip() + '\n').encode(), settings=author_env)).decode())
        if parent != base and upstream != base:
            await self._git(cache, 'merge-tree', '--write-tree', upstream, commit, error='followup_rebase_required')
        patch = await self._git(cache, 'diff', *DIFF, '--binary', parent, commit, '--')
        if not patch:
            raise TransferError('no_publishable_changes')
        stats = await self._git(cache, 'diff', *DIFF, '--numstat', '-z', parent, commit, '--')
        files = []
        for line in stats.split(b'\0')[:-1]:
            added, deleted, filename = line.split(b'\t', 2)
            check_secret(filename)
            binary = added == b'-'
            filename_text = os.fsdecode(filename)
            if binary:
                found = await self._git(cache, 'ls-tree', '-z', commit, '--', filename_text)
                if found:
                    object_type, sha = found.split(b'\t', 1)[0].split()[1:]
                    if object_type == b'blob':
                        check_secret(await self._git(cache, 'cat-file', 'blob', sha.decode()))
            files.append(Change(filename.decode(errors='replace'), int(added) if not binary else 0,
                                int(deleted) if not binary else 0, binary))
        in_hunk = False
        for line in patch.splitlines():
            if line.startswith(b'diff --git '):
                in_hunk = False
            elif line.startswith(b'@@ '):
                in_hunk = True
            elif in_hunk and line.startswith(b'+'):
                check_secret(line[1:])
        warnings = []
        if any(file.binary for file in files):
            warnings.append('binary_files')
        if any(file.path.startswith('.github/workflows/') for file in files):
            warnings.append('workflow_changes')
        if len(files) > 40 or sum(file.additions + file.deletions for file in files) > 2000 or len(patch) > 256_000:
            warnings.append('large_diff')
        behind = int(await self._git(cache, 'rev-list', '--count', f'{base}..{upstream}'))
        await self._git(cache, 'update-ref', f'refs/contribos/previews/{preview_id}', commit)
        return GitPreview(commit, upstream, behind, hashlib.sha256(patch).hexdigest(),
                          safe_text(patch.decode(errors='replace')), files, warnings)

    async def push_new(self, repo: str, fork: str, branch: str, commit: str, token: str) -> str:
        cache = self._cache(repo)
        return await self._push_branch(cache, github_url(fork), branch, commit, token)

    async def push_followup(self, repo: str, fork: str, branch: str, commit: str, parent: str, token: str) -> str:
        return await self._push_branch(self._cache(repo), github_url(fork), branch, commit, token, parent=parent)

    async def _push_branch(self, cache: Path, url: str, branch: str, commit: str, token: str,
                           *, parent: str | None = None, allow_file: bool = False) -> str:
        ref = f'refs/heads/{branch}'
        await self._git(cache, 'check-ref-format', ref)
        commit = _oid(commit)
        if parent is not None:
            parents = await self._git(cache, 'rev-list', '--parents', '-n', '1', commit)
            if parents.decode().split() != [commit, _oid(parent)]:
                raise TransferError('followup_rebase_required')
        result = await self._git(cache, 'ls-remote', '--heads', url, ref, token=token,
                                 allow_file=allow_file, error='remote_branch_lookup_failed')
        if result.strip() == f'{commit}\t{ref}'.encode():
            return 'existing'
        expected = f'{parent}\t{ref}'.encode() if parent else b''
        if result.strip() != expected:
            raise TransferError('remote_branch_conflict')
        # Compare-and-swap the advertised ref. An empty lease only creates; a parent lease
        # only advances by the single verified child above, including during concurrent pushes.
        await self._git(cache, 'push', '--porcelain', f'--force-with-lease={ref}:{parent or ""}', url, f'{commit}:{ref}',
                        token=token, allow_file=allow_file, error='push_failed')
        return 'pushed'
