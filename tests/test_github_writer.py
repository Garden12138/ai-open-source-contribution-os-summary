import asyncio
import base64
import json
import secrets

import httpx
import pytest

from contribos.config import Settings
from contribos.github import GitHubCredentials
from contribos.publish.git_transfer import GitTransfer, TransferError
from contribos.publish.github_writer import Account, GitHubWriter, WriterError, WriterSession
from test_git_transfer import git
from test_host import local_repo

UPSTREAM = 'owner/repo'
FORK = 'publisher/repo'
BRANCH = 'contribos/issue-1-01abcdf23456'
COMMIT = 'a' * 40


def test_writer_reconciles_github_writes_after_ambiguous_failure(monkeypatch):
    canary = secrets.token_urlsafe(32)
    monkeypatch.setenv('CONTRIBOS_GITHUB_TOKEN', canary)
    writes = []
    fork = {'fork': True, 'full_name': FORK, 'parent': {'full_name': UPSTREAM},
            'default_branch': 'main', 'permissions': {'push': True}}
    pr = {'number': 3, 'state': 'open', 'draft': True, 'base': {'ref': 'main', 'repo': {'full_name': UPSTREAM}},
          'head': {'ref': BRANCH, 'sha': COMMIT, 'repo': {'full_name': FORK}}}

    def respond(request):
        assert canary not in str(request.url) and request.headers['authorization'] == 'Bearer ' + canary
        path = request.url.path
        if path == '/user':
            return httpx.Response(200, json={'login': 'publisher', 'id': 7, 'type': 'User'})
        if path == f'/repos/{FORK}':
            return httpx.Response(200, json=fork) if 'fork' in writes else httpx.Response(404)
        if path == f'/repos/{FORK}/git/ref/heads/main':
            return httpx.Response(200, json={})
        if path == f'/repos/{UPSTREAM}/forks':
            assert request.method == 'POST'
            writes.append('fork')
            return httpx.Response(202, json={})
        assert path == f'/repos/{UPSTREAM}/pulls'
        if request.method == 'GET':
            assert request.url.params['head'] == 'publisher:' + BRANCH
            return httpx.Response(200, json=[pr] if 'pull' in writes else [])
        assert json.loads(request.content) == {'title': 'Title', 'body': 'Body', 'head': 'publisher:' + BRANCH,
                                               'base': 'main', 'draft': True}
        writes.append('pull')
        raise httpx.ReadTimeout(canary)

    async def check():
        settings = Settings()
        writer = GitHubWriter(GitHubCredentials(settings), GitTransfer(settings), transport=httpx.MockTransport(respond))
        async with writer.open() as session:
            assert session.destination(UPSTREAM, await session.identity()) == FORK
            assert await session.ensure_fork(UPSTREAM, FORK) == FORK
            with pytest.raises(WriterError, match='github_publish_request_failed') as error:
                await session.ensure_pull(UPSTREAM, FORK, BRANCH, 'main', 'Title', 'Body', COMMIT)
            assert canary not in str(error.value)
        async with writer.open() as session:
            assert await session.ensure_fork(UPSTREAM, FORK) == FORK
            result = await session.ensure_pull(UPSTREAM, FORK, BRANCH, 'main', 'Title', 'Body', COMMIT)
            assert result.draft and result.url == f'https://github.com/{UPSTREAM}/pull/3'

    asyncio.run(check())
    assert writes == ['fork', 'pull']


@pytest.mark.parametrize(('override', 'branch', 'rules', 'tip', 'error'), [
    ({'owner': {'id': 8, 'type': 'User', 'login': 'owner'}}, BRANCH, [], None, 'owned_destination_conflict'),
    ({'owner': {'id': 7, 'type': 'Organization', 'login': 'owner'}}, BRANCH, [], None, 'owned_destination_conflict'),
    ({'full_name': 'other/repo'}, BRANCH, [], None, 'owned_destination_conflict'),
    ({'permissions': {'push': False}}, BRANCH, [], None, 'owned_destination_conflict'),
    ({'default_branch': 'stable'}, BRANCH, [], None, 'default_branch_changed'),
    ({'default_branch': BRANCH}, BRANCH, [], None, 'unsafe_publish_branch'),
    ({}, 'release', [], None, 'unsafe_publish_branch'),
    ({}, BRANCH, [{'type': 'creation'}], None, 'unsafe_publish_branch'),
    ({}, BRANCH, [], {'protected': True, 'commit': {'sha': COMMIT}}, 'unsafe_publish_branch'),
    ({}, BRANCH, [], {'protected': False, 'commit': {'sha': 'b' * 40}}, 'remote_branch_conflict'),
])
def test_owned_publication_requires_personal_writable_repo_and_safe_branch(override, branch, rules, tip, error):
    repo = {'id': 19, 'full_name': UPSTREAM, 'owner': {'id': 7, 'type': 'User', 'login': 'owner'},
            'permissions': {'push': True}, 'default_branch': 'main', **override}

    def respond(request):
        assert request.method == 'GET'
        if request.url.path == f'/repos/{UPSTREAM}':
            return httpx.Response(200, json=repo)
        if '/rules/branches/' in request.url.path:
            return httpx.Response(200, json=rules)
        assert request.url.path == f'/repos/{UPSTREAM}/branches/{branch}'
        return httpx.Response(200, json=tip) if tip else httpx.Response(404)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            session = WriterSession(http, GitTransfer(Settings()), secrets.token_urlsafe(32), 1)
            default = BRANCH if override.get('default_branch') == BRANCH else 'main'
            with pytest.raises(WriterError, match=error):
                await session.owned_repository(UPSTREAM, Account('Owner', 7), branch, default, commit=COMMIT)

    asyncio.run(check())


def test_third_party_repository_with_same_name_is_not_reused_as_a_fork():
    def respond(request):
        assert request.method == 'GET' and request.url.path == f'/repos/{FORK}'
        return httpx.Response(200, json={'full_name': FORK, 'fork': True, 'parent': {'full_name': 'other/repo'},
                                        'permissions': {'push': True}})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            session = WriterSession(http, GitTransfer(Settings()), secrets.token_urlsafe(32), 1)
            with pytest.raises(WriterError, match='fork_destination_conflict'):
                await session.ensure_fork(UPSTREAM, FORK)

    asyncio.run(check())


def test_git_push_does_not_overwrite_same_name_and_token_is_only_in_environment(tmp_path, local_repo, monkeypatch):
    remote = tmp_path / 'fork.git'
    git(tmp_path, 'clone', '--bare', str(local_repo), str(remote))
    canary = secrets.token_urlsafe(32)
    parent = git(local_repo, 'rev-parse', 'HEAD')
    encoded = base64.b64encode(f'x-access-token:{canary}'.encode()).decode()
    git(local_repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'next')
    commit = git(local_repo, 'rev-parse', 'HEAD')
    cache = tmp_path / 'cache.git'
    git(tmp_path, 'clone', '--bare', str(local_repo), str(cache))
    actual_spawn = asyncio.create_subprocess_exec
    child_envs = []

    async def spawn(*argv, env, **kwargs):
        assert all(canary not in arg and encoded not in arg for arg in argv)
        if 'push' in argv or 'ls-remote' in argv:
            child_envs.append(env['GIT_CONFIG_VALUE_0'])
            assert env['GIT_CONFIG_KEY_0'] == 'http.extraHeader'
        return await actual_spawn(*argv, env=env, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)

    async def check():
        transfer = GitTransfer(Settings(home=tmp_path))
        assert await transfer._push_branch(cache, str(remote), BRANCH, commit, canary, allow_file=True) == 'pushed'
        assert await transfer._push_branch(cache, str(remote), BRANCH, commit, canary, allow_file=True) == 'existing'
        with pytest.raises(TransferError, match='remote_branch_conflict'):
            await transfer._push_branch(cache, str(remote), 'main', commit, canary, allow_file=True)
        assert git(remote, 'rev-parse', 'main') == parent
        assert git(remote, 'rev-parse', BRANCH) == commit

    asyncio.run(check())
    assert child_envs == ['Authorization: Basic ' + encoded] * 4


def test_branch_created_between_lookup_and_push_is_preserved(tmp_path, local_repo):
    remote = tmp_path / 'fork.git'
    git(tmp_path, 'clone', '--bare', str(local_repo), str(remote))
    parent = git(local_repo, 'rev-parse', 'HEAD')
    git(local_repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'next')
    commit = git(local_repo, 'rev-parse', 'HEAD')
    cache = tmp_path / 'cache.git'
    git(tmp_path, 'clone', '--bare', str(local_repo), str(cache))

    class RacingTransfer(GitTransfer):
        async def _git(self, cache, *args, **kwargs):
            output = await super()._git(cache, *args, **kwargs)
            if args[0] == 'ls-remote' and not output:
                git(remote, 'update-ref', f'refs/heads/{BRANCH}', parent)
            return output

    async def check():
        transfer = RacingTransfer(Settings(home=tmp_path))
        with pytest.raises(TransferError, match='push_failed'):
            await transfer._push_branch(cache, str(remote), BRANCH, commit, secrets.token_urlsafe(32), allow_file=True)
        assert git(remote, 'rev-parse', BRANCH) == parent

    asyncio.run(check())
