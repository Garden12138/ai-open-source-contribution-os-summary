"""Read-only PR polling and the local contribution history."""

import asyncio
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from contribos.agent.engine import EngineEvent
from contribos.github import GitHubClient, GitHubError, GitHubReader
from contribos.tasks.events import TaskEvents
from contribos.tasks.issue import parse_issue_url
from contribos.tasks.store import TaskError, TaskStore, contribution_metadata, utc_now

INTERVAL = 30 * 60


def _utc(value: str) -> str:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('Missing timezone')
    return stamp.astimezone(timezone.utc).isoformat()


async def _public_summary(reader: GitHubReader, repo: str, number: int) -> dict:
    prefix = f'/repos/{repo}'
    path = f'{prefix}/pulls/{number}'
    pull = await reader.get(path, cached=True)
    sha = pull['head']['sha']
    if not re.fullmatch('(?:[0-9a-f]{40}|[0-9a-f]{64})', sha):
        raise GitHubError('github_invalid_response')
    votes = {}
    async for review in reader.pages(path + '/reviews'):
        author = (review.get('user') or {}).get('login')
        if author and review['state'] in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'}:
            votes[author] = review['state']
    decision = next((vote for vote in ('CHANGES_REQUESTED', 'APPROVED') if vote in votes.values()), None)
    status = await reader.get(f'{prefix}/commits/{sha}/status', params={'per_page': 1}, cached=True)
    states = {status['state'].upper()} if status['total_count'] else set()
    async for run in reader.pages(f'{prefix}/commits/{sha}/check-runs', 'check_runs'):
        if run['status'] != 'completed':
            states.add('PENDING')
        elif run['conclusion'] in {'success', 'skipped', 'neutral'}:
            states.add('SUCCESS')
        else:
            states.add('FAILURE')
    checks = next((s for s in ('ERROR', 'FAILURE', 'PENDING', 'SUCCESS') if s in states), None)
    return {'title': pull['title'], 'state': pull['state'].upper(), 'isDraft': pull['draft'],
            'mergedAt': pull['merged_at'], 'createdAt': pull['created_at'], 'headRefOid': sha,
            'reviewDecision': decision, 'totalCommentsCount': pull['comments'] + pull['review_comments'],
            'statusCheckRollup': {'state': checks}, 'review_source': 'reviews',
            'repository': {'primaryLanguage': {'name': pull['base']['repo'].get('language')}}}


async def _snapshot(reader: GitHubReader, repo: str, number: int) -> tuple[dict, dict]:
    parse_issue_url(f'https://github.com/{repo}/issues/{number}')
    try:
        pr = await reader.pull_summary(repo, number)
        if pr is None or pr['totalCommentsCount'] is None:
            pr = await _public_summary(reader, repo, number)
        checks = (pr['statusCheckRollup'] or {}).get('state')
        language = (pr['repository']['primaryLanguage'] or {}).get('name')
        if (pr['state'] not in {'OPEN', 'CLOSED', 'MERGED'} or type(pr['isDraft']) is not bool
                or not re.fullmatch('(?:[0-9a-f]{40}|[0-9a-f]{64})', pr['headRefOid'])
                or not isinstance(pr['title'], str) or language is not None and not isinstance(language, str)
                or checks not in {None, 'EXPECTED', 'PENDING', 'SUCCESS', 'FAILURE', 'ERROR'}
                or pr['reviewDecision'] not in {None, 'REVIEW_REQUIRED', 'APPROVED', 'CHANGES_REQUESTED'}
                or pr['state'] == 'MERGED' and not pr['mergedAt']
                or type(pr['totalCommentsCount']) is not int or not 0 <= pr['totalCommentsCount'] < 2**63):
            raise ValueError()
        fields = {'state': 'open' if pr['state'] == 'OPEN' else 'closed',
                  'draft': pr['isDraft'], 'merged': bool(pr['mergedAt']), 'head_sha': pr['headRefOid'],
                  'review_decision': pr['reviewDecision'], 'checks_state': checks,
                  'comments_count': pr['totalCommentsCount']}
        metadata = {'title': pr['title'], 'created_at': _utc(pr['createdAt']),
                    'merged_at': _utc(pr['mergedAt']) if pr['mergedAt'] else None, 'language': language,
                    'review_source': pr.get('review_source', 'github')}
        return fields, metadata
    except (TypeError, AttributeError, KeyError, ValueError):
        raise GitHubError('github_invalid_response') from None


class PRTracker:
    def __init__(self, store: TaskStore, github: GitHubClient, events: TaskEvents) -> None:
        self.store, self.github, self.events = store, github, events
        self.jobs: dict[int, asyncio.Task] = {}
        self.errors: dict[int, str] = {}
        self.slots = asyncio.Semaphore(2)
        self.scheduler: asyncio.Task | None = None

    def schedule(self) -> None:
        self.scheduler = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            for row in self.store.db.execute("SELECT * FROM pull_requests WHERE state='open' AND merged=0"):
                self.start(row)
            await asyncio.sleep(INTERVAL)

    def start(self, row: sqlite3.Row) -> None:
        if row['id'] not in self.jobs:
            self.errors.pop(row['id'], None)
            self.jobs[row['id']] = asyncio.create_task(self._refresh(row))

    async def _refresh(self, row: sqlite3.Row) -> None:
        try:
            async with self.slots, self.github.read() as reader:
                fields, updates = await _snapshot(reader, row['repo_full_name'], row['number'])
            old = json.loads(row['data_json'])
            metadata = {**old, **updates}
            if updates['language'] is None and old.get('language'):
                metadata['language'] = old['language']
            changed = metadata != old or any(row[k] != v for k, v in fields.items())
            with self.store.transaction():
                # Publishing or deleting a task can change this record while a read is in flight.
                applied = self.store.db.execute('''UPDATE pull_requests SET state=:state, draft=:draft, merged=:merged,
                    head_sha=:head_sha, review_decision=:review_decision, checks_state=:checks_state,
                    comments_count=:comments_count, data_json=:metadata, last_synced_at=:now
                    WHERE id=:id AND task_id IS :previous_task AND repo_full_name=:repo AND number=:number
                      AND last_synced_at=:previous AND head_sha=:previous_head
                      AND draft=:previous_draft AND data_json=:previous_metadata''',
                    {**fields, 'metadata': self.store.encode(metadata), 'now': utc_now(), 'id': row['id'],
                     'repo': row['repo_full_name'], 'number': row['number'],
                     'previous_task': row['task_id'], 'previous': row['last_synced_at'], 'previous_head': row['head_sha'],
                     'previous_draft': row['draft'], 'previous_metadata': row['data_json']}).rowcount
                if applied and changed and row['task_id']:
                    self.events.publish(row['task_id'], None, EngineEvent('pr.updated', fields))
        except GitHubError as error:
            self.errors[row['id']] = error.message
        finally:
            self.jobs.pop(row['id'], None)

    async def close(self) -> None:
        pending = [*self.jobs.values()]
        if self.scheduler:
            pending.append(self.scheduler)
        for job in pending:
            job.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def request(self, *, task_id: str | None = None, pr_id: int | None = None) -> None:
        row = self.store.db.execute('SELECT * FROM pull_requests WHERE task_id=? OR id=?',
                                    (task_id, pr_id)).fetchone()
        if row is None:
            raise TaskError('pr_not_found', 'This pull request is not tracked.', 404)
        self.start(row)

    def stats(self) -> dict[str, Any]:
        items, seen = [], set()
        months = defaultdict(lambda: {'submitted': 0, 'merged': 0})
        days, repos, languages = Counter(), Counter(), Counter()
        for row in self.store.contributions():
            identity = (row['repo_full_name'].casefold(), row['number'])
            if identity in seen:
                continue
            seen.add(identity)
            data = contribution_metadata(row)
            created = _utc(data['created_at']) if data['created_at'] else None
            merged = _utc(data['merged_at']) if row['merged'] and data.get('merged_at') else None
            language = data['language']
            repos[row['repo_full_name']] += 1
            languages[language or 'Unknown'] += 1
            for key, timestamp in (('submitted', created), ('merged', merged)):
                if timestamp:
                    day = timestamp[:10]
                    months[day[:7]][key] += 1
                    if key == 'submitted':
                        days[day] += 1
            items.append({**{k: row[k] for k in ('id', 'task_id', 'repo_full_name', 'number', 'url', 'state',
                          'draft', 'merged', 'checks_state', 'review_decision', 'comments_count', 'last_synced_at')},
                          'title': data['title'],
                          'review_source': data.get('review_source'), 'language': language, 'created_at': created,
                          'merged_at': merged, 'has_bounty': data['has_bounty'],
                          'refreshing': row['id'] in self.jobs, 'error': self.errors.get(row['id'])})
        items.sort(key=lambda item: item['created_at'] or '', reverse=True)
        return self.store.sanitize({'pull_requests': items, 'months': months, 'days': days,
                                    'repositories': repos, 'languages': languages})
