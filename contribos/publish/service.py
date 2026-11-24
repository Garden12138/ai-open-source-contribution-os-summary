"""Persist, confirm and reconcile immutable Draft PR previews."""

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import asdict
from typing import Any

from contribos.agent.engine import EngineEvent
from contribos.publish.git_transfer import Author, GitTransfer, SECRET, TransferError, export_candidate, snapshot_workspace
from contribos.publish.github_writer import GitHubWriter, PullRequest, WriterError
from contribos.sandbox.base import SandboxError
from contribos.tasks.issue import IssueError, IssueRef
from contribos.tasks.store import TaskError, utc_now
from contribos.tasks.supervisor import TaskSupervisor

TEST_COMMAND = re.compile(r'\b(?:pytest|(?:python|python3) -m unittest|node --test|(?:cargo|npm|pnpm|bun|yarn|go) test)\b')
MESSAGES = {
    'git_author_required': 'Set a Git author name and email in Settings.',
    'github_login_required': 'Run gh auth login to prepare and publish a Draft PR.',
    'fork_owner_is_upstream': 'Use an upstream repository that this GitHub account can fork.',
    'fork_destination_conflict': 'The destination repository is not a writable fork of this upstream.',
    'remote_branch_conflict': 'The remote branch already points to a different commit.',
    'base_not_on_upstream': 'The workspace base no longer belongs to the upstream default branch.',
    'workspace_has_conflicts': 'Resolve Git conflicts before preparing a Draft PR.',
    'no_publishable_changes': 'The workspace has no changes to publish.',
    'secret_detected': 'The preview was blocked because the changes contain a credential pattern.',
    'workspace_changed': 'The workspace changed. Prepare a new preview.',
    'default_branch_changed': 'The upstream default branch changed. Prepare a new preview.',
    'github_account_changed': 'The GitHub account changed since preview. Restore it before retrying.',
    'pull_request_conflict': 'The existing PR does not point to the confirmed commit and base branch.',
}


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class Publisher:
    def __init__(self, supervisor: TaskSupervisor, *, transfer: GitTransfer | None = None,
                 writer: GitHubWriter | None = None) -> None:
        self.supervisor, self.store = supervisor, supervisor.store
        self.transfer = transfer or GitTransfer(supervisor.settings)
        self.writer = writer or GitHubWriter(supervisor.github.credentials, self.transfer)
        self.preparing: set[str] = set()
        self.errors: dict[str, dict[str, str]] = {}

    def recover(self) -> None:
        for row in self.store.db.execute("SELECT id,status FROM publications WHERE status IN ('confirmed','running')").fetchall():
            self._status(row['id'], row['status'], 'unknown', 'publish_interrupted')

    def _latest(self, task_id: str) -> sqlite3.Row | None:
        return self.store.db.execute('SELECT * FROM publications WHERE task_id=? ORDER BY rowid DESC LIMIT 1',
                                     (task_id,)).fetchone()

    def view(self, task_id: str) -> dict[str, Any]:
        self.store.get(task_id)
        row = self._latest(task_id)
        publication = None if row is None else {
            **{key: row[key] for key in ('id', 'status', 'preview_hash', 'error_code', 'pr_url', 'pr_number')},
            'preview': json.loads(row['preview_json']), 'steps': json.loads(row['steps_json']),
            'message': MESSAGES.get(row['error_code']),
        }
        return self.store.sanitize({'publication': publication, 'preparing': task_id in self.preparing,
                                    'error': self.errors.get(task_id)})

    def _event(self, task_id: str, kind: str, **payload: Any) -> None:
        self.supervisor.events.publish(task_id, None, EngineEvent('publish.' + kind, payload))

    def _check_task(self, task_id: str) -> None:
        if self.store.get(task_id)['status'] != 'ready':
            raise TaskError('task_not_publishable', 'A new Draft PR requires an unpublished task.')

    def prepare(self, task_id: str) -> None:
        self._check_task(task_id)
        previous = self._latest(task_id)
        if previous and previous['status'] in {'confirmed', 'running', 'unknown', 'succeeded'}:
            raise TaskError('publication_pending', 'Resolve the current publication before preparing another preview.')
        self.supervisor.background(task_id, lambda: self._prepare(task_id))
        self.preparing.add(task_id)
        self.errors.pop(task_id, None)
        self._event(task_id, 'preparing')

    async def _prepare(self, task_id: str) -> None:
        try:
            task, sandbox = await self.supervisor.workspace(task_id)
            author = Author(self.store.setting('git_author_name', ''), self.store.setting('git_author_email', ''),
                            self.store.setting('git_dco_signoff', False))
            author.environment()
            async with self.writer.open() as writer:
                fork = writer.destination(task.repo_full_name, await writer.identity())
            issue = await self.supervisor.issue_loader(IssueRef(task.repo_full_name, task.issue_number))
            default = issue.get('default_branch') or task.default_branch
            draft = await self.supervisor.draft_pr(task_id)
            if SECRET.search((draft.title + '\n' + draft.body).encode()):
                raise TransferError('secret_detected')
            source = await snapshot_workspace(sandbox, task, default)
            preview_id = uuid.uuid4().hex
            candidate = await export_candidate(sandbox, task, source)
            try:
                result = await self.transfer.preview(task.repo_full_name, default, candidate, author,
                                                     self.store.clean(draft.commit_message), preview_id)
            finally:
                with suppress(OSError):
                    candidate.bundle.unlink(missing_ok=True)
            warnings = list(result.warnings)
            if draft.fallback:
                warnings.append('draft_template')
            linked = {pr.get('html_url') or pr.get('number') for pr in task.linked_prs}
            review = self.store.db.execute("""SELECT status,verdict,summary,diff_hash_before FROM reviews
                WHERE task_id=? ORDER BY rowid DESC LIMIT 1""", (task_id,)).fetchone()
            preview = {**asdict(result), 'source': asdict(source), 'base_sha': source.base, 'warnings': warnings,
                'repo': task.repo_full_name, 'default_branch': default, 'fork': fork, 'author': asdict(author),
                'branch': f'contribos/issue-{task.issue_number}-{preview_id[:12]}', 'draft': True,
                'title': draft.title, 'body': draft.body, 'commit_message': draft.commit_message,
                'checks': {'issue_state': issue.get('state'), 'new_linked_prs': [pr for pr in issue.get('linked_prs', [])
                    if (pr.get('html_url') or pr.get('number')) not in linked], 'tests': self._tests(task_id),
                    'review': {**dict(review), 'current': review['status'] == 'completed'
                               and review['diff_hash_before'] == result.diff_hash} if review else None}}
            with self.store.transaction():
                self._insert(task_id, preview_id, preview)
            self._event(task_id, 'previewed')
        except Exception as error:
            self.errors[task_id] = self._diagnostic(error, 'publish_preview_failed')
            self._event(task_id, 'preview_failed', **self.errors[task_id])
        finally:
            self.preparing.discard(task_id)

    def _tests(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.store.db.execute("""SELECT type,payload_json FROM task_events
            WHERE task_id=? AND type IN ('command.started','command.completed') ORDER BY seq DESC LIMIT 150""", (task_id,))
        completed, tests = {}, []
        for row in rows:
            payload = json.loads(row['payload_json'])
            key = payload.get('tool_call_id')
            if row['type'] == 'command.completed':
                if isinstance(key, str):
                    completed.setdefault(key, payload)
                continue
            command = payload.get('command') or payload.get('title', '')
            if isinstance(command, list):
                command = ' '.join(str(item) for item in command)
            if isinstance(command, str) and TEST_COMMAND.search(command) and isinstance(key, str) and key in completed:
                tests.append({'command': command, 'exit_code': completed[key].get('exit_code'),
                              'status': completed[key].get('status')})
                if len(tests) == 5:
                    break
        return tests

    def _insert(self, task_id: str, preview_id: str, preview: dict[str, Any]) -> None:
        preview = self.store.sanitize(preview)
        preview_hash = _digest({'id': preview_id, **preview})
        self.store.db.execute("UPDATE publications SET status='superseded' WHERE task_id=? AND status='previewed'", (task_id,))
        self.store.db.execute("""INSERT INTO publications(id,task_id,kind,preview_json,preview_hash,status,
            fork_full_name,branch,commit_sha,created_at) VALUES (?,?,'create_pr',?,?,'previewed',?,?,?,?)""",
            (preview_id, task_id, self.store.encode(preview), preview_hash, preview['fork'], preview['branch'],
             preview['commit_sha'], utc_now()))

    def edit(self, task_id: str, preview_id: str, title: str, body: str) -> dict[str, Any]:
        self._check_task(task_id)
        if task_id in self.preparing:
            raise TaskError('task_busy', 'Wait for the current preview to finish.')
        if (not title.strip() or len(title.splitlines()) != 1 or '\0' in title + body
                or SECRET.search((title + '\n' + body).encode()) or self.store.clean(title + body) != title + body):
            raise TaskError('invalid_pr_text', 'Provide a one-line title and PR text without credentials.', 422)
        with self.store.transaction():
            row = self._latest(task_id)
            if not row or row['id'] != preview_id or row['status'] != 'previewed':
                raise TaskError('stale_preview', 'Load the latest Draft PR preview.')
            preview = json.loads(row['preview_json'])
            if (title, body) != (preview['title'], preview['body']):
                self._insert(task_id, uuid.uuid4().hex, {**preview, 'title': title, 'body': body})
        self._event(task_id, 'previewed')
        return self.view(task_id)

    def confirm(self, task_id: str, preview_hash: str, *, reconcile: bool = False) -> None:
        self._check_task(task_id)
        expected = 'unknown' if reconcile else 'previewed'
        with self.store.transaction():
            row = self._latest(task_id)
            if not row or row['status'] != expected or row['preview_hash'] != preview_hash:
                raise TaskError('stale_preview', 'Load the latest Draft PR preview.')
            updated = self.store.db.execute("""UPDATE publications SET status='confirmed',error_code=NULL,
                confirmed_at=COALESCE(confirmed_at,?) WHERE id=? AND status=? AND preview_hash=?""",
                (utc_now(), row['id'], expected, preview_hash)).rowcount
            if not updated:
                raise TaskError('stale_preview', 'Load the latest Draft PR preview.')
            self.supervisor.background(task_id, lambda: self._publish(row))
        self._event(task_id, 'confirmed')

    def _status(self, pub_id: str, previous: str, status: str, error: str | None = None) -> int:
        return self.store.db.execute('UPDATE publications SET status=?,error_code=?,finished_at=? WHERE id=? AND status=?',
            (status, error, utc_now() if status != 'running' else None, pub_id, previous)).rowcount

    def _step(self, pub_id: str, steps: dict[str, Any], name: str, result: Any) -> None:
        steps[name] = result
        self.store.db.execute("UPDATE publications SET steps_json=? WHERE id=? AND status='running'",
                              (self.store.encode(steps), pub_id))

    @staticmethod
    def _diagnostic(error: Exception, fallback: str) -> dict[str, str]:
        if isinstance(error, (TaskError, IssueError)):
            return {'error_code': error.code, 'message': error.message}
        code = str(error) if isinstance(error, (TransferError, WriterError, SandboxError)) else fallback
        return {'error_code': code, 'message': MESSAGES.get(code, 'The Draft PR operation could not complete.')}

    async def _publish(self, row: sqlite3.Row) -> None:
        pub_id, task_id = row['id'], row['task_id']
        if not self._status(pub_id, 'confirmed', 'running'):
            return
        self._event(task_id, 'running')
        data, steps = json.loads(row['preview_json']), json.loads(row['steps_json'])
        try:
            repo, fork, branch, default = (data[key] for key in ('repo', 'fork', 'branch', 'default_branch'))
            if not steps.get('source_checked'):
                task, sandbox = await self.supervisor.workspace(task_id)
                issue = await self.supervisor.issue_loader(IssueRef(repo, task.issue_number))
                if issue.get('default_branch', default) != default:
                    raise TransferError('default_branch_changed')
                fresh = await snapshot_workspace(sandbox, task, default, fetch=False)
                if asdict(fresh) != data['source']:
                    raise TransferError('workspace_changed')
                self._step(pub_id, steps, 'source_checked', True)
            async with self.writer.open() as writer:
                if writer.destination(repo, await writer.identity()).casefold() != fork.casefold():
                    raise WriterError('github_account_changed')
                self._step(pub_id, steps, 'fork', await writer.ensure_fork(repo, fork))
                self._step(pub_id, steps, 'push', await writer.push(repo, fork, branch, data['commit_sha']))
                pull = await writer.ensure_pull(repo, fork, branch, default, data['title'], data['body'], data['commit_sha'])
                self._step(pub_id, steps, 'pull', asdict(pull))
            self._succeed(row, data, pull)
            self._event(task_id, 'succeeded', pr_url=pull.url)
        except asyncio.CancelledError:
            self._status(pub_id, 'running', 'unknown', 'publish_interrupted')
            raise
        except Exception as error:
            detail = self._diagnostic(error, 'publish_interrupted')
            status = 'superseded' if detail['error_code'] in {'workspace_changed', 'default_branch_changed'} else 'unknown'
            self._status(pub_id, 'running', status, detail['error_code'])
            self._event(task_id, 'failed', status=status, **detail)

    def _succeed(self, row: sqlite3.Row, data: dict[str, Any], pull: PullRequest) -> None:
        now = utc_now()
        with self.store.transaction():
            changed = self.store.db.execute("""UPDATE publications SET status='succeeded',pr_number=?,pr_url=?,finished_at=?
                WHERE id=? AND status='running'""", (pull.number, pull.url, now, row['id'])).rowcount
            if not changed:
                raise TaskError('stale_preview', 'The publication changed before completion.')
            self.store.db.execute("""INSERT INTO pull_requests(task_id,repo_full_name,number,url,state,draft,merged,head_sha,
                last_synced_at) VALUES (?,?,?,?,?,?,0,?,?) ON CONFLICT(task_id) DO NOTHING""",
                (row['task_id'], data['repo'], pull.number, pull.url, pull.state, pull.draft, data['commit_sha'], now))
            self.store.db.execute("UPDATE tasks SET status='published',updated_at=? WHERE id=? AND status='ready'",
                                  (now, row['task_id']))
