from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from typing import Protocol

from app.sandbox_worker.contracts import (
    MAX_WORKER_MESSAGE_BYTES,
    SandboxWorkerContractError,
    SandboxWorkerRequest,
    SandboxWorkerResponse,
)


class SandboxWorkerProcessError(RuntimeError):
    pass


class SandboxWorkerRemoteError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SandboxWorkerClient(Protocol):
    async def request(
        self,
        request: SandboxWorkerRequest,
    ) -> SandboxWorkerResponse: ...


class SandboxWorkerProcessClient:
    """One-request subprocess transport; no business database is shared."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        module: str = "app.sandbox_worker",
        environment: Mapping[str, str] | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        if timeout_seconds < 1 or timeout_seconds > 60:
            raise ValueError("Sandbox Worker process timeout is invalid")
        self.executable = executable or sys.executable
        if not os.path.isabs(self.executable):
            raise ValueError(
                "Sandbox Worker executable must be an absolute path"
            )
        if module != "app.sandbox_worker":
            raise ValueError("Sandbox Worker module is unsupported")
        worker_environment = {
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if environment is not None:
            if set(environment) != set(worker_environment):
                raise ValueError(
                    "Sandbox Worker environment must use the exact allowlist"
                )
            worker_environment = dict(environment)
        self.module = module
        self.environment = worker_environment
        self.timeout_seconds = timeout_seconds

    async def request(
        self,
        request: SandboxWorkerRequest,
    ) -> SandboxWorkerResponse:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "-m",
            self.module,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(request.to_bytes()),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SandboxWorkerProcessError(
                "Sandbox Worker process timed out"
            ) from exc
        if process.returncode != 0:
            raise SandboxWorkerProcessError(
                "Sandbox Worker process failed with a redacted error"
            )
        if stderr or len(stdout) > MAX_WORKER_MESSAGE_BYTES + 1:
            raise SandboxWorkerProcessError(
                "Sandbox Worker process violated its output contract"
            )
        try:
            response = SandboxWorkerResponse.from_bytes(stdout)
        except SandboxWorkerContractError as exc:
            raise SandboxWorkerProcessError(
                "Sandbox Worker returned an invalid response"
            ) from exc
        if (
            response.request_id != request.request_id
            or response.correlation_id != request.correlation_id
        ):
            raise SandboxWorkerProcessError(
                "Sandbox Worker response does not match its request"
            )
        if not response.ok:
            raise SandboxWorkerRemoteError(
                response.error_code or "sandbox_worker_error",
                response.error_message or "Sandbox Worker request failed",
            )
        return response


class FakeSandboxWorkerClient:
    def __init__(
        self,
        *,
        result: Mapping[str, object] | None = None,
    ) -> None:
        self.result = dict(result or {"worker": "fake"})
        self.requests: list[SandboxWorkerRequest] = []

    async def request(
        self,
        request: SandboxWorkerRequest,
    ) -> SandboxWorkerResponse:
        self.requests.append(request)
        return SandboxWorkerResponse(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            ok=True,
            result=dict(self.result),
        )
