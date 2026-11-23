"""Durable task and turn state, with transactional queue claims."""

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from contribos.security import redact
from contribos.tasks.issue import IssueRef
from contribos.tasks.instructions import CONTINUE_PROMPT

Message = dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class TaskStore:
    def __init__(self, db: sqlite3.Connection, clean: Callable[[str], str] = redact) -> None:
        self.db, self.clean = db, clean

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.clean(value)
        if isinstance(value, dict):
            return {self.clean(key): self.sanitize(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.sanitize(item) for item in value]
        return value

    def encode(self, value: Any) -> str:
        return json.dumps(self.sanitize(value), ensure_ascii=False)

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.db.execute('SELECT value_json FROM settings WHERE key = ?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def opportunity(self, opportunity_id: str) -> IssueRef:
        row = self.db.execute('SELECT repo_full_name,issue_number FROM opportunities WHERE id=?',
                              (opportunity_id,)).fetchone()
        if not row:
            raise TaskError('opportunity_not_found', 'This opportunity does not exist.', 404)
        return IssueRef(row['repo_full_name'], row['issue_number'])

    def create(self, ref: IssueRef, profile: str, isolation: str, opportunity_id: str | None = None) -> sqlite3.Row:
        task_id, now = uuid.uuid4().hex, utc_now()
        self.db.execute('''INSERT INTO tasks (id,opportunity_id,repo_full_name,issue_number,title,agent_profile,
                        isolation,runtime_managed,created_at,updated_at) VALUES (?,?,?,?,?,?,?,1,?,?)''',
                        (task_id, opportunity_id, ref.repo, ref.number, f'{ref.repo} #{ref.number}', profile,
                         isolation, now, now))
        return self.get(task_id)

    def get(self, task_id: str) -> sqlite3.Row:
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise TaskError('task_not_found', 'This task does not exist.', 404)
        return row

    def list_tasks(self) -> list[sqlite3.Row]:
        return self.db.execute('SELECT * FROM tasks ORDER BY created_at DESC, id').fetchall()

    def recoverable(self) -> list[sqlite3.Row]:
        return self.db.execute('''SELECT * FROM tasks WHERE delete_requested=1 OR (runtime_managed=1 AND
            (status='preparing' OR agent_state IN ('running','waiting_retry','queued')))''').fetchall()

    def ready(self, task_id: str, issue: Message, default_branch: str, base_sha: str, work_branch: str,
              sandbox_ref: str | None = None) -> None:
        with self.transaction():
            changed = self.db.execute('''UPDATE tasks SET status='ready',agent_state='queued',title=?,
                issue_snapshot_json=?,default_branch=?,base_sha=?,work_branch=?,sandbox_ref=?,updated_at=?
                WHERE id=? AND status='preparing' AND agent_state='idle' ''',
                (self.clean(issue.get('title') or self.get(task_id)['title']), self.encode(issue),
                 self.clean(default_branch), self.clean(base_sha), self.clean(work_branch),
                 self.clean(sandbox_ref) if sandbox_ref else None, utc_now(), task_id)).rowcount
            if changed:
                self._insert_turn(task_id, 'user', None, None, priority=3)

    def setup_failed(self, task_id: str, code: str, message: str) -> None:
        self.db.execute('''UPDATE tasks SET status='setup_failed',last_error_code=?,last_error_message=?,
            updated_at=? WHERE id=? AND status='preparing' ''', (code, self.clean(message), utc_now(), task_id))

    def adopt(self, task_id: str) -> None:
        with self.transaction():
            task = self.get(task_id)
            if task['runtime_managed']:
                return
            self.db.execute('UPDATE tasks SET runtime_managed=1 WHERE id=? AND runtime_managed=0', (task_id,))
            self.db.execute("""UPDATE turns SET status='interrupted',completed_at=?
                            WHERE task_id=? AND status='running'""", (utc_now(), task_id))
            if task['agent_state'] == 'running':
                self._agent_state(task_id, 'running', 'idle')

    def session(self, task_id: str, session_id: str, caps: Message, *, recap_pending: bool = False) -> None:
        self.db.execute('UPDATE tasks SET session_id=?,agent_caps_json=?,recap_pending=?,updated_at=? WHERE id=?',
                        (self.clean(session_id), self.encode(caps), int(recap_pending), utc_now(), task_id))

    def recap_delivered(self, task_id: str, session_id: str) -> None:
        self.db.execute('UPDATE tasks SET recap_pending=0 WHERE id=? AND session_id=? AND recap_pending=1',
                        (task_id, self.clean(session_id)))

    def agent_update(self, task_id: str, kind: str, payload: Message) -> None:
        mode = payload.get('mode')
        if kind == 'mode.changed' and isinstance(mode, str):
            self.db.execute('UPDATE tasks SET mode=?,updated_at=? WHERE id=?',
                            (self.clean(mode), utc_now(), task_id))
        elif kind == 'usage.updated':
            tokens = [value if type(value) is int and 0 <= value <= 2**63 - 1 else None
                      for value in (payload.get('tokens_input'), payload.get('tokens_output'))]
            if any(value is not None for value in tokens):
                self.db.execute('''UPDATE tasks SET tokens_input=COALESCE(?,tokens_input),
                    tokens_output=COALESCE(?,tokens_output),updated_at=? WHERE id=?''', (*tokens, utc_now(), task_id))

    def delete(self, task_id: str) -> None:
        with self.transaction():
            row = self.get(task_id)
            self.db.execute('DELETE FROM tasks WHERE id=? AND status=?', (task_id, row['status']))

    def request_delete(self, task_id: str, requested: bool) -> None:
        self.db.execute('UPDATE tasks SET delete_requested=?,updated_at=? WHERE id=? AND delete_requested=?',
                        (int(requested), utc_now(), task_id, int(not requested)))

    def _insert_turn(self, task_id: str, kind: str, prompt: str | None, client_message_id: str | None,
                     *, priority: int = 0, retry_count: int = 0, retry_at: str | None = None,
                     continuation_used: bool = False) -> sqlite3.Row:
        turn_id = uuid.uuid4().hex
        self.db.execute('''INSERT INTO turns (id,task_id,kind,client_message_id,prompt_text,
            queue_priority,retry_count,retry_at,continuation_used) VALUES (?,?,?,?,?,?,?,?,?)''',
            (turn_id, task_id, kind, client_message_id, prompt, priority, retry_count, retry_at, continuation_used))
        return self.db.execute('SELECT * FROM turns WHERE id=?', (turn_id,)).fetchone()

    def enqueue(self, task_id: str, text: str, client_message_id: str | None = None, *,
                kind: str = 'user', interrupt: bool = False) -> tuple[sqlite3.Row, bool]:
        with self.transaction():
            task = self.get(task_id)
            if client_message_id is not None:
                client_message_id = self.clean(client_message_id)
                existing = self.db.execute('SELECT * FROM turns WHERE client_message_id=?',
                                           (client_message_id,)).fetchone()
                if existing:
                    if existing['task_id'] != task_id:
                        raise TaskError('message_id_conflict', 'This message ID is already in use.')
                    return existing, False
            if task['status'] not in {'ready', 'published'}:
                raise TaskError('task_not_ready', 'This task cannot accept messages yet.')
            if task['delete_requested']:
                raise TaskError('task_deleting', 'This task is being deleted.')
            priority = 2 if interrupt or task['agent_state'] == 'error' else 0
            turn = self._insert_turn(task_id, kind, self.clean(text), client_message_id, priority=priority)
            self.db.execute("""INSERT INTO task_events(task_id,turn_id,type,payload_json,created_at)
                VALUES (?,?,'message.user',?,?)""", (task_id, turn['id'], self.encode({'text': text}), utc_now()))
            if task['agent_state'] in {'idle', 'error'}:
                self._agent_state(task_id, task['agent_state'], 'queued')
            return turn, True

    def running(self, task_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM turns WHERE task_id=? AND status='running'", (task_id,)).fetchone()

    def next_turn(self, task_id: str) -> sqlite3.Row | None:
        return self.db.execute('''SELECT * FROM turns WHERE task_id=? AND status='queued'
            ORDER BY queue_priority DESC, rowid LIMIT 1''', (task_id,)).fetchone()

    def turns(self, task_id: str) -> list[sqlite3.Row]:
        return self.db.execute('SELECT * FROM turns WHERE task_id=? ORDER BY rowid', (task_id,)).fetchall()

    def _agent_state(self, task_id: str, previous: str, next_state: str,
                     error: tuple[str, str] | None = None) -> int:
        code, message = error or (None, None)
        return self.db.execute('''UPDATE tasks SET agent_state=?,last_error_code=?,last_error_message=?,updated_at=?
            WHERE id=? AND agent_state=?''', (next_state, code, self.clean(message) if message else None,
                                              utc_now(), task_id, previous)).rowcount

    def claim(self, task_id: str, *, now: str | None = None) -> sqlite3.Row | None:
        with self.transaction():
            task = self.get(task_id)
            if task['agent_state'] not in {'queued', 'waiting_retry'} or self.running(task_id):
                return None
            turn = self.next_turn(task_id)
            now = now or utc_now()
            if not turn or (turn['retry_at'] and turn['retry_at'] > now):
                return None
            changed = self.db.execute("UPDATE turns SET status='running',started_at=? WHERE id=? AND status='queued'",
                                      (now, turn['id'])).rowcount
            if changed:
                self._agent_state(task_id, task['agent_state'], 'running')
                return self.running(task_id)
            return None

    def finish(self, task_id: str, turn_id: str, status: str, *, reason: str | None = None,
               error: tuple[str, str] | None = None, retry_at: str | None = None,
               retry_count: int = 0, auto_continue: bool = False, continuation_used: bool = False,
               recovered: bool = False) -> bool:
        with self.transaction():
            changed = self.db.execute('''UPDATE turns SET status=?,stop_reason=?,error_code=?,completed_at=?
                WHERE task_id=? AND id=? AND status='running' ''',
                (status, reason, error[0] if error else None, utc_now(), task_id, turn_id)).rowcount
            if not changed:
                return False
            if retry_at or auto_continue:
                self._insert_turn(task_id, 'auto_retry' if retry_at else 'continue', CONTINUE_PROMPT, None,
                                  priority=3, retry_count=retry_count, retry_at=retry_at,
                                  continuation_used=continuation_used or auto_continue)
            state = ('waiting_retry' if retry_at else 'error' if error and not recovered
                     else 'queued' if self.next_turn(task_id) else 'idle')
            self._agent_state(task_id, 'running', state, error)
            return True

    def stop_retry(self, task_id: str) -> bool:
        with self.transaction():
            if not self._agent_state(task_id, 'waiting_retry', 'idle'):
                return False
            self.db.execute('''UPDATE turns SET status='interrupted',completed_at=?
                WHERE task_id=? AND status='queued' AND retry_at IS NOT NULL''', (utc_now(), task_id))
            if self.next_turn(task_id):
                self._agent_state(task_id, 'idle', 'queued')
            return True

    def recovery_failed(self, task_id: str, code: str, message: str) -> None:
        with self.transaction():
            task = self.get(task_id)
            self._agent_state(task_id, task['agent_state'], 'error', (code, message))

    def save_diff(self, task_id: str, text: str) -> Message | None:
        text = self.clean(text)
        diff_hash = hashlib.sha256(text.encode()).hexdigest()
        files: list[Message] = []
        in_hunk = False
        for line in text.splitlines():
            if line.startswith('diff --git '):
                path = line.rsplit(' b/', 1)[-1] if ' b/' in line else line.removeprefix('diff --git ')
                files.append({'path': path, 'additions': 0, 'deletions': 0})
                in_hunk = False
            elif files and line.startswith('@@ '):
                in_hunk = True
            elif files and in_hunk and line.startswith(('+', '-')):
                files[-1]['additions' if line.startswith('+') else 'deletions'] += 1
        additions = sum(file['additions'] for file in files)
        deletions = sum(file['deletions'] for file in files)
        changed = self.db.execute('''INSERT INTO task_diffs
            (task_id,diff_text,files_json,additions,deletions,diff_hash,updated_at) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET diff_text=excluded.diff_text,files_json=excluded.files_json,
                additions=excluded.additions,deletions=excluded.deletions,diff_hash=excluded.diff_hash,
                updated_at=excluded.updated_at WHERE task_diffs.diff_hash != excluded.diff_hash''',
            (task_id, text, self.encode(files), additions, deletions, diff_hash, utc_now())).rowcount
        return {'has_changes': bool(text), 'files': files, 'additions': additions, 'deletions': deletions} if changed else None

    def diff(self, task_id: str) -> Message:
        self.get(task_id)
        row = self.db.execute('SELECT diff_text,files_json,additions,deletions,updated_at FROM task_diffs WHERE task_id=?',
                              (task_id,)).fetchone()
        if row:
            result = dict(row)
            result['files'] = json.loads(result.pop('files_json'))
            return self.sanitize(result)
        return {'diff_text': '', 'files': [], 'additions': 0, 'deletions': 0, 'updated_at': None}
