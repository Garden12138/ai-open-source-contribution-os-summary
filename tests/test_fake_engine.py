import asyncio

from contribos.agent.engine import AgentEngine, EngineEvent, RequestResponse, TaskContext
from contribos.agent.fake import FakeEngine


def test_fake_replays_requests_cancel_continue_and_close():
    async def check():
        agent = FakeEngine([[EngineEvent('permission.requested', {'request_id': 'p'})], []], hold_turn=True)
        assert isinstance(agent, AgentEngine)
        task = TaskContext('t', 'owner/repo', 1, '/repo', '/context')
        sid = await agent.ensure_session(task)
        assert await agent.ensure_session(task) == sid
        turn = asyncio.create_task(agent.prompt(sid, []))
        events = agent.events(sid)
        assert (await anext(events)).type == 'permission.requested'
        await agent.respond('p', RequestResponse(option_id='yes'))
        assert (await anext(events)).type == 'permission.resolved'
        await agent.cancel(sid)
        assert (await turn).stop_reason == 'cancelled'
        assert (await anext(events)).type == 'turn.completed'
        agent.hold_turn = False
        assert (await agent.prompt(sid, [])).stop_reason == 'end_turn'
        await agent.close()
        assert [e.type async for e in events] == ['turn.completed']
    asyncio.run(asyncio.wait_for(check(), 2))
