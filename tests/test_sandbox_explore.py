from __future__ import annotations

import asyncio
import hashlib
import io
import os
import subprocess
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.artifacts import ExecutionArtifactBundle
from app.provenance import content_hash
from app.sandbox_worker.explore import (
    DockerExploreRuntime,
    ExploreError,
    ExploreInspection,
    ExploreService,
    RepositoryArchive,
)
from app.sandbox_worker.specs import (
    JobSpec,
    JobSpecSigner,
    SandboxPolicy,
    SandboxStage,
)


HASH = "1" * 64
DEFAULT_IMAGE = "sha256:" + "5" * 64


def _archive(
    tmp_path: Path,
    *,
    base_sha: str = "a" * 40,
) -> RepositoryArchive:
    root_name = f"contribution-{base_sha}"
    archive_path = tmp_path / "repository.tar"
    with tarfile.open(archive_path, mode="w") as bundle:
        root = tarfile.TarInfo(root_name)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        bundle.addfile(root)
        for name, content, mode in (
            (
                f"{root_name}/pyproject.toml",
                b"[project]\nname='fixture'\n",
                0o644,
            ),
            (
                f"{root_name}/src/main.py",
                b"print('fixture')\n",
                0o755,
            ),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = mode
            bundle.addfile(member, io.BytesIO(content))
    return RepositoryArchive.capture(
        repository_full_name="fixture/contribution",
        base_commit_sha=base_sha,
        path=archive_path,
        allowed_root=tmp_path,
    )


def _spec(
    archive: RepositoryArchive,
    policy: SandboxPolicy,
    *,
    stage: SandboxStage = SandboxStage.EXPLORE,
    image: str = DEFAULT_IMAGE,
) -> JobSpec:
    artifacts = () if stage is SandboxStage.EXPLORE else ("3" * 64,)
    paths = ("src/main.py",) if stage is SandboxStage.IMPLEMENT else ()
    return JobSpec(
        spec_id="explore-spec-1",
        execution_attempt_id="attempt-1",
        correlation_id="explore-correlation-1",
        stage=stage,
        repository_full_name=archive.repository_full_name,
        base_commit_sha=archive.base_commit_sha,
        task_id="task-1",
        task_record_hash=HASH,
        analysis_version_id="analysis-1",
        analysis_record_hash=HASH,
        analysis_output_hash=HASH,
        snapshot_id="opportunity-snapshot-1",
        snapshot_inputs_hash=HASH,
        plan_version_id="plan-1",
        plan_content_hash=HASH,
        plan_record_hash=HASH,
        plan_approval_id="approval-1",
        approval_hash=HASH,
        approved_state_version_id="state-approved-1",
        approved_state_record_hash=HASH,
        provider_contract_hash=HASH,
        repository_archive_hash=archive.archive_hash,
        runner_image_digest=image,
        sandbox_policy_version=policy.version,
        sandbox_policy_hash=policy.policy_hash,
        input_artifact_hashes=artifacts,
        allowed_change_paths=paths,
        commands=(),
    )


def _git_archive(tmp_path: Path) -> RepositoryArchive:
    repository = tmp_path / "trusted-git-fixture"
    repository.mkdir()
    subprocess.run(
        ("git", "init", "--initial-branch=main", str(repository)),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Fixture"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "fixture@example.test",
        ),
        check=True,
    )
    (repository / "src").mkdir()
    (repository / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n",
        encoding="utf-8",
    )
    (repository / "src" / "main.py").write_text(
        "print('fixture')\n",
        encoding="utf-8",
    )
    subprocess.run(
        ("git", "-C", str(repository), "add", "--all"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-m", "fixture"),
        check=True,
        capture_output=True,
    )
    base_sha = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive_path = tmp_path / "exact-base.tar"
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "archive",
            "--format=tar",
            f"--prefix=contribution-{base_sha}/",
            f"--output={archive_path}",
            base_sha,
        ),
        check=True,
    )
    return RepositoryArchive.capture(
        repository_full_name="fixture/contribution",
        base_commit_sha=base_sha,
        path=archive_path,
        allowed_root=tmp_path,
    )


def _malicious_archive(
    tmp_path: Path,
    *,
    attack: str,
) -> RepositoryArchive:
    base_sha = "d" * 40
    root_name = f"contribution-{base_sha}"
    archive_path = tmp_path / f"malicious-{attack}.tar"
    with tarfile.open(archive_path, mode="w") as bundle:
        root = tarfile.TarInfo(root_name)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        bundle.addfile(root)
        project = b"[project]\nname='malicious-fixture'\n"
        valid = tarfile.TarInfo(f"{root_name}/pyproject.toml")
        valid.size = len(project)
        valid.mode = 0o644
        bundle.addfile(valid, io.BytesIO(project))
        if attack == "path-traversal":
            payload = b"must-not-escape\n"
            malicious = tarfile.TarInfo(
                f"{root_name}/../escaped-host-file.txt"
            )
            malicious.size = len(payload)
            malicious.mode = 0o644
            bundle.addfile(malicious, io.BytesIO(payload))
        elif attack == "symlink-escape":
            malicious = tarfile.TarInfo(f"{root_name}/escape-link")
            malicious.type = tarfile.SYMTYPE
            malicious.linkname = "../../host-only-target"
            malicious.mode = 0o777
            bundle.addfile(malicious)
        else:
            raise AssertionError(f"unknown malicious archive attack {attack}")
    return RepositoryArchive.capture(
        repository_full_name="fixture/contribution",
        base_commit_sha=base_sha,
        path=archive_path,
        allowed_root=tmp_path,
    )


class _FakeExploreRuntime:
    def __init__(self, *, wrong_size: bool = False) -> None:
        self.wrong_size = wrong_size
        self.materialize_calls = 0
        self.inspect_calls = 0

    async def materialize(
        self,
        *,
        archive: RepositoryArchive,
        destination: Path,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> None:
        self.materialize_calls += 1
        assert image_digest == DEFAULT_IMAGE
        assert archive.archive_hash
        assert policy.network_mode == "none"
        (destination / "src").mkdir()
        (destination / "pyproject.toml").write_bytes(
            b"[project]\nname='fixture'\n"
        )
        (destination / "src" / "main.py").write_bytes(
            b"print('fixture')\n"
        )

    async def inspect(
        self,
        *,
        snapshot,  # type: ignore[no-untyped-def]
        image_digest: str,
        policy: SandboxPolicy,
    ) -> ExploreInspection:
        self.inspect_calls += 1
        return ExploreInspection.from_wire(
            {
                "inventory_hash": "7" * 64,
                "file_count": snapshot.file_count,
                "total_bytes": snapshot.total_bytes
                + (1 if self.wrong_size else 0),
                "sample_paths": ["pyproject.toml", "src/main.py"],
                "manifest_paths": ["pyproject.toml"],
            }
        )


def test_explore_binds_signed_spec_archive_snapshot_and_inventory(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(key_id="explore-key", signing_key=b"e" * 32)
    runtime = _FakeExploreRuntime()
    signed = signer.sign(_spec(archive, policy))

    result = asyncio.run(
        ExploreService(
            signer=signer,
            policy=policy,
            runtime=runtime,
        ).run(signed, archive)
    )

    assert runtime.materialize_calls == 1
    assert runtime.inspect_calls == 1
    assert result.spec_hash == signed.spec_hash
    assert result.repository_full_name == "fixture/contribution"
    assert result.base_commit_sha == "a" * 40
    assert result.repository_archive_hash == archive.archive_hash
    assert result.runner_image_digest == DEFAULT_IMAGE
    assert result.sandbox_policy_hash == policy.policy_hash
    assert result.file_count == 2
    assert result.manifest_paths == ("pyproject.toml",)
    payload = result.to_wire()
    result_hash = payload.pop("result_hash")
    assert result_hash == content_hash(payload)
    bundle = ExecutionArtifactBundle.from_result(result)
    assert [item.role for item in bundle.contents] == ["stage-result"]
    assert bundle.contents[0].artifact_id == result.result_hash


def test_explore_rejects_wrong_stage_archive_tampering_and_wrong_inventory(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(key_id="explore-key", signing_key=b"e" * 32)

    implement = signer.sign(
        _spec(archive, policy, stage=SandboxStage.IMPLEMENT)
    )
    with pytest.raises(ExploreError, match="Explore JobSpec"):
        asyncio.run(
            ExploreService(
                signer=signer,
                policy=policy,
                runtime=_FakeExploreRuntime(),
            ).run(implement, archive)
        )

    mismatched = replace(archive, archive_hash="8" * 64)
    with pytest.raises(ExploreError, match="does not match"):
        asyncio.run(
            ExploreService(
                signer=signer,
                policy=policy,
                runtime=_FakeExploreRuntime(),
            ).run(signer.sign(_spec(archive, policy)), mismatched)
        )

    archive.path.write_bytes(archive.path.read_bytes() + b"tampered")
    with pytest.raises(ExploreError, match="changed"):
        asyncio.run(
            ExploreService(
                signer=signer,
                policy=policy,
                runtime=_FakeExploreRuntime(),
            ).run(signer.sign(_spec(archive, policy)), archive)
        )

    fresh_archive = _archive(tmp_path, base_sha="b" * 40)
    with pytest.raises(ExploreError, match="frozen snapshot"):
        asyncio.run(
            ExploreService(
                signer=signer,
                policy=policy,
                runtime=_FakeExploreRuntime(wrong_size=True),
            ).run(
                signer.sign(_spec(fresh_archive, policy)),
                fresh_archive,
            )
        )


def test_repository_archive_capture_is_bounded_and_path_safe(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    assert archive.archive_hash == hashlib.sha256(
        archive.path.read_bytes()
    ).hexdigest()

    symlink = tmp_path / "archive-link.tar"
    symlink.symlink_to(archive.path)
    with pytest.raises(ValueError, match="symlink"):
        RepositoryArchive.capture(
            repository_full_name=archive.repository_full_name,
            base_commit_sha=archive.base_commit_sha,
            path=symlink,
            allowed_root=tmp_path,
        )
    with pytest.raises(ValueError, match="base commit"):
        RepositoryArchive.capture(
            repository_full_name=archive.repository_full_name,
            base_commit_sha="main",
            path=archive.path,
            allowed_root=tmp_path,
        )


def test_docker_explore_commands_are_shell_free_networkless_and_bounded(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    runtime = DockerExploreRuntime(
        docker_environment={"PATH": "/usr/bin", "HOME": "/tmp/worker"}
    )
    policy = SandboxPolicy()
    materialize = runtime.build_materialize_argv(
        archive=archive,
        destination=destination.resolve(),
        image_digest=DEFAULT_IMAGE,
        policy=policy,
        container_name="contribos-explore-materialize-" + "1" * 32,
    )

    for expected in (
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "no-new-privileges=true",
        "--pids-limit",
        "--memory",
        "--memory-swap",
        "--cpus",
        "--tmpfs",
    ):
        assert expected in materialize
    joined = " ".join(materialize)
    assert "/input/repository.tar" in joined
    assert "dst=/output" in joined
    assert "readonly" in joined
    assert "/var/run/docker.sock" not in joined
    assert "GITHUB_TOKEN" not in joined
    assert "SSH_AUTH_SOCK" not in joined
    assert "GIT_CONFIG_NOSYSTEM=1" in joined
    assert "core.hooksPath" in joined
    assert "protocol.allow" in joined
    assert materialize[-5:-3] == ("-I", "-c")

    with pytest.raises(ValueError, match="disallowed"):
        DockerExploreRuntime(
            docker_environment={
                "PATH": "/usr/bin",
                "GITHUB_TOKEN": "github_pat_explorecanary12345678",
            }
        )


@pytest.mark.skipif(
    os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
def test_real_read_only_explore_on_exact_archive(tmp_path: Path) -> None:
    image = os.environ.get("CONTRIBOS_SANDBOX_RUNNER_IMAGE", "")
    if not image.startswith("sha256:"):
        pytest.fail("CONTRIBOS_SANDBOX_RUNNER_IMAGE must be a local image digest")
    archive = _git_archive(tmp_path)
    policy = SandboxPolicy()
    signer = JobSpecSigner(key_id="real-explore-key", signing_key=b"r" * 32)
    docker_environment = {
        name: os.environ[name]
        for name in (
            "DOCKER_CERT_PATH",
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "HOME",
            "PATH",
            "TMPDIR",
        )
        if name in os.environ
    }

    result = asyncio.run(
        ExploreService(
            signer=signer,
            policy=policy,
            runtime=DockerExploreRuntime(
                docker_environment=docker_environment
            ),
        ).run(
            signer.sign(_spec(archive, policy, image=image)),
            archive,
        )
    )

    assert result.base_commit_sha == archive.base_commit_sha
    assert result.repository_archive_hash == archive.archive_hash
    assert result.file_count == 2
    assert result.manifest_paths == ("pyproject.toml",)


@pytest.mark.skipif(
    os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
@pytest.mark.sandbox_malicious
@pytest.mark.parametrize("attack", ("path-traversal", "symlink-escape"))
def test_real_explore_rejects_malicious_archive_paths(
    tmp_path: Path,
    attack: str,
) -> None:
    image = os.environ.get("CONTRIBOS_SANDBOX_RUNNER_IMAGE", "")
    if not image.startswith("sha256:"):
        pytest.fail("CONTRIBOS_SANDBOX_RUNNER_IMAGE must be a local image digest")
    archive = _malicious_archive(tmp_path, attack=attack)
    policy = SandboxPolicy()
    signer = JobSpecSigner(
        key_id=f"real-malicious-{attack}",
        signing_key=b"z" * 32,
    )
    docker_environment = {
        name: os.environ[name]
        for name in (
            "DOCKER_CERT_PATH",
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "HOME",
            "PATH",
            "TMPDIR",
        )
        if name in os.environ
    }

    with pytest.raises(
        ExploreError,
        match="container failed with a redacted error",
    ):
        asyncio.run(
            ExploreService(
                signer=signer,
                policy=policy,
                runtime=DockerExploreRuntime(
                    docker_environment=docker_environment
                ),
            ).run(
                signer.sign(_spec(archive, policy, image=image)),
                archive,
            )
        )
    assert not (tmp_path / "escaped-host-file.txt").exists()
    assert not (tmp_path / "host-only-target").exists()
