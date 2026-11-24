import asyncio
import json
from contextlib import closing

from contribos.agent.engine import EngineEvent
from contribos.db import connect, migrate
from contribos.tasks.events import TaskEvents
from contribos.tasks.issue import IssueRef
from contribos.tasks.store import TaskStore


def test_event_redaction_replay_and_snapshot(tmp_path):
    async def check():
        with closing(connect(tmp_path / 'db')) as db:
            migrate(db)
            store = TaskStore(db)
            tid = store.create(IssueRef('owner/repo', 1), 'codex', 'host')['id']
            store.ready(tid, {'title': 'Issue'}, 'main', 'base', 'work')
            turn = store.claim(tid)['id']
            events = TaskEvents(store)
            first = events.publish(tid, turn, EngineEvent('turn.started'))
            in_progress = 'in progress ' + 'x' * 17000
            events.publish(tid, turn, EngineEvent('message.delta', {'text': in_progress}))
            canary = 'ghp_' + 'canary0123456789'
            command = events.publish(tid, turn, EngineEvent('command.completed', {
                'output_tail': 'x' * 17000 + canary,
            }))
            stream = events.stream(tid, first.seq)
            assert (await anext(stream)).startswith(': connected')
            # This event lands after the replay watermark, while the client is replaying.
            live = events.publish(tid, turn, EngineEvent('message.delta', {'text': ' live'}))
            replay = await anext(stream)
            assert replay.startswith(f'id: {command.seq}\n')
            assert canary not in replay and '[REDACTED]' in replay
            snapshot = await anext(stream)
            assert snapshot.startswith('event: stream.snapshot')
            assert json.loads(snapshot.splitlines()[1].removeprefix('data: '))['payload']['items'][0]['payload']['text'] == in_progress
            assert (await anext(stream)) == live.sse()
            assert not live.sse().startswith('id:')
            assert len(events.history(tid)) == 2
            assert canary not in ''.join(row[0] for row in db.execute('SELECT payload_json FROM task_events'))
            await stream.aclose()
            assert not events.subscribers
            events.publish(tid, turn, EngineEvent('turn.completed'))
            assert tid not in events.progress
    asyncio.run(check())
