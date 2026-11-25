import asyncio
import json
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest

from contribos.config import Settings
from contribos.db import MIGRATIONS, connect, migrate
from contribos.github import GitHubClient, GitHubError
from contribos.tasks.events import TaskEvents
from contribos.tasks.store import TaskStore
from contribos.tracker import PRTracker, _snapshot
from contribos.web.app import create_app
from test_task_routes import ORIGIN

SHA = 'a' * 40
NOW = '2026-11-01T09:30:00Z'
MERGED = '2026-12-01T10:30:00Z'
SUMMARY = {'title': 'A change', 'state': 'MERGED', 'isDraft': False, 'mergedAt': MERGED,
           'createdAt': NOW, 'headRefOid': SHA, 'totalCommentsCount': 3, 'reviewDecision': 'APPROVED',
           'statusCheckRollup': {'state': 'SUCCESS'}, 'repository': {'primaryLanguage': {'name': 'Python'}}}


def task_pr(db):
    db.execute("""INSERT INTO tasks(id,repo_full_name,issue_number,title,status,agent_profile,isolation,
        issue_snapshot_json,created_at,updated_at) VALUES ('task','owner/test',1,'issue','published','codex','host',
        '{"has_bounty":true}',?,?)""", (NOW, NOW))
    db.execute("""INSERT INTO pull_requests(task_id,repo_full_name,number,url,state,draft,merged,head_sha,last_synced_at)
        VALUES ('task','owner/test',3,'https://github.com/owner/test/pull/3','open',1,0,?,?)""", (SHA, NOW))


def test_history_migration_preserves_published_pr_and_allows_taskless_import(tmp_path):
    db = connect(tmp_path / 'db')
    old = tmp_path / 'old'
    old.mkdir()
    for path in sorted(MIGRATIONS.glob('*.sql'))[:2]:
        shutil.copy(path, old)
    migrate(db, old)
    task_pr(db)
    migrate(db)
    existing = dict(db.execute('SELECT * FROM pull_requests').fetchone())
    assert existing['task_id'] == 'task' and existing['head_sha'] == SHA and existing['number'] == 3
    insert = """INSERT INTO pull_requests(repo_full_name,number,url,state,draft,merged,head_sha,last_synced_at)
        VALUES (?,5,'https://github.com/owner/test/pull/5','closed',0,1,?,?)"""
    db.execute(insert, ('owner/test', SHA, NOW))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ('OWNER/test', SHA, NOW))
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    db.close()


def test_scheduler_refreshes_open_only_and_stats_use_submission_and_merge_dates(tmp_path, monkeypatch):
    canary = secrets.token_urlsafe(32)
    monkeypatch.setenv('CONTRIBOS_GITHUB_TOKEN', canary)
    monkeypatch.setattr('contribos.tracker.INTERVAL', 0.04)
    calls = []

    def github(request):
        body = json.loads(request.content)
        assert body['query'].lstrip().startswith('query(') and request.url.path == '/graphql'
        assert request.headers['Authorization'] == 'Bearer ' + canary
        n = body['variables']['number']
        calls.append(n)
        value = {**SUMMARY, 'title': 'Title ' + canary}
        return httpx.Response(200, json={'data': {'repository': {'pullRequest': value}}})

    async def check():
        settings = Settings(home=tmp_path)
        db = connect(settings.database_path)
        migrate(db)
        task_pr(db)
        db.execute("""INSERT INTO pull_requests(repo_full_name,number,url,state,draft,merged,head_sha,last_synced_at,data_json)
            VALUES ('owner/other',1,'https://github.com/owner/other/pull/1','closed',0,0,?,?,?)""",
            (SHA, NOW, json.dumps({'created_at': NOW, 'language': 'TypeScript'})))
        from contribos.tasks.supervisor import TaskSupervisor
        app = create_app(settings, supervisor_factory=lambda s, store: TaskSupervisor(s, store,
            github=GitHubClient(s, transport=httpx.MockTransport(github))))
        headers = {'Authorization': 'Bearer ' + settings.local_token_path.read_text().strip()}
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN) as http:
            for _ in range(20):
                if db.execute("SELECT merged FROM pull_requests WHERE task_id='task'").fetchone()[0]:
                    break
                await asyncio.sleep(.01)
            await asyncio.sleep(.09)
            assert calls == [3]
            assert (await http.get('/api/stats')).status_code == 401
            stats = (await http.get('/api/stats', headers=headers)).json()
            assert stats['months'] == {'2026-11': {'submitted': 2, 'merged': 0}, '2026-12': {'submitted': 0, 'merged': 1}}
            assert stats['days'] == {'2026-11-01': 2}
            assert stats['languages'] == {'TypeScript': 1, 'Python': 1}
            pr, imported = stats['pull_requests']
            assert pr['has_bounty'] and imported['task_id'] is None
            assert pr['merged'] and pr['review_decision'] == 'APPROVED' and pr['comments_count'] == 3 and pr['checks_state'] == 'SUCCESS'
            assert (await http.post('/api/tasks/task/pr/refresh', headers=headers)).status_code == 202
            await asyncio.sleep(.02)
            assert calls == [3, 3]
            assert db.execute("SELECT count(*) FROM task_events WHERE type='pr.updated'").fetchone()[0] == 1
            assert canary not in '\n'.join(db.iterdump()) + str(stats)
            assert (await http.post('/api/tasks/unknown/pr/refresh', headers=headers)).status_code == 404
        db.close()
    asyncio.run(check())


@pytest.mark.parametrize('graphql', ['anonymous', 'forbidden', 'null_count'])
def test_rest_summary_paginates_votes_and_checks_and_combines_status_contexts(tmp_path, monkeypatch, graphql):
    monkeypatch.delenv('CONTRIBOS_GITHUB_TOKEN', raising=False)
    monkeypatch.setattr(Settings, 'tool', lambda *_: None)
    if graphql != 'anonymous':
        monkeypatch.setenv('CONTRIBOS_GITHUB_TOKEN', secrets.token_urlsafe(32))

    def github(request):
        assert ('Authorization' in request.headers) == (graphql != 'anonymous')
        if request.url.path == '/graphql':
            assert request.method == 'POST'
            if graphql == 'forbidden':
                return httpx.Response(200, json={'errors': [{'type': 'FORBIDDEN'}]})
            return httpx.Response(200, json={'data': {'repository': {'pullRequest': {**SUMMARY, 'totalCommentsCount': None}}}})
        assert request.method == 'GET'
        path, page = request.url.path, request.url.params.get('page')
        if path.endswith('/pulls/3'):
            result = {'head': {'sha': SHA}, 'title': 'T', 'state': 'open', 'draft': True, 'merged_at': None,
                      'created_at': NOW, 'comments': 2, 'review_comments': 3, 'base': {'repo': {'language': 'Python'}}}
        elif path.endswith('/reviews'):
            result = ([{'state': 'COMMENTED', 'user': {'login': 'A'}}] * 98
                      + [{'state': 'CHANGES_REQUESTED', 'user': {'login': 'B'}},
                         {'state': 'APPROVED', 'user': {'login': 'A'}}] if page == '1' else
                      [{'state': 'DISMISSED', 'user': {'login': 'B'}}])
        elif path.endswith('/status'):
            result = {'state': 'pending', 'total_count': 0}
        else:
            assert path.endswith('/check-runs')
            result = {'check_runs': [{'status': 'completed', 'conclusion': 'success'}] * 100 if page == '1'
                      else [{'status': 'completed', 'conclusion': 'failure'}]}
        return httpx.Response(200, json=result)

    async def check():
        async with GitHubClient(Settings(home=tmp_path), transport=httpx.MockTransport(github)).read() as reader:
            fields, meta = await _snapshot(reader, 'owner/test', 3)
            assert fields['review_decision'] == 'APPROVED' and fields['checks_state'] == 'FAILURE'
            assert meta['review_source'] == 'reviews' and fields['comments_count'] == 5
    asyncio.run(check())


def test_background_failure_and_stale_refresh_leave_published_values_intact(tmp_path):
    gate = asyncio.Event()

    class Reader:
        @asynccontextmanager
        async def read(self):
            yield self

        async def pull_summary(self, *_):
            await gate.wait()
            return SUMMARY

    async def check():
        db = connect(tmp_path / 'db')
        migrate(db)
        task_pr(db)
        store = TaskStore(db)
        reader = Reader()
        service = PRTracker(store, reader, TaskEvents(store))
        service.request(task_id='task')
        service.request(task_id='task')
        assert len(service.jobs) == 1
        await asyncio.sleep(0)
        db.execute("UPDATE pull_requests SET head_sha=? WHERE task_id='task'", ('b' * 40,))
        gate.set()
        await asyncio.gather(*service.jobs.values())
        assert db.execute('SELECT state, head_sha FROM pull_requests').fetchone()[:] == ('open', 'b' * 40)
        assert not db.execute('SELECT * FROM task_events').fetchone()

        async def fail(*_):
            raise GitHubError('github_rate_limited', 'Anonymous GitHub API rate limit reached.')
        reader.pull_summary = fail
        service.request(task_id='task')
        await asyncio.gather(*service.jobs.values())
        assert service.stats()['pull_requests'][0]['error'] == 'Anonymous GitHub API rate limit reached.'
        assert db.execute('SELECT last_synced_at FROM pull_requests').fetchone()[0] == NOW
        await service.close()
        db.close()
    asyncio.run(check())


@pytest.mark.parametrize('history', ['no_snapshot', 'complete_snapshot', 'older_import', 'merged_import'])
def test_deleting_published_task_retains_history_during_refresh(tmp_path, history):
    from contribos.agent.fake import FakeEngine
    from test_task_routes import application
    from test_task_supervisor import until

    async def check():
        app, headers = application(tmp_path, FakeEngine())
        fetched, release = asyncio.Event(), asyncio.Event()

        class Reader:
            @asynccontextmanager
            async def read(self):
                yield self

            async def pull_summary(self, *_):
                fetched.set()
                await release.wait()
                return SUMMARY

        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=headers) as http:
            db = app.state.db
            tracker = app.state.supervisor.tracker
            tracker.github = Reader()
            task_pr(db)
            db.execute("""INSERT INTO publications(id,task_id,kind,preview_json,preview_hash,status,created_at,finished_at)
                VALUES('pub','task','create_pr','{"title":"Published title"}','hash','succeeded',?,?)""", (NOW, NOW))
            db.execute("""INSERT INTO repositories VALUES('owner/test','{"language":"TypeScript"}',?)""", (NOW,))
            merged = history == 'merged_import'
            if history == 'complete_snapshot':
                db.execute("UPDATE pull_requests SET data_json=?", (json.dumps({'title': 'Published title',
                    'created_at': NOW, 'language': 'TypeScript', 'has_bounty': True}),))
            if history in {'older_import', 'merged_import'}:
                db.execute("""INSERT INTO pull_requests(task_id,repo_full_name,number,url,state,draft,merged,head_sha,
                    last_synced_at,data_json) VALUES(NULL,'Owner/Test',3,'https://github.com/Owner/Test/pull/3',
                    'closed',0,?,?,?,?)""", (int(merged), 'c' * 40, MERGED if merged else '2026-10-01T10:30:00Z',
                                             json.dumps({'title': 'Merged title', 'merged_at': MERGED} if merged else {})))
            tracker.request(task_id='task')
            await fetched.wait()
            polling = list(tracker.jobs.values())
            assert (await http.delete('/api/tasks/task')).status_code == 202
            await until(lambda: not db.execute('SELECT 1 FROM tasks').fetchone())
            release.set()
            await asyncio.gather(*polling)
            stats = (await http.get('/api/stats')).json()
            pr, = stats['pull_requests']
            assert pr['task_id'] is None and pr['has_bounty'] and pr['language'] == 'TypeScript'
            assert pr['title'] == ('Merged title' if merged else 'Published title')
            assert pr['state'] == ('closed' if merged else 'open') and bool(pr['merged']) == merged
            assert stats['months']['2026-11']['submitted'] == 1 and stats['days'] == {'2026-11-01': 1}
            if merged:
                assert stats['months']['2026-12']['merged'] == 1
            assert not db.execute('SELECT 1 FROM publications').fetchone()
            # A fresh read of the detached row can still track its remote PR.
            tracker.request(pr_id=pr['id'])
            await asyncio.gather(*tracker.jobs.values())
            assert tracker.stats()['pull_requests'][0]['merged']
            assert not db.execute('SELECT 1 FROM task_events').fetchone()

    asyncio.run(check())
