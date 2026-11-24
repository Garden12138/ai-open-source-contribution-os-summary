import asyncio
import json
from contextlib import closing

import pytest

from contribos.agent.engine import EngineEvent
from contribos.agent.fake import FakeEngine
from contribos.config import Settings
from contribos.db import connect, migrate
from contribos.publish.draft import DRAFT_PROMPT, draft_from_response
from contribos.tasks.store import TaskStore
from test_task_supervisor import offline, start, until

DRAFT = {'title': 'Fix word count', 'body': '## Summary\nCount words.', 'commit_message': 'Fix word count'}


@pytest.mark.parametrize('reply, fallback', [
    ('Earlier prose\n```json\n' + json.dumps(DRAFT) + '\n```', False),
    ('```json\n{"title":"invalid"}\n```', True),
    ('```json\n' + json.dumps(DRAFT) + '\n```\n```json\n{broken\n```', True),
    ('```json\n' + json.dumps({**DRAFT, 'title': 'two\nlines'}) + '\n```', True),
])
def test_pr_draft_uses_final_json_or_english_template(reply, fallback):
    draft = draft_from_response(reply, 9)
    assert draft.fallback is fallback
    assert draft.title == ('Fix issue #9' if fallback else DRAFT['title'])


def test_pr_draft_runs_in_implementation_session(tmp_path):
    async def check():
        settings = Settings(home=tmp_path)
        reply = '```json\n' + json.dumps(DRAFT) + '\n```'
        engine = FakeEngine(scripts=[[], [EngineEvent('message.agent', {'text': reply})]])
        with closing(connect(settings.database_path)) as db:
            migrate(db)
            store = TaskStore(db)
            supervisor = offline(settings, store, engine)
            try:
                task_id = await start(supervisor, store)
                original_session = store.get(task_id)['session_id']
                results = []

                async def prepare():
                    results.append(await supervisor.draft_pr(task_id))

                supervisor.background(task_id, prepare)
                await until(lambda: results)
                assert results[0].title == DRAFT['title'] and not results[0].fallback
                assert store.get(task_id)['session_id'] == original_session
                assert engine.prompts[-1][0].text == DRAFT_PROMPT
                assert [(turn['kind'], turn['status']) for turn in store.turns(task_id)] == [
                    ('user', 'completed'), ('pr_draft', 'completed')]
            finally:
                await supervisor.shutdown()

    asyncio.run(check())
