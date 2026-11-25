"""Atomic daily recommendations and durable progress for host-side scans."""

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from contribos.discovery.domain import DiscoveryRules, IssueFacts, RepositoryFacts, SelectionCandidate
from contribos.discovery.scoring import filter_issue, score_issue, select_daily_opportunities
from contribos.github import GitHubClient, GitHubError, GitHubReader
from contribos.tasks.issue import linked_pull_requests, parse_issue_url
from contribos.tasks.store import TaskError, TaskStore, utc_now

DEFAULT_QUERIES = ('label:bounty', 'label:"good first issue"', 'label:"help wanted"')
PUBLIC_OPEN = 'is:public is:issue is:open no:assignee archived:false'
SMALL_LABELS = {'good first issue', 'size: xs', 'size: s', 'size/xs', 'size/s', 'small', 'trivial'}


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def repository_facts(name: str, repo: dict, community: dict) -> RepositoryFacts:
    return RepositoryFacts(name, repo.get('description') or '', repo.get('language'),
        (repo.get('license') or {}).get('spdx_id'), repo.get('stargazers_count', 0),
        repo.get('forks_count', 0), bool(repo.get('archived')), bool(repo.get('disabled')),
        tuple(repo.get('topics') or ()), timestamp(repo['pushed_at']) if repo.get('pushed_at') else None,
        bool((community.get('files') or {}).get('contributing')), community.get('health_percentage'))


def issue_facts(item: dict, queries: list[str], repo: RepositoryFacts) -> IssueFacts:
    url = f"https://github.com/{repo.full_name}/issues/{item['number']}"
    return IssueFacts(item['id'], item['number'], item['title'], item.get('body') or '', url,
        item['state'], tuple(label['name'] for label in item.get('labels', [])), item.get('comments', 0),
        len(item.get('assignees') or []), item.get('author_association'), timestamp(item['created_at']),
        timestamp(item['updated_at']), tuple(queries), repo)


def repo_json(repo: RepositoryFacts) -> dict:
    return {**asdict(repo), 'pushed_at': repo.pushed_at.isoformat() if repo.pushed_at else None}


async def has_linked_pr(reader: GitHubReader, repo: str, number: int) -> bool:
    page = 1
    while True:
        events = await reader.get(f'/repos/{repo}/issues/{number}/timeline', cached=True,
                                  params={'per_page': 100, 'page': page})
        if not isinstance(events, list):
            raise GitHubError('github_invalid_response')
        if linked_pull_requests(events):
            return True
        if len(events) < 100:
            return False
        page += 1


async def external_pr_outcomes(reader: GitHubReader, repo: str, now: datetime) -> tuple[int, int]:
    cutoff = now - timedelta(days=90)
    page, closed, merged = 1, 0, 0
    while True:
        pulls = await reader.get(f'/repos/{repo}/pulls', cached=True, params={
            'state': 'closed', 'sort': 'updated', 'direction': 'desc', 'per_page': 100, 'page': page})
        if not isinstance(pulls, list):
            raise GitHubError('github_invalid_response')
        for pr in pulls:
            if timestamp(pr['updated_at']) < cutoff:
                return closed, merged
            if not cutoff <= timestamp(pr['closed_at']) <= now:
                continue
            if (pr.get('author_association') in {'FIRST_TIMER', 'FIRST_TIME_CONTRIBUTOR', 'CONTRIBUTOR', 'NONE'}
                    and (pr.get('user') or {}).get('type') == 'User'):
                closed += 1
                merged += int(bool(pr.get('merged_at')))
        if len(pulls) < 100:
            return closed, merged
        page += 1


class DiscoveryService:
    def __init__(self, store: TaskStore, github: GitHubClient) -> None:
        self.store, self.github = store, github
        self.db = store.db
        self.job: asyncio.Task | None = None
        self.scheduler: asyncio.Task | None = None
        self.notify_ready: Callable[[], None] | None = None

    def schedule(self) -> None:
        self.scheduler = asyncio.create_task(self._schedule_loop())

    async def _schedule_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            self.scheduled_scan(datetime.now().astimezone())

    def scheduled_scan(self, local_time: datetime) -> None:
        day = local_time.date().isoformat()
        if (self.store.setting('auto_scan_enabled', True)
                and local_time.strftime('%H:%M') == self.store.setting('scan_time', '09:00')
                and self.store.setting('last_scheduled_scan_day') != day):
            self.start()
            self.store.set_setting('last_scheduled_scan_day', day)

    def recover(self) -> None:
        self.db.execute("""UPDATE scan_runs SET status='interrupted', finished_at=?, error_code='service_restart'
                           WHERE status='running'""", (utc_now(),))

    def start(self, repository: str | None = None) -> dict:
        with self.store.transaction():
            row = self.db.execute("SELECT id FROM scan_runs WHERE status='running'").fetchone()
            if row:
                return self.scan(row['id'])
            run_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc)
            self.db.execute("INSERT INTO scan_runs(id,status,started_at) VALUES (?,'running',?)",
                            (run_id, now.isoformat()))
        self.job = asyncio.create_task(self._run(run_id, repository, now))
        return self.scan(run_id)

    async def close(self) -> None:
        if self.scheduler:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)
        if self.job and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        self.recover()

    def scan(self, run_id: str) -> dict:
        row = self.db.execute('SELECT * FROM scan_runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise TaskError('scan_not_found', 'This scan does not exist.', 404)
        result = dict(row)
        result['stats'] = json.loads(result.pop('stats_json'))
        return result

    async def _repository(self, reader: GitHubReader, name: str, now: datetime) -> tuple[RepositoryFacts, bool]:
        cached = self.db.execute('SELECT * FROM repositories WHERE full_name=?', (name,)).fetchone()
        previous = None
        if cached:
            data = json.loads(cached['data_json'])
            if 'has_contributing_guide' in data:
                data['pushed_at'] = timestamp(data['pushed_at']) if data['pushed_at'] else None
                previous = RepositoryFacts(**data)
                if not previous.sync_error and now - timestamp(cached['fetched_at']) < timedelta(days=1):
                    return previous, False
        path = f'/repos/{name}'
        try:
            repo = await reader.get(path, cached=True)
            if repo.get('private') is not False:
                raise GitHubError('github_invalid_repository')
        except GitHubError as error:
            if error.code in {'github_rate_limited', 'github_invalid_repository'}:
                raise
            return replace(previous or repository_facts(name, {}, {}), sync_error=error.code), True
        try:
            community = await reader.get(f'{path}/community/profile', cached=True)
        except GitHubError as error:
            if error.code == 'github_rate_limited':
                raise
            community = {}
        result = repository_facts(name, repo, community)
        try:
            closed, merged = await external_pr_outcomes(reader, name, now)
            result = replace(result, external_prs_closed=closed, external_prs_merged=merged)
        except GitHubError as error:
            if error.code == 'github_rate_limited':
                raise
        return result, True

    def _save_repository(self, repo: RepositoryFacts, now: datetime) -> None:
        self.db.execute("""INSERT INTO repositories VALUES (?,?,?) ON CONFLICT(full_name) DO UPDATE SET
            data_json=excluded.data_json, fetched_at=excluded.fetched_at""",
            (repo.full_name, self.store.encode(repo_json(repo)), now.isoformat()))

    def _save_issue(self, issue: IssueFacts, now: datetime, rules: DiscoveryRules) -> SelectionCandidate | None:
        score = score_issue(issue, rules, now)
        decision = filter_issue(issue, rules, now)
        encode = self.store.encode
        data = {'source_queries': issue.source_queries, 'body': issue.body, 'updated_at': issue.updated_at.isoformat(),
                'has_linked_pr': issue.has_linked_pr,
                'small_change': bool(SMALL_LABELS.intersection(label.lower() for label in issue.labels))
                                and 'scope_may_be_too_large' not in score.risk_reasons}
        row = self.db.execute("""INSERT INTO opportunities(id,repo_full_name,issue_number,title,url,state,
            labels_json,data_json,score_total,score_json,reasons_json,filtered_out,filter_reasons_json,
            first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT
            DO UPDATE SET repo_full_name=excluded.repo_full_name,issue_number=excluded.issue_number,title=excluded.title,url=excluded.url,state=excluded.state,labels_json=excluded.labels_json,
            data_json=excluded.data_json,score_total=excluded.score_total,score_json=excluded.score_json,
            reasons_json=excluded.reasons_json,filtered_out=excluded.filtered_out,
            filter_reasons_json=excluded.filter_reasons_json,last_seen_at=excluded.last_seen_at RETURNING id""",
            (str(issue.github_issue_id), issue.repository.full_name, issue.number, self.store.clean(issue.title),
             issue.html_url, issue.state, encode(issue.labels), encode(data), score.total, encode(asdict(score)),
             encode(score.risk_reasons), int(not decision.eligible), encode(decision.reasons),
             now.isoformat(), now.isoformat())).fetchone()
        if decision.eligible:
            return SelectionCandidate(row['id'], score.total, score.components['project_impact'],
                                      score.has_bounty, score.is_strategic, score.is_tech_match)
        return None

    async def _run(self, run_id: str, repository: str | None, now: datetime) -> None:
        terms = ('repo:' + repository,) if repository else DEFAULT_QUERIES
        rules = DiscoveryRules(
            preferred_languages=tuple(self.store.setting('preferred_languages', DiscoveryRules().preferred_languages)),
            strategic_keywords=tuple(self.store.setting('strategic_keywords', DiscoveryRules().strategic_keywords)))
        stats = dict(queries=0, queries_total=len(terms), candidates=0, processed=0, eligible=0, selected=0)

        def progress() -> None:
            self.db.execute("UPDATE scan_runs SET stats_json=? WHERE id=? AND status='running'",
                            (self.store.encode(stats), run_id))

        try:
            by_id: dict[int, tuple[dict, list[str]]] = {}
            repos: dict[str, RepositoryFacts] = {}
            refreshed = []
            issues = []
            async with self.github.read() as reader:
                for term in terms:
                    for item in await reader.search_issues(f'{PUBLIC_OPEN} {term}', 12):
                        if 'pull_request' not in item:
                            if type(item.get('id')) is not int or item['id'] <= 0 or item.get('state') not in {'open', 'closed'}:
                                raise GitHubError('github_invalid_response')
                            by_id.setdefault(item['id'], (item, []))[1].append(term)
                    stats.update(queries=stats['queries'] + 1, candidates=len(by_id))
                    progress()
                for item, queries in by_id.values():
                    ref = parse_issue_url(item['html_url'])
                    if (ref.number != item['number'] or self.store.clean(ref.repo) != ref.repo
                            or ref.number > 2**63 - 1):
                        raise GitHubError('github_invalid_response')
                    if ref.repo not in repos:
                        repo, changed = await self._repository(reader, ref.repo, now)
                        repos[ref.repo] = repo
                        if changed:
                            refreshed.append(repo)
                    issue = issue_facts(item, queries, repos[ref.repo])
                    if filter_issue(issue, rules, now).eligible:
                        try:
                            linked = await has_linked_pr(reader, ref.repo, ref.number)
                            issue = replace(issue, has_linked_pr=linked)
                        except GitHubError as error:
                            if error.code == 'github_rate_limited':
                                raise
                    issues.append(issue)
                    stats['processed'] += 1
                    progress()
            with self.store.transaction():
                ignored = {r[0] for r in self.db.execute("SELECT opportunity_id FROM opportunity_marks WHERE mark='ignored'")}
                candidates = []
                for repo in refreshed:
                    self._save_repository(repo, now)
                for issue in issues:
                    candidate = self._save_issue(issue, now, rules)
                    if candidate and candidate.opportunity_id not in ignored:
                        candidates.append(candidate)
                picks = select_daily_opportunities(candidates)
                day = now.astimezone().date().isoformat()
                self.db.execute('DELETE FROM daily_picks WHERE day=?', (day,))
                self.db.executemany('INSERT INTO daily_picks VALUES (?,?,?,?)',
                    ((day, rank, p.opportunity_id, p.selection_reason) for rank, p in enumerate(picks, 1)))
                stats.update(eligible=len(candidates), selected=len(picks))
                self.db.execute("""UPDATE scan_runs SET status='completed',finished_at=?,stats_json=?
                                   WHERE id=? AND status='running'""", (utc_now(), self.store.encode(stats), run_id))
        except Exception as error:
            code = error.code if isinstance(error, GitHubError) else 'scan_failed'
            self.db.execute("""UPDATE scan_runs SET status='failed',finished_at=?,error_code=?
                               WHERE id=? AND status='running'""", (utc_now(), code, run_id))
            return
        if picks and self.notify_ready:
            self.notify_ready()

    def opportunities(self, *, view: str = 'all', lang: str = '', label: str = '', q: str = '',
                      bounty: bool | None = None, small: bool = False, sort: str = 'score',
                      offset: int = 0, limit: int = 50, opportunity_id: str | None = None) -> list[dict]:
        source = """ FROM opportunities o JOIN repositories r ON r.full_name=o.repo_full_name
                     LEFT JOIN opportunity_marks m ON m.opportunity_id=o.id"""
        conditions, params = [], []
        bucket = 'NULL'
        order = {'score': 'o.score_total DESC', 'updated': "json_extract(o.data_json,'$.updated_at') DESC",
                 'stars': "json_extract(r.data_json,'$.stars') DESC"}[sort]
        if opportunity_id is not None:
            conditions.append('o.id=?')
            params.append(opportunity_id)
        else:
            conditions.append('o.filtered_out=0')
            if view in {'starred', 'ignored'}:
                conditions.append('m.mark=?')
                params.append(view)
            else:
                conditions.append("coalesce(m.mark,'')!='ignored'")
            if view == 'daily':
                source += ' JOIN daily_picks p ON p.opportunity_id=o.id'
                conditions.append('p.day=?')
                params.append(datetime.now().date().isoformat())
                bucket, order = 'p.bucket', 'p.rank'
            if lang:
                conditions.append("json_extract(r.data_json,'$.language')=? COLLATE NOCASE")
                params.append(lang)
            if label:
                conditions.append('EXISTS (SELECT 1 FROM json_each(o.labels_json) WHERE value=? COLLATE NOCASE)')
                params.append(label)
            if q:
                conditions.append("instr(lower(o.title || ' ' || o.repo_full_name),lower(?))>0")
                params.append(q)
            if bounty is not None:
                conditions.append("json_extract(o.score_json,'$.has_bounty')=?")
                params.append(bounty)
            if small:
                conditions.append("json_extract(o.data_json,'$.small_change')=1")
        rows = self.db.execute(f"""SELECT o.*,r.data_json AS repo_json,m.mark,{bucket} AS bucket {source}
            WHERE {' AND '.join(conditions)} ORDER BY {order},o.id LIMIT ? OFFSET ?""", (*params, limit, offset))
        results = []
        for row in rows:
            data = json.loads(row['data_json'])
            if opportunity_id is None:
                data.pop('body', None)
            results.append({**data, **{k: row[k] for k in ('id','repo_full_name','issue_number','title','url','state',
                                                           'mark','score_total','bucket')},
                            'score': json.loads(row['score_json']), 'labels': json.loads(row['labels_json']),
                            'repository': json.loads(row['repo_json'])})
        return results

    def detail(self, opportunity_id: str) -> dict:
        rows = self.opportunities(opportunity_id=opportunity_id)
        if not rows:
            raise TaskError('opportunity_not_found', 'This opportunity does not exist.', 404)
        return rows[0]

    def mark(self, opportunity_id: str, mark: str | None) -> dict:
        self.store.opportunity(opportunity_id)
        if mark is None:
            self.db.execute('DELETE FROM opportunity_marks WHERE opportunity_id=?', (opportunity_id,))
        else:
            self.db.execute("""INSERT INTO opportunity_marks(opportunity_id,mark,updated_at) VALUES (?,?,?)
                ON CONFLICT(opportunity_id) DO UPDATE SET mark=excluded.mark,updated_at=excluded.updated_at""",
                (opportunity_id, mark, utc_now()))
        return self.detail(opportunity_id)
