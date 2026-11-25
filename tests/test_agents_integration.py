"""Opt-in acceptance against a user-owned issue and separately logged-in profiles."""

import asyncio
import contextlib
import os
import uuid

import pytest
from acp.schema import TextContentBlock

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.engine import RequestResponse, TaskContext
from contribos.agent.profiles import PROFILES
from contribos.config import Settings
from contribos.sandbox.host import HostSandbox
from contribos.sandbox.sbx import SbxSandbox
from contribos.tasks.instructions import first_prompt, task_instructions
from contribos.tasks.issue import fetch_issue, parse_issue_url


@pytest.mark.parametrize('profile_id,isolation', [
    *(pytest.param(name, 'host', marks=pytest.mark.agents) for name in PROFILES),
    *(pytest.param(name, 'sbx', marks=pytest.mark.sandbox) for name, profile in PROFILES.items() if profile.open_model),
])
def test_real_agent_acceptance(profile_id, isolation):
    selected = os.environ.get('CONTRIBOS_IT_AGENTS', '').split(',')
    if profile_id not in selected:
        pytest.skip('Set CONTRIBOS_IT_AGENTS to explicitly select real agents.')
    issue_url = os.environ.get('CONTRIBOS_IT_ISSUE_URL')
    if not issue_url:
        pytest.skip('Provide your own CONTRIBOS_IT_ISSUE_URL.')
    if isolation == 'host' and os.environ.get('CONTRIBOS_IT_TRUST_REPO') != '1':
        pytest.skip('Explicitly set CONTRIBOS_IT_TRUST_REPO=1 for the host acceptance test.')
    if isolation == 'sbx' and os.environ.get('CONTRIBOS_IT_SBX') != '1':
        pytest.skip('Set CONTRIBOS_IT_SBX=1 to run against Docker Sandboxes.')

    async def check():
        ref = parse_issue_url(issue_url)
        snapshot = await fetch_issue(ref)
        task = TaskContext(uuid.uuid4().hex, ref.repo, ref.number, '', '', trust_confirmed=isolation == 'host', isolation=isolation,
            issue=snapshot, linked_prs=snapshot['linked_prs'])
        marker = uuid.uuid4().hex
        task.issue['body'] += f'\nAcceptance marker: {marker}'
        settings = Settings()
        profile = PROFILES[profile_id]
        if profile.open_model:
            settings.open_model.update(provider=os.environ.get('CONTRIBOS_IT_MODEL_PROVIDER', 'deepseek'),
                                       model=os.environ['CONTRIBOS_IT_MODEL'],
                                       xcli_port=int(os.environ.get('CONTRIBOS_IT_XCLI_PORT', '8317')))
        sandbox = HostSandbox(settings) if isolation == 'host' else SbxSandbox(settings, profile)
        events = []
        command_started = asyncio.Event()
        async def refresh():
            await sandbox.diff()
        agent = AcpEngine(lambda: sandbox.spawn_agent(profile), refresh_diff=refresh,
                          stop_process=sandbox.stop, error_mapper=profile.classify_error)
        reader = None
        try:
            await sandbox.prepare(task)
            task.instructions = task_instructions(task)
            await sandbox.write_task_context(task)
            sid = await agent.ensure_session(task)
            async def consume():
                async for event in agent.events(sid):
                    events.append(event)
                    if event.type == 'command.started':
                        command_started.set()
                    elif event.type == 'permission.requested':
                        # A noninteractive test cannot grant host escalation on the user's behalf.
                        option = next((o['optionId'] for o in event.payload['options']
                                       if isolation == 'sbx' and o['kind'] == 'allow_once'), None)
                        await agent.respond(event.payload['request_id'], RequestResponse(option_id=option, cancelled=not option))
                    elif event.type == 'question.asked':
                        await agent.respond(event.payload['request_id'], RequestResponse(cancelled=True))
            reader = asyncio.create_task(consume())
            async def prompt(text):
                return await asyncio.wait_for(agent.prompt(task.session_id, [TextContentBlock(text=text)]), 240)
            probe = await prompt(
                'Before planning, use a tool to read the context file '
                f'{task.context_dir}/issue.md. It contains a public acceptance marker. '
                'Reply with the complete marker verbatim. Do not edit any files.'
            )
            assert probe.stop_reason == 'end_turn'
            assert any(marker in e.payload.get('text', '') for e in events if e.type == 'message.agent'), (
                'The Agent did not return the context marker.'
            )
            assert (await prompt(first_prompt(task))).stop_reason == 'end_turn'
            assert (await prompt('Implement the proposed solution and add relevant tests.')).stop_reason == 'end_turn'
            assert (await prompt('Run the relevant tests. Report exact commands and results.')).stop_reason == 'end_turn'
            assert await sandbox.diff()
            assert any(e.type == 'files.changed' for e in events)
            assert any(e.type == 'command.completed' for e in events)
            command_started.clear()
            slow = asyncio.create_task(prompt('Run a Python command that sleeps for 60 seconds; do not edit files.'))
            try:
                await asyncio.wait_for(command_started.wait(), 60)
                await agent.cancel(task.session_id)
                assert (await asyncio.wait_for(slow, 10)).stop_reason == 'cancelled'
            finally:
                if not slow.done():
                    await agent.cancel(task.session_id)
                    slow.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await slow
            task.recap = 'Implementation and tests completed; a long sleep was cancelled. Preserve the current changes.'
            await sandbox.stop()
            if isolation == 'sbx':
                await sandbox.restore(task)
            await agent.ensure_session(task)
            assert (await prompt('Continue from where you left off. Summarize the current changes and test results.')).stop_reason == 'end_turn'
        finally:
            await agent.close()
            if isolation == 'sbx':
                await sandbox.destroy()
            else:
                await sandbox.stop()
            if reader:
                await reader
            # Keep the independent workspace for user review.
    asyncio.run(check())
