"""Serve the local application and inspect its prerequisites."""

import argparse
import asyncio
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from contribos import __version__
from contribos.agent.profiles import PROFILES, agent_statuses
from contribos.config import Settings, server_origin, validate_bind_host
from contribos.security import redact, without_github_credentials


def serve(settings: Settings, *, demo: bool = False, desktop: bool = False) -> int:
    import uvicorn

    from contribos.desktop_mode import DesktopMode, DesktopServer, login_path
    from contribos.web.app import create_app

    origin = server_origin(settings)
    mode = None
    smoke = False
    if desktop:
        mode = DesktopMode.from_environment()
        smoke = os.environ.pop('CONTRIBOS_DESKTOP_SMOKE', '0') == '1'
        os.environ['PATH'], resolved = login_path()
        settings = replace(settings, login_path_resolved=resolved)

    def run(config: Settings) -> None:
        # Bootstrap URLs contain a login code; HTTP access logging must stay off.
        app = create_app(config, demo=demo, desktop=mode)
        options = dict(host=validate_bind_host(config.host), port=config.port, access_log=False,
                       timeout_graceful_shutdown=2)
        if mode:
            def smoke_check() -> None:
                result = doctor_report(replace(config, tool_paths=dict(config.tool_paths)))
                available = sum(check['status'] == 'ok' for check in result)
                print(f'CONTRIBOS_DOCTOR checks={len(result)} ok={available}', file=sys.stderr, flush=True)

            DesktopServer(uvicorn.Config(app, **options), smoke_check=smoke_check if smoke else None).run()
        else:
            code = app.state.auth.issue_startup_code()
            print(f"Login (valid for 60 seconds, one use): {origin}/desktop/bootstrap?code={code}", flush=True)
            uvicorn.run(app, **options)

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
    env = without_github_credentials(os.environ)
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
                    text=True, timeout=5, check=False, env=env,
                )
                detail = redact(result.stdout.strip())[:160] if result.returncode == 0 else "Git version check failed."
                results.append(CheckResult(name, "ok" if result.returncode == 0 else "error",
                                           f'{executable} · {detail}' if settings else detail))
            else:
                # gh can include account and credential diagnostics; never capture them.
                result = subprocess.run(
                    [executable, "auth", "status", "--hostname", "github.com"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=5, check=False, env=env,
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
        if settings.login_path_resolved is not None:
            results.append(CheckResult('login_path', 'ok' if settings.login_path_resolved else 'warning',
                'Loaded PATH from the login shell.' if settings.login_path_resolved
                else 'Login shell PATH unavailable. Using the default PATH and configured tool paths.'))
        for name in ('node', 'sbx', 'docker'):
            executable = settings.tool(name)
            results.append(CheckResult(name, 'ok' if executable else 'missing', executable or 'Command not found.'))
        try:
            free = shutil.disk_usage(settings.home).free // (1024 ** 3)
            results.append(CheckResult('disk', 'ok', f'{free} GiB available in the data directory.'))
        except OSError:
            results.append(CheckResult('disk', 'error', 'Disk usage is unavailable.'))
    return results


def _sandbox_checks(settings: Settings, *, run: bool = False) -> list[CheckResult]:
    from contribos.agent.profiles import PROFILES
    from contribos.db import connect, migrate
    from contribos.sandbox.base import SandboxError
    from contribos.sandbox.probes import CHECKS, probe_empty_vm
    from contribos.sandbox.resources import resource_limits
    from contribos.sandbox.sbx import SbxSandbox
    from contribos.tasks.store import TaskStore, utc_now

    snapshot = {}
    if run:
        try:
            with closing(connect(settings.database_path)) as db:
                migrate(db)
                store = TaskStore(db)
                settings.tool_paths.update(store.setting('tool_paths', {}))
                vm = SbxSandbox(settings, PROFILES['codex'], extra_domains=store.setting('sandbox_extra_domains', []))
                vm.resource_limits = lambda: resource_limits(store.setting('resource_limits', {}))
                checks = asyncio.run(probe_empty_vm(vm))
                snapshot = {'time': utc_now(), 'checks': checks}
                store.set_setting('last_sandbox_probe', snapshot)
        except (ValueError, OSError, SandboxError, sqlite3.Error):
            return [CheckResult('sandbox_probe', 'error', 'Could not complete or save the sandbox check.')]
    elif settings.database_path.is_file():
        try:
            with closing(sqlite3.connect(settings.database_path.as_uri() + '?mode=ro', uri=True)) as db:
                snapshot = TaskStore(db).setting('last_sandbox_probe', {})
        except (ValueError, sqlite3.Error):
            return [CheckResult('sandbox_probe', 'error', 'Could not read the last sandbox check.')]
    if not snapshot:
        return [CheckResult('sandbox_probe', 'not_run', 'Run contribos doctor --probe-sandbox to check a fresh VM.')]
    return [CheckResult(name, snapshot['checks'][name], detail + ' · ' + snapshot['time'])
            for name, detail in CHECKS.items()]


def doctor_report(settings: Settings | None = None) -> list[dict[str, str]]:
    return [asdict(result) for result in doctor_checks(settings) + _sandbox_checks(settings or Settings())]


def doctor(*, probe_sandbox: bool = False) -> int:
    results = doctor_checks() + _sandbox_checks(Settings(), run=probe_sandbox)
    for result in results:
        print(f"{result.name}: [{result.status}] {result.detail}")
    return int(any(result.status != "ok" and (result.status != "not_run" or probe_sandbox) for result in results))


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
    server.add_argument("--desktop", action="store_true", help="Run as the desktop shell's backend")
    diagnostics = commands.add_parser("doctor", help="Check prerequisites and show the last sandbox probe")
    diagnostics.add_argument("--probe-sandbox", action="store_true", help="Test isolation in a disposable task VM")
    commands.add_parser("agents", help="Check Agent installation, isolated login and configuration")
    legacy = commands.add_parser("import-legacy", help="Import discovery data and published GitHub PRs from a legacy database")
    legacy.add_argument("--db", type=Path, required=True, help="Read-only legacy SQLite database")
    chat = commands.add_parser("chat", help="Chat with an ACP Agent in an independent workspace")
    chat.add_argument("issue_url", nargs="?")
    chat.add_argument("--agent", choices=tuple(PROFILES), default="codex")
    chat.add_argument("--isolation", choices=("host", "sbx"), help="Choose trusted host execution or a Docker Sandbox")
    chat.add_argument("--trust-repo", action="store_true", help="Explicitly trust this repository and choose host execution")
    chat.add_argument("--resume", metavar="TASK_ID", help="Continue a task using its saved isolation")
    args = parser.parse_args(argv)
    if args.command == "import-legacy":
        from contribos.legacy import import_legacy
        try:
            counts = import_legacy(args.db, Settings().database_path)
        except (ValueError, IndexError, TypeError, OSError, sqlite3.Error, OverflowError):
            print("legacy_import_failed: Could not read or import the legacy database.", file=sys.stderr)
            return 1
        print("Imported " + ", ".join(f"{value} {key}" for key, value in counts.items()))
        return 0
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
        return doctor(probe_sandbox=args.probe_sandbox)
    if args.command == "serve":
        try:
            return serve(Settings(host=args.host, port=args.port), demo=args.demo, desktop=args.desktop)
        except ValueError:
            parser.error("Cannot start: check the loopback host, port, local token file and desktop launch environment.")
    parser.print_help()
    return 0
