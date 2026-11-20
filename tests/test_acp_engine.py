import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from acp.schema import TextContentBlock

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.engine import AgentError, RequestResponse, TaskContext
from contribos.agent.normalize import Normalizer

SCRIPT = Path(__file__).with_name('fake_acp_agent.py')


def context(tmp_path, **kwargs):
    (tmp_path / 'issue.md').write_text('An offline task')
    return TaskContext('task', 'owner/repo', 1, str(tmp_path), str(tmp_path), **kwargs)


def engine(scenario, **kwargs):
    async def spawn():
        return await asyncio.create_subprocess_exec(sys.executable, str(SCRIPT),
            env={**os.environ, 'FAKE_ACP_SCENARIO': scenario}, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    return AcpEngine(spawn, **kwargs)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


async def collect_until(agent, sid, kind):
    events = []
    async for event in agent.events(sid):
        events.append(event)
        if event.type == kind:
            return events
    return events


def test_workflow_normalization_modes_and_refresh(tmp_path):
    async def check():
        refreshes = []
        async def refresh():
            refreshes.append((tmp_path / 'agent-output.txt').read_text())
        agent = engine('workflow', refresh_diff=refresh)
        try:
            sid = await agent.ensure_session(context(tmp_path))
            assert agent.capabilities.load_session
            assert agent.capabilities.modes == ('plan', 'build')
            await agent.set_mode(sid, 'build')
            result = await agent.prompt(sid, [TextContentBlock(text='Go')])
            events = await collect_until(agent, sid, 'turn.completed')
            assert result.stop_reason == 'end_turn'
            messages = [e.payload['text'] for e in events if e.type == 'message.agent']
            assert messages == ['A plan.', 'Done.']
            assert [e.payload['text'] for e in events if e.type == 'reasoning.summary'] == ['Inspect the task.']
            assert any(e.type == 'command.completed' and e.payload['exit_code'] == 0 for e in events)
            assert any(e.type == 'files.changed' and e.payload['paths'] == [str(tmp_path / 'agent-output.txt')] for e in events)
            assert any(e.type == 'mode.changed' and e.payload['mode'] == 'build' for e in events)
            assert 'plan.updated' in [e.type for e in events]
            assert refreshes == ['implemented\n', 'implemented\n']
        finally:
            await agent.close()
    run(check())


@pytest.mark.parametrize('scenario', ['echo', 'resume', 'no_restore', 'load_fail'])
def test_restore_or_recreate(tmp_path, scenario):
    async def check():
        agent = engine(scenario)
        try:
            task = context(tmp_path, session_id='previous')
            sid = await agent.ensure_session(task)
            assert sid == ('previous' if scenario in {'echo', 'resume'} else 'fake-session')
            assert agent.capabilities.resume_session == (scenario == 'resume')
            assert await agent.ensure_session(task) == sid
            await agent.prompt(sid, [TextContentBlock(text='Continue')])
            events = await collect_until(agent, sid, 'turn.completed')
            assert ('system.session_recreated' in [e.type for e in events]) == (scenario in {'no_restore', 'load_fail'})
        finally:
            await agent.close()
    run(check())


@pytest.mark.parametrize('option_id', ['allow', 'deny', None])
def test_permission_round_trip_and_expiry(tmp_path, option_id):
    async def check():
        agent = engine('permission')
        try:
            sid = await agent.ensure_session(context(tmp_path))
            turn = asyncio.create_task(agent.prompt(sid, [TextContentBlock(text='Go')]))
            events = await collect_until(agent, sid, 'permission.requested')
            request_id = events[-1].payload['request_id']
            assert not turn.done()
            with pytest.raises(AgentError, match='Agent request failed'):
                await agent.respond(request_id, RequestResponse(option_id='invalid'))
            await agent.respond(request_id, RequestResponse(option_id=option_id, cancelled=option_id is None))
            assert (await turn).stop_reason == 'end_turn'
            with pytest.raises(AgentError) as exc:
                await agent.respond(request_id, RequestResponse(option_id='allow'))
            assert exc.value.code == 'request_expired'
            rest = await collect_until(agent, sid, 'turn.completed')
            text = next(e.payload['text'] for e in rest if e.type == 'message.agent')
            assert (option_id or 'cancelled') in text
        finally:
            await agent.close()
    run(check())


def test_sandbox_permission_allows_once(tmp_path):
    async def check():
        agent = engine('permission')
        try:
            sid = await agent.ensure_session(context(tmp_path, isolation='container'))
            await agent.prompt(sid, [TextContentBlock(text='Go')])
            events = await collect_until(agent, sid, 'turn.completed')
            assert not any(e.type == 'permission.requested' for e in events)
            assert any(e.type == 'permission.resolved' and e.payload['auto'] for e in events)
        finally:
            await agent.close()
    run(check())


def test_structured_question_waits_for_response(tmp_path):
    async def check():
        agent = engine('question')
        try:
            sid = await agent.ensure_session(context(tmp_path))
            turn = asyncio.create_task(agent.prompt(sid, [TextContentBlock(text='Go')]))
            events = await collect_until(agent, sid, 'question.asked')
            request = events[-1].payload
            assert request['schema']['properties']['test']['enum'] == ['unit', 'all']
            assert not turn.done()
            await agent.respond(request['request_id'], RequestResponse(answers={'test': 'unit'}))
            await turn
            events = await collect_until(agent, sid, 'turn.completed')
            assert any(e.type == 'question.answered' and e.payload['answers'] == {'test': 'unit'} for e in events)
        finally:
            await agent.close()
    run(check())


def test_cancel_slow_turn_and_continue(tmp_path):
    async def check():
        agent = engine('slow')
        try:
            sid = await agent.ensure_session(context(tmp_path))
            for _ in range(2):
                turn = asyncio.create_task(agent.prompt(sid, [TextContentBlock(text='Continue')]))
                # The chunk has no newline and is buffered; allow the SDK notification to arrive.
                while not agent.normalizer.parts:
                    await asyncio.sleep(.01)
                await agent.cancel(sid)
                assert (await turn).stop_reason == 'cancelled'
                await collect_until(agent, sid, 'turn.completed')
        finally:
            await agent.close()
    run(check())


@pytest.mark.parametrize('scenario', ['exit', 'error', 'forbidden'])
def test_failures_and_client_fs_rejection(tmp_path, scenario):
    async def check():
        agent = engine(scenario)
        try:
            sid = await agent.ensure_session(context(tmp_path))
            if scenario == 'forbidden':
                await agent.prompt(sid, [TextContentBlock(text='Go')])
                events = await collect_until(agent, sid, 'turn.completed')
                assert any(e.type == 'system.warning' for e in events)
            else:
                with pytest.raises(AgentError):
                    await agent.prompt(sid, [TextContentBlock(text='Go')])
                events = await collect_until(agent, sid, 'turn.failed')
                assert events[-1].payload['error_code'] in {'agent_exited', 'agent_disconnected', 'agent_request_failed'}
        finally:
            await agent.close()
    run(check())


def test_normalizer_limits_unknown_events_and_redacts_split_tokens():
    events = []
    normalizer = Normalizer(events.append)
    for kind in ['agent_message_chunk', 'agent_thought_chunk']:
        normalizer.update({'sessionUpdate': kind, 'content': {'text': 'ghp_' }})
        normalizer.update({'sessionUpdate': kind, 'content': {'text': 'a' * 5000}})
    normalizer.update({'sessionUpdate': 'future_update'})
    assert all('ghp_' not in json.dumps(e.payload) for e in events)
    assert events[-1].payload == {'type': 'future_update'}
    normalizer.update({'sessionUpdate': 'agent_thought_chunk', 'content': {'text': '思' * 5000}})
    normalizer.flush()
    assert len(events[-1].payload['text'].encode()) <= 4096
    normalizer.update({'sessionUpdate': 'tool_call', 'toolCallId': 'x', 'kind': 'execute', 'title': 'Test'})
    normalizer.update({'sessionUpdate': 'tool_call_update', 'toolCallId': 'x', 'status': 'failed',
        'content': [{'content': {'text': 'a' * 20000 + 'TAIL'}}]})
    assert len(events[-1].payload['output_tail'].encode()) <= 16384
    assert events[-1].payload['output_tail'].endswith('TAIL')


def test_process_restart_invalidates_old_permission_and_restores_session(tmp_path):
    async def check():
        agent = engine('permission')
        task = context(tmp_path)
        try:
            sid = await agent.ensure_session(task)
            turn = asyncio.create_task(agent.prompt(sid, [TextContentBlock(text='Go')]))
            events = await collect_until(agent, sid, 'permission.requested')
            request_id = events[-1].payload['request_id']
            agent.process.kill()
            await agent.process.wait()
            with pytest.raises(AgentError):
                await turn
            with pytest.raises(AgentError) as error:
                await agent.respond(request_id, RequestResponse(option_id='allow'))
            assert error.value.code == 'request_expired'
            assert await agent.ensure_session(task) == sid
        finally:
            await agent.close()
    run(check())
