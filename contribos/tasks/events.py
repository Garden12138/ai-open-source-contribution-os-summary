"""Redacted durable events, live updates, and replay from a sequence cursor."""

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any

from contribos.agent.engine import EngineEvent
from contribos.agent.normalize import clipped
from contribos.tasks.store import TaskStore, utc_now

PAGE_SIZE = 200
REQUEST_EVENTS = {'permission.requested', 'question.asked'}
RESOLUTION_EVENTS = {'permission.resolved', 'question.answered', 'system.request_expired'}


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    turn_id: str | None
    type: str
    payload: dict[str, Any]
    created_at: str
    seq: int | None = None

    def sse(self) -> str:
        prefix = f'id: {self.seq}\n' if self.seq is not None else ''
        return f'{prefix}event: {self.type}\ndata: {json.dumps(asdict(self), ensure_ascii=False)}\n\n'


class TaskEvents:
    def __init__(self, store: TaskStore, *, heartbeat: float = 15) -> None:
        self.store, self.heartbeat = store, heartbeat
        self.subscribers: dict[str, set[asyncio.Queue[TaskEvent | None]]] = {}
        self.progress: dict[str, dict[tuple[str | None, str], TaskEvent]] = {}

    def publish(self, task_id: str, turn_id: str | None, event: EngineEvent) -> TaskEvent:
        event_type = event.type if re.fullmatch(r'[a-z][a-z0-9_.]*', event.type) else 'agent.other'
        payload = self.store.sanitize(event.payload)
        if event_type == 'command.completed' and isinstance(payload.get('output_tail'), str):
            payload['output_tail'] = clipped(payload['output_tail'], 16384, tail=True)
        if event_type == 'reasoning.summary' and isinstance(payload.get('text'), str):
            payload['text'] = clipped(payload['text'], 4096)
        now = utc_now()
        seq = None
        if event.persistent:
            seq = self.store.db.execute('''INSERT INTO task_events (task_id,turn_id,type,payload_json,created_at)
                VALUES (?,?,?,?,?)''', (task_id, turn_id, event_type, json.dumps(payload, ensure_ascii=False), now)).lastrowid
        result = TaskEvent(task_id, turn_id, event_type, payload, now, seq)
        self._snapshot(result)
        self.notify(result)
        return result

    def _snapshot(self, event: TaskEvent) -> None:
        task_id, turn_id, kind = event.task_id, event.turn_id, event.type
        progress = self.progress.get(task_id, {})
        if kind in {'message.delta', 'reasoning.delta', 'command.started'}:
            key = (turn_id, kind if kind.endswith('.delta') else str(event.payload.get('tool_call_id')))
            if kind.endswith('.delta'):
                previous = progress[key].payload['text'] if key in progress else ''
                text = previous + event.payload.get('text', '')
                if kind == 'reasoning.delta':
                    text = clipped(text, 4096)
                event = TaskEvent(task_id, turn_id, kind,
                                  {'text': text},
                                  event.created_at)
            if len(progress) >= 64 and key not in progress:
                del progress[next(iter(progress))]
            progress[key] = event
        elif kind == 'message.agent':
            progress.pop((turn_id, 'message.delta'), None)
        elif kind == 'reasoning.summary':
            progress.pop((turn_id, 'reasoning.delta'), None)
        elif kind == 'command.completed':
            progress.pop((turn_id, str(event.payload.get('tool_call_id'))), None)
        elif kind in {'turn.completed', 'turn.failed', 'turn.interrupted'}:
            progress = {key: value for key, value in progress.items() if key[0] != turn_id}
        if progress:
            self.progress[task_id] = progress
        else:
            self.progress.pop(task_id, None)

    def notify(self, event: TaskEvent) -> None:
        for queue in tuple(self.subscribers.get(event.task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # The subscriber can reconnect to the durable cursor and current snapshot.
                self._close_queue(queue)
                self.subscribers[event.task_id].discard(queue)

    @staticmethod
    def _close_queue(queue: asyncio.Queue[TaskEvent | None]) -> None:
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)

    def close(self, task_id: str) -> None:
        self.progress.pop(task_id, None)
        for queue in self.subscribers.pop(task_id, ()):
            self._close_queue(queue)

    def history(self, task_id: str, after: int = 0, *, limit: int = PAGE_SIZE,
                through: int = 2**63 - 1) -> list[TaskEvent]:
        rows = self.store.db.execute('''SELECT * FROM task_events WHERE task_id=? AND seq>? AND seq<=?
            ORDER BY seq LIMIT ?''', (task_id, after, through, limit)).fetchall()
        return [self.from_row(row) for row in rows]

    def from_row(self, row: sqlite3.Row) -> TaskEvent:
        kind = row['type'] if re.fullmatch(r'[a-z][a-z0-9_.]*', row['type']) else 'agent.other'
        return TaskEvent(row['task_id'], row['turn_id'], kind, self.store.sanitize(json.loads(row['payload_json'])),
                         row['created_at'], row['seq'])

    def notify_message(self, turn: sqlite3.Row) -> None:
        row = self.store.db.execute("""SELECT * FROM task_events WHERE task_id=? AND turn_id=? AND type='message.user'""",
                                    (turn['task_id'], turn['id'])).fetchone()
        self.notify(self.from_row(row))

    def pending(self, task_id: str) -> dict[str, TaskEvent]:
        rows = self.store.db.execute('''SELECT * FROM task_events WHERE task_id=? AND
            type IN ('permission.requested','question.asked','permission.resolved','question.answered',
                     'system.request_expired') ORDER BY seq''', (task_id,))
        requests: dict[str, TaskEvent] = {}
        for row in rows:
            event = self.from_row(row)
            request_id = event.payload.get('request_id')
            if isinstance(request_id, str):
                if event.type in REQUEST_EVENTS:
                    requests[request_id] = event
                else:
                    requests.pop(request_id, None)
        return requests

    def remembered_permissions(self, task_id: str) -> list[TaskEvent]:
        rows = self.store.db.execute("""SELECT * FROM task_events WHERE task_id=? AND
            type IN ('permission.requested', 'permission.remembered') ORDER BY seq""", (task_id,))
        requests: dict[str, TaskEvent] = {}
        remembered = []
        for row in rows:
            event = self.from_row(row)
            request_id = event.payload.get('request_id')
            if not isinstance(request_id, str):
                continue
            if event.type == 'permission.requested':
                requests[request_id] = event
            elif request_id in requests:
                remembered.append(requests.pop(request_id))
        return remembered

    async def stream(self, task_id: str, after: int = 0) -> AsyncIterator[str]:
        queue: asyncio.Queue[TaskEvent | None] = asyncio.Queue(maxsize=256)
        self.subscribers.setdefault(task_id, set()).add(queue)
        try:
            through = self.store.db.execute('SELECT COALESCE(MAX(seq),0) FROM task_events WHERE task_id=?',
                                            (task_id,)).fetchone()[0]
            snapshot = [asdict(event) for event in self.progress.get(task_id, {}).values()]
            yield ': connected\n\n'
            while after < through and (page := self.history(task_id, after, through=through)):
                for event in page:
                    after = event.seq
                    yield event.sse()
            if snapshot:
                yield TaskEvent(task_id, None, 'stream.snapshot', {'items': snapshot}, utc_now()).sse()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), self.heartbeat)
                except TimeoutError:
                    yield ': heartbeat\n\n'
                    continue
                if event is None:
                    return
                if event.seq is None or event.seq > after:
                    if event.seq is not None:
                        after = event.seq
                    yield event.sse()
        finally:
            subscribers = self.subscribers.get(task_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self.subscribers.pop(task_id, None)
