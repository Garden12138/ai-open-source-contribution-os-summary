"""Serve the local application and inspect its prerequisites."""

import argparse
import asyncio
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from contribos import __version__
from contribos.agent.profiles import agent_statuses
from contribos.config import Settings, server_origin, validate_bind_host
from contribos.security import redact


def serve(settings: Settings, *, demo: bool = False) -> int:
    import uvicorn

    from contribos.web.app import create_app

    origin = server_origin(settings)

    def run(config: Settings) -> None:
        app = create_app(config, demo=demo)
        code = app.state.auth.issue_startup_code()
        print(f"Login (valid for 60 seconds, one use): {origin}/desktop/bootstrap?code={code}", flush=True)
        # Bootstrap URLs contain a login code; HTTP access logging must stay off.
        uvicorn.run(app, host=validate_bind_host(config.host), port=config.port, access_log=False,
                    timeout_graceful_shutdown=2)

    if demo:
        with tempfile.TemporaryDirectory(prefix='contribos-demo-') as directory:
            run(replace(settings, home=Path(directory).resolve(), tool_paths={}))
    else:
        run(settings)
    return 0


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


def doctor_checks(settings: Settings | None = None) -> list[CheckResult]:
    results = [CheckResult(
        "python", "ok" if sys.version_info >= (3, 11) else "error",
        f"Python {platform.python_version()} (requires >=3.11)",
    )]
    for name in ("git", "gh"):
        executable = settings.tool(name) if settings else shutil.which(name)
        if executable is None:
            results.append(CheckResult(name, "missing", f"{name} is not installed or is not on PATH."))
            continue
        try:
            if name == "git":
                result = subprocess.run(
                    [executable, "--version"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, timeout=5, check=False,
                )
                detail = redact(result.stdout.strip())[:160] if result.returncode == 0 else "Git version check failed."
                results.append(CheckResult(name, "ok" if result.returncode == 0 else "error",
                                           f'{executable} · {detail}' if settings else detail))
            else:
                # gh can include account and credential diagnostics; never capture them.
                result = subprocess.run(
                    [executable, "auth", "status", "--hostname", "github.com"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=5, check=False,
                )
                results.append(CheckResult(
                    name, "ok" if result.returncode == 0 else "not_logged_in",
                    (f'{executable} · ' if settings else '') + ("Logged in to github.com." if result.returncode == 0
                    else "GitHub login unavailable; run gh auth status or gh auth login."),
                ))
        except subprocess.TimeoutExpired:
            results.append(CheckResult(name, "error", "Check timed out after 5 seconds."))
        except OSError:
            results.append(CheckResult(name, "error", "Could not run the prerequisite check."))
    statuses = agent_statuses(settings) if settings else agent_statuses()
    results.extend(CheckResult(s.id, s.status, s.detail + (" Warning: " + s.isolation_warning if s.isolation_warning else "")) for s in statuses)
    if settings:
        for name in ('node', 'sbx', 'docker'):
            executable = settings.tool(name)
            results.append(CheckResult(name, 'ok' if executable else 'missing', executable or 'Command not found.'))
        try:
            free = shutil.disk_usage(settings.home).free // (1024 ** 3)
            results.append(CheckResult('disk', 'ok', f'{free} GiB available in the data directory.'))
        except OSError:
            results.append(CheckResult('disk', 'error', 'Disk usage is unavailable.'))
    return results


def doctor_report(settings: Settings | None = None) -> list[dict[str, str]]:
    return [asdict(result) for result in doctor_checks(settings)]


def doctor() -> int:
    results = doctor_checks()
    for result in results:
        print(f"{result.name}: [{result.status}] {result.detail}")
    return 0 if all(result.status == "ok" for result in results) else 1


def agents() -> int:
    statuses = agent_statuses()
    for status in statuses:
        print(f"{status.id}: [{status.status}] {status.detail}")
        print(f"  Login: {status.login_hint}")
        if status.isolation_warning:
            print(f"  Warning: {status.isolation_warning}")
    return 0 if all(s.status == "ok" for s in statuses) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contribos", description=__doc__)
    parser.add_argument("--version", action="version", version=f"ContribOS {__version__}")
    commands = parser.add_subparsers(dest="command")
    server = commands.add_parser("serve", help="Serve on a loopback address")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", default=8765, type=int)
    server.add_argument("--demo", action="store_true", help="Run an offline demonstration with temporary data")
    commands.add_parser("doctor", help="Check Python, git and GitHub login")
    commands.add_parser("agents", help="Check Agent installation, isolated login and configuration")
    chat = commands.add_parser("chat", help="Chat with an ACP Agent in an independent workspace")
    chat.add_argument("issue_url", nargs="?")
    chat.add_argument("--agent", choices=("codex", "cursor", "antigravity"), default="codex")
    chat.add_argument("--isolation", choices=("host", "sbx"), help="Choose trusted host execution or a Docker Sandbox")
    chat.add_argument("--trust-repo", action="store_true", help="Explicitly trust this repository and choose host execution")
    chat.add_argument("--resume", metavar="TASK_ID", help="Continue a task using its saved isolation")
    args = parser.parse_args(argv)
    if args.command == "chat":
        from contribos.tasks.chat import run_chat
        if bool(args.issue_url) == bool(args.resume):
            parser.error("Provide one issue URL or --resume TASK_ID.")
        try:
            return asyncio.run(run_chat(Settings(), args.agent, args.issue_url, trust_repo=args.trust_repo,
                                        isolation=args.isolation, resume_task=args.resume))
        except KeyboardInterrupt:
            return 130
    if args.command == "agents":
        return agents()
    if args.command == "doctor":
        return doctor()
    if args.command == "serve":
        try:
            return serve(Settings(host=args.host, port=args.port), demo=args.demo)
        except ValueError:
            parser.error("Cannot start: check the loopback host, port and local token file.")
    parser.print_help()
    return 0
