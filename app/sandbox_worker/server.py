from __future__ import annotations

import os
import sys

from app.sandbox_worker.contracts import (
    SANDBOX_WORKER_PROTOCOL_VERSION,
    SandboxWorkerContractError,
    SandboxWorkerRequest,
    SandboxWorkerResponse,
)


def handle(request: SandboxWorkerRequest) -> SandboxWorkerResponse:
    if request.operation != "probe":
        return SandboxWorkerResponse(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            ok=False,
            result={},
            error_code="unsupported_operation",
            error_message="Sandbox Worker operation is not implemented",
        )
    if request.payload:
        return SandboxWorkerResponse(
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            ok=False,
            result={},
            error_code="invalid_probe_payload",
            error_message="Sandbox Worker probe payload must be empty",
        )
    return SandboxWorkerResponse(
        request_id=request.request_id,
        correlation_id=request.correlation_id,
        ok=True,
        result={
            "worker_pid": os.getpid(),
            "protocol_version": SANDBOX_WORKER_PROTOCOL_VERSION,
            "process_boundary": True,
            "supported_operations": ["probe"],
            "environment_keys": sorted(os.environ),
        },
    )


def main() -> None:
    raw = sys.stdin.buffer.read()
    try:
        request = SandboxWorkerRequest.from_bytes(raw)
        response = handle(request)
    except SandboxWorkerContractError:
        raise SystemExit(2) from None
    sys.stdout.buffer.write(response.to_bytes())
    sys.stdout.buffer.flush()
