from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Sequence
from uuid import uuid4

import uvicorn

from app.config import Settings
from app.database import Database
from app.github import GitHubClient
from app.jobs import JobService
from app.sandbox_worker.doctor import SandboxDoctor
from app.providers import (
    CredentialedModelGatewayUpstream,
    GatewayProviderCredential,
    GatewayTaskAuthorizer,
    GatewayTaskTokenCodec,
    InternalModelGateway,
    NvidiaNimHostedTransport,
    create_model_gateway_app,
)
from app.worker import (
    DISCOVERY_JOB_KIND,
    ContribOSProviderWorker,
    ContribOSSandboxWorker,
    ContribOSWorker,
    DiscoveryJobWorker,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contribos", description="AI Open Source Contribution OS"
    )
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Start the API and web dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    scan = subparsers.add_parser("scan", help="Run one GitHub discovery scan")
    scan.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="GitHub search query; repeat to use more than one",
    )
    scan.add_argument("--top", type=int, default=None, help="Number of daily picks")

    worker = subparsers.add_parser("worker", help="Run the durable job worker")
    worker.add_argument("--once", action="store_true", help="Process at most one job")
    worker.add_argument("--poll-interval", type=float, default=2)
    worker.add_argument(
        "--worker-id",
        default=f"{socket.gethostname()}-{uuid4()}",
    )

    provider_worker = subparsers.add_parser(
        "provider-worker",
        help="Run model-provider jobs without GitHub or Docker credentials",
    )
    provider_worker.add_argument("--once", action="store_true")
    provider_worker.add_argument("--poll-interval", type=float, default=2)
    provider_worker.add_argument(
        "--worker-id",
        default=f"provider-{socket.gethostname()}-{uuid4()}",
    )

    gateway = subparsers.add_parser(
        "model-gateway",
        help="Run the internal NVIDIA NIM credential gateway",
    )
    gateway.add_argument("--host", default="127.0.0.1")
    gateway.add_argument("--port", type=int, default=8001)

    sandbox_worker = subparsers.add_parser(
        "sandbox-worker",
        help="Run Docker-only execution jobs without GitHub/model credentials",
    )
    sandbox_worker.add_argument("--once", action="store_true")
    sandbox_worker.add_argument("--poll-interval", type=float, default=2)
    sandbox_worker.add_argument(
        "--worker-id",
        default=f"sandbox-{socket.gethostname()}-{uuid4()}",
    )

    publisher_worker = subparsers.add_parser("publisher-worker", help="Run exact user-confirmed publication using host gh authentication")
    publisher_worker.add_argument("--once", action="store_true")
    publisher_worker.add_argument("--poll-interval", type=float, default=2)
    publisher_worker.add_argument("--worker-id", default=f"publisher-{socket.gethostname()}-{uuid4()}")

    doctor = subparsers.add_parser(
        "doctor",
        help="Verify Sandbox Worker runtime prerequisites",
    )
    doctor.add_argument(
        "--runner-image",
        required=True,
        help="Digest-pinned local Sandbox Runner image",
    )
    return parser


async def run_scan(
    settings: Settings, queries: list[str] | None, top_n: int | None
) -> int:
    database = Database(settings.database_url)
    database.create_schema()
    try:
        with database.session() as session:
            job, _ = JobService(session).enqueue(
                kind=DISCOVERY_JOB_KIND,
                idempotency_key=str(uuid4()),
                payload={"queries": queries, "top_n": top_n},
            )
        worker = DiscoveryJobWorker(
            database,
            settings,
            lambda: GitHubClient(settings),
            worker_id=f"cli-scan-{uuid4()}",
        )
        completed = await worker.run_once()
        if completed is None or completed.id != job.id:
            raise RuntimeError("The queued discovery job was not processed")
        print(
            json.dumps(
                {
                    "job_id": completed.id,
                    "status": completed.state,
                    **completed.result_data,
                    "error_code": completed.error_code,
                },
                ensure_ascii=False,
            )
        )
        return 0 if completed.state == "succeeded" else 1
    finally:
        database.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in {None, "serve"}:
        settings = Settings.from_env()
        uvicorn.run(
            "app.api:app",
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", 8000),
            reload=getattr(args, "reload", False),
        )
        return 0
    if args.command == "doctor":
        try:
            report = SandboxDoctor(
                runner_image=args.runner_image,
            ).run()
        except ValueError as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                report.to_wire(),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if report.ok else 1
    settings = Settings.from_env()
    if args.command == "scan":
        return asyncio.run(run_scan(settings, args.queries, args.top))
    if args.command == "publisher-worker":
        return asyncio.run(run_publisher_worker(settings, args))
    if args.command == "worker":
        return asyncio.run(run_worker(settings, args))
    if args.command == "provider-worker":
        return asyncio.run(run_provider_worker(settings, args))
    if args.command == "model-gateway":
        return run_model_gateway(settings, args)
    if args.command == "sandbox-worker":
        return asyncio.run(run_sandbox_worker(settings, args))
    parser.error(f"Unknown command: {args.command}")
    return 2


async def run_worker(settings: Settings, args: argparse.Namespace) -> int:
    database = Database(settings.database_url)
    database.create_schema()
    try:
        worker = ContribOSWorker(
            database,
            settings,
            lambda: GitHubClient(settings),
            worker_id=args.worker_id,
        )
        if args.once:
            await worker.run_once()
            return 0
        await worker.run_forever(poll_interval_seconds=args.poll_interval)
        return 0
    finally:
        database.close()


async def run_provider_worker(
    settings: Settings,
    args: argparse.Namespace,
) -> int:
    database = Database(settings.database_url)
    database.create_schema()
    try:
        worker = ContribOSProviderWorker(
            database,
            settings,
            worker_id=args.worker_id,
        )
        if args.once:
            await worker.run_once()
            return 0
        await worker.run_forever(poll_interval_seconds=args.poll_interval)
        return 0
    finally:
        database.close()


def run_model_gateway(settings: Settings, args: argparse.Namespace) -> int:
    if settings.model_gateway_signing_key is None:
        raise ValueError("MODEL_GATEWAY_SIGNING_KEY is required")
    from app.providers.studio import UnconfiguredModelUpstream
    api_key = (_gateway_secret(environment_name="NVIDIA_API_KEY", file_environment_name="NVIDIA_API_KEY_FILE")
        if os.getenv("NVIDIA_API_KEY") or os.getenv("NVIDIA_API_KEY_FILE") else None)
    gateway = InternalModelGateway(
        authorizer=GatewayTaskAuthorizer(
            GatewayTaskTokenCodec(settings.model_gateway_signing_key)
        ),
        upstream=CredentialedModelGatewayUpstream(
            credential=GatewayProviderCredential(api_key),
            transport=NvidiaNimHostedTransport(
                proxy_url=settings.nvidia_https_proxy,
            ),
        ) if api_key else UnconfiguredModelUpstream(),
        provider_name="nvidia_nim",
    )
    from app.providers.model_secrets import GatewaySecretStore
    from app.providers.studio import StudioGateway
    store = GatewaySecretStore(settings.model_gateway_secret_root, environment_key=api_key)
    uvicorn.run(
        create_model_gateway_app(StudioGateway(gateway, store, proxy_url=settings.nvidia_https_proxy),
            secret_store=store, management_key=settings.model_gateway_management_key),
        host=args.host,
        port=args.port,
    )
    return 0


async def run_sandbox_worker(
    settings: Settings,
    args: argparse.Namespace,
) -> int:
    database = Database(settings.database_url)
    database.create_schema()
    try:
        worker = ContribOSSandboxWorker(
            database,
            settings,
            worker_id=args.worker_id,
        )
        if args.once:
            await worker.run_once()
            return 0
        await worker.run_forever(poll_interval_seconds=args.poll_interval)
        return 0
    finally:
        database.close()


def _gateway_secret(
    *,
    environment_name: str,
    file_environment_name: str,
) -> str:
    path_value = os.getenv(file_environment_name)
    raw = None
    if path_value:
        path = Path(path_value)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{file_environment_name} is invalid")
        raw = path.read_text(encoding="utf-8")
    if raw is None:
        raw = os.getenv(environment_name)
    value = "" if raw is None else raw.strip()
    if not value or len(value) > 8_192 or any(char.isspace() for char in value):
        raise ValueError(f"{environment_name} is not configured safely")
    return value
async def run_publisher_worker(settings: Settings, args: argparse.Namespace) -> int:
    from app.publication import PublicationWorker
    from app.workbench import WorkbenchError
    if any(os.environ.get(name) for name in (
        "GITHUB_TOKEN", "GH_TOKEN", "NVIDIA_API_KEY", "OPENAI_API_KEY",
        "MODEL_GATEWAY_SIGNING_KEY", "SANDBOX_JOB_SPEC_SIGNING_KEY", "DOCKER_HOST",
    )) or Path("/var/run/docker.sock").exists() and os.access("/var/run/docker.sock", os.W_OK):
        raise WorkbenchError("Publisher 必须使用独立环境：仅 gh 登录、数据库和产物；不能访问 Docker 或模型凭证")
    database = Database(settings.database_url)
    database.create_schema()
    try:
        worker = PublicationWorker(database, settings, worker_id=args.worker_id)
        if args.once:
            await worker.run_once()
            return 0
        while True:
            job = await worker.run_once()
            if job is None:
                await asyncio.sleep(max(0.1, args.poll_interval))
    finally:
        database.close()

if __name__ == "__main__":
    raise SystemExit(main())
