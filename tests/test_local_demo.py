"""Offline trial boundaries; process lifecycle is also exercised by real HTTP smoke."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scripts import local


def config(root: Path) -> None:
    root.mkdir(exist_ok=True)
    (root / "config.json").write_text(json.dumps({"mode": local.MARKER, "root": str(root),
        "port": 18765, "instance": "synthetic-instance", "access": "synthetic-local-access",
        "signing": "ab" * 32}))


def test_children_do_not_inherit_credentials_or_live_flags(tmp_path: Path, monkeypatch) -> None:
    for key in ("NVIDIA_API_KEY", "GITHUB_TOKEN", "MODEL_GATEWAY_MANAGEMENT_KEY_FILE",
                "CONTRIBOS_RUN_AGENT_LIVE_ACCEPTANCE", "HTTPS_PROXY", "PUBLISHER_MODE"):
        monkeypatch.setenv(key, "synthetic-private-canary")
    env = local.clean_environment(tmp_path)
    assert not any("canary" in value for value in env.values())
    assert env["DATABASE_URL"] == f"sqlite+pysqlite:///{tmp_path / 'demo.db'}"


@pytest.mark.parametrize("change", ["marker", "root", "symlink"])
def test_unowned_or_redirected_directory_is_rejected(tmp_path: Path, change: str) -> None:
    config(tmp_path)
    target = tmp_path
    if change == "symlink":
        target = tmp_path / "redirect"
        target.symlink_to(tmp_path, target_is_directory=True)
    else:
        value = json.loads((tmp_path / "config.json").read_text())
        value["mode" if change == "marker" else "root"] = "unowned"
        (tmp_path / "config.json").write_text(json.dumps(value))
    with pytest.raises(RuntimeError):
        local.configuration(target)


def test_stop_never_signals_a_reused_pid(tmp_path: Path, monkeypatch) -> None:
    config(tmp_path)
    (tmp_path / "processes.json").write_text(json.dumps({"serve": 42}))
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout="unrelated-user-service"))
    monkeypatch.setattr(local.os, "kill", lambda *a: pytest.fail("Signalled unrelated process"))
    local.stop(tmp_path)
    assert (tmp_path / "config.json").exists()


def test_owned_process_requires_the_exact_command(tmp_path: Path, monkeypatch) -> None:
    command = f"{local.sys.executable} {local.SCRIPT} _serve --data-dir {tmp_path}"
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=command + "-other\n"))
    assert not local.owned_process(42, "serve", tmp_path)
    monkeypatch.setattr(local.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=command + "\n"))
    assert local.owned_process(42, "serve", tmp_path)


def test_demo_scan_is_offline_and_browser_mutations_keep_csrf(tmp_path: Path, monkeypatch) -> None:
    from app.github import GitHubClient
    from app.worker import ContribOSWorker
    monkeypatch.setattr(GitHubClient, "__init__", lambda *a, **kw:
                        pytest.fail("Real GitHub client instantiated"))
    config(tmp_path)
    app = local.create_demo_app(tmp_path)
    with TestClient(app) as client:
        assert "离线试用" in client.get("/").text
        assert client.get("/api/local-demo").json()["mode"] == local.MARKER
        assert client.get("/api/v1/model-settings").json()["credential_management_available"] is False
        denied = client.post("/api/v1/scans", json={}, headers={"Origin": "https://evil.test"})
        assert denied.status_code == 403
        assert client.post("/api/v1/scans", json={}).status_code == 401
        csrf = client.get("/api/v1/meta").json()["csrf_token"]
        scan = client.post("/api/v1/scans", json={}, headers={"Origin": "http://testserver",
            "X-CSRF-Token": csrf, "Idempotency-Key": "demo-test"})
        assert scan.status_code == 202
        worker = ContribOSWorker(app.state.database, local.demo_settings(tmp_path),
                                local.SyntheticGitHub, worker_id="demo-test")
        assert asyncio.run(worker.run_once()).state == "succeeded"
        picks = client.get("/api/v1/opportunities/daily").json()["picks"]
        assert len(picks) == 1
        assert "离线样例" in picks[0]["opportunity"]["title"]
