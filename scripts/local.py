#!/usr/bin/env python3
"""Isolated, credential-free local demo. Never loads .env or starts a Gateway."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import gzip
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
sys.path.insert(0, str(REPO))
MARKER = "contribos-offline-demo-v1"


class SyntheticGitHub:
    """Only synthetic metadata. Unsupported archive operations fail locally."""
    rate_limit_remaining = None
    rate_limit_reset_at = None

    async def __aenter__(self) -> SyntheticGitHub:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        return [{"id": 71001, "number": 41,
                 "title": "[离线样例] Fix a small planning validation bug",
                 "body": "Synthetic issue: preserve immutable plan provenance and verify a small validation fix with deterministic tests. No real GitHub repository is used.",
                 "html_url": "https://github.com/fixture/planning/issues/41",
                 "repository_url": "https://api.github.com/repos/fixture/planning",
                 "state": "open", "labels": [{"name": "help wanted"}],
                 "comments": 1, "assignees": [], "author_association": "MEMBER",
                 "created_at": now, "updated_at": now}][:limit]

    async def get_repository_bundle(self, full_name: str) -> dict[str, Any]:
        return {"repository": {"id": 72001, "full_name": full_name,
                "description": "离线合成仓库，仅用于试用", "html_url": f"https://github.com/{full_name}",
                "language": "Python", "license": {"spdx_id": "MIT"}, "stargazers_count": 1500,
                "forks_count": 50, "open_issues_count": 8, "archived": False, "disabled": False,
                "default_branch": "main", "topics": ["planning"],
                "pushed_at": datetime.now(timezone.utc).isoformat()},
                "community": {"health_percentage": 90, "files": {}}}


def configuration(root: Path) -> dict[str, Any]:
    if root.is_symlink() or (root / "config.json").is_symlink():
        raise RuntimeError("Demo directory and configuration must not be symlinks")
    value = json.loads((root / "config.json").read_text())
    if value.get("mode") != MARKER or value.get("root") != str(root):
        raise RuntimeError("Directory is not an owned offline demo")
    return value


def demo_settings(root: Path):
    from app.config import Settings
    config = configuration(root)
    return Settings(database_url=f"sqlite+pysqlite:///{root / 'demo.db'}",
                    artifact_root=str(root / "artifacts"), local_access_token=config["access"],
                    analysis_provider="fake", implementation_provider="fake", review_provider="fake",
                    github_queries=("offline-demo",), sandbox_stage_runtime="fake",
                    sandbox_job_spec_signing_key=bytes.fromhex(config["signing"]),
                    publisher_mode="none")


def create_demo_app(root: Path):
    from app.api import create_app
    from fastapi.responses import HTMLResponse
    application = create_app(demo_settings(root))
    application.state.github_client_factory = SyntheticGitHub

    @application.get("/api/local-demo", include_in_schema=False)
    def identity() -> dict[str, str]:
        return {"mode": MARKER, "instance": configuration(root)["instance"]}

    @application.middleware("http")
    async def demo_notice(request, call_next):
        if request.url.path == "/":
            html = (REPO / "app/static/index.html").read_text()
            notice = '<aside role="status" style="padding:12px;background:#fff3cd;color:#332600">离线试用：合成 Issue、Fake 分析、预设 Patch 和模拟测试；未调用真实 AI。运行 ./scripts/local-smoke.sh 可创建完整样例结果。发布功能关闭。</aside>'
            return HTMLResponse(html.replace('<body class="studio-app">', '<body class="studio-app">' + notice),
                                headers={"Cache-Control": "no-cache"})
        return await call_next(request)
    return application


def clean_environment(root: Path) -> dict[str, str]:
    """An allowlist prevents inherited provider keys and live opt-in flags."""
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if k in os.environ}
    env.update(PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1",
               DATABASE_URL=f"sqlite+pysqlite:///{root / 'demo.db'}", ARTIFACT_ROOT=str(root / "artifacts"))
    return env


def owned_process(pid: int, role: str, root: Path) -> bool:
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "args="], capture_output=True, text=True)
    expected = f"{sys.executable} {SCRIPT} _{role} --data-dir {root}"
    return result.returncode == 0 and result.stdout.strip() == expected


def stop(root: Path) -> None:
    configuration(root)
    state = root / "processes.json"
    if not state.exists():
        print("Demo is stopped; data retained.")
        return
    processes = json.loads(state.read_text())
    for role, pid in processes.items():
        if owned_process(pid, role, root):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while any(owned_process(pid, role, root) for role, pid in processes.items()):
        if time.monotonic() > deadline:
            raise RuntimeError("Owned process did not stop; inspect demo logs")
        time.sleep(0.1)
    state.unlink()
    print("Demo stopped; database and artifacts retained.")


def start(root: Path, port: int) -> None:
    import httpx
    config = configuration(root)
    state = root / "processes.json"
    if state.exists():
        raise RuntimeError("Demo state exists; run local-stop.sh before starting again")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    # Migrate once before API and Worker start; concurrent first boot can race DDL.
    from app.database import Database
    database = Database(f"sqlite+pysqlite:///{root / 'demo.db'}")
    try:
        database.create_schema()
    finally:
        database.close()
    config["port"] = port
    (root / "config.json").write_text(json.dumps(config))
    processes = {}
    try:
        for role in ("serve", "worker"):
            with (root / f"{role}.log").open("ab") as log:
                child = subprocess.Popen([sys.executable, str(SCRIPT), "_" + role, "--data-dir", str(root)],
                                         cwd=REPO, env=clean_environment(root), stdout=log, stderr=log,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
            processes[role] = child.pid
        state.write_text(json.dumps(processes))
        deadline = time.monotonic() + 25
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                if not all(owned_process(pid, role, root) for role, pid in processes.items()):
                    raise RuntimeError("Demo process exited; inspect serve.log and worker.log")
                try:
                    health = client.get(f"http://127.0.0.1:{port}/health")
                    identity = client.get(f"http://127.0.0.1:{port}/api/local-demo").json()
                    if health.status_code == 200 and identity.get("instance") == config["instance"]:
                        print(f"Offline demo: http://127.0.0.1:{port}\nData and logs: {root}")
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.2)
        raise RuntimeError("Demo startup timed out")
    except BaseException:
        stop(root)
        raise


def smoke(root: Path) -> dict[str, Any]:
    """Use real HTTP, jobs and persistence with deterministic fake stage results."""
    import httpx
    from app.archives import RepositoryArchiveStore
    config = configuration(root)
    run = uuid4().hex
    with httpx.Client(base_url=f"http://127.0.0.1:{config['port']}", trust_env=False,
                      timeout=15, headers={"Authorization": "Bearer " + config["access"]}) as client:
        assert client.get("/api/local-demo").json() == {"mode": MARKER, "instance": config["instance"]}
        def post(path: str, data: dict[str, Any]) -> dict[str, Any]:
            response = client.post(path, json=data, headers={"Idempotency-Key": uuid4().hex})
            if response.status_code >= 400:
                raise RuntimeError(f"Demo API {path} returned {response.status_code}: {response.text[:500]}")
            return response.json()

        def wait_job(job: dict[str, Any]) -> None:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                value = client.get(f"/api/v1/jobs/{job['id']}").json()
                if value["state"] == "succeeded":
                    return
                if value["state"] in {"failed", "cancelled", "timed_out"}:
                    raise RuntimeError(f"Demo job {value['state']}: {value.get('error_code')}")
                time.sleep(0.2)
            raise RuntimeError("Demo job timed out")

        wait_job(post("/api/v1/scans", {}))
        board = client.get("/api/v1/opportunities/daily").json()
        pick = board["picks"][0]
        opportunity = pick["opportunity"]["id"]
        endpoint = f"/api/v1/opportunities/{opportunity}/analyses"
        wait_job(post(endpoint, {"snapshot_id": pick["snapshot_id"]}))
        versions = client.get(endpoint).json()["versions"]
        analysis = next(v for v in versions if v["snapshot_id"] == pick["snapshot_id"])
        task = post("/api/v1/tasks", {"analysis_version_id": analysis["id"]})
        plan = post(f"/api/v1/tasks/{task['id']}/plan-versions", {
            "goal": "离线样例：验证方案、预设补丁、测试与审查的应用链路",
            "acceptance_criteria": ["Immutable provenance is retained", "Fake verification completes"],
            "files_to_inspect": ["app/planning.py"], "files_likely_to_change": ["app/plans.py"],
            "implementation_steps": ["Use the pre-scripted fake patch; no repository code is executed"],
            "tests_to_add_or_run": ["Simulate focused verification"],
            "commands_to_run": [{"command_id": "focused_tests", "purpose": "Fake verification",
                                 "argv": ["python", "-m", "pytest", "-q", "tests/test_planning.py"]}],
            "risks": ["Demonstration only; no AI or real test execution"], "questions_for_maintainer": []})
        approval = post(f"/api/v1/plan-versions/{plan['id']}/approve",
                        {"base_commit_sha": "a" * 40, "actor_id": "offline-demo"})
        incoming = root / ("incoming-" + run + ".gz")
        incoming.write_bytes(gzip.compress(b"synthetic-demo-archive-no-executable-code"))
        archive_hash = RepositoryArchiveStore(root / "artifacts").put_file(incoming)
        incoming.unlink()
        execution = post(f"/api/v1/plan-versions/{plan['id']}/executions", {
            "approval_id": approval["id"], "base_commit_sha": "a" * 40,
            "repository_archive_hash": archive_hash, "runner_image_digest": "sha256:" + "c" * 64,
            "actor_id": "offline-demo"})
        eid = execution["id"]

        def wait_stage(stage: str) -> dict[str, Any]:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                detail = client.get(f"/api/v1/executions/{eid}").json()
                current = detail["current_stage"]
                if current["stage"] == stage and current["status"] == "succeeded":
                    return detail
                if current["status"] in {"failed", "cancelled", "timed_out"}:
                    raise RuntimeError(f"Demo stage failed: {current.get('reason_code')}")
                time.sleep(0.2)
            raise RuntimeError(f"Demo {stage} timed out")

        wait_stage("explore")
        changes = post(f"/api/v1/executions/{eid}/change-sets", {"source": "fake"})
        detail = wait_stage("verify")
        review = post(f"/api/v1/executions/{eid}/reviews", {"actor_id": "offline-demo", "reviewer": "fake"})
        assert review["verdict"] == "pass"
        result = {"mode": MARKER, "real_ai": False, "real_repository_tests": False,
                  "external_writes": 0, "run": run, "completed_at": datetime.now(timezone.utc).isoformat(),
                  "opportunity_id": opportunity, "snapshot_id": pick["snapshot_id"],
                  "analysis_id": analysis["id"], "task_id": task["id"], "plan_id": plan["id"],
                  "execution_id": eid, "review_id": review["id"], "review_verdict": review["verdict"],
                  "change_set_hash": changes["change_set_hash"], "artifact_manifests": len(detail["artifact_manifests"]),
                  "result_url": f"http://127.0.0.1:{config['port']}/api/v1/executions/{eid}",
                  "web_url": f"http://127.0.0.1:{config['port']}/#/contributions"}
        (root / "last-smoke.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "smoke", "_serve", "_worker"))
    parser.add_argument("--data-dir", type=Path, default=REPO / ".local-demo")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    # Child processes do not inherit any application settings or credentials.
    root = args.data_dir.expanduser().absolute()
    if root.is_symlink():
        parser.error("Demo directory must not be a symlink")
    if args.action == "start":
        if root.exists() and not (root / "config.json").exists() and any(root.iterdir()):
            parser.error("Refusing nonempty directory without demo ownership marker")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        if not (root / "config.json").exists():
            config = {"mode": MARKER, "root": str(root), "port": args.port, "instance": uuid4().hex,
                      "access": secrets.token_urlsafe(32), "signing": secrets.token_hex(32)}
            with (root / "config.json").open("x") as stream:
                os.chmod(root / "config.json", 0o600)
                json.dump(config, stream)
    if args.action in {"start", "stop"}:
        with (root / ".control.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start(root, args.port) if args.action == "start" else stop(root)
    elif args.action == "smoke":
        print(json.dumps(smoke(root), indent=2, ensure_ascii=False))
    else:
        environment = clean_environment(root)
        os.environ.clear()
        os.environ.update(environment)
        if args.action == "_serve":
            import uvicorn
            uvicorn.run(create_demo_app(root), host="127.0.0.1", port=configuration(root)["port"], access_log=False)
        else:
            from app.database import Database
            from app.worker import ContribOSWorker
            settings = demo_settings(root)
            database = Database(settings.database_url)
            database.create_schema()
            try:
                asyncio.run(ContribOSWorker(database, settings, SyntheticGitHub,
                            worker_id="offline-demo-worker").run_forever(poll_interval_seconds=0.2))
            finally:
                database.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, AssertionError) as exc:
        print(f"Offline demo failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
