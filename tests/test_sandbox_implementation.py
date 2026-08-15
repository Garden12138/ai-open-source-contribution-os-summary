from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.artifacts import ExecutionArtifactBundle
from app.provenance import content_hash
from app.sandbox_worker.explore import ExploreResult, RepositoryArchive
from app.sandbox_worker.implementation import (
    ChangeOperation,
    ChangeOperationKind,
    DisposableWorkspace,
    DockerImplementRuntime,
    ImplementationChangeSet,
    ImplementationError,
    ImplementationInspection,
    ImplementService,
)
from app.sandbox_worker.specs import (
    JobSpec,
    JobSpecSigner,
    SandboxCommand,
    SandboxPolicy,
    SandboxStage,
)
from app.sandbox_worker.verification import (
    DockerVerifyRuntime,
    VerifyCommandStatus,
    VerifyService,
)


HASH = "1" * 64
IMAGE = "sha256:" + "5" * 64
ORIGINAL = "print('before')\n"
UPDATED = "print('after')\n"
PROJECT = "[project]\nname='fixture'\n"
NEW_TEST = "def test_main():\n    assert True\n"


def _file_entry(path: str, content: str) -> dict[str, object]:
    encoded = content.encode("utf-8")
    return {
        "path": path,
        "type": "file",
        "size": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "executable": False,
    }


def _inspection_wire(*, wrong_paths: bool = False) -> dict[str, object]:
    baseline = [
        _file_entry("pyproject.toml", PROJECT),
        _file_entry("src/main.py", ORIGINAL),
    ]
    if wrong_paths:
        result = [
            _file_entry("outside.py", "unexpected\n"),
            *baseline,
        ]
        changed = ["outside.py"]
        unified_diff = (
            "diff --git a/outside.py b/outside.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/outside.py\n"
            "@@ -0,0 +1 @@\n"
            "+unexpected\n"
        )
    else:
        result = [
            _file_entry("pyproject.toml", PROJECT),
            _file_entry("src/main.py", UPDATED),
            _file_entry("tests/test_main.py", NEW_TEST),
        ]
        changed = ["src/main.py", "tests/test_main.py"]
        unified_diff = (
            "diff --git a/src/main.py b/src/main.py\n"
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1 +1 @@\n"
            "-print('before')\n"
            "+print('after')\n"
            "diff --git a/tests/test_main.py b/tests/test_main.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/tests/test_main.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def test_main():\n"
            "+    assert True\n"
        )
    return {
        "baseline_inventory_hash": content_hash(baseline),
        "result_inventory_hash": content_hash(result),
        "baseline_file_count": len(baseline),
        "result_file_count": len(result),
        "baseline_total_bytes": sum(
            item["size"] for item in baseline
        ),
        "result_total_bytes": sum(item["size"] for item in result),
        "changed_paths": changed,
        "baseline_inventory": baseline,
        "result_inventory": result,
        "unified_diff": unified_diff,
        "diff_hash": hashlib.sha256(
            unified_diff.encode("utf-8")
        ).hexdigest(),
    }


def _archive(tmp_path: Path) -> RepositoryArchive:
    base_sha = "a" * 40
    root_name = f"contribution-{base_sha}"
    path = tmp_path / "implementation-source.tar"
    with tarfile.open(path, mode="w") as bundle:
        for name, content in (
            (f"{root_name}/pyproject.toml", PROJECT.encode("utf-8")),
            (f"{root_name}/src/main.py", ORIGINAL.encode("utf-8")),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            bundle.addfile(member, io.BytesIO(content))
    return RepositoryArchive.capture(
        repository_full_name="fixture/contribution",
        base_commit_sha=base_sha,
        path=path,
        allowed_root=tmp_path,
    )


def _explore_result(
    archive: RepositoryArchive,
    policy: SandboxPolicy,
) -> ExploreResult:
    payload = {
        "version": "sandbox-explore-result-v1",
        "spec_id": "explore-spec-1",
        "spec_hash": "2" * 64,
        "execution_attempt_id": "attempt-1",
        "repository_full_name": archive.repository_full_name,
        "base_commit_sha": archive.base_commit_sha,
        "repository_archive_hash": archive.archive_hash,
        "runner_image_digest": IMAGE,
        "sandbox_policy_version": policy.version,
        "sandbox_policy_hash": policy.policy_hash,
        "snapshot_manifest_hash": "3" * 64,
        "inventory_hash": "4" * 64,
        "file_count": 2,
        "total_bytes": len(ORIGINAL.encode()) + len(b"[project]\nname='fixture'\n"),
        "sample_paths": ("pyproject.toml", "src/main.py"),
        "manifest_paths": ("pyproject.toml",),
    }
    return ExploreResult(**payload, result_hash=content_hash(payload))


def _change_set() -> ImplementationChangeSet:
    return ImplementationChangeSet(
        change_set_id="change-set-1",
        plan_version_id="plan-1",
        plan_content_hash=HASH,
        plan_record_hash=HASH,
        operations=(
            ChangeOperation(
                path="src/main.py",
                kind=ChangeOperationKind.WRITE,
                expected_prior_hash=hashlib.sha256(
                    ORIGINAL.encode("utf-8")
                ).hexdigest(),
                content=UPDATED,
                executable=False,
            ),
            ChangeOperation(
                path="tests/test_main.py",
                kind=ChangeOperationKind.WRITE,
                expected_prior_hash=None,
                content=NEW_TEST,
                executable=False,
            ),
        ),
    )


def _spec(
    archive: RepositoryArchive,
    policy: SandboxPolicy,
    explore: ExploreResult,
    change_set: ImplementationChangeSet,
) -> JobSpec:
    return JobSpec(
        spec_id="implement-spec-1",
        execution_attempt_id="attempt-1",
        correlation_id="implement-correlation-1",
        stage=SandboxStage.IMPLEMENT,
        repository_full_name=archive.repository_full_name,
        base_commit_sha=archive.base_commit_sha,
        task_id="task-1",
        task_record_hash=HASH,
        analysis_version_id="analysis-1",
        analysis_record_hash=HASH,
        analysis_output_hash=HASH,
        snapshot_id="snapshot-1",
        snapshot_inputs_hash=HASH,
        plan_version_id="plan-1",
        plan_content_hash=HASH,
        plan_record_hash=HASH,
        plan_approval_id="approval-1",
        approval_hash=HASH,
        approved_state_version_id="approved-state-1",
        approved_state_record_hash=HASH,
        provider_contract_hash=HASH,
        repository_archive_hash=archive.archive_hash,
        runner_image_digest=IMAGE,
        sandbox_policy_version=policy.version,
        sandbox_policy_hash=policy.policy_hash,
        input_artifact_hashes=(
            explore.result_hash,
            change_set.change_set_hash,
        ),
        allowed_change_paths=(
            "src/main.py",
            "tests/test_main.py",
        ),
        commands=(),
    )


class _FakeImplementRuntime:
    def __init__(self, *, wrong_paths: bool = False) -> None:
        self.wrong_paths = wrong_paths
        self.created = 0
        self.materialized = 0
        self.applied = 0
        self.destroyed: list[str] = []

    async def create_workspace(
        self,
        *,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> DisposableWorkspace:
        self.created += 1
        return DisposableWorkspace(
            workspace_id="workspace:fake-1",
            volume_name="contribos-workspace-" + "9" * 32,
            runner_image_digest=image_digest,
            sandbox_policy_hash=policy.policy_hash,
        )

    async def materialize(
        self,
        *,
        workspace: DisposableWorkspace,
        archive: RepositoryArchive,
        policy: SandboxPolicy,
    ) -> None:
        self.materialized += 1
        assert archive.archive_hash
        assert workspace.sandbox_policy_hash == policy.policy_hash

    async def apply(
        self,
        *,
        workspace: DisposableWorkspace,
        change_set: ImplementationChangeSet,
        allowed_change_paths: tuple[str, ...],
        policy: SandboxPolicy,
    ) -> ImplementationInspection:
        self.applied += 1
        return ImplementationInspection.from_wire(
            _inspection_wire(wrong_paths=self.wrong_paths)
        )

    async def destroy(self, workspace: DisposableWorkspace) -> None:
        self.destroyed.append(workspace.volume_name)


def test_implementation_is_bound_to_plan_explore_and_structured_changes(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    policy = SandboxPolicy()
    explore = _explore_result(archive, policy)
    change_set = _change_set()
    signer = JobSpecSigner(key_id="implement-key", signing_key=b"i" * 32)
    runtime = _FakeImplementRuntime()

    execution = asyncio.run(
        ImplementService(
            signer=signer,
            policy=policy,
            runtime=runtime,
        ).run(
            signer.sign(_spec(archive, policy, explore, change_set)),
            archive,
            explore,
            change_set,
        )
    )

    assert runtime.created == 1
    assert runtime.materialized == 1
    assert runtime.applied == 1
    assert runtime.destroyed == []
    assert execution.result.explore_result_hash == explore.result_hash
    assert execution.result.change_set_hash == change_set.change_set_hash
    assert execution.result.changed_paths == (
        "src/main.py",
        "tests/test_main.py",
    )
    assert len(execution.result.baseline_inventory) == 2
    assert len(execution.result.result_inventory) == 3
    assert "diff --git a/src/main.py b/src/main.py" in (
        execution.result.unified_diff
    )
    assert execution.result.diff_hash == hashlib.sha256(
        execution.result.unified_diff.encode("utf-8")
    ).hexdigest()
    bundle = ExecutionArtifactBundle.from_result(execution.result)
    assert [item.role for item in bundle.contents] == [
        "stage-result",
        "file-inventory",
        "unified-diff",
    ]
    assert bundle.contents[0].artifact_id == execution.result.result_hash
    assert bundle.contents[2].artifact_id == execution.result.diff_hash
    assert execution.result.result_hash == content_hash(
        execution.result.hash_payload()
    )
    asyncio.run(runtime.destroy(execution.workspace))


def test_implementation_rejects_unapproved_paths_and_destroys_failed_workspace(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    policy = SandboxPolicy()
    explore = _explore_result(archive, policy)
    change_set = _change_set()
    signer = JobSpecSigner(key_id="implement-key", signing_key=b"i" * 32)
    spec = _spec(archive, policy, explore, change_set)
    outside_spec = replace(
        spec,
        allowed_change_paths=("src/main.py",),
    )
    runtime = _FakeImplementRuntime()
    with pytest.raises(ImplementationError, match="approved plan"):
        asyncio.run(
            ImplementService(
                signer=signer,
                policy=policy,
                runtime=runtime,
            ).run(
                signer.sign(outside_spec),
                archive,
                explore,
                change_set,
            )
        )
    assert runtime.created == 0

    failed_runtime = _FakeImplementRuntime(wrong_paths=True)
    with pytest.raises(ImplementationError, match="do not match"):
        asyncio.run(
            ImplementService(
                signer=signer,
                policy=policy,
                runtime=failed_runtime,
            ).run(
                signer.sign(spec),
                archive,
                explore,
                change_set,
            )
        )
    assert failed_runtime.destroyed == [
        "contribos-workspace-" + "9" * 32
    ]


def test_implementation_rejects_wrong_plan_and_artifact_chain(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    policy = SandboxPolicy()
    explore = _explore_result(archive, policy)
    change_set = _change_set()
    signer = JobSpecSigner(key_id="implement-key", signing_key=b"i" * 32)
    spec = _spec(archive, policy, explore, change_set)
    wrong_plan = replace(change_set, plan_record_hash="8" * 64)
    with pytest.raises(ImplementationError, match="PlanVersion"):
        asyncio.run(
            ImplementService(
                signer=signer,
                policy=policy,
                runtime=_FakeImplementRuntime(),
            ).run(
                signer.sign(spec),
                archive,
                explore,
                wrong_plan,
            )
        )
    with pytest.raises(ImplementationError, match="artifact inputs"):
        asyncio.run(
            ImplementService(
                signer=signer,
                policy=policy,
                runtime=_FakeImplementRuntime(),
            ).run(
                signer.sign(
                    replace(
                        spec,
                        input_artifact_hashes=(
                            explore.result_hash,
                            "8" * 64,
                        ),
                    )
                ),
                archive,
                explore,
                change_set,
            )
        )


def test_change_set_rejects_traversal_duplicates_and_credentials() -> None:
    with pytest.raises(ValueError, match="change path"):
        ChangeOperation(
            path="../escape.py",
            kind=ChangeOperationKind.WRITE,
            expected_prior_hash=None,
            content="safe",
            executable=False,
        )


def test_implementation_inspection_rejects_inventory_and_diff_tampering() -> None:
    payload = _inspection_wire()
    with pytest.raises(ImplementationError, match="inventory evidence"):
        ImplementationInspection.from_wire(
            {**payload, "result_total_bytes": payload["result_total_bytes"] + 1}
        )
    with pytest.raises(ImplementationError, match="diff hash"):
        ImplementationInspection.from_wire(
            {**payload, "diff_hash": "f" * 64}
        )
    with pytest.raises(ValueError, match="Credential-like"):
        ChangeOperation(
            path="config.py",
            kind=ChangeOperationKind.WRITE,
            expected_prior_hash=None,
            content="api_key=supersecretvalue",
            executable=False,
        )
    operation = ChangeOperation(
        path="src/main.py",
        kind=ChangeOperationKind.DELETE,
        expected_prior_hash="1" * 64,
    )
    with pytest.raises(ValueError, match="operations"):
        ImplementationChangeSet(
            change_set_id="duplicate",
            plan_version_id="plan-1",
            plan_content_hash=HASH,
            plan_record_hash=HASH,
            operations=(operation, operation),
        )


def test_docker_implementation_mounts_only_one_writable_volume(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    policy = SandboxPolicy()
    workspace = DisposableWorkspace(
        workspace_id="workspace:fixture",
        volume_name="contribos-workspace-" + "9" * 32,
        runner_image_digest=IMAGE,
        sandbox_policy_hash=policy.policy_hash,
    )
    runtime = DockerImplementRuntime(
        docker_environment={"PATH": "/usr/bin", "HOME": "/tmp/worker"}
    )
    materialize = runtime.build_materialize_argv(
        workspace=workspace,
        archive=archive,
        policy=policy,
        container_name="contribos-implement-materialize-" + "1" * 32,
    )
    change_path = tmp_path / "change-set.json"
    change_path.write_text("{}\n", encoding="utf-8")
    apply = runtime.build_apply_argv(
        workspace=workspace,
        change_set_path=str(change_path),
        policy=policy,
        container_name="contribos-implement-apply-" + "2" * 32,
    )

    joined = " ".join(apply)
    assert (
        f"type=volume,src={workspace.volume_name},dst=/workspace" in joined
    )
    assert "dst=/input/change-set.json,readonly" in joined
    assert joined.count("type=volume") == 1
    for expected in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges=true",
        "--user 65532:65532",
        "--pull never",
    ):
        assert expected in joined
    assert "dst=/output" in " ".join(materialize)
    assert "/var/run/docker.sock" not in joined
    assert "GITHUB_TOKEN" not in joined
    assert "GIT_CONFIG_NOSYSTEM=1" in joined
    assert "core.hooksPath" in joined
    assert "protocol.allow" in joined

    with pytest.raises(ValueError, match="disallowed"):
        DockerImplementRuntime(
            docker_environment={
                "PATH": "/usr/bin",
                "SSH_AUTH_SOCK": "/tmp/forbidden",
            }
        )


@pytest.mark.skipif(
    os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
def test_real_plan_bound_implementation_in_disposable_volume(
    tmp_path: Path,
) -> None:
    image = os.environ.get("CONTRIBOS_SANDBOX_RUNNER_IMAGE", "")
    if not image.startswith("sha256:"):
        pytest.fail("CONTRIBOS_SANDBOX_RUNNER_IMAGE must be a local image digest")
    archive = _archive(tmp_path)
    policy = SandboxPolicy()
    explore = _explore_result(archive, policy)
    explore_payload = explore.hash_payload()
    explore_payload["runner_image_digest"] = image
    explore = ExploreResult(
        **explore_payload,
        result_hash=content_hash(explore_payload),
    )
    change_set = _change_set()
    spec = replace(
        _spec(archive, policy, explore, change_set),
        runner_image_digest=image,
    )
    signer = JobSpecSigner(key_id="real-implement", signing_key=b"r" * 32)
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
    runtime = DockerImplementRuntime(
        docker_environment=docker_environment
    )
    execution = asyncio.run(
        ImplementService(
            signer=signer,
            policy=policy,
            runtime=runtime,
        ).run(
            signer.sign(spec),
            archive,
            explore,
            change_set,
        )
    )
    try:
        assert execution.result.changed_paths == change_set.paths
        assert execution.result.baseline_inventory_hash != (
            execution.result.result_inventory_hash
        )
        assert execution.result.result_file_count == 3
        assert [
            item.path for item in execution.result.result_inventory
        ] == [
            "pyproject.toml",
            "src/main.py",
            "tests/test_main.py",
        ]
        assert execution.result.unified_diff.startswith(
            "diff --git a/src/main.py b/src/main.py\n"
        )
        assert execution.result.diff_hash == hashlib.sha256(
            execution.result.unified_diff.encode("utf-8")
        ).hexdigest()
    finally:
        asyncio.run(runtime.destroy(execution.workspace))
        asyncio.run(runtime.destroy(execution.workspace))


@pytest.mark.skipif(
    os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
@pytest.mark.sandbox_malicious
def test_real_fresh_container_verify_has_no_network_credentials_or_writes(
    tmp_path: Path,
) -> None:
    image = os.environ.get("CONTRIBOS_SANDBOX_RUNNER_IMAGE", "")
    if not image.startswith("sha256:"):
        pytest.fail("CONTRIBOS_SANDBOX_RUNNER_IMAGE must be a local image digest")
    archive = _archive(tmp_path)
    policy = SandboxPolicy()
    explore = _explore_result(archive, policy)
    explore_payload = explore.hash_payload()
    explore_payload["runner_image_digest"] = image
    explore = ExploreResult(
        **explore_payload,
        result_hash=content_hash(explore_payload),
    )
    change_set = _change_set()
    implement_spec = replace(
        _spec(archive, policy, explore, change_set),
        runner_image_digest=image,
    )
    signer = JobSpecSigner(key_id="real-verify", signing_key=b"r" * 32)
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
    implement_runtime = DockerImplementRuntime(
        docker_environment=docker_environment
    )
    implementation = asyncio.run(
        ImplementService(
            signer=signer,
            policy=policy,
            runtime=implement_runtime,
        ).run(
            signer.sign(implement_spec),
            archive,
            explore,
            change_set,
        )
    )
    try:
        host_only = tmp_path / "host-only-canary.txt"
        host_home = tmp_path / "host-home"
        host_ssh = host_home / ".ssh"
        host_ssh.mkdir(parents=True)
        host_only.write_text(
            "github_pat_hostfilecanary12345678\n",
            encoding="utf-8",
        )
        (host_ssh / "id_ed25519").write_text(
            "test-only-ssh-private-key-canary\n",
            encoding="utf-8",
        )
        denied_host_paths = (
            str(host_only),
            str(host_ssh / "id_ed25519"),
            str(host_ssh / "config"),
        )
        probe = "\n".join(
            (
                "import os",
                "import socket",
                "import stat",
                "import subprocess",
                "from pathlib import Path",
                f"assert Path('src/main.py').read_text() == {UPDATED!r}",
                "assert not any(name in os.environ for name in (",
                "    'GITHUB_TOKEN', 'GH_TOKEN', 'OPENAI_API_KEY',",
                "    'CODEX_API_KEY', 'SSH_AUTH_SOCK'))",
                "assert os.environ.get('HOME') == '/tmp'",
                f"denied_host_paths = {denied_host_paths!r}",
                "for denied_path in denied_host_paths:",
                "    try:",
                "        Path(denied_path).read_bytes()",
                "    except OSError:",
                "        pass",
                "    else:",
                "        raise AssertionError('host-only file is readable')",
                "for denied_path in (",
                "    '/tmp/.ssh/id_ed25519',",
                "    '/root/.ssh/id_ed25519',",
                "    '/root/.ssh/id_rsa',",
                "    '/home/sandbox/.ssh/id_ed25519',",
                "):",
                "    try:",
                "        Path(denied_path).read_bytes()",
                "    except OSError:",
                "        pass",
                "    else:",
                "        raise AssertionError('SSH material is readable')",
                "try:",
                "    Path('src/main.py').write_text('forbidden')",
                "except OSError:",
                "    pass",
                "else:",
                "    raise AssertionError('workspace is writable')",
                "connection = socket.socket()",
                "connection.settimeout(0.2)",
                "try:",
                "    connection.connect(('1.1.1.1', 53))",
                "except OSError:",
                "    pass",
                "else:",
                "    raise AssertionError('network is available')",
                "finally:",
                "    connection.close()",
                "print('safe verification log')",
                "print('github_' + 'pat_' + 'verifycanary12345678')",
                "git_root = Path('/tmp/git-safety')",
                "subprocess.run(['git', 'init', str(git_root)], check=True,",
                "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
                "Path('/tmp/.gitconfig').write_text(",
                "    '[alias]\\npwn = !touch /tmp/global-marker\\n')",
                "failed_alias = subprocess.run(['git', 'pwn'], cwd=git_root,",
                "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
                "assert failed_alias.returncode != 0",
                "assert not Path('/tmp/global-marker').exists()",
                "assert subprocess.check_output([",
                "    'git', 'config', '--get', 'core.hooksPath'",
                "], cwd=git_root, text=True).strip() == '/dev/null'",
                "hook = git_root / '.git/hooks/pre-commit'",
                "hook.parent.mkdir(exist_ok=True)",
                "hook.write_text('#!/bin/sh\\ntouch /tmp/hook-marker\\n')",
                "hook.chmod(hook.stat().st_mode | stat.S_IXUSR)",
                "(git_root / 'tracked.txt').write_text('safe\\n')",
                "subprocess.run(['git', 'config', 'user.name', 'Sandbox'],",
                "    cwd=git_root, check=True)",
                "subprocess.run([",
                "    'git', 'config', 'user.email', 'sandbox@example.invalid'",
                "], cwd=git_root, check=True)",
                "subprocess.run(['git', 'add', 'tracked.txt'],",
                "    cwd=git_root, check=True)",
                "subprocess.run(['git', 'commit', '-m', 'safe'],",
                "    cwd=git_root, check=True, stdout=subprocess.DEVNULL,",
                "    stderr=subprocess.DEVNULL)",
                "assert not Path('/tmp/hook-marker').exists()",
                "clone = subprocess.run([",
                "    'git', 'clone', f'file://{git_root}', '/tmp/clone'",
                "], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
                "assert clone.returncode != 0",
                "helper = Path('/tmp/credential-helper')",
                "helper.write_text(",
                "    '#!/bin/sh\\ntouch /tmp/credential-marker\\n')",
                "helper.chmod(helper.stat().st_mode | stat.S_IXUSR)",
                "subprocess.run([",
                "    'git', 'config', 'credential.helper', str(helper)",
                "], cwd=git_root, check=True)",
                "credential = subprocess.run([",
                "    'git', 'credential', 'fill'",
                "], cwd=git_root, input='protocol=https\\nhost=example.com\\n\\n',",
                "    text=True, stdout=subprocess.DEVNULL,",
                "    stderr=subprocess.DEVNULL)",
                "assert credential.returncode != 0",
                "assert not Path('/tmp/credential-marker').exists()",
                "lfs = Path('/tmp/lfs-filter')",
                "lfs.write_text('#!/bin/sh\\ntouch /tmp/lfs-marker\\ncat\\n')",
                "lfs.chmod(lfs.stat().st_mode | stat.S_IXUSR)",
                "subprocess.run([",
                "    'git', 'config', 'filter.lfs.process', str(lfs)",
                "], cwd=git_root, check=True)",
                "(git_root / '.gitattributes').write_text('*.bin filter=lfs\\n')",
                "(git_root / 'payload.bin').write_bytes(b'safe')",
                "subprocess.run(['git', 'add', '.gitattributes', 'payload.bin'],",
                "    cwd=git_root, check=True)",
                "assert not Path('/tmp/lfs-marker').exists()",
            )
        )
        probe_lines = probe.splitlines()
        git_start = probe_lines.index(
            "git_root = Path('/tmp/git-safety')"
        )
        hook_start = probe_lines.index(
            "hook = git_root / '.git/hooks/pre-commit'"
        )
        protocol_start = probe_lines.index(
            "clone = subprocess.run(["
        )
        filters_start = probe_lines.index(
            "helper = Path('/tmp/credential-helper')"
        )
        isolation_probe = "\n".join(probe_lines[:git_start])
        runtime_boundary_probe = "\n".join(
            (
                "import os",
                "import socket",
                "from pathlib import Path",
                "assert not os.environ.get('DOCKER_HOST')",
                "for socket_path in (",
                "    '/var/run/docker.sock', '/run/docker.sock',",
                "):",
                "    assert not Path(socket_path).exists()",
                "    client = socket.socket(socket.AF_UNIX)",
                "    client.settimeout(0.2)",
                "    try:",
                "        client.connect(socket_path)",
                "    except OSError:",
                "        pass",
                "    else:",
                "        raise AssertionError('Docker socket is reachable')",
                "    finally:",
                "        client.close()",
                "for address in ('169.254.169.254', '100.100.100.200'):",
                "    client = socket.socket(socket.AF_INET)",
                "    client.settimeout(0.2)",
                "    try:",
                "        client.connect((address, 80))",
                "    except OSError:",
                "        pass",
                "    else:",
                "        raise AssertionError('metadata IP is reachable')",
                "    finally:",
                "        client.close()",
                "print('runtime socket and metadata boundaries denied')",
            )
        )
        network_exfiltration_probe = "\n".join(
            (
                "import socket",
                "for address, port in (",
                "    ('1.1.1.1', 443), ('93.184.216.34', 80),",
                "):",
                "    client = socket.socket(socket.AF_INET)",
                "    client.settimeout(0.2)",
                "    try:",
                "        client.connect((address, port))",
                "    except OSError:",
                "        pass",
                "    else:",
                "        raise AssertionError('public TCP is reachable')",
                "    finally:",
                "        client.close()",
                "for domain in (",
                "    'example.com',",
                "    'workspace-hash-canary.example.com',",
                "    'metadata.google.internal',",
                "):",
                "    try:",
                "        socket.getaddrinfo(domain, 443)",
                "    except OSError:",
                "        pass",
                "    else:",
                "        raise AssertionError('DNS resolution is available')",
                "dns = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)",
                "dns.settimeout(0.2)",
                "try:",
                "    dns.sendto(b'workspace-hash-canary', ('8.8.8.8', 53))",
                "except OSError:",
                "    pass",
                "else:",
                "    raise AssertionError('DNS datagram egress is available')",
                "finally:",
                "    dns.close()",
                "print('public network and DNS exfiltration denied')",
            )
        )
        privilege_probe = "\n".join(
            (
                "import os",
                "import socket",
                "import stat",
                "import subprocess",
                "from pathlib import Path",
                "assert os.getuid() == 65532",
                "assert os.getgid() == 65532",
                "status = {}",
                "for line in Path('/proc/self/status').read_text().splitlines():",
                "    if ':' in line:",
                "        key, value = line.split(':', 1)",
                "        status[key] = value.strip()",
                "for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):",
                "    assert int(status[key], 16) == 0",
                "assert status['NoNewPrivs'] == '1'",
                "for operation in (",
                "    lambda: os.setuid(0),",
                "    lambda: os.setgid(0),",
                "    lambda: os.setgroups([]),",
                "    lambda: os.nice(-20),",
                "):",
                "    try:",
                "        operation()",
                "    except OSError:",
                "        pass",
                "    else:",
                "        raise AssertionError('privilege escalation succeeded')",
                "try:",
                "    raw = socket.socket(",
                "        socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP",
                "    )",
                "except OSError:",
                "    pass",
                "else:",
                "    raw.close()",
                "    raise AssertionError('raw socket capability is available')",
                "try:",
                "    os.mknod('/tmp/forbidden-device',",
                "        stat.S_IFCHR | 0o600, os.makedev(1, 3))",
                "except OSError:",
                "    pass",
                "else:",
                "    raise AssertionError('device creation is available')",
                "assert not any(",
                "    stat.S_ISBLK(path.stat().st_mode)",
                "    for path in Path('/dev').iterdir()",
                ")",
                "for path in ('/dev/mem', '/dev/kmsg', '/dev/sda'):",
                "    try:",
                "        Path(path).read_bytes()",
                "    except OSError:",
                "        pass",
                "    else:",
                "        raise AssertionError('host device is readable')",
                "unshare = subprocess.run(",
                "    ['unshare', '--user', '--map-root-user', 'true'],",
                "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,",
                ")",
                "assert unshare.returncode != 0",
                "print('privilege device and capability abuse denied')",
            )
        )
        git_submodule_probe = "\n".join(
            (
                "import subprocess",
                "from pathlib import Path",
                "root = Path('/tmp/git-submodule')",
                "subprocess.run(['git', 'init', str(root)], check=True,",
                "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
                "subprocess.run(['git', 'config', 'user.name', 'Sandbox'],",
                "    cwd=root, check=True)",
                "subprocess.run([",
                "    'git', 'config', 'user.email', 'sandbox@example.invalid'",
                "], cwd=root, check=True)",
                "(root / 'tracked.txt').write_text('safe\\n')",
                "subprocess.run(['git', 'add', 'tracked.txt'],",
                "    cwd=root, check=True)",
                "subprocess.run(['git', 'commit', '-m', 'base'], cwd=root,",
                "    check=True, stdout=subprocess.DEVNULL,",
                "    stderr=subprocess.DEVNULL)",
                "head = subprocess.check_output([",
                "    'git', 'rev-parse', 'HEAD'",
                "], cwd=root, text=True).strip()",
                "helper = Path('/tmp/submodule-helper')",
                "helper.write_text(",
                "    '#!/bin/sh\\ntouch /tmp/submodule-marker\\nexit 1\\n')",
                "helper.chmod(0o700)",
                "(root / '.gitmodules').write_text(",
                "    '[submodule \"evil\"]\\n'",
                "    'path = deps/evil\\nurl = ext::/tmp/submodule-helper\\n')",
                "subprocess.run([",
                "    'git', 'update-index', '--add', '--cacheinfo',",
                "    f'160000,{head},deps/evil'",
                "], cwd=root, check=True)",
                "subprocess.run(['git', 'add', '.gitmodules'],",
                "    cwd=root, check=True)",
                "attempt = subprocess.run([",
                "    'git', 'submodule', 'update', '--init', '--recursive'",
                "], cwd=root, stdout=subprocess.DEVNULL,",
                "    stderr=subprocess.DEVNULL)",
                "assert attempt.returncode != 0",
                "assert not Path('/tmp/submodule-marker').exists()",
                "print('submodule external helper denied')",
            )
        )
        git_config_probe = "\n".join(
            (
                "import subprocess",
                "from pathlib import Path",
                *probe_lines[git_start:hook_start],
            )
        )
        git_hook_probe = "\n".join(
            (
                "import stat",
                "import subprocess",
                "from pathlib import Path",
                "git_root = Path('/tmp/git-safety')",
                *probe_lines[hook_start:protocol_start],
            )
        )
        git_protocol_probe = "\n".join(
            (
                "import subprocess",
                "from pathlib import Path",
                "git_root = Path('/tmp/git-safety')",
                *probe_lines[protocol_start:filters_start],
            )
        )
        git_filters_probe = "\n".join(
            (
                "import stat",
                "import subprocess",
                "from pathlib import Path",
                "git_root = Path('/tmp/git-safety')",
                *probe_lines[filters_start:],
            )
        )
        assert max(
            map(
                len,
                (
                    isolation_probe,
                    runtime_boundary_probe,
                    network_exfiltration_probe,
                    privilege_probe,
                    git_submodule_probe,
                    git_config_probe,
                    git_hook_probe,
                    git_protocol_probe,
                    git_filters_probe,
                ),
            )
        ) <= 2_000
        commands = (
            SandboxCommand(
                command_id="verify-isolation",
                argv=("python3", "-I", "-c", isolation_probe),
            ),
            SandboxCommand(
                command_id="verify-runtime-sockets",
                argv=("python3", "-I", "-c", runtime_boundary_probe),
            ),
            SandboxCommand(
                command_id="verify-network-exfiltration",
                argv=(
                    "python3",
                    "-I",
                    "-c",
                    network_exfiltration_probe,
                ),
            ),
            SandboxCommand(
                command_id="verify-privilege-boundaries",
                argv=("python3", "-I", "-c", privilege_probe),
            ),
            SandboxCommand(
                command_id="verify-git-config",
                argv=("python3", "-I", "-c", git_config_probe),
            ),
            SandboxCommand(
                command_id="verify-git-hook",
                argv=("python3", "-I", "-c", git_hook_probe),
            ),
            SandboxCommand(
                command_id="verify-git-protocol",
                argv=("python3", "-I", "-c", git_protocol_probe),
            ),
            SandboxCommand(
                command_id="verify-git-submodule",
                argv=("python3", "-I", "-c", git_submodule_probe),
            ),
            SandboxCommand(
                command_id="verify-git-filters",
                argv=("python3", "-I", "-c", git_filters_probe),
            ),
        )
        verify_spec = replace(
            implement_spec,
            spec_id="verify-spec-real-1",
            stage=SandboxStage.VERIFY,
            input_artifact_hashes=(implementation.result.result_hash,),
            allowed_change_paths=(),
            commands=commands,
        )
        verification = asyncio.run(
            VerifyService(
                signer=signer,
                policy=policy,
                runtime=DockerVerifyRuntime(
                    docker_environment=docker_environment
                ),
            ).run(
                signer.sign(verify_spec),
                implementation.result,
                implementation.workspace,
            )
        )
        assert [
            item.status for item in verification.command_evidence
        ] == [
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
        ]
        assert verification.before_inventory_hash == (
            implementation.result.result_inventory_hash
        )
        assert verification.after_inventory_hash == (
            implementation.result.result_inventory_hash
        )
        assert all(
            item.status is VerifyCommandStatus.PASSED
            for item in verification.command_evidence
        )
        first_evidence = verification.command_evidence[0]
        boundary_evidence = verification.command_evidence[1]
        network_evidence = verification.command_evidence[2]
        privilege_evidence = verification.command_evidence[3]
        submodule_evidence = next(
            item
            for item in verification.command_evidence
            if item.command_id == "verify-git-submodule"
        )
        assert "safe verification log" in first_evidence.stdout_log
        assert "[REDACTED]" in first_evidence.stdout_log
        assert (
            "runtime socket and metadata boundaries denied"
            in boundary_evidence.stdout_log
        )
        assert (
            "public network and DNS exfiltration denied"
            in network_evidence.stdout_log
        )
        assert (
            "privilege device and capability abuse denied"
            in privilege_evidence.stdout_log
        )
        assert "submodule external helper denied" in (
            submodule_evidence.stdout_log
        )
        assert "github_pat_verifycanary" not in str(verification.to_wire())
        assert "hostfilecanary" not in str(verification.to_wire())
        assert "ssh-private-key-canary" not in str(
            verification.to_wire()
        )
        assert all(
            item.started_at is not None
            and item.completed_at is not None
            and item.resource_usage.max_rss_bytes >= 0
            for item in verification.command_evidence
        )
        assert [
            item.command_id for item in verification.test_results
        ] == [
            item.command_id for item in commands
        ]
        assert all(
            item.outcome is VerifyCommandStatus.PASSED
            for item in verification.test_results
        )
        assert verification.test_results_hash == content_hash(
            [item.to_wire() for item in verification.test_results]
        )
    finally:
        asyncio.run(implement_runtime.destroy(implementation.workspace))
        asyncio.run(implement_runtime.destroy(implementation.workspace))


@pytest.mark.skipif(
    os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1",
    reason="set CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1 for real Docker acceptance",
)
@pytest.mark.sandbox_malicious
def test_real_verify_resource_exhaustion_is_bounded_and_structured(
    tmp_path: Path,
) -> None:
    image = os.environ.get("CONTRIBOS_SANDBOX_RUNNER_IMAGE", "")
    if not image.startswith("sha256:"):
        pytest.fail("CONTRIBOS_SANDBOX_RUNNER_IMAGE must be a local image digest")
    policy = SandboxPolicy(
        cpu_limit=0.5,
        memory_bytes=96 * 1024 * 1024,
        pids_limit=32,
        timeout_seconds=30,
        disk_bytes=64 * 1024 * 1024,
        max_log_bytes=4_096,
    )
    archive = _archive(tmp_path)
    explore = _explore_result(archive, policy)
    explore_payload = explore.hash_payload()
    explore_payload["runner_image_digest"] = image
    explore = ExploreResult(
        **explore_payload,
        result_hash=content_hash(explore_payload),
    )
    change_set = _change_set()
    implement_spec = replace(
        _spec(archive, policy, explore, change_set),
        runner_image_digest=image,
    )
    signer = JobSpecSigner(
        key_id="real-resource-limits",
        signing_key=b"x" * 32,
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
    implement_runtime = DockerImplementRuntime(
        docker_environment=docker_environment
    )
    implementation = asyncio.run(
        ImplementService(
            signer=signer,
            policy=policy,
            runtime=implement_runtime,
        ).run(
            signer.sign(implement_spec),
            archive,
            explore,
            change_set,
        )
    )
    try:
        pid_probe = "\n".join(
            (
                "import subprocess",
                "children = []",
                "limited = False",
                "try:",
                "    for _ in range(64):",
                "        try:",
                "            children.append(subprocess.Popen(['sleep', '5']))",
                "        except OSError:",
                "            limited = True",
                "            break",
                "finally:",
                "    for child in children:",
                "        child.terminate()",
                "    for child in children:",
                "        child.wait()",
                "assert limited",
                "print('PID exhaustion bounded')",
            )
        )
        resource_probe = "\n".join(
            (
                "import os",
                "import subprocess",
                "from pathlib import Path",
                "cgroup = Path('/sys/fs/cgroup')",
                "quota, period = map(int,",
                "    (cgroup / 'cpu.max').read_text().split())",
                "assert quota / period <= 0.5",
                "assert int((cgroup / 'memory.max').read_text()) "
                "<= 96 * 1024 * 1024",
                "assert int((cgroup / 'pids.max').read_text()) <= 32",
                "burn = \"import time\\nend=time.monotonic()+0.4\\n\"",
                "burn += \"while time.monotonic()<end: pass\\n\"",
                "workers = [subprocess.Popen([",
                "    'python3', '-I', '-c', burn",
                "]) for _ in range(4)]",
                "assert all(worker.wait() == 0 for worker in workers)",
                "memory = subprocess.run([",
                "    'python3', '-I', '-c',",
                "    'value=bytearray(160*1024*1024); print(len(value))',",
                "], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
                "assert memory.returncode != 0",
                "target = Path('/tmp/disk-exhaustion')",
                "limited = False",
                "try:",
                "    with target.open('wb', buffering=0) as handle:",
                "        for _ in range(64):",
                "            try:",
                "                handle.write(b'x' * 1024 * 1024)",
                "            except OSError:",
                "                limited = True",
                "                break",
                "finally:",
                "    target.unlink(missing_ok=True)",
                "assert limited",
                "print('CPU memory and disk exhaustion bounded')",
            )
        )
        log_probe = "import sys; sys.stdout.write('x' * 8192)"
        commands = (
            SandboxCommand(
                command_id="resource-pid-limit",
                argv=("python3", "-I", "-c", pid_probe),
            ),
            SandboxCommand(
                command_id="resource-cpu-memory-disk",
                argv=("python3", "-I", "-c", resource_probe),
            ),
            SandboxCommand(
                command_id="resource-log-limit",
                argv=("python3", "-I", "-c", log_probe),
            ),
            SandboxCommand(
                command_id="resource-not-run-after-limit",
                argv=("python3", "-I", "-c", "raise SystemExit(99)"),
            ),
        )
        verify_spec = replace(
            implement_spec,
            spec_id="verify-resource-limits-real",
            stage=SandboxStage.VERIFY,
            input_artifact_hashes=(implementation.result.result_hash,),
            allowed_change_paths=(),
            commands=commands,
        )
        verification = asyncio.run(
            VerifyService(
                signer=signer,
                policy=policy,
                runtime=DockerVerifyRuntime(
                    docker_environment=docker_environment
                ),
            ).run(
                signer.sign(verify_spec),
                implementation.result,
                implementation.workspace,
            )
        )
        assert [
            item.status for item in verification.command_evidence
        ] == [
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.PASSED,
            VerifyCommandStatus.OUTPUT_LIMIT,
            VerifyCommandStatus.NOT_RUN,
        ], [
            (
                item.status,
                item.exit_code,
                item.stdout_log,
                item.stderr_log,
            )
            for item in verification.command_evidence
        ]
        assert verification.succeeded is False
        assert "PID exhaustion bounded" in (
            verification.command_evidence[0].stdout_log
        )
        assert "CPU memory and disk exhaustion bounded" in (
            verification.command_evidence[1].stdout_log
        )
        exhausted = verification.command_evidence[2]
        assert exhausted.raw_logs_omitted is True
        assert exhausted.stdout_log == "[OMITTED: output limit exceeded]"
        assert exhausted.stdout_bytes == 8_192
        assert implementation.workspace.inventory_hash == (
            verification.after_inventory_hash
        )
    finally:
        asyncio.run(implement_runtime.destroy(implementation.workspace))
        asyncio.run(implement_runtime.destroy(implementation.workspace))
