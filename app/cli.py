from __future__ import annotations

import argparse
import asyncio
import json
import socket
from typing import Sequence
from uuid import uuid4

import uvicorn

from app.config import Settings
from app.database import Database
from app.github import GitHubClient
from app.jobs import JobService
from app.sandbox_worker.doctor import SandboxDoctor
from app.worker import DISCOVERY_JOB_KIND, ContribOSWorker, DiscoveryJobWorker


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
    if args.command == "worker":
        return asyncio.run(run_worker(settings, args))
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


if __name__ == "__main__":
    raise SystemExit(main())
