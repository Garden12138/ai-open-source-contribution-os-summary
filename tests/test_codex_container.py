from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from app.providers import (
    CodexExecInvocation,
    ProviderRunError,
    ProviderStage,
)
from app.sandbox_worker.container import (
    CONTAINER_SCHEMA_PATH,
    CONTAINER_WORKSPACE,
    ContainerIsolationPolicy,
    DockerCodexExecRunner,
    ReadOnlySnapshot,
)


IMAGE = "fixture/codex@sha256:" + "1" * 64


def _snapshot(tmp_path: Path) -> ReadOnlySnapshot:
    root = tmp_path / "snapshot-root"
    source = root / "snapshot"
    source.mkdir(parents=True)
    (source / "README.md").write_text("immutable fixture\n", encoding="utf-8")
    return ReadOnlySnapshot.capture(
        snapshot_id="snapshot-container-1",
        source=source,
        allowed_root=root,
    )


def _invocation(snapshot_id: str) -> CodexExecInvocation:
    return CodexExecInvocation(
        stage=ProviderStage.INSPECT,
        request_id="container-inspect-1",
        correlation_id="container-analysis-1",
        snapshot_id=snapshot_id,
        input_hash="1" * 64,
        prompt="Inspect only the immutable mounted snapshot.",
        output_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        model="fixture-model",
    )


def test_container_runner_builds_a_digest_pinned_hardened_codex_command(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    runner = DockerCodexExecRunner(
        snapshot=snapshot,
        image=IMAGE,
        docker_environment={"PATH": "/usr/bin:/bin"},
    )

    argv = runner.build_argv(
        model="fixture-model",
        schema_path=schema,
        container_name="contribos-codex-fixture",
    )

    assert argv[:2] == ("docker", "run")
    assert "--rm" in argv
    assert argv[argv.index("--pull") + 1] == "never"
    assert "--init" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert (
        argv[argv.index("--security-opt") + 1]
        == "no-new-privileges=true"
    )
    assert argv[argv.index("--pids-limit") + 1] == "128"
    assert argv[argv.index("--user") + 1] == "65532:65532"
    assert argv[argv.index("--workdir") + 1] == CONTAINER_WORKSPACE
    mounts = [
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "--mount"
    ]
    assert len(mounts) == 2
    assert any(
        f"src={snapshot.source}" in mount
        and f"dst={CONTAINER_WORKSPACE}" in mount
        and "readonly" in mount
        for mount in mounts
    )
    assert any(
        f"dst={CONTAINER_SCHEMA_PATH}" in mount and "readonly" in mount
        for mount in mounts
    )
    assert "/var/run/docker.sock" not in " ".join(argv)
    assert "CODEX_API_KEY" not in " ".join(argv)
    assert argv[argv.index("--entrypoint") + 1] == "codex"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("--output-schema") + 1] == CONTAINER_SCHEMA_PATH
    assert argv[-1] == "-"

    with pytest.raises(ValueError, match="digest-pinned"):
        DockerCodexExecRunner(
            snapshot=snapshot,
            image="fixture/codex:latest",
            docker_environment={"PATH": "/usr/bin:/bin"},
        )
    for variable in (
        "CODEX_API_KEY",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "SSH_AUTH_SOCK",
    ):
        with pytest.raises(ValueError, match=variable):
            DockerCodexExecRunner(
                snapshot=snapshot,
                image=IMAGE,
                docker_environment={
                    "PATH": "/usr/bin:/bin",
                    variable: "must-not-cross-the-supervisor-boundary",
                },
            )


def test_container_snapshot_fails_closed_on_escape_credentials_or_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    source = root / "snapshot"
    source.mkdir(parents=True)
    (source / "safe.txt").write_text("safe", encoding="utf-8")
    snapshot = ReadOnlySnapshot.capture(
        snapshot_id="snapshot-container-2",
        source=source,
        allowed_root=root,
    )
    runner = DockerCodexExecRunner(
        snapshot=snapshot,
        image=IMAGE,
        docker_environment={"PATH": "/usr/bin:/bin"},
        docker_executable="/missing/docker",
    )

    (source / "safe.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ProviderRunError, match="snapshot changed"):
        asyncio.run(runner.run(_invocation(snapshot.snapshot_id)))

    (source / "safe.txt").write_text("safe", encoding="utf-8")
    snapshot = ReadOnlySnapshot.capture(
        snapshot_id="snapshot-container-2",
        source=source,
        allowed_root=root,
    )
    mismatch_runner = DockerCodexExecRunner(
        snapshot=snapshot,
        image=IMAGE,
        docker_environment={"PATH": "/usr/bin:/bin"},
        docker_executable="/missing/docker",
    )
    with pytest.raises(ProviderRunError, match="does not match"):
        asyncio.run(mismatch_runner.run(_invocation("different-snapshot")))

    (source / ".env").write_text("SECRET=canary", encoding="utf-8")
    with pytest.raises(ValueError, match="denied"):
        ReadOnlySnapshot.capture(
            snapshot_id="snapshot-container-3",
            source=source,
            allowed_root=root,
        )
    (source / ".env").unlink()
    (source / "escape").symlink_to("../../outside")
    with pytest.raises(ValueError, match="symlink escapes"):
        ReadOnlySnapshot.capture(
            snapshot_id="snapshot-container-4",
            source=source,
            allowed_root=root,
        )
    with pytest.raises(ValueError, match="inside its allowed root"):
        ReadOnlySnapshot.capture(
            snapshot_id="snapshot-container-5",
            source=tmp_path,
            allowed_root=root,
        )


_RUN_DOCKER = os.environ.get("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") == "1"


@pytest.mark.skipif(
    not _RUN_DOCKER,
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
def test_real_codex_image_and_read_only_container_policy(
    tmp_path: Path,
) -> None:
    image = os.environ.get("CONTRIBOS_CODEX_IMAGE", "")
    if not image:
        pytest.fail("CONTRIBOS_CODEX_IMAGE must contain a pinned image ID")
    snapshot = _snapshot(tmp_path)
    policy = ContainerIsolationPolicy()
    docker_environment = {
        name: value
        for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "HOME", "PATH")
        if (value := os.environ.get(name))
    }
    codex_argv = policy.build_argv(
        docker_executable="docker",
        image=image,
        snapshot=snapshot,
        schema_path=None,
        container_name="contribos-codex-acceptance-version",
        entrypoint="codex",
        command=("--version",),
    )
    codex = subprocess.run(
        codex_argv,
        env=docker_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert codex.returncode == 0
    assert "codex-cli 0.146.0-alpha.3.1" in codex.stdout

    secret_parent_environment = {
        **docker_environment,
        "GITHUB_TOKEN": "github_pat_containercanary12345678",
        "OPENAI_API_KEY": "provider-container-canary",
        "SSH_AUTH_SOCK": "/tmp/contribos-ssh-agent-canary",
    }
    isolation_argv = policy.build_argv(
        docker_executable="docker",
        image=image,
        snapshot=snapshot,
        schema_path=None,
        container_name="contribos-codex-acceptance-isolation",
        entrypoint="node",
        command=(
            "-e",
            (
                "const fs=require('node:fs');"
                "const path=require('node:path');"
                "const hostHome=process.argv[1];"
                "const denied=['GITHUB_TOKEN','OPENAI_API_KEY',"
                "'SSH_AUTH_SOCK','CODEX_API_KEY'];"
                "if(denied.some(key=>process.env[key]))process.exit(10);"
                "if(process.env.HOME===hostHome)process.exit(11);"
                "if(fs.existsSync(path.join(hostHome,'.ssh')))process.exit(12);"
                "if(fs.existsSync('/var/run/docker.sock'))process.exit(13);"
                "if(fs.readFileSync('/workspace/README.md','utf8')!=="
                "'immutable fixture\\n')process.exit(14);"
                "try{fs.writeFileSync('/workspace/README.md','changed');"
                "process.exit(15)}catch(error){}"
                "try{fs.writeFileSync('/rootfs-write','changed');"
                "process.exit(16)}catch(error){}"
                "if(process.env.CODEX_HOME!=='/run/codex')process.exit(17);"
                "fs.writeFileSync('/run/codex/runtime-ok','ok');"
                "require('node:dns').lookup('example.com',error=>"
                "process.exit(error?0:18))"
            ),
            os.environ.get("HOME", ""),
        ),
    )
    isolated = subprocess.run(
        isolation_argv,
        env=secret_parent_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert isolated.returncode == 0, isolated.stderr
    assert (
        snapshot.source / "README.md"
    ).read_text(encoding="utf-8") == "immutable fixture\n"

    runner = DockerCodexExecRunner(
        snapshot=snapshot,
        image=image,
        docker_environment=docker_environment,
        timeout_seconds=30,
    )
    unauthenticated = asyncio.run(
        runner.run(_invocation(snapshot.snapshot_id))
    )
    assert unauthenticated.return_code != 0
    assert unauthenticated.return_code not in {125, 126, 127}
    assert (
        snapshot.source / "README.md"
    ).read_text(encoding="utf-8") == "immutable fixture\n"
