"""Script replay for orchestration tests and offline demonstrations."""

import asyncio
import uuid
from collections.abc import AsyncIterator

from acp.schema import TextContentBlock

from contribos.agent.engine import AgentCapabilities, AgentError, ContentBlock, EngineEvent, RequestResponse, TaskContext, TurnResult
from contribos.tasks.instructions import recovery_prompt


class FakeEngine:
    def __init__(self, scripts: list[list[EngineEvent]] | None = None, *, hold_turn: bool = False,
                 event_delay: float = 0) -> None:
        self.capabilities = AgentCapabilities(load_session=True, modes=('plan', 'build'))
        self.scripts = list(scripts or [[EngineEvent('message.agent', {'text': 'Ready to work offline.'})]])
        self.hold_turn = hold_turn
        self.event_delay = event_delay
        self.queue: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        self.pending: dict[str, asyncio.Future[RequestResponse]] = {}
        self.prompts: list[list[ContentBlock]] = []
        self.cancelled = asyncio.Event()
        self.session_id: str | None = None
        self.task: TaskContext | None = None
        self.running = False
        self.closed = False

    async def ensure_session(self, task: TaskContext) -> str:
        if self.closed:
            raise AgentError('engine_closed')
        self.task = task
        self.session_id = task.session_id or self.session_id or uuid.uuid4().hex
        task.session_id = self.session_id
        return self.session_id

    async def prompt(self, session_id: str, blocks: list[ContentBlock]) -> TurnResult:
        if self.closed or session_id != self.session_id:
            raise AgentError('session_missing')
        if self.running:
            raise AgentError('turn_running')
        self.running = True
        self.cancelled.clear()
        if self.task and self.task.recap_pending:
            blocks = [TextContentBlock(text=recovery_prompt(self.task)), *blocks]
        self.prompts.append(blocks)
        try:
            script = self.scripts.pop(0) if self.scripts else []
            for event in script:
                if self.cancelled.is_set():
                    break
                if event.type in {'permission.requested', 'question.asked'}:
                    request_id = event.payload.get('request_id', uuid.uuid4().hex)
                    future = asyncio.get_running_loop().create_future()
                    self.pending[request_id] = future
                    self.queue.put_nowait(EngineEvent(event.type, {**event.payload, 'request_id': request_id}))
                    result = await future
                    self.pending.pop(request_id, None)
                    self.queue.put_nowait(EngineEvent('permission.resolved' if event.type == 'permission.requested' else 'question.answered',
                        {'request_id': request_id, 'option_id': result.option_id, 'answers': result.answers, 'cancelled': result.cancelled}))
                else:
                    self.queue.put_nowait(event)
                if event.type == 'turn.failed':
                    raise AgentError(event.payload.get('error_code', 'fake_error'))
                await asyncio.sleep(self.event_delay)
            if self.hold_turn and not self.cancelled.is_set():
                await self.cancelled.wait()
            reason = 'cancelled' if self.cancelled.is_set() else 'end_turn'
            if self.task and reason == 'end_turn':
                self.task.recap_pending = False
            self.queue.put_nowait(EngineEvent('turn.completed', {'stop_reason': reason}))
            return TurnResult(reason)
        finally:
            self.running = False

    async def cancel(self, session_id: str) -> None:
        self.cancelled.set()
        for future in self.pending.values():
            if not future.done():
                future.set_result(RequestResponse(cancelled=True))

    async def respond(self, request_id: str, response: RequestResponse) -> None:
        future = self.pending.get(request_id)
        if not future or future.done():
            raise AgentError('request_expired')
        future.set_result(response)

    async def set_mode(self, session_id: str, mode: str) -> None:
        if mode in self.capabilities.modes:
            self.queue.put_nowait(EngineEvent('mode.changed', {'mode': mode}))

    async def events(self, session_id: str) -> AsyncIterator[EngineEvent]:
        while (event := await self.queue.get()) is not None:
            yield event

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.cancel(self.session_id or '')
            self.queue.put_nowait(None)
