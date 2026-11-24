"""Terminal debugging entrypoint; task supervision over HTTP belongs to M3."""

import asyncio
import contextlib
import json
import os
import re
import signal
import sqlite3
import stat
import sys
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, TextIO

from acp.schema import TextContentBlock

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.engine import AgentEngine, AgentError, EngineEvent, RequestResponse, TaskContext, TurnResult, validate_answers
from contribos.agent.profiles import AgentProfile, PROFILES
from contribos.config import Settings
from contribos.github import GitHubClient
from contribos.db import connect, migrate
from contribos.sandbox.base import Sandbox, SandboxError
from contribos.sandbox.host import HostSandbox, safe_text
from contribos.sandbox.sbx import SbxSandbox
from contribos.tasks.instructions import CONTINUE_PROMPT, first_prompt, task_instructions
from contribos.tasks.issue import IssueError, IssueRef, fetch_issue, parse_issue_url


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Terminal:
    def __init__(self, output: TextIO | None = None) -> None:
        self.output = output or sys.stdout
        self.reader: asyncio.StreamReader | None = None
        self.transport: Any = None

    async def readline(self) -> str | None:
        mode = os.fstat(sys.stdin.fileno()).st_mode
        if stat.S_ISREG(mode) or (stat.S_ISCHR(mode) and not sys.stdin.isatty()):
            value = sys.stdin.readline()
            return value.rstrip('\r\n') if value else None
        if self.reader is None:
            self.reader = asyncio.StreamReader()
            protocol = asyncio.StreamReaderProtocol(self.reader)
            stdin = sys.stdin
            if sys.stdin.isatty():
                # The read transport sets O_NONBLOCK. Reopen the TTY so stdout keeps its flags.
                fd = os.open(os.ttyname(sys.stdin.fileno()), os.O_RDONLY | os.O_NOCTTY)
                stdin = os.fdopen(fd, 'rb', buffering=0)
            try:
                self.transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, stdin)
            except BaseException:
                if stdin is not sys.stdin:
                    stdin.close()
                raise
        value = await self.reader.readline()
        return value.decode('utf-8', errors='replace').rstrip('\r\n') if value else None

    def write(self, text: str) -> None:
        # Agent/repository output must not inject terminal controls (including OSC).
        text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
        text = ''.join(c for c in safe_text(text) if c in '\n\t' or ord(c) >= 32)
        self.output.write(text)
        self.output.flush()

    def close(self) -> None:
        if self.transport:
            self.transport.close()


def question_response(payload: dict[str, Any], line: str) -> RequestResponse:
    if line.strip() == '/cancel':
        return RequestResponse(cancelled=True)
    schema = payload.get('schema') or {}
    properties = schema.get('properties') or {}
    if len(properties) == 1 and not line.lstrip().startswith('{'):
        name, spec = next(iter(properties.items()))
        value: Any = line
        if spec.get('type') != 'string':
            value = json.loads(line)
        answers = {name: value}
    else:
        answers = json.loads(line)
    validate_answers(schema, answers)
    return RequestResponse(answers=answers)


class ChatSession:
    def __init__(self, settings: Settings, profile: AgentProfile, console: Terminal, *,
                 sandbox: Sandbox | None = None,
                 engine_factory: Callable[[Sandbox, AgentProfile, Callable[[], Awaitable[None]]], AgentEngine] | None = None) -> None:
        self.settings, self.profile, self.console = settings, profile, console
        self.sandbox = sandbox
        self.engine_factory = engine_factory or self._make_engine
        self.engine: AgentEngine | None = None
        self.connection = connect(settings.database_path)
        try:
            migrate(self.connection)
        except sqlite3.Error:
            self.connection.close()
            raise
        self.task: TaskContext | None = None
        self.turn: asyncio.Task[TurnResult | AgentError] | None = None
        self.turn_id: str | None = None
        self.messages: deque[str] = deque()
        self.requests: deque[EngineEvent] = deque()
        self.inbox: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.eof = False
        self.quitting = False
        self.current_diff = ''
        self.message_streamed = False

    def _make_engine(self, sandbox: Sandbox, profile: AgentProfile, refresh: Callable[[], Awaitable[None]]) -> AgentEngine:
        values = tuple(v for k, v in os.environ.items() if v and any(p in k.upper() for p in ('TOKEN', 'SECRET', 'API_KEY', 'PASSWORD')))
        return AcpEngine(lambda: sandbox.spawn_agent(profile), refresh_diff=refresh,
                         stop_process=sandbox.stop, error_mapper=profile.classify_error, secrets=values)

    def _event(self, event: EngineEvent) -> None:
        if event.persistent:
            self.connection.execute('INSERT INTO task_events(task_id,turn_id,type,payload_json,created_at) VALUES (?,?,?,?,?)',
                (self.task.task_id, self.turn_id, event.type, safe_text(json.dumps(event.payload, ensure_ascii=False)), utc_now()))

    async def _refresh_diff(self) -> None:
        self.current_diff = await self.sandbox.diff()
        event = EngineEvent('diff.updated', {'has_changes': bool(self.current_diff)})
        self._event(event)
        self.console.write('[diff updated]\n')

    def select_sandbox(self, isolation: str) -> Sandbox:
        if self.sandbox is not None:
            if self.sandbox.kind != isolation:
                raise SandboxError('task_isolation_mismatch')
        elif isolation == 'host':
            self.sandbox = HostSandbox(self.settings)
        elif isolation == 'sbx' and self.profile.sbx:
            self.sandbox = SbxSandbox(self.settings, self.profile)
        else:
            raise SandboxError('sandbox_agent_unavailable')
        return self.sandbox

    async def create(self, ref: IssueRef, snapshot: dict[str, Any], isolation: str) -> None:
        sandbox = self.select_sandbox(isolation)
        snapshot = json.loads(safe_text(json.dumps(snapshot, ensure_ascii=False)))
        task_id = uuid.uuid4().hex
        self.task = TaskContext(task_id, ref.repo, ref.number, '', '', isolation=sandbox.kind,
            trust_confirmed=sandbox.kind == 'host',
            issue=snapshot, linked_prs=snapshot.get('linked_prs', []))
        now = utc_now()
        self.connection.execute('''INSERT INTO tasks(id,repo_full_name,issue_number,title,agent_profile,isolation,
            issue_snapshot_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)''',
            (task_id, ref.repo, ref.number, snapshot['title'], self.profile.id, sandbox.kind,
             safe_text(json.dumps(snapshot)), now, now))
        label = 'host (trusted repository)' if sandbox.kind == 'host' else sandbox.kind
        self.console.write(f'Task: {task_id}\nIsolation: {label}\nPreparing independent workspace...\n')
        try:
            workspace = await sandbox.prepare(self.task)
            self.task.repo_dir, self.task.context_dir = workspace.repo_dir, workspace.context_dir
            self.task.default_branch, self.task.base_sha, self.task.work_branch = (
                workspace.default_branch, workspace.base_sha, workspace.work_branch)
            self.task.sandbox_ref = workspace.reference
            self.task.instructions = task_instructions(self.task)
            await sandbox.write_task_context(self.task)
            if isinstance(sandbox, HostSandbox):
                sandbox.claim_runtime()
            self.connection.execute('''UPDATE tasks SET status='ready',default_branch=?,base_sha=?,work_branch=?,
                sandbox_ref=?,updated_at=? WHERE id=? AND status='preparing' ''',
                (self.task.default_branch, self.task.base_sha, self.task.work_branch, workspace.reference, utc_now(), task_id))
        except (SandboxError, OSError):
            self.connection.execute("UPDATE tasks SET status='setup_failed',last_error_code='workspace_setup_failed',updated_at=? WHERE id=? AND status='preparing'", (utc_now(), task_id))
            raise

    async def resume(self, task_id: str, isolation: str | None = None) -> None:
        row = self.connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row or row['status'] not in {'ready', 'published'} or row['delete_requested']:
            raise SandboxError('task_not_resumable')
        if row['agent_profile'] != self.profile.id:
            raise SandboxError('agent_profile_mismatch')
        if isolation and isolation != row['isolation']:
            raise SandboxError('task_isolation_mismatch')
        sandbox = self.select_sandbox(row['isolation'])
        snapshot = json.loads(row['issue_snapshot_json'])
        self.task = TaskContext(task_id, row['repo_full_name'], row['issue_number'], '', '',
            session_id=row['session_id'], isolation=sandbox.kind, trust_confirmed=sandbox.kind == 'host',
            issue=snapshot, sandbox_ref=row['sandbox_ref'],
            linked_prs=snapshot.get('linked_prs', []), default_branch=row['default_branch'],
            base_sha=row['base_sha'], work_branch=row['work_branch'], recap_pending=bool(row['recap_pending']))
        await sandbox.restore(self.task)
        self.task.instructions = task_instructions(self.task)
        self.connection.execute("UPDATE turns SET status='interrupted',completed_at=? WHERE task_id=? AND status='running'", (utc_now(), task_id))
        self.connection.execute("UPDATE tasks SET runtime_managed=0,agent_state='idle',updated_at=? WHERE id=? AND agent_state=?",
                                (utc_now(), task_id, row['agent_state']))
        label = 'host (previously trusted)' if sandbox.kind == 'host' else sandbox.kind
        self.console.write(f'Task: {task_id}\nIsolation: {label}\n')

    async def _read_input(self) -> None:
        while True:
            try:
                line = await self.console.readline()
            except (OSError, ValueError, RuntimeError):
                self.console.write('\n[terminal_input_failed] Input closed.\n')
                line = None
            self.inbox.put_nowait(('input', line))
            if line is None:
                return

    async def _read_events(self) -> None:
        async for event in self.engine.events(self.task.session_id or ''):
            self.inbox.put_nowait(('event', event))

    async def _start_turn(self, message: str, *, first: bool = False) -> None:
        if self.turn is not None:
            self.messages.append(message)
            return
        if not first:
            rows = self.connection.execute("SELECT type,payload_json FROM task_events WHERE task_id=? AND type IN ('message.user','message.agent') ORDER BY seq DESC LIMIT 12", (self.task.task_id,)).fetchall()
            recap = '\n'.join(r['type'] + ': ' + json.loads(r['payload_json']).get('text', '') for r in reversed(rows))
            self.task.recap = safe_text(recap[-8000:] + '\nCurrent diff:\n' + (await self.sandbox.diff())[-16000:])
        try:
            sid = await self.engine.ensure_session(self.task)
        except AgentError as exc:
            self.connection.execute("UPDATE tasks SET agent_state='error',last_error_code=?,last_error_message=?,updated_at=? WHERE id=? AND agent_state IN ('idle','error')", (exc.code, exc.message, utc_now(), self.task.task_id))
            self._event(EngineEvent('turn.failed', {'error_code': exc.code, 'message': exc.message}))
            self.console.write(f'[{exc.code}] {exc.message}\nUse /continue after correcting the problem.\n')
            return
        self.connection.execute('UPDATE tasks SET session_id=?,agent_caps_json=?,recap_pending=?,updated_at=? WHERE id=?',
            (sid, json.dumps(asdict(self.engine.capabilities)), int(self.task.recap_pending), utc_now(), self.task.task_id))
        changed = self.connection.execute("UPDATE tasks SET agent_state='running',updated_at=? WHERE id=? AND agent_state IN ('idle','error')", (utc_now(), self.task.task_id)).rowcount
        if not changed:
            raise AgentError('task_busy')
        self.turn_id = uuid.uuid4().hex
        self.connection.execute("INSERT INTO turns(id,task_id,kind,status,started_at) VALUES (?,?,?,'running',?)", (self.turn_id, self.task.task_id, 'continue' if message == CONTINUE_PROMPT else 'user', utc_now()))
        self._event(EngineEvent('message.user', {'text': safe_text(message)}))
        async def prompt() -> TurnResult | AgentError:
            try:
                return await self.engine.prompt(sid, [TextContentBlock(text=safe_text(message))])
            except AgentError as exc:
                return exc
        self.turn = asyncio.create_task(prompt())

    async def _display_event(self, event: EngineEvent) -> None:
        self._event(event)
        payload = event.payload
        if event.type == 'message.delta':
            self.message_streamed = True
            self.console.write(payload.get('text', ''))
        elif event.type == 'message.agent':
            if not self.message_streamed:
                self.console.write(payload.get('text', ''))
            self.message_streamed = False
        elif event.type == 'reasoning.summary':
            self.console.write('\n[thinking] ' + payload.get('text', '') + '\n')
        elif event.type in {'permission.requested', 'question.asked'}:
            self.requests.append(event)
            self.console.write('\n' + json.dumps(payload, ensure_ascii=False) + '\n' +
                ('Allow this operation? [y/n] ' if event.type == 'permission.requested' else 'Answer (single value or JSON object; /cancel): '))
            if self.eof:
                await self.engine.respond(payload['request_id'], RequestResponse(cancelled=True))
                self.requests.pop()
        elif event.type in {'command.started', 'command.completed', 'files.changed', 'tool.activity', 'plan.updated', 'system.session_recreated', 'system.agent_exited'}:
            self.console.write('\n[' + event.type + '] ' + json.dumps(payload, ensure_ascii=False) + '\n')
        elif event.type in {'turn.completed', 'turn.failed'} and self.turn:
            result = await self.turn
            reason = result.stop_reason if isinstance(result, TurnResult) else None
            status = 'interrupted' if reason == 'cancelled' else ('failed' if isinstance(result, AgentError) else 'completed')
            self.connection.execute("UPDATE turns SET status=?,stop_reason=?,error_code=?,completed_at=? WHERE id=? AND status='running'", (status, reason, result.code if isinstance(result, AgentError) else None, utc_now(), self.turn_id))
            self.connection.execute("""UPDATE tasks SET agent_state=?,last_error_code=?,last_error_message=?,
                recap_pending=?,updated_at=? WHERE id=? AND agent_state='running'""",
                ('error' if isinstance(result, AgentError) else 'idle', result.code if isinstance(result, AgentError) else None,
                 result.message if isinstance(result, AgentError) else None, int(self.task.recap_pending), utc_now(), self.task.task_id))
            self.console.write(f'\n[{event.type}] ' + json.dumps(payload, ensure_ascii=False) + '\n> ')
            self.turn = None
            self.turn_id = None
            self.requests.clear()
            if self.messages and not self.quitting:
                await self._start_turn(self.messages.popleft())

    async def _interrupt(self) -> None:
        if not self.turn:
            self.quitting = True
            return
        await self.engine.cancel(self.task.session_id)
        try:
            await asyncio.wait_for(asyncio.shield(self.turn), 9)
        except TimeoutError:
            await self.sandbox.stop()
            self.console.write('\nAgent did not stop in time; process stopped. Use /continue to reconnect.\n')
        else:
            if self.sandbox.kind == 'sbx':
                await self.sandbox.stop()

    async def _input(self, line: str | None) -> None:
        if line is None:
            self.eof = True
            for request in self.requests:
                with contextlib.suppress(AgentError):
                    await self.engine.respond(request.payload['request_id'], RequestResponse(cancelled=True))
            self.requests.clear()
            return
        if line in {'/quit', '/exit'}:
            self.quitting = True
            if self.turn:
                await self._interrupt()
        elif line == '/diff':
            self.console.write((await self.sandbox.diff()) or '(no changes)\n')
        elif self.requests:
            request = self.requests[0]
            try:
                if request.type == 'permission.requested':
                    if line.lower() not in {'y', 'n'}:
                        raise ValueError()
                    kind = 'allow_once' if line.lower() == 'y' else 'reject_once'
                    option = next((o for o in request.payload['options'] if o['kind'] == kind), None)
                    response = RequestResponse(option_id=option['optionId'] if option else None, cancelled=option is None)
                else:
                    response = question_response(request.payload, line)
                await self.engine.respond(request.payload['request_id'], response)
                self.requests.popleft()
            except (ValueError, AgentError):
                self.console.write('Invalid or expired response; use the displayed options.\n')
        elif line.strip():
            await self._start_turn(CONTINUE_PROMPT if line == '/continue' else line)

    async def converse(self, *, resume: bool = False) -> None:
        self.engine = self.engine_factory(self.sandbox, self.profile, self._refresh_diff)
        input_task = asyncio.create_task(self._read_input())
        events_task = asyncio.create_task(self._read_events())
        loop = asyncio.get_running_loop()
        previous = signal.getsignal(signal.SIGINT)
        loop.add_signal_handler(signal.SIGINT, self.inbox.put_nowait, ('interrupt', None))
        self.console.write('Commands: /continue /diff /quit; Ctrl-C cancels the current turn.\n')
        try:
            await self._start_turn(CONTINUE_PROMPT if resume else first_prompt(self.task), first=not resume)
            while not ((self.eof or self.quitting) and not self.turn and not self.messages):
                kind, value = await self.inbox.get()
                if kind == 'event':
                    await self._display_event(value)
                elif kind == 'interrupt':
                    self.messages.clear()
                    await self._interrupt()
                else:
                    await self._input(value)
                if self.quitting:
                    self.messages.clear()
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous)
            input_task.cancel()
            await self.engine.close()
            if self.turn:
                await self.turn
                self.connection.execute("UPDATE turns SET status='interrupted',completed_at=? WHERE id=? AND status='running'", (utc_now(), self.turn_id))
                self.connection.execute("UPDATE tasks SET agent_state='idle',updated_at=? WHERE id=? AND agent_state='running'", (utc_now(), self.task.task_id))
            events_task.cancel()
            for job in (input_task, events_task):
                with contextlib.suppress(asyncio.CancelledError):
                    await job

    async def close(self) -> None:
        try:
            if self.sandbox:
                with contextlib.suppress(OSError, SandboxError):
                    await self.sandbox.stop()
        finally:
            if isinstance(self.sandbox, (HostSandbox, SbxSandbox)):
                self.sandbox.release_runtime()
            self.console.close()
            self.connection.close()


async def run_chat(settings: Settings, agent: str, issue_url: str | None, *, trust_repo: bool = False,
                   isolation: str | None = None, resume_task: str | None = None, console: Terminal | None = None,
                   loader: Callable[[IssueRef], Awaitable[dict[str, Any]]] | None = None,
                   sandbox: Sandbox | None = None,
                   engine_factory: Callable[..., AgentEngine] | None = None) -> int:
    console = console or Terminal()
    session: ChatSession | None = None
    try:
        level = isolation or (sandbox.kind if sandbox else 'host')
        if trust_repo and level != 'host':
            raise SandboxError('host_trust_requires_host')
        if not resume_task:
            ref = parse_issue_url(issue_url or '')
            if level == 'host' and not trust_repo:
                console.write(f'本机直跑 {ref.repo}：Agent 和仓库代码将以你的用户身份运行。仅用于你信任的仓库。\n输入 yes 确认信任并选择本机直跑：')
                if await console.readline() != 'yes':
                    console.write('\n[host_trust_required] No task was created.\n')
                    return 2
        session = ChatSession(settings, PROFILES[agent], console, sandbox=sandbox, engine_factory=engine_factory)
        if resume_task:
            await session.resume(resume_task, isolation)
        else:
            session.select_sandbox(level)
            snapshot = await loader(ref) if loader else await fetch_issue(ref, github=GitHubClient(settings))
            await session.create(ref, snapshot, level)
        await session.converse(resume=bool(resume_task))
        return 0
    except (IssueError, SandboxError, AgentError) as exc:
        code = exc.code if isinstance(exc, AgentError) else str(exc)
        message = {
            'sbx_unavailable': 'Install Docker Sandboxes and ensure sbx is on PATH.',
            'sbx_deny_all_required': 'Docker Sandboxes needs an initialized deny-all policy.',
            'sbx_network_policy_unsafe': 'Docker Sandboxes has egress rules outside the task allowlist. Check sbx policy ls.',
            'sbx_credential_policy_unsafe': 'Check sbx credential and SSH forwarding settings for sources outside the task profile.',
            'sbx_oauth_required': 'Configure the Docker Sandboxes OAuth credential for this Agent.',
            'sandbox_agent_unavailable': 'The selected Agent is unavailable in this isolation level.',
        }.get(code, exc.message if isinstance(exc, IssueError) else
              'Chat could not proceed. Use contribos agents to inspect Agent setup.')
        console.write(f'\n[{code}] {message}\n')
        return 1
    except (OSError, ValueError):
        console.write('\n[chat_unavailable] Could not open the local task or terminal.\n')
        return 1
    except sqlite3.Error:
        console.write('\n[local_database_unavailable] Could not access the local task database.\n')
        return 1
    finally:
        if session:
            await session.close()
        else:
            console.close()
