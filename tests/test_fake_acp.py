import asyncio
import os
import sys
from pathlib import Path

from acp import Client, spawn_agent_process
from acp.schema import ClientCapabilities, TextContentBlock

SCRIPT = Path(__file__).with_name('fake_acp_agent.py')


class RecordingClient(Client):
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)


def test_scripted_agent_runs_real_sdk_transport_and_own_tools(tmp_path):
    (tmp_path / 'issue.md').write_text('Offline issue')

    async def run():
        client = RecordingClient()
        async with spawn_agent_process(client, sys.executable, str(SCRIPT), env={**os.environ, 'FAKE_ACP_SCENARIO': 'workflow'}) as (conn, proc):
            initialized = await conn.initialize(protocol_version=1, client_capabilities=ClientCapabilities(fs=None, terminal=None))
            assert initialized.agent_capabilities.load_session
            session = await conn.new_session(cwd=str(tmp_path), additional_directories=[str(tmp_path)], mcp_servers=[])
            result = await conn.prompt(session_id=session.session_id, prompt=[TextContentBlock(text='Implement')])
            assert result.stop_reason == 'end_turn'
            assert (tmp_path / 'agent-output.txt').read_text() == 'implemented\n'
            updates = [u.model_dump(by_alias=True) for u in client.updates]
            assert {u.get('kind') for u in updates} >= {'read', 'edit', 'execute'}
            assert any(u.get('rawOutput') == {'exit_code': 0} for u in updates)
    asyncio.run(asyncio.wait_for(run(), 10))
