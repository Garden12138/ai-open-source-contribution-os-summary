"""Loopback control endpoints reserved for the desktop shell."""

import asyncio
import json
import os
import pwd
import re
import secrets
import signal
import subprocess
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from socket import socket
from types import FrameType

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from uvicorn import Config, Server

from contribos.security import without_github_credentials
from contribos.tasks.events import REQUEST_EVENTS, TaskEvent, TaskEvents

STARTUP_ENV = 'CONTRIBOS_STARTUP_CODE'
TOKEN_ENV = 'CONTRIBOS_SHELL_TOKEN'
SHELL_PATHS = frozenset({'/api/desktop/status', '/api/notifications/stream'})
NOTICES = {
    'turn.completed': ('turn_completed', 'Agent finished a turn'),
    'turn.failed': ('turn_error', 'Agent stopped with an error'),
    'task.setup_failed': ('turn_error', 'Task setup failed'),
    'system.runtime_failed': ('turn_error', 'Task stopped with an error'),
    'system.recovery_failed': ('turn_error', 'Task could not resume'),
    'permission.requested': ('request_waiting', 'Permission requested'),
    'question.asked': ('request_waiting', 'Agent asked a question'),
    'publish.succeeded': ('publish_result', 'Draft PR published'),
    'publish.failed': ('publish_result', 'Draft PR publishing failed'),
    'discovery.daily_ready': ('daily_recommendations', 'Daily recommendations are ready'),
}


@dataclass(frozen=True)
class DesktopMode:
    startup_code: str = field(repr=False)
    shell_token: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> 'DesktopMode':
        code = os.environ.pop(STARTUP_ENV, '')
        token = os.environ.pop(TOKEN_ENV, '')
        if code == token or not all(re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value) for value in (code, token)):
            raise ValueError('Desktop launch credentials are missing or invalid.')
        return cls(code, token)

    def authorized(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(' ')
        return scheme.lower() == 'bearer' and token.isascii() and secrets.compare_digest(token, self.shell_token)


def login_path(shell: str | None = None, *, timeout: float = 5) -> tuple[str, bool]:
    marker = 'CONTRIBOS_PATH='
    try:
        shell = shell or os.environ.get('SHELL') or pwd.getpwuid(os.getuid()).pw_shell
        if not os.path.isabs(shell):
            return os.defpath, False
        with subprocess.Popen(
            [shell, '-l', '-c', r'printf "\nCONTRIBOS_PATH=%s\n" "$PATH"'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            env=without_github_credentials(os.environ),
        ) as process:
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return os.defpath, False
        path = next((line[len(marker):] for line in reversed(os.fsdecode(output).splitlines())
                     if line.startswith(marker)), '')
        if process.returncode == 0 and all(os.path.isabs(part) for part in path.split(os.pathsep)) and '\0' not in path:
            return path, True
    except (KeyError, OSError):
        pass
    return os.defpath, False


class DesktopServer(Server):
    def __init__(self, config: Config, *, smoke_check: Callable[[], None] | None = None) -> None:
        super().__init__(config)
        self.smoke_check = smoke_check

    async def startup(self, sockets: list[socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started and not self.should_exit:
            print(f'CONTRIBOS_READY port={self.config.port}', flush=True)
            if self.smoke_check:
                threading.Thread(target=self.smoke_check, daemon=True).start()

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if sig == signal.SIGTERM:
            self.should_exit = True
        else:
            super().handle_exit(sig, frame)


class DesktopNotifications:
    def __init__(self, events: TaskEvents) -> None:
        self.events = events
        self.queues: set[asyncio.Queue[str]] = set()

    def status(self) -> dict[str, int]:
        active = self.events.store.db.execute("SELECT task_id FROM turns WHERE status='running'").fetchall()
        return {
            'running_tasks': len(active),
            'pending_requests': sum(len(self.events.pending(row['task_id'])) for row in active),
        }

    def on_task_event(self, event: TaskEvent) -> None:
        if event.type == 'turn.failed' and (
            event.payload.get('error_code') == 'task_runtime_failed'
            or self.events.store.get(event.task_id)['agent_state'] == 'waiting_retry'
        ):
            return
        self.publish(event.type, event.task_id)

    def publish(self, kind: str, task_id: str | None = None) -> None:
        notice = NOTICES.get(kind)
        if not notice or not self.queues:
            return
        category, title = notice
        if not self.events.store.setting('notifications', {}).get(category, True):
            return
        message = {'category': category, 'title': title, 'task_id': task_id}
        frame = f'event: notification\ndata: {json.dumps(message)}\n\n'
        for queue in tuple(self.queues):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self.queues.add(queue)
        try:
            yield ': connected\n\n'
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), self.events.heartbeat)
                except TimeoutError:
                    frame = ': heartbeat\n\n'
                yield frame
        finally:
            self.queues.discard(queue)


router = APIRouter()


@router.get('/api/desktop/status')
async def status(request: Request) -> dict[str, int]:
    return request.app.state.desktop_notifications.status()


@router.get('/api/notifications/stream')
async def notifications(request: Request) -> StreamingResponse:
    return StreamingResponse(request.app.state.desktop_notifications.stream(), media_type='text/event-stream',
                             headers={'X-Accel-Buffering': 'no'})
