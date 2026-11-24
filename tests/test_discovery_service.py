import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from contribos.db import connect, migrate
from contribos.discovery.service import DiscoveryService
from contribos.github import GitHubError
from contribos.tasks.store import TaskStore


class FakeGitHub:
    def __init__(self):
        self.repo_calls = []
        self.query_calls = 0
        self.fail_repo = self.fail_search = False
        self.title = 'A small fix'
        self.now = datetime.now(timezone.utc).isoformat()

    @asynccontextmanager
    async def read(self):
        yield self

    async def search_issues(self, query, limit):
        if self.fail_search:
            raise GitHubError('github_rate_limited')
        self.query_calls += 1
        numbers = [1, 2] if query.endswith('label:bounty') else [1, 3]
        return [dict(id=i, number=i, html_url=f'https://github.com/acme/sample/issues/{i}', state='open',
                     title=self.title if i == 2 else f'Issue {i}', body='Steps to reproduce this issue. ' * 4,
                     labels=[{'name': 'good first issue'}], updated_at=self.now, created_at=self.now)
                for i in numbers]

    async def get(self, path, *, cached, params=None):
        if path.endswith(('/pulls', '/timeline')):
            return []
        if path.endswith('/community/profile'):
            return {'health_percentage': 80, 'files': {'contributing': {}}}
        self.repo_calls.append(path)
        if self.fail_repo:
            raise GitHubError('github_read_failed')
        return {'private': False, 'language': 'Python', 'license': {'spdx_id': 'MIT'}, 'pushed_at': self.now}


@pytest.fixture
def discovery(tmp_path):
    db = connect(tmp_path / 'discovery.db')
    migrate(db)
    try:
        yield DiscoveryService(TaskStore(db), FakeGitHub())
    finally:
        db.close()


async def run_scan(service):
    run = service.start()
    assert service.start()['id'] == run['id']
    await service.job
    return service.scan(run['id'])


def test_scan_deduplicates_caches_and_persists_daily_picks(discovery):
    db = discovery.db
    first = asyncio.run(run_scan(discovery))
    assert first['status'] == 'completed'
    assert first['stats']['candidates'] == first['stats']['eligible'] == first['stats']['selected'] == 3
    row = db.execute("SELECT * FROM opportunities WHERE id='1'").fetchone()
    assert len(json.loads(row['data_json'])['source_queries']) == 3
    assert [r[0] for r in db.execute('SELECT rank FROM daily_picks ORDER BY rank')] == [1, 2, 3]
    db.execute("INSERT INTO opportunity_marks VALUES ('1','ignored','','now')")
    discovery.github.title = 'Updated issue'
    second = asyncio.run(run_scan(discovery))
    assert second['stats']['selected'] == 2
    assert discovery.github.repo_calls == ['/repos/acme/sample']
    assert discovery.github.query_calls == 6
    assert db.execute("SELECT title FROM opportunities WHERE id='2'").fetchone()[0] == 'Updated issue'
    assert db.execute("SELECT first_seen_at FROM opportunities WHERE id='1'").fetchone()[0] == row['first_seen_at']
    assert db.execute("SELECT count(*) FROM opportunities").fetchone()[0] == 3


def test_metadata_retry_and_failed_scan_preserves_picks(discovery):
    discovery.github.fail_repo = True
    assert asyncio.run(run_scan(discovery))['stats']['eligible'] == 0
    discovery.github.fail_repo = False
    assert asyncio.run(run_scan(discovery))['stats']['eligible'] == 3
    assert len(discovery.github.repo_calls) == 2
    discovery.github.fail_search = True
    failed = asyncio.run(run_scan(discovery))
    assert (failed['status'], failed['error_code']) == ('failed', 'github_rate_limited')
    assert discovery.db.execute('SELECT count(*) FROM daily_picks').fetchone()[0] == 3


def test_cancelled_scan_is_recovered(discovery):
    async def run():
        scan = discovery.start()
        await discovery.close()
        assert discovery.scan(scan['id'])['status'] == 'interrupted'
    asyncio.run(run())


def test_daily_schedule_honors_local_setting_and_survives_restart(discovery):
    now = datetime.now().astimezone().replace(hour=10, minute=15)
    discovery.store.set_setting('scan_time', '10:15')
    notices = []
    discovery.notify_ready = lambda: notices.append('ready')

    async def run():
        discovery.scheduled_scan(now.replace(minute=14))
        assert discovery.job is None
        discovery.scheduled_scan(now)
        await discovery.job
        other = DiscoveryService(discovery.store, discovery.github)
        other.scheduled_scan(now)
        assert other.job is None
        assert notices == ['ready']
    asyncio.run(run())


def test_external_pr_window_and_linked_timeline_pagination():
    from datetime import timedelta
    from contribos.discovery.service import external_pr_outcomes, has_linked_pr

    now = datetime.now(timezone.utc)
    def pull(days, association, merged=True, user_type='User'):
        date = (now - timedelta(days=days)).isoformat()
        return dict(author_association=association, user={'type': user_type}, closed_at=date,
                    updated_at=now.isoformat(), merged_at=date if merged else None)

    class Reader:
        async def get(self, path, *, cached, params):
            first = params['page'] == 1
            if path.endswith('/timeline'):
                return ([{}] * 100 if first else [{'event': 'cross-referenced',
                        'source': {'issue': {'number': 7, 'pull_request': {}}}}])
            assert params['state'] == 'closed' and params['sort'] == 'updated'
            return ([pull(1, 'MEMBER')] * 100 if first else [pull(90, 'CONTRIBUTOR'), pull(0, 'FIRST_TIMER', False),
                    pull(91, 'NONE'), pull(1, 'NONE', user_type='Bot'), pull(2, 'OWNER')])

    async def run():
        assert await has_linked_pr(Reader(), 'o/r', 1)
        assert await external_pr_outcomes(Reader(), 'o/r', now) == (2, 1)
    asyncio.run(run())
