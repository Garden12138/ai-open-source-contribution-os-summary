"""Read a legacy discovery database and merge contribution history into v2."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from contribos.db import connect, migrate
from contribos.discovery.domain import RepositoryFacts
from contribos.discovery.service import SMALL_LABELS, repo_json
from contribos.sandbox.host import safe_text
from contribos.security import load_or_create_local_token
from contribos.tasks.store import TaskStore


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _repository(name: str, store: TaskStore) -> str:
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', name)
            or any(part in {'.', '..'} for part in name.split('/')) or store.clean(name) != name):
        raise ValueError('Invalid repository name.')
    return name


def _repositories(source: sqlite3.Connection, store: TaskStore) -> int:
    count = 0
    for row in source.execute('SELECT * FROM repositories'):
        name = _repository(row['full_name'], store)
        if store.db.execute('SELECT 1 FROM repositories WHERE full_name=? COLLATE NOCASE', (name,)).fetchone():
            continue
        repo = RepositoryFacts(name, row['description'] or '', row['language'],
            row['license_spdx'], row['stars'], row['forks'], bool(row['archived']), bool(row['disabled']),
            tuple(json.loads(row['topics'])), datetime.fromisoformat(_utc(row['pushed_at'])) if row['pushed_at'] else None,
            bool(row['has_contributing_guide']), row['health_percentage'])
        count += store.db.execute('INSERT INTO repositories VALUES (?,?,?) ON CONFLICT DO NOTHING',
            (repo.full_name, store.encode(repo_json(repo)), _utc(row['last_synced_at']))).rowcount
    return count


def _opportunities(source: sqlite3.Connection, store: TaskStore) -> tuple[int, dict[int, str]]:
    count, ids = 0, {}
    for row in source.execute('''SELECT o.*, r.full_name FROM opportunities o
                                 JOIN repositories r ON r.id=o.repository_id'''):
        repo = store.db.execute('SELECT full_name FROM repositories WHERE full_name=? COLLATE NOCASE',
                                (row['full_name'],)).fetchone()[0]
        number = row['issue_number']
        labels = json.loads(row['labels'])
        reasons = json.loads(row['risk_reasons'])
        score = {key: bool(row[key]) for key in ('has_bounty', 'is_strategic', 'is_tech_match')}
        score.update(total=row['score_total'], components=json.loads(row['score_components']),
                     risk_penalty=row['risk_penalty'], risk_reasons=reasons, bounty_amount_usd=row['bounty_amount_usd'])
        data = {'body': row['body'], 'source_queries': json.loads(row['source_queries']),
                'updated_at': _utc(row['issue_updated_at']), 'has_linked_pr': None,
                'small_change': bool(SMALL_LABELS.intersection(label.lower() for label in labels))
                                and 'scope_may_be_too_large' not in reasons}
        count += store.db.execute('''INSERT INTO opportunities(id,repo_full_name,issue_number,title,url,state,
            labels_json,data_json,score_total,score_json,reasons_json,filtered_out,filter_reasons_json,
            first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING''',
            (str(int(row['github_issue_id'])), repo, number, store.clean(row['title']),
             f'https://github.com/{repo}/issues/{number}', store.clean(row['state']), store.encode(labels),
             store.encode(data), row['score_total'], store.encode(score), store.encode(reasons), int(not row['eligible']),
             store.encode(json.loads(row['filter_reasons'])), _utc(row['first_seen_at']), _utc(row['last_seen_at']))).rowcount
        saved = store.db.execute('SELECT id FROM opportunities WHERE id=? OR (repo_full_name=? AND issue_number=?)',
                                 (str(row['github_issue_id']), repo, number)).fetchall()
        if len(saved) != 1:
            raise ValueError('Conflicting opportunity identities.')
        ids[row['id']] = saved[0][0]
    return count, ids


def _preferences(source: sqlite3.Connection, store: TaskStore) -> int:
    row = source.execute('SELECT * FROM user_preference_versions ORDER BY version DESC LIMIT 1').fetchone()
    if not row:
        return 0
    values = {key: row[key] for key in ('primary_goal', 'weekly_hours', 'minimum_bounty_usd')}
    values.update(preferred_languages=json.loads(row['preferred_languages']), scan_time=row['auto_scan_local_time'],
                  auto_scan_enabled=bool(row['auto_scan_enabled']))
    return sum(store.db.execute('INSERT INTO settings VALUES (?,?,?) ON CONFLICT DO NOTHING',
               (key, store.encode(value), _utc(row['created_at']))).rowcount for key, value in values.items())


def _marks(source: sqlite3.Connection, store: TaskStore, ids: dict[int, str]) -> int:
    count = 0
    latest = source.execute('''SELECT *, row_number() OVER
        (PARTITION BY opportunity_id ORDER BY sequence DESC) AS rank FROM opportunity_disposition_versions''')
    for row in latest:
        mark = {'shortlisted': 'starred', 'dismissed': 'ignored'}.get(row['state'])
        if row['rank'] == 1 and mark and row['opportunity_id'] in ids:
            count += store.db.execute('INSERT INTO opportunity_marks VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                (ids[row['opportunity_id']], mark, store.clean(row['reason_code'] or ''), _utc(row['created_at']))).rowcount
    return count


def _pull_requests(source: sqlite3.Connection, store: TaskStore, tables: set[str]) -> int:
    count = 0
    for row in source.execute('''SELECT pr.*, p.title, p.upstream_repository, o.has_bounty, r.language
        FROM draft_pull_requests pr JOIN publish_intents p ON p.id=pr.publish_intent_id
        JOIN contribution_tasks t ON t.id=pr.task_id JOIN opportunities o ON o.id=t.opportunity_id
        JOIN repositories r ON r.id=o.repository_id WHERE pr.provider='github' ORDER BY pr.created_at'''):
        repo, number = _repository(row['upstream_repository'], store), row['number']
        url = f'https://github.com/{repo}/pull/{number}'
        if url.casefold() != row['html_url'].rstrip('/').casefold():
            raise ValueError('Invalid published PR URL.')
        if store.db.execute('SELECT 1 FROM pull_requests WHERE repo_full_name=? COLLATE NOCASE AND number=?',
                            (repo, number)).fetchone():
            continue
        event = source.execute("""SELECT event_type,occurred_at FROM pull_request_events
            WHERE draft_pull_request_id=? AND event_type IN ('closed','merged','opened')
            ORDER BY occurred_at DESC, rowid DESC LIMIT 1""", (row['id'],)).fetchone() if 'pull_request_events' in tables else None
        state = event[0] if event else 'opened'
        created = _utc(row['created_at'])
        data = {'title': row['title'], 'created_at': created, 'language': row['language'],
                'has_bounty': bool(row['has_bounty']), 'merged_at': _utc(event[1]) if state == 'merged' else None}
        count += store.db.execute('''INSERT INTO pull_requests(repo_full_name,number,url,state,draft,merged,
            head_sha,last_synced_at,data_json) VALUES (?,?,?,?,?,?,?,?,?)''', (repo, number, url,
            'open' if state == 'opened' else 'closed', int(state == 'opened'), int(state == 'merged'),
            store.clean(row['head_commit_sha']), _utc(event[1]) if event else created, store.encode(data))).rowcount
    return count


def import_legacy(path: Path, destination: Path) -> dict[str, int]:
    path = path.expanduser().resolve(strict=True)
    if destination.exists() and path.samefile(destination):
        raise ValueError('Select a separate legacy database.')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute('PRAGMA trusted_schema=OFF')
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'repositories', 'opportunities'} <= tables or 'github_issue_id' not in {
                row['name'] for row in source.execute('PRAGMA table_info(opportunities)')}:
            raise ValueError('Unrecognized legacy schema.')
        token_path = destination.with_name('local-token')
        token = load_or_create_local_token(token_path) if token_path.exists() else None

        def clean(text: str) -> str:
            return safe_text(text.replace(token, '[REDACTED]') if token else text)

        with closing(connect(destination)) as db:
            migrate(db)
            store = TaskStore(db, clean)
            with store.transaction():
                repos = _repositories(source, store)
                opportunities, ids = _opportunities(source, store)
                return {'repositories': repos, 'opportunities': opportunities,
                        'preferences': _preferences(source, store) if 'user_preference_versions' in tables else 0,
                        'marks': _marks(source, store, ids) if 'opportunity_disposition_versions' in tables else 0,
                        'pull_requests': _pull_requests(source, store, tables) if 'draft_pull_requests' in tables else 0}
