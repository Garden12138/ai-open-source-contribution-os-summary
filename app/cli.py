from __future__ import annotations

import argparse
import asyncio
import json
from typing import Sequence

import uvicorn

from app.config import Settings
from app.database import Database
from app.github import GitHubClient
from app.service import DiscoveryService


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
    return parser


async def run_scan(
    settings: Settings, queries: list[str] | None, top_n: int | None
) -> int:
    database = Database(settings.database_url)
    database.create_schema()
    try:
        with database.session() as session:
            async with GitHubClient(settings) as github:
                run = await DiscoveryService(session, github, settings).scan(
                    queries, top_n=top_n
                )
        print(
            json.dumps(
                {
                    "id": run.id,
                    "status": run.status,
                    "candidates": run.candidate_count,
                    "eligible": run.eligible_count,
                    "selected": run.selected_count,
                    "rate_limit_remaining": run.rate_limit_remaining,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        database.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command in {None, "serve"}:
        uvicorn.run(
            "app.api:app",
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", 8000),
            reload=getattr(args, "reload", False),
        )
        return 0
    if args.command == "scan":
        return asyncio.run(run_scan(settings, args.queries, args.top))
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
