"""Run durable task queues independently of HTTP requests and event subscribers."""

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from acp.schema import TextContentBlock

from contribos.agent.acp_engine import AcpEngine
from contribos.agent.engine import AgentEngine, AgentError, EngineEvent, RequestResponse, TaskContext, validate_answers
from contribos.agent.profiles import PROFILES, AgentProfile
from contribos.config import Settings
from contribos.sandbox.base import Sandbox, SandboxError
from contribos.sandbox.host import HostSandbox
from contribos.sandbox.read_file import __file__ as READ_FILE_PATH
from contribos.tasks.events import RESOLUTION_EVENTS, TaskEvents
from contribos.tasks.instructions import CONTINUE_PROMPT, first_prompt, task_instructions
from contribos.tasks.issue import IssueError, IssueRef, fetch_issue
from contribos.tasks.store import TaskError, TaskStore

SandboxFactory = Callable[[], Sandbox]
EngineFactory = Callable[[Sandbox, AgentProfile], AgentEngine]
Isolation = Literal['host', 'sbx', 'container']
RETRY_DELAYS = (15, 60, 180)
READ_FILE = Path(READ_FILE_PATH).read_text(encoding='utf-8')


@dataclass
class _Runtime:
    context: TaskContext
    sandbox: Sandbox
    profile: AgentProfile
    engine: AgentEngine | None = None
    reader: asyncio.Task[None] | None = None
    job: asyncio.Task[None] | None = None
    diff_job: asyncio.Task[None] | None = None
    issue_job: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    turn_id: str | None = None
    deleting: bool = False
    cleanup: asyncio.Task[None] | None = None
    recovery: str | None = None
    disconnected: bool = False
    last_activity: float = field(default_factory=time.monotonic)
    sandbox_suspended: bool = False
    allowed_tools: set[str] = field(default_factory=set)


def _permission_key(payload: dict[str, Any]) -> str | None:
    tool = payload.get('tool_call')
    if not isinstance(tool, dict) or not (tool.get('rawInput') or tool.get('locations')):
        return None
    return json.dumps({k: v for k, v in tool.items() if k != 'toolCallId'}, sort_keys=True, ensure_ascii=False)


def _one_time_allow(payload: dict[str, Any]) -> str | None:
    return next((option.get('optionId') for option in payload.get('options', [])
                 if option.get('kind') == 'allow_once'), None)


class TaskSupervisor:
    def __init__(self, settings: Settings, store: TaskStore, *, events: TaskEvents | None = None,
                 issue_loader: Callable[[IssueRef], Awaitable[dict[str, Any]]] = fetch_issue,
                 sandbox_factories: dict[str, SandboxFactory] | None = None,
                 engine_factory: EngineFactory | None = None,
                 cancel_timeout: float = 10, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.store = store
        self.events = events or TaskEvents(store)
        self.issue_loader = issue_loader
        self.sandboxes = sandbox_factories if sandbox_factories is not None else {'host': lambda: HostSandbox(settings)}
        self.engine_factory = engine_factory or self._engine
        self.cancel_timeout = cancel_timeout
        self.sleep, self.clock = sleep, clock
        self.slots = asyncio.Semaphore(max(1, int(store.setting('concurrency', 2))))
        self.runtimes: dict[str, _Runtime] = {}
        self.closing = False
        self.maintenance: asyncio.Task[None] | None = None

    def _engine(self, sandbox: Sandbox, profile: AgentProfile) -> AgentEngine:
        return AcpEngine(lambda: sandbox.spawn_agent(profile), stop_process=sandbox.stop,
                         error_mapper=profile.classify_error, clean_text=self.store.clean, replay_session_updates=False)

    def _sandbox(self, isolation: str) -> Sandbox:
        factory = self.sandboxes.get(isolation)
        if factory is None:
            raise TaskError('sandbox_unavailable', 'The selected execution environment is unavailable.', 503)
        sandbox = factory()
        if sandbox.kind != isolation:
            raise TaskError('sandbox_unavailable', 'The selected execution environment is unavailable.', 503)
        return sandbox

    def create(self, ref: IssueRef, *, agent: str | None = None, isolation: Isolation | None = None,
               trust_confirmed: bool = False, trust_owner: bool = False,
               opportunity_id: str | None = None) -> sqlite3.Row:
        if self.store.clean(ref.repo) != ref.repo or not 0 < ref.number <= 2**63 - 1:
            raise TaskError('invalid_issue_url', 'Provide a GitHub Issue URL.', 422)
        if agent is None:
            agent = self.store.setting('default_agent', 'codex')
        profile = PROFILES.get(agent)
        if profile is None:
            raise TaskError('unknown_agent', 'The selected Agent does not exist.', 422)
        if isolation is None:
            configured = self.store.setting('sandbox_backend', 'sbx')
            isolation = configured if configured in {'sbx', 'container'} else 'sbx'
        owner = ref.repo.split('/', 1)[0].casefold()
        if trust_owner and (isolation != 'host' or trust_confirmed is not True):
            raise TaskError('host_trust_required', 'Confirm repository trust before adding the owner.', 403)
        sandbox = self._sandbox(isolation)
        with self.store.transaction():
            trusted = self.store.setting('trusted_owners', [])
            known = owner in {name.casefold() for name in trusted}
            if isolation == 'host' and not known and trust_confirmed is not True:
                raise TaskError('host_trust_required', 'Confirm repository trust to run on the host.', 403)
            row = self.store.create(ref, profile.id, isolation, opportunity_id)
            if trust_owner and not known:
                self.store.set_setting('trusted_owners', [*trusted, owner])
        runtime = self._runtime(row, sandbox)
        self._schedule(runtime)
        return row

    def _runtime(self, row: sqlite3.Row, sandbox: Sandbox | None = None) -> _Runtime:
        task_id = row['id']
        if task_id not in self.runtimes:
            snapshot = json.loads(row['issue_snapshot_json'])
            context = TaskContext(task_id, row['repo_full_name'], row['issue_number'], '', '',
                isolation=cast(Isolation, row['isolation']), trust_confirmed=row['isolation'] == 'host',
                session_id=row['session_id'], issue=snapshot, linked_prs=snapshot.get('linked_prs', []),
                default_branch=row['default_branch'] or '', base_sha=row['base_sha'] or '',
                work_branch=row['work_branch'] or '', sandbox_ref=row['sandbox_ref'],
                recap_pending=bool(row['recap_pending']))
            runtime = _Runtime(context, sandbox or self._sandbox(row['isolation']), PROFILES[row['agent_profile']])
            if row['isolation'] == 'host':
                runtime.allowed_tools = {key for event in self.events.remembered_permissions(task_id)
                                         if (key := _permission_key(event.payload)) and '[REDACTED]' not in key}
            self.runtimes[task_id] = runtime
        return self.runtimes[task_id]

    def _schedule(self, runtime: _Runtime) -> None:
        if not self.closing and not runtime.deleting and (runtime.job is None or runtime.job.done()):
            runtime.job = asyncio.create_task(self._run(runtime))

    def _publish(self, runtime: _Runtime, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.events.publish(runtime.context.task_id, runtime.turn_id, EngineEvent(kind, payload or {}))

    def start(self) -> None:
        self.maintenance = asyncio.create_task(self._sweep_idle())
        for task in self.store.recoverable():
            task_id = task['id']
            if task['delete_requested']:
                try:
                    self.delete(task_id)
                except TaskError:
                    self.store.request_delete(task_id, False)
                continue
            self._expire_requests(task_id)
            if turn := self.store.running(task_id):
                self.store.finish(task_id, turn['id'], 'interrupted', reason='cancelled')
                self.events.publish(task_id, turn['id'], EngineEvent('turn.interrupted', {'reason': 'service_restart'}))
            try:
                runtime = self._runtime(task)
            except TaskError as error:
                if task['status'] == 'preparing':
                    self.store.setup_failed(task_id, error.code, error.message)
                else:
                    self.store.recovery_failed(task_id, error.code, error.message)
                continue
            if task['agent_state'] in {'running', 'waiting_retry'}:
                runtime.recovery = 'service_restart'
            self._schedule(runtime)

    def send(self, task_id: str, text: str, client_message_id: str | None = None, *,
             interrupt: bool = False, kind: str = 'user') -> sqlite3.Row:
        row = self.store.get(task_id)
        runtime = self._runtime(row)
        if runtime.deleting:
            raise TaskError('task_deleting', 'This task is being deleted.')
        if not row['runtime_managed'] and row['status'] in {'ready', 'published'}:
            if isinstance(runtime.sandbox, HostSandbox):
                try:
                    runtime.sandbox.attach(runtime.context)
                    runtime.sandbox.claim_runtime()
                except (OSError, SandboxError):
                    raise TaskError('workspace_unavailable', 'This workspace is unavailable or already open.') from None
            self.store.adopt(task_id)
        turn, created = self.store.enqueue(task_id, text, client_message_id, kind=kind, interrupt=interrupt)
        if created:
            self.events.notify_message(turn)
            if interrupt:
                self.interrupt(task_id)
            self._schedule(runtime)
        return turn

    def continue_task(self, task_id: str) -> sqlite3.Row:
        return self.send(task_id, CONTINUE_PROMPT, kind='continue')

    def refresh_issue(self, task_id: str) -> None:
        row = self.store.get(task_id)
        if row['status'] not in {'ready', 'published'} or row['delete_requested']:
            raise TaskError('task_not_ready', 'The Issue snapshot is unavailable.')
        runtime = self._runtime(row)
        if runtime.issue_job is None or runtime.issue_job.done():
            runtime.issue_job = asyncio.create_task(self._refresh_issue(runtime))

    async def _refresh_issue(self, runtime: _Runtime) -> None:
        context = runtime.context
        try:
            issue = await self.issue_loader(IssueRef(context.repo_full_name, context.issue_number))
        except (IssueError, OSError):
            self._publish(runtime, 'issue.refresh_failed', {'message': 'Could not refresh the GitHub Issue.'})
            return
        self.store.update_issue(context.task_id, issue)
        self._publish(runtime, 'issue.updated')

    def interrupt(self, task_id: str) -> bool:
        self.store.get(task_id)
        runtime = self.runtimes.get(task_id)
        if runtime and runtime.turn_id:
            runtime.stop.set()
            return True
        stopped = self.store.stop_retry(task_id)
        if stopped and runtime:
            runtime.wake.set()
        return stopped

    async def respond(self, task_id: str, request_id: str, response: RequestResponse) -> None:
        self.store.get(task_id)
        runtime = self.runtimes.get(task_id)
        request = self.events.pending(task_id).get(request_id)
        if not runtime or not runtime.engine or not request or runtime.turn_id != request.turn_id:
            raise TaskError('request_expired', 'This request has expired.')
        grant = None
        if request.type == 'permission.requested':
            options = request.payload.get('options', [])
            option = next((item for item in options if item.get('optionId') == response.option_id), None)
            if response.option_id is None or option is None:
                raise TaskError('invalid_option', 'Choose a permission option from this request.', 422)
            if option.get('kind') == 'reject_always':
                response = RequestResponse(cancelled=True)
            if option.get('kind') == 'allow_always':
                once = _one_time_allow(request.payload)
                grant = _permission_key(request.payload)
                if not once or not grant or '[REDACTED]' in grant:
                    raise TaskError('invalid_option', 'Choose a one-time option for this request.', 422)
                response = RequestResponse(option_id=once)
        else:
            try:
                validate_answers(request.payload.get('schema') or {}, response.answers)
            except ValueError:
                raise TaskError('invalid_answers', 'Provide valid answers for this request.', 422) from None
        try:
            await runtime.engine.respond(request_id, response)
        except AgentError as error:
            raise TaskError(error.code, error.message) from None
        if grant:
            runtime.allowed_tools.add(grant)
            self._publish(runtime, 'permission.remembered', {'request_id': request_id})

    async def read_file(self, task_id: str, path: str) -> dict[str, str]:
        row = self.store.get(task_id)
        relative = PurePosixPath(path)
        if (relative.is_absolute() or not relative.parts or '\\' in path or '\x00' in path
                or any(part.casefold() in {'..', '.git'} for part in relative.parts)):
            raise TaskError('invalid_file_path', 'Use a file path relative to the workspace.', 422)
        if row['status'] not in {'ready', 'published'}:
            raise TaskError('task_not_ready', 'This workspace is unavailable.')
        runtime = self._runtime(row)
        if runtime.deleting:
            raise TaskError('task_deleting', 'This task is being deleted.')
        try:
            if not runtime.context.repo_dir:
                if isinstance(runtime.sandbox, HostSandbox):
                    runtime.sandbox.attach(runtime.context)
                else:
                    await runtime.sandbox.restore(runtime.context)
            if isinstance(runtime.sandbox, HostSandbox):
                status, output = await runtime.sandbox.read_text(relative.as_posix())
            else:
                result = await runtime.sandbox.exec(['python3', '-I', '-S', '-c', READ_FILE, relative.as_posix()], timeout=10)
                status, output = result.returncode, result.stdout
        except (SandboxError, OSError):
            raise TaskError('workspace_unavailable', 'Could not read from this workspace.') from None
        if status == 2:
            raise TaskError('file_unavailable', 'Only UTF-8 text files up to 1 MiB can be viewed.', 422)
        if status:
            raise TaskError('file_not_found', 'This workspace file is unavailable.', 404)
        return {'path': self.store.clean(relative.as_posix()), 'text': self.store.clean(output)}

    def delete(self, task_id: str) -> None:
        runtime = self._runtime(self.store.get(task_id))
        if not runtime.deleting:
            self.store.request_delete(task_id, True)
            runtime.deleting = True
            runtime.cleanup = asyncio.create_task(self._delete(runtime))

    async def _delete(self, runtime: _Runtime) -> None:
        task_id = runtime.context.task_id
        try:
            await self._stop(runtime)
            if isinstance(runtime.sandbox, HostSandbox):
                await runtime.sandbox.destroy_task(runtime.context)
            else:
                if not runtime.context.repo_dir and self.store.get(task_id)['status'] != 'preparing':
                    await runtime.sandbox.restore(runtime.context)
                await runtime.sandbox.destroy()
            self.store.delete(task_id)
            self.runtimes.pop(task_id, None)
        except (OSError, SandboxError):
            self.store.request_delete(task_id, False)
            runtime.deleting = False
            self._publish(runtime, 'task.delete_failed', {'error_code': 'workspace_delete_failed'})

    async def _prepare(self, runtime: _Runtime) -> bool:
        context = runtime.context
        self._publish(runtime, 'task.preparing')
        try:
            context.issue = self.store.sanitize(await self.issue_loader(IssueRef(context.repo_full_name, context.issue_number)))
            context.linked_prs = context.issue.get('linked_prs', [])
            workspace = await runtime.sandbox.prepare(context)
            context.repo_dir, context.context_dir = workspace.repo_dir, workspace.context_dir
            context.default_branch, context.base_sha, context.work_branch = (
                workspace.default_branch, workspace.base_sha, workspace.work_branch)
            context.sandbox_ref = workspace.reference
            self._instructions(context)
            await runtime.sandbox.write_task_context(context)
            if isinstance(runtime.sandbox, HostSandbox):
                runtime.sandbox.claim_runtime()
            self.store.ready(context.task_id, context.issue, context.default_branch, context.base_sha,
                             context.work_branch, context.sandbox_ref)
            self._publish(runtime, 'task.ready')
            return True
        except (IssueError, SandboxError, OSError) as error:
            code = str(error) if isinstance(error, (IssueError, SandboxError)) else 'workspace_setup_failed'
            message = 'Could not prepare this task. Check the Issue and execution environment.'
            self.store.setup_failed(context.task_id, code, message)
            self._publish(runtime, 'task.setup_failed', {'error_code': code, 'message': message})
            return False

    def _instructions(self, context: TaskContext) -> None:
        policy = 'implement' if self.store.setting('first_turn_policy', 'plan') == 'implement' else 'plan'
        context.instructions = self.store.clean(task_instructions(context, first_turn_policy=policy))

    async def _run(self, runtime: _Runtime) -> None:
        runtime.last_activity = time.monotonic()
        try:
            await self._process_queue(runtime)
        except Exception:
            task_id = runtime.context.task_id
            code, message = 'task_runtime_failed', 'The task runtime could not complete its work.'
            if self.store.get(task_id)['status'] == 'preparing':
                self.store.setup_failed(task_id, code, message)
            elif turn := self.store.running(task_id):
                self.store.finish(task_id, turn['id'], 'failed', error=(code, message))
                self.events.publish(task_id, turn['id'], EngineEvent('turn.failed',
                    {'error_code': code, 'message': message}))
            else:
                self.store.recovery_failed(task_id, code, message)
            self._publish(runtime, 'system.runtime_failed', {'error_code': code, 'message': message})
            with suppress(OSError, SandboxError):
                await self._disconnect(runtime)
        finally:
            runtime.last_activity = time.monotonic()

    async def _process_queue(self, runtime: _Runtime) -> None:
        task_id = runtime.context.task_id
        if self.store.get(task_id)['status'] == 'preparing' and not await self._prepare(runtime):
            return
        while not self.closing and not runtime.deleting:
            if runtime.recovery is not None:
                source, runtime.recovery = runtime.recovery, None
                if not await self._recover(runtime, source):
                    return
            state = self.store.get(task_id)['agent_state']
            if state not in {'queued', 'waiting_retry'}:
                return
            next_turn = self.store.next_turn(task_id)
            if next_turn and next_turn['retry_at']:
                delay = (datetime.fromisoformat(next_turn['retry_at']) - self.clock()).total_seconds()
                if delay > 0:
                    await self._wait_retry(runtime, delay)
                    continue
            async with self.slots:
                turn = self.store.claim(task_id, now=self.clock().isoformat())
                if not turn:
                    return
                runtime.turn_id = turn['id']
                runtime.stop.clear()
                try:
                    await self._turn(runtime, turn)
                finally:
                    runtime.turn_id = None

    def _expire_requests(self, task_id: str) -> None:
        for request_id, request in self.events.pending(task_id).items():
            self.events.publish(task_id, request.turn_id, EngineEvent('system.request_expired', {'request_id': request_id}))

    async def _recover(self, runtime: _Runtime, reason: str) -> bool:
        task_id = runtime.context.task_id
        await self._disconnect(runtime)
        try:
            await runtime.sandbox.restore(runtime.context)
            self._instructions(runtime.context)
            if reason != 'forced_cancel':
                await self._refresh_diff(runtime)
            await self._connect(runtime)
        except (AgentError, SandboxError, OSError) as error:
            code = error.code if isinstance(error, AgentError) else 'workspace_unavailable'
            message = error.message if isinstance(error, AgentError) else 'Could not restore the workspace. Check the execution environment.'
            await self._disconnect(runtime)
            self.store.recovery_failed(task_id, code, message)
            self._publish(runtime, 'system.recovery_failed', {'error_code': code, 'message': message})
            return False
        self._publish(runtime, 'system.recovered', {'reason': reason})
        if reason == 'service_restart' and self.store.get(task_id)['agent_state'] == 'idle':
            if self.store.setting('auto_continue_on_restart', False) is True:
                self.continue_task(task_id)
        return True

    async def _wait_retry(self, runtime: _Runtime, delay: float) -> None:
        runtime.wake.clear()
        jobs = [asyncio.ensure_future(self.sleep(delay)), asyncio.create_task(runtime.wake.wait())]
        try:
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _connect(self, runtime: _Runtime) -> str:
        context = runtime.context
        if not context.repo_dir:
            await runtime.sandbox.restore(context)
            runtime.sandbox_suspended = False
        if not context.instructions:
            self._instructions(context)
        if isinstance(runtime.sandbox, HostSandbox):
            runtime.sandbox.claim_runtime()
        if runtime.engine is None:
            if context.session_id:
                context.recap = self._recap(context.task_id)
            runtime.engine = self.engine_factory(runtime.sandbox, runtime.profile)
            runtime.reader = asyncio.create_task(self._read(runtime, runtime.engine))
        sid = await runtime.engine.ensure_session(context)
        context.session_id = sid
        self.store.session(context.task_id, sid, asdict(runtime.engine.capabilities), recap_pending=context.recap_pending)
        return sid

    def _recap(self, task_id: str) -> str:
        messages = self.store.db.execute("""SELECT type,payload_json FROM task_events
            WHERE task_id=? AND type IN ('message.user','message.agent') ORDER BY seq DESC LIMIT 12""", (task_id,)).fetchall()
        transcript = '\n'.join(f"{row['type']}: {json.loads(row['payload_json']).get('text', '')}"
                               for row in reversed(messages))
        diff = self.store.db.execute('SELECT diff_text FROM task_diffs WHERE task_id=?', (task_id,)).fetchone()
        return self.store.clean(transcript[-8000:] + '\nCurrent diff:\n' + (diff[0][-16000:] if diff else ''))

    async def _read(self, runtime: _Runtime, engine: AgentEngine) -> None:
        async for event in engine.events(runtime.context.session_id or ''):
            # Turn state is written once, after prompt completion and cancellation checks.
            if event.type in {'turn.completed', 'turn.failed'}:
                continue
            if event.type == 'permission.requested':
                key = _permission_key(event.payload)
                option_id = _one_time_allow(event.payload)
                request_id = event.payload.get('request_id')
                if key in runtime.allowed_tools and option_id and request_id:
                    try:
                        await engine.respond(request_id, RequestResponse(option_id=option_id))
                    except AgentError:
                        pass
                    else:
                        self._publish(runtime, 'permission.resolved', {'auto': True, 'option_id': option_id})
                        continue
            request_id = event.payload.get('request_id')
            if request_id and event.type in RESOLUTION_EVENTS and request_id not in self.events.pending(runtime.context.task_id):
                continue
            self.events.publish(runtime.context.task_id, runtime.turn_id, event)
            if event.type in {'mode.changed', 'usage.updated'}:
                self.store.agent_update(runtime.context.task_id, event.type, event.payload)
            if (event.type == 'system.agent_exited' and runtime.engine is engine
                    and runtime.recovery != 'forced_cancel'):
                runtime.disconnected = True
                runtime.recovery = 'agent_exit'
                self._expire_requests(runtime.context.task_id)
                self._schedule(runtime)
            if event.type == 'files.changed' and event.payload.get('status') in {'completed', 'failed'}:
                if runtime.diff_job:
                    runtime.diff_job.cancel()
                runtime.diff_job = asyncio.create_task(self._debounced_diff(runtime))

    async def _debounced_diff(self, runtime: _Runtime) -> None:
        await asyncio.sleep(2)
        if reason == 'cancelled':
            self._finish(runtime, turn, reason, None)
            await self._refresh_diff(runtime)
            return
        await self._refresh_diff(runtime)

    async def _refresh_diff(self, runtime: _Runtime) -> None:
        if not runtime.context.repo_dir:
            return
        try:
            diff = await runtime.sandbox.diff()
            changed = self.store.save_diff(runtime.context.task_id, diff)
            if changed:
                self._publish(runtime, 'diff.updated', changed)
        except (OSError, SandboxError):
            self._publish(runtime, 'system.warning', {'code': 'diff_refresh_failed'})

    async def _turn(self, runtime: _Runtime, turn: sqlite3.Row) -> None:
        self._publish(runtime, 'turn.started', {'kind': turn['kind']})
        reason = None
        error = None
        try:
            sid = await self._connect(runtime)
            if runtime.stop.is_set():
                reason = 'cancelled'
            else:
                text = turn['prompt_text'] if turn['prompt_text'] is not None else first_prompt(runtime.context)
                had_recap = runtime.context.recap_pending
                reason = await self._prompt(runtime, sid, text)
                if had_recap and not runtime.context.recap_pending:
                    self.store.recap_delivered(runtime.context.task_id, sid)
        except AgentError as caught:
            if runtime.stop.is_set():
                reason = 'cancelled'
            else:
                error = caught
        except (OSError, SandboxError):
            error = AgentError('workspace_unavailable', 'The task workspace is unavailable.')
        finally:
            await asyncio.sleep(0)
            if runtime.diff_job:
                runtime.diff_job.cancel()
                with suppress(asyncio.CancelledError):
                    await runtime.diff_job
                runtime.diff_job = None
        self._expire_requests(runtime.context.task_id)
        await self._refresh_diff(runtime)
        if runtime.disconnected or (error and error.code in {'agent_exited', 'agent_disconnected'}):
            if reason != 'cancelled':
                runtime.recovery = 'agent_exit'
                self.store.finish(runtime.context.task_id, turn['id'], 'interrupted', reason='cancelled')
                self._publish(runtime, 'turn.interrupted', {'reason': 'agent_exit'})
                return
        self._finish(runtime, turn, reason, error)

    def _finish(self, runtime: _Runtime, turn: sqlite3.Row, reason: str | None, error: AgentError | None) -> None:
        status = 'failed' if error else 'interrupted' if reason == 'cancelled' else 'completed'
        payload = {'error_code': error.code, 'message': error.message} if error else {'stop_reason': reason}
        count = turn['retry_count']
        delay = RETRY_DELAYS[count] if error and error.retryable and count < len(RETRY_DELAYS) else None
        retry_at = (self.clock() + timedelta(seconds=delay)).isoformat() if delay is not None else None
        auto_continue = reason in {'max_tokens', 'max_turn_requests'} and not turn['continuation_used']
        self.store.finish(runtime.context.task_id, turn['id'], status, reason=reason,
                          error=(error.code, error.message) if error else None, retry_at=retry_at,
                          retry_count=count + 1 if retry_at else count, auto_continue=auto_continue,
                          continuation_used=bool(turn['continuation_used']))
        self._publish(runtime, f'turn.{status}', payload)
        if retry_at:
            self._publish(runtime, 'system.retry_scheduled', {'delay_seconds': delay, 'retry_count': count + 1,
                'retry_at': retry_at, 'error_code': error.code})

    async def _prompt(self, runtime: _Runtime, sid: str, text: str) -> str:
        engine = runtime.engine
        host = runtime.sandbox if isinstance(runtime.sandbox, HostSandbox) else None
        baseline = host.record_processes() if host else set()
        prompt = asyncio.create_task(engine.prompt(sid, [TextContentBlock(text=self.store.clean(text))]))
        interrupt = asyncio.create_task(runtime.stop.wait())
        observe = asyncio.create_task(self._observe_processes(host)) if host else None
        cancel = None
        try:
            await asyncio.wait({prompt, interrupt}, return_when=asyncio.FIRST_COMPLETED)
            if not runtime.stop.is_set():
                return prompt.result().stop_reason
            cancel = asyncio.create_task(engine.cancel(sid))
            # Leave time inside the cancel deadline to signal the group if ACP is unresponsive.
            await asyncio.wait({prompt}, timeout=max(0, self.cancel_timeout - 0.5))
            if not prompt.done() or (host and host.record_processes() - baseline):
                if host:
                    host.force_stop()
                self._publish(runtime, 'system.cancel_forced')
                runtime.recovery = 'forced_cancel'
            return 'cancelled'
        finally:
            for job in (interrupt, cancel, observe, prompt):
                if job is not None:
                    if not job.done():
                        job.cancel()
                    with suppress(asyncio.CancelledError, AgentError):
                        await job

    async def _observe_processes(self, sandbox: HostSandbox) -> None:
        while True:
            sandbox.record_processes()
            await asyncio.sleep(0.25)

    async def _disconnect(self, runtime: _Runtime) -> None:
        engine, reader = runtime.engine, runtime.reader
        runtime.engine = runtime.reader = None
        runtime.disconnected = False
        self._expire_requests(runtime.context.task_id)
        if engine is not None:
            try:
                await asyncio.shield(engine.close())
            finally:
                if reader:
                    await reader

    async def _sweep_idle(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for runtime in tuple(self.runtimes.values()):
                if runtime.deleting or (runtime.job and not runtime.job.done()):
                    continue
                age = now - runtime.last_activity
                suspend = age >= 7200 and bool(runtime.context.repo_dir) and not runtime.sandbox_suspended
                if suspend or (age >= 1800 and runtime.engine is not None):
                    runtime.job = asyncio.create_task(self._suspend_idle(runtime, suspend))

    async def _suspend_idle(self, runtime: _Runtime, suspend: bool) -> None:
        try:
            await self._disconnect(runtime)
            if isinstance(runtime.sandbox, HostSandbox):
                runtime.sandbox.release_runtime()
            if suspend:
                await runtime.sandbox.stop()
                runtime.context.repo_dir = runtime.context.context_dir = ''
                runtime.sandbox_suspended = True
        except (OSError, SandboxError):
            self._publish(runtime, 'system.warning', {'code': 'idle_stop_failed'})
        finally:
            runtime.job = None
            if self.store.get(runtime.context.task_id)['agent_state'] in {'queued', 'waiting_retry'}:
                self._schedule(runtime)

    async def shutdown(self) -> None:
        self.closing = True
        if self.maintenance:
            self.maintenance.cancel()
            with suppress(asyncio.CancelledError):
                await self.maintenance
        await asyncio.gather(*((runtime.cleanup if runtime.deleting else self._stop(runtime))
                               for runtime in tuple(self.runtimes.values())))

    async def _stop(self, runtime: _Runtime) -> None:
        turn_id = runtime.turn_id
        if runtime.issue_job:
            runtime.issue_job.cancel()
            with suppress(asyncio.CancelledError):
                await runtime.issue_job
        if runtime.job and not runtime.job.done():
            runtime.job.cancel()
            with suppress(asyncio.CancelledError):
                await runtime.job
        task_id = runtime.context.task_id
        if turn_id and self.store.finish(task_id, turn_id, 'interrupted', reason='cancelled'):
            self.events.publish(task_id, turn_id, EngineEvent('turn.interrupted', {'stop_reason': 'cancelled'}))
        try:
            await self._disconnect(runtime)
        finally:
            if isinstance(runtime.sandbox, HostSandbox):
                runtime.sandbox.release_runtime()
            self.events.close(task_id)
