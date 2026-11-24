"""Offline SDK agent; scenarios are selected with FAKE_ACP_SCENARIO."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from acp import Agent, RequestError, run_agent
from acp import schema as s


class ScriptedAgent(Agent):
    def __init__(self) -> None:
        self.scenario = os.environ.get('FAKE_ACP_SCENARIO', 'workflow')
        self.cancelled = asyncio.Event()
        self.cwd = Path.cwd()
        self.context = self.cwd
        self.mode = 'build' if self.scenario.startswith('review_') else 'plan'

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, client_capabilities: Any = None, **kwargs: Any) -> s.InitializeResponse:
        assert not client_capabilities.fs or not (client_capabilities.fs.read_text_file or client_capabilities.fs.write_text_file)
        assert not client_capabilities.terminal
        caps = s.AgentCapabilities(
            load_session=self.scenario not in {'no_restore', 'exit_recreate'},
            session_capabilities=s.SessionCapabilities(resume={} if self.scenario in {'resume', 'exit_resume'} else None),
        )
        return s.InitializeResponse(protocol_version=1, agent_capabilities=caps, auth_methods=[])

    async def new_session(self, cwd: str, additional_directories: list[str] | None = None,
                          mcp_servers: list[Any] | None = None, **kwargs: Any) -> s.NewSessionResponse:
        assert mcp_servers == []
        assert additional_directories and len(additional_directories) == 1
        self.cwd, self.context = Path(cwd), Path(additional_directories[0])
        sid = f'fake-{os.getpid()}' if self.scenario == 'exit_recreate' else 'fake-session'
        return s.NewSessionResponse(session_id=sid, modes=s.SessionModeState(
            current_mode_id=self.mode, available_modes=[s.SessionMode(id='plan', name='Plan'), s.SessionMode(id='build', name='Build')],
        ))

    async def load_session(self, cwd: str, session_id: str, additional_directories: list[str] | None = None,
                           mcp_servers: list[Any] | None = None, **kwargs: Any) -> s.LoadSessionResponse:
        if self.scenario == 'load_fail':
            raise RequestError(-32000, 'Session is unavailable')
        await self.new_session(cwd, additional_directories, mcp_servers)
        await self.message(session_id, 'loaded')
        return s.LoadSessionResponse()

    async def resume_session(self, **kwargs: Any) -> s.ResumeSessionResponse:
        await self.load_session(**kwargs)
        return s.ResumeSessionResponse()

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> s.SetSessionModeResponse:
        self.mode = mode_id
        await self.conn.session_update(session_id, s.CurrentModeUpdate(current_mode_id=mode_id))
        return s.SetSessionModeResponse()

    async def message(self, sid: str, text: str) -> None:
        await self.conn.session_update(sid, s.AgentMessageChunk(content=s.TextContentBlock(text=text)))

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> s.PromptResponse:
        self.cancelled.clear()
        scenario = self.scenario
        if scenario in {'exit_resume', 'exit_recreate'} and prompt[-1].text == 'Crash now':
            (self.cwd / 'preserved.txt').write_text('Current edits\n')
            os._exit(23)
        if scenario in {'no_cancel', 'leaves_child'}:
            if prompt[-1].text == 'Run a long test':
                child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)',
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=scenario == 'leaves_child')
                (self.cwd / 'child-pid').write_text(str(child.pid))
                await self.message(session_id, 'Test is running\n')
                if scenario == 'no_cancel':
                    await child.wait()
                else:
                    await self.cancelled.wait()
                return s.PromptResponse(stop_reason='cancelled')
            await self.message(session_id, 'Agent is ready')
            return s.PromptResponse(stop_reason='end_turn')
        if scenario == 'exit':
            os._exit(23)
        if scenario == 'error':
            raise RequestError(-32000, os.environ.get('FAKE_ACP_ERROR', 'Rate limit reached'))
        if scenario == 'slow':
            await self.message(session_id, 'waiting')
            await self.cancelled.wait()
            return s.PromptResponse(stop_reason='cancelled')
        if scenario == 'permission':
            result = await self.conn.request_permission(session_id=session_id,
                tool_call=s.ToolCallProgress(tool_call_id='permission-tool', title='Run test', kind='execute'),
                options=[s.PermissionOption(option_id='allow', name='Allow once', kind='allow_once'),
                         s.PermissionOption(option_id='deny', name='Reject once', kind='reject_once')])
            await self.message(session_id, result.model_dump_json(by_alias=True))
        elif scenario == 'question':
            result = await self.conn.create_elicitation(message='Which test?', mode=s.ElicitationFormSessionMode(
                session_id=session_id, requested_schema=s.ElicitationSchema(type='object', properties={
                    'test': {'type': 'string', 'enum': ['unit', 'all']}}, required=['test'])))
            await self.message(session_id, result.model_dump_json(by_alias=True))
        elif scenario == 'forbidden':
            try:
                await self.conn.read_text_file(session_id=session_id, path=str(self.cwd / 'input.txt'))
            except RequestError:
                await self.message(session_id, 'client filesystem denied')
        elif scenario in {'echo', 'resume', 'no_restore', 'load_fail', 'exit_resume', 'exit_recreate'}:
            for block in prompt:
                await self.message(session_id, block.text)
        elif scenario == 'environment':
            keys = ('CONTRIBOS_GITHUB_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN', 'SSH_AUTH_SOCK', 'GH_CONFIG_DIR',
                    'GIT_CONFIG_GLOBAL', 'GIT_TERMINAL_PROMPT')
            await self.message(session_id, json.dumps({k: os.environ.get(k) for k in keys}))
        elif scenario.startswith('review_'):
            assert self.mode == 'plan' and 'Diff excerpt' in prompt[-1].text
            assert '+Changed' in prompt[-1].text
            if scenario == 'review_permission':
                permission = await self.conn.request_permission(session_id=session_id,
                    tool_call=s.ToolCallProgress(tool_call_id='inspect-review', title='Read test sources', kind='read'),
                    options=[s.PermissionOption(option_id='allow', name='Allow once', kind='allow_once')])
                assert permission.outcome.option_id == 'allow'
            if scenario == 'review_mutates':
                (self.cwd / 'README.md').write_text('Modified by reviewer\n')
            result = {'verdict': 'request_changes', 'summary': 'Add a regression test.', 'findings': [
                {'severity': 'medium', 'file': 'README.md', 'line': 1, 'message': 'Cover the change with a test.'}]}
            payload = '{broken' if scenario == 'review_malformed' else json.dumps(result)
            await self.message(session_id, f'```json\n{payload}\n```')
        else:
            await self.conn.session_update(session_id, s.AgentThoughtChunk(content=s.TextContentBlock(text='Inspect ')))
            await self.conn.session_update(session_id, s.AgentThoughtChunk(content=s.TextContentBlock(text='the task.')))
            await self.message(session_id, 'A ')
            await self.message(session_id, 'plan.')
            await self.conn.session_update(session_id, s.ToolCallStart(tool_call_id='read', title='Read context', kind='read'))
            text = (self.context / 'issue.md').read_text()
            await self.conn.session_update(session_id, s.ToolCallProgress(tool_call_id='read', status='completed',
                content=[s.ContentToolCallContent(content=s.TextContentBlock(text=text))]))
            target = self.cwd / 'agent-output.txt'
            await self.conn.session_update(session_id, s.ToolCallStart(tool_call_id='edit', title='Write output', kind='edit',
                locations=[s.ToolCallLocation(path=str(target))]))
            target.write_text('implemented\n')
            await self.conn.session_update(session_id, s.ToolCallProgress(tool_call_id='edit', status='completed'))
            await self.conn.session_update(session_id, s.ToolCallStart(tool_call_id='exec', title='Run test', kind='execute',
                raw_input={'command': 'python -c "assert True"'}))
            proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'assert True', cwd=self.cwd)
            code = await proc.wait()
            await self.conn.session_update(session_id, s.ToolCallProgress(tool_call_id='exec', status='completed',
                raw_output={'exit_code': code}, content=[s.ContentToolCallContent(content=s.TextContentBlock(text='tests passed'))]))
            await self.conn.session_update(session_id, s.AgentPlanUpdate(entries=[s.PlanEntry(content='Implement', priority='high', status='completed')]))
            await self.message(session_id, 'Done.')
        return s.PromptResponse(stop_reason='end_turn')

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self.cancelled.set()


if __name__ == '__main__':
    asyncio.run(run_agent(ScriptedAgent(), use_unstable_protocol=True))
