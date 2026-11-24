"""One ACP client for every profile, using the official SDK's stdio connection."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from typing import Any

from acp import Client, RequestError, connect_to_agent
from acp import schema as s

from contribos.agent.engine import (
    AgentCapabilities, AgentError, ContentBlock, EngineEvent, RequestResponse, TaskContext, TurnResult,
)
from contribos.agent.normalize import Normalizer
from contribos.security import redact


class AcpEngine(Client):
    def __init__(self, spawn: Callable[[], Awaitable[asyncio.subprocess.Process]], *,
                 refresh_diff: Callable[[], Awaitable[None]] | None = None,
                 stop_process: Callable[[], Awaitable[None]] | None = None,
                 error_mapper: Callable[[Exception], AgentError] | None = None,
                 secrets: tuple[str, ...] = (),
                 clean_text: Callable[[str], str] = redact, replay_session_updates: bool = True) -> None:
        self.spawn = spawn
        self.refresh_diff = refresh_diff
        self.stop_process = stop_process
        self.error_mapper = error_mapper
        self.secrets = secrets
        self.clean_text = clean_text
        self.replay_session_updates = replay_session_updates
        self.capabilities = AgentCapabilities()
        self.process: asyncio.subprocess.Process | None = None
        self.connection: Any = None
        self.task: TaskContext | None = None
        self.queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        self.normalizer = Normalizer(self._emit, self._clean)
        self.pending: dict[str, tuple[asyncio.Future[RequestResponse], str, set[str]]] = {}
        self.watcher: asyncio.Task[None] | None = None
        self.running = False
        self.closed = False
        self.session_ready = False
        self.restoring = False

    def _clean(self, text: str) -> str:
        for value in self.secrets:
            if value:
                text = text.replace(value, '[REDACTED]')
        return self.clean_text(text)

    def _emit(self, event: EngineEvent) -> None:
        def clean(value: Any) -> Any:
            if isinstance(value, str):
                return self._clean(value)
            if isinstance(value, dict):
                return {self._clean(str(k)): clean(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            return value
        self.queue.put_nowait(EngineEvent(event.type, clean(event.payload)))

    def _invalidate(self) -> None:
        for future, _, _ in self.pending.values():
            if not future.done():
                future.set_result(RequestResponse(cancelled=True))
        self.pending.clear()

    async def _watch(self) -> None:
        assert self.process is not None
        code = await self.process.wait()
        self._invalidate()
        self.normalizer.flush()
        if not self.closed:
            self._emit(EngineEvent('system.agent_exited', {'exit_code': code}))

    async def _call(self, call: Awaitable[Any], *, timeout: float | None = None) -> Any:
        rpc = asyncio.ensure_future(call)
        try:
            done, _ = await asyncio.wait({rpc, self.watcher}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if self.watcher in done:
                if rpc in done and not rpc.cancelled() and rpc.exception() is None:
                    return rpc.result()
                raise AgentError('agent_exited', 'Agent process exited; continue to reconnect.')
            if rpc in done:
                return rpc.result()
            raise AgentError('agent_timeout', 'Agent request timed out.')
        except AgentError:
            raise
        except Exception as exc:
            if self.error_mapper:
                raise self.error_mapper(exc) from None
            code = 'agent_disconnected' if isinstance(exc, (ConnectionError, EOFError)) else 'agent_request_failed'
            raise AgentError(code) from None
        finally:
            if not rpc.done():
                rpc.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rpc

    async def ensure_session(self, task: TaskContext) -> str:
        if self.closed:
            raise AgentError('engine_closed')
        if self.task and self.task.task_id != task.task_id:
            raise AgentError('task_mismatch')
        if self.session_ready and self.process and self.process.returncode is None and self.task and self.task.session_id:
            return self.task.session_id
        await self._stop_process()
        self.task = task
        try:
            self.process = await self.spawn()
            self.connection = connect_to_agent(self, self.process.stdin, self.process.stdout, use_unstable_protocol=True)
            self.watcher = asyncio.create_task(self._watch())
            initialized = await self._call(self.connection.initialize(protocol_version=1,
                client_capabilities=s.ClientCapabilities(fs=None, terminal=None,
                    elicitation=s.ElicitationCapabilities(form={})),
                client_info=s.Implementation(name='contribos', version='2.0.0')), timeout=20)
        except AgentError:
            raise
        except Exception:
            raise AgentError('agent_start_failed', 'Could not start Agent; check installation and login.') from None
        caps = initialized.agent_capabilities
        lifecycle = caps.session_capabilities
        prompt_caps = caps.prompt_capabilities
        prompt_types = ('text',) + tuple(k for k in ('image', 'audio', 'embedded_context') if getattr(prompt_caps, k, False))
        self.capabilities = AgentCapabilities(load_session=bool(caps.load_session),
            resume_session=lifecycle is not None and lifecycle.resume is not None,
            prompt_types=prompt_types, auth_methods=tuple(m.id for m in initialized.auth_methods or []))
        arguments = dict(cwd=task.repo_dir, additional_directories=[task.context_dir], mcp_servers=[])
        restored = False
        if task.session_id:
            methods = []
            if self.capabilities.resume_session:
                methods.append(self.connection.resume_session)
            if self.capabilities.load_session:
                methods.append(self.connection.load_session)
            for method in methods:
                self.restoring = True
                try:
                    session = await self._call(method(session_id=task.session_id, **arguments), timeout=20)
                    restored = True
                    break
                except AgentError as exc:
                    if exc.retryable or exc.code in {'agent_exited', 'agent_disconnected', 'agent_timeout',
                                                   'authentication_required', 'quota_exhausted', 'agent_region_unavailable'}:
                        raise
                finally:
                    self.restoring = False
        if not restored:
            previous = task.session_id
            session = await self._call(self.connection.new_session(**arguments), timeout=20)
            task.session_id = session.session_id
            if previous:
                task.recap_pending = True
                self._emit(EngineEvent('system.session_recreated', {}))
        modes = getattr(session, 'modes', None)
        configs = getattr(session, 'config_options', None) or []
        models = tuple(v.value for c in configs if getattr(c, 'category', None) == 'model' for v in getattr(c, 'options', []) if hasattr(v, 'value'))
        self.capabilities = replace(self.capabilities, modes=tuple(m.id for m in modes.available_modes) if modes else (), models=models)
        self.normalizer.flush()
        self.session_ready = True
        return task.session_id

    async def _refresh(self) -> None:
        if self.refresh_diff:
            try:
                await self.refresh_diff()
            except Exception:
                self._emit(EngineEvent('system.warning', {'code': 'diff_refresh_failed'}))

    async def prompt(self, session_id: str, blocks: list[ContentBlock]) -> TurnResult:
        if self.running:
            raise AgentError('turn_running')
        if not self.task or self.task.session_id != session_id:
            raise AgentError('session_missing')
        self.running = True
        try:
            if self.task.recap_pending:
                from contribos.tasks.instructions import recovery_prompt
                blocks = [s.TextContentBlock(text=recovery_prompt(self.task)), *blocks]
            result = await self._call(self.connection.prompt(session_id=session_id, prompt=blocks))
            if result.stop_reason != 'cancelled':
                self.task.recap_pending = False
            self.normalizer.flush()
            await self._refresh()
            self._emit(EngineEvent('turn.completed', {'stop_reason': result.stop_reason}))
            return TurnResult(result.stop_reason)
        except AgentError as exc:
            self.normalizer.flush()
            await self._refresh()
            self._emit(EngineEvent('turn.failed', {'error_code': exc.code, 'message': exc.message, 'retryable': exc.retryable}))
            raise
        finally:
            self.running = False
            self._invalidate()

    async def cancel(self, session_id: str) -> None:
        self._invalidate()
        if self.process and self.process.returncode is None:
            try:
                await self.connection.cancel(session_id=session_id)
            except (ConnectionError, OSError):
                raise AgentError('agent_disconnected') from None

    async def set_mode(self, session_id: str, mode: str) -> None:
        if mode in self.capabilities.modes:
            await self._call(self.connection.set_session_mode(session_id=session_id, mode_id=mode), timeout=20)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if self.restoring and not self.replay_session_updates:
            return
        if self.normalizer.update(update.model_dump(by_alias=True, exclude_none=True, mode='json')):
            await self._refresh()

    async def _ask(self, kind: str, payload: dict[str, Any], options: set[str] | None = None) -> RequestResponse:
        self.normalizer.flush()
        request_id = uuid.uuid4().hex
        future: asyncio.Future[RequestResponse] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (future, kind, options or set())
        self._emit(EngineEvent('permission.requested' if kind == 'permission' else 'question.asked',
                               {**payload, 'request_id': request_id}))
        try:
            result = await future
            self._emit(EngineEvent('permission.resolved' if kind == 'permission' else 'question.answered',
                {'request_id': request_id, 'cancelled': result.cancelled, 'option_id': result.option_id,
                 'answers': result.answers, 'auto': False}))
            return result
        finally:
            self.pending.pop(request_id, None)

    async def request_permission(self, session_id: str, tool_call: Any, options: list[Any], **kwargs: Any) -> s.RequestPermissionResponse:
        if self.task and self.task.isolation != 'host':
            option = next((o for o in options if o.kind == 'allow_once'), None)
            self._emit(EngineEvent('permission.resolved', {'auto': True, 'option_id': option.option_id if option else None}))
            result = RequestResponse(option_id=option.option_id if option else None, cancelled=option is None)
        else:
            result = await self._ask('permission', {'tool_call': tool_call.model_dump(by_alias=True, exclude_none=True),
                'options': [o.model_dump(by_alias=True) for o in options]}, {o.option_id for o in options})
        outcome = s.DeniedOutcome() if result.cancelled or not result.option_id else s.AllowedOutcome(option_id=result.option_id)
        return s.RequestPermissionResponse(outcome=outcome)

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> Any:
        if not isinstance(mode, (s.ElicitationFormSessionMode, s.ElicitationFormRequestMode)):
            return s.DeclineElicitationResponse()
        result = await self._ask('question', {
            'message': message, 'schema': mode.requested_schema.model_dump(by_alias=True, exclude_none=True),
        })
        return s.CancelElicitationResponse() if result.cancelled else s.AcceptElicitationResponse(content=result.answers or {})

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    async def respond(self, request_id: str, response: RequestResponse) -> None:
        pending = self.pending.get(request_id)
        if not pending or pending[0].done():
            raise AgentError('request_expired', 'This request has expired.')
        future, kind, options = pending
        if kind == 'permission' and not response.cancelled and response.option_id not in options:
            raise AgentError('invalid_option')
        future.set_result(response)

    async def _unsupported(self, *args: Any, **kwargs: Any) -> Any:
        self._emit(EngineEvent('system.warning', {'code': 'unsupported_client_capability'}))
        raise RequestError.method_not_found('client capability disabled')

    read_text_file = write_text_file = create_terminal = terminal_output = _unsupported
    release_terminal = wait_for_terminal_exit = kill_terminal = _unsupported

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found('unsupported extension')

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self._emit(EngineEvent('agent.other', {'type': method}))

    async def events(self, session_id: str) -> AsyncIterator[EngineEvent]:
        while (event := await self.queue.get()) is not None:
            yield event

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._invalidate()
        try:
            await self._stop_process()
        finally:
            self.normalizer.flush()
            self.queue.put_nowait(None)

    async def _stop_process(self) -> None:
        self.session_ready = False
        if self.connection:
            with contextlib.suppress(ConnectionError, OSError):
                await self.connection.close()
        if self.stop_process and self.process:
            await self.stop_process()
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.watcher:
            await self.watcher
        self.connection, self.process, self.watcher = None, None, None
