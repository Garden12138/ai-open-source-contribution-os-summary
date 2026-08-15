from __future__ import annotations

import asyncio
import base64
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from app.artifacts import ExecutionArtifactBundle
from app.provenance import content_hash
from app.sandbox_worker.dependencies import (
    DependencyEcosystem,
    DependencyExecution,
    DependencyPreparationResult,
    DependencyWorkspace,
)
from app.sandbox_worker.implementation import (
    DisposableWorkspace,
    ImplementResult,
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
    NormalizedTestResult,
    VerificationError,
    VerifyCommandEvidence,
    VerifyCommandStatus,
    VerifyInspection,
    VerifyResourceUsage,
    VerifyService,
)


HASH = "1" * 64
IMAGE = "sha256:" + "5" * 64
STARTED_AT = "2026-07-30T03:00:00.000000Z"
COMPLETED_AT = "2026-07-30T03:00:00.010000Z"
OLD_SOURCE = b"print('before')\n"
NEW_SOURCE = b"print('after')\n"
BASELINE_INVENTORY = [
    {
        "path": "src/main.py",
        "type": "file",
        "size": len(OLD_SOURCE),
        "sha256": hashlib.sha256(OLD_SOURCE).hexdigest(),
        "executable": False,
    }
]
RESULT_INVENTORY = [
    {
        "path": "src/main.py",
        "type": "file",
        "size": len(NEW_SOURCE),
        "sha256": hashlib.sha256(NEW_SOURCE).hexdigest(),
        "executable": False,
    }
]
INVENTORY = content_hash(RESULT_INVENTORY)
UNIFIED_DIFF = (
    "diff --git a/src/main.py b/src/main.py\n"
    "--- a/src/main.py\n"
    "+++ b/src/main.py\n"
    "@@ -1 +1 @@\n"
    "-print('before')\n"
    "+print('after')\n"
)


def _resource_usage() -> VerifyResourceUsage:
    return VerifyResourceUsage(
        user_cpu_ms=2,
        system_cpu_ms=1,
        max_rss_bytes=1024,
        minor_page_faults=3,
        major_page_faults=0,
        voluntary_context_switches=1,
        involuntary_context_switches=0,
    )


def _implement_result(policy: SandboxPolicy) -> ImplementResult:
    payload = {
        "version": "sandbox-implement-result-v2",
        "spec_id": "implement-spec-1",
        "spec_hash": "2" * 64,
        "execution_attempt_id": "attempt-1",
        "explore_result_hash": "3" * 64,
        "change_set_id": "change-set-1",
        "change_set_hash": "4" * 64,
        "repository_full_name": "fixture/contribution",
        "base_commit_sha": "a" * 40,
        "repository_archive_hash": "5" * 64,
        "plan_version_id": "plan-1",
        "plan_content_hash": HASH,
        "plan_record_hash": HASH,
        "runner_image_digest": IMAGE,
        "sandbox_policy_version": policy.version,
        "sandbox_policy_hash": policy.policy_hash,
        "baseline_inventory_hash": content_hash(BASELINE_INVENTORY),
        "result_inventory_hash": INVENTORY,
        "baseline_file_count": 1,
        "result_file_count": 1,
        "baseline_total_bytes": len(OLD_SOURCE),
        "result_total_bytes": len(NEW_SOURCE),
        "changed_paths": ("src/main.py",),
        "baseline_inventory": BASELINE_INVENTORY,
        "result_inventory": RESULT_INVENTORY,
        "unified_diff": UNIFIED_DIFF,
        "diff_hash": hashlib.sha256(
            UNIFIED_DIFF.encode("utf-8")
        ).hexdigest(),
    }
    return ImplementResult(**payload, result_hash=content_hash(payload))


def _command() -> SandboxCommand:
    return SandboxCommand(
        command_id="focused-tests",
        argv=("python3", "-I", "-c", "assert 2 + 2 == 4"),
        working_directory=".",
    )


def _spec(
    implement: ImplementResult,
    policy: SandboxPolicy,
    *,
    command: SandboxCommand | None = None,
    dependency_result_hash: str | None = None,
) -> JobSpec:
    commands = (command or _command(),)
    artifacts = (implement.result_hash,)
    if dependency_result_hash is not None:
        artifacts = (*artifacts, dependency_result_hash)
    return JobSpec(
        spec_id="verify-spec-1",
        execution_attempt_id=implement.execution_attempt_id,
        correlation_id="verify-correlation-1",
        stage=SandboxStage.VERIFY,
        repository_full_name=implement.repository_full_name,
        base_commit_sha=implement.base_commit_sha,
        task_id="task-1",
        task_record_hash=HASH,
        analysis_version_id="analysis-1",
        analysis_record_hash=HASH,
        analysis_output_hash=HASH,
        snapshot_id="snapshot-1",
        snapshot_inputs_hash=HASH,
        plan_version_id=implement.plan_version_id,
        plan_content_hash=implement.plan_content_hash,
        plan_record_hash=implement.plan_record_hash,
        plan_approval_id="approval-1",
        approval_hash=HASH,
        approved_state_version_id="approved-state-1",
        approved_state_record_hash=HASH,
        provider_contract_hash=HASH,
        repository_archive_hash=implement.repository_archive_hash,
        runner_image_digest=implement.runner_image_digest,
        sandbox_policy_version=policy.version,
        sandbox_policy_hash=policy.policy_hash,
        input_artifact_hashes=artifacts,
        allowed_change_paths=(),
        commands=commands,
    )


def _workspace(
    implement: ImplementResult,
    policy: SandboxPolicy,
) -> DisposableWorkspace:
    return DisposableWorkspace(
        workspace_id="workspace:verify-1",
        volume_name="contribos-workspace-" + "9" * 32,
        runner_image_digest=implement.runner_image_digest,
        sandbox_policy_hash=policy.policy_hash,
        inventory_hash=implement.result_inventory_hash,
    )


class _FakeVerifyRuntime:
    def __init__(
        self,
        *,
        status: VerifyCommandStatus = VerifyCommandStatus.PASSED,
        inventory_hash: str = INVENTORY,
    ) -> None:
        self.status = status
        self.inventory_hash = inventory_hash
        self.calls = 0
        self.dependency_workspace: DependencyWorkspace | None = None

    async def run(
        self,
        *,
        workspace: DisposableWorkspace,
        commands: tuple[SandboxCommand, ...],
        policy: SandboxPolicy,
        dependency_workspace: DependencyWorkspace | None = None,
    ) -> VerifyInspection:
        self.calls += 1
        self.dependency_workspace = dependency_workspace
        command = commands[0]
        exit_code = 0 if self.status is VerifyCommandStatus.PASSED else 1
        return VerifyInspection(
            before_inventory_hash=self.inventory_hash,
            after_inventory_hash=self.inventory_hash,
            command_evidence=(
                VerifyCommandEvidence(
                    command_id=command.command_id,
                    command_hash=content_hash(command.to_wire()),
                    status=self.status,
                    exit_code=exit_code,
                    started_at=STARTED_AT,
                    completed_at=COMPLETED_AT,
                    resource_usage=_resource_usage(),
                    stdout_hash="a" * 64,
                    stdout_bytes=5,
                    stdout_log="safe\n",
                    stderr_hash="b" * 64,
                    stderr_bytes=0,
                    stderr_log="",
                    logs_truncated=False,
                    raw_logs_omitted=False,
                    duration_ms=10,
                ),
            ),
        )


def _dependency_execution(
    implement: ImplementResult,
    policy: SandboxPolicy,
) -> DependencyExecution:
    plan_hash = "d" * 64
    inventory_hash = "e" * 64
    payload = {
        "version": "dependency-preparation-result-v1",
        "execution_attempt_id": implement.execution_attempt_id,
        "implement_result_hash": implement.result_hash,
        "plan_hash": plan_hash,
        "proxy_policy_hash": "f" * 64,
        "runner_image_digest": implement.runner_image_digest,
        "sandbox_policy_hash": policy.policy_hash,
        "ecosystem": "python",
        "dependency_inventory_hash": inventory_hash,
        "file_count": 3,
        "total_bytes": 1024,
    }
    result = DependencyPreparationResult(
        execution_attempt_id=implement.execution_attempt_id,
        implement_result_hash=implement.result_hash,
        plan_hash=plan_hash,
        proxy_policy_hash="f" * 64,
        runner_image_digest=implement.runner_image_digest,
        sandbox_policy_hash=policy.policy_hash,
        ecosystem=DependencyEcosystem.PYTHON,
        dependency_inventory_hash=inventory_hash,
        file_count=3,
        total_bytes=1024,
        result_hash=content_hash(payload),
    )
    return DependencyExecution(
        result=result,
        workspace=DependencyWorkspace(
            workspace_id="dependencies:verify-1",
            volume_name="contribos-dependencies-" + "8" * 32,
            runner_image_digest=implement.runner_image_digest,
            sandbox_policy_hash=policy.policy_hash,
            plan_hash=plan_hash,
            inventory_hash=inventory_hash,
        ),
    )


def test_verify_binds_implement_workspace_commands_and_result() -> None:
    policy = SandboxPolicy()
    implement = _implement_result(policy)
    workspace = _workspace(implement, policy)
    signer = JobSpecSigner(key_id="verify-key", signing_key=b"v" * 32)
    runtime = _FakeVerifyRuntime()

    result = asyncio.run(
        VerifyService(
            signer=signer,
            policy=policy,
            runtime=runtime,
        ).run(
            signer.sign(_spec(implement, policy)),
            implement,
            workspace,
        )
    )

    assert runtime.calls == 1
    assert result.succeeded is True
    assert result.implement_result_hash == implement.result_hash
    assert result.workspace_inventory_hash == implement.result_inventory_hash
    assert result.before_inventory_hash == result.after_inventory_hash
    assert result.command_evidence[0].status is VerifyCommandStatus.PASSED
    assert result.commands == (_command(),)
    assert result.command_evidence[0].started_at == STARTED_AT
    assert result.command_evidence[0].resource_usage.max_rss_bytes == 1024
    assert result.command_evidence[0].stdout_log == "safe\n"
    assert result.test_results == (
        NormalizedTestResult.from_evidence(result.command_evidence[0]),
    )
    assert result.test_results[0].outcome is VerifyCommandStatus.PASSED
    assert result.test_results_hash == content_hash(
        [item.to_wire() for item in result.test_results]
    )
    bundle = ExecutionArtifactBundle.from_result(result)
    assert [item.role for item in bundle.contents] == [
        "stage-result",
        "normalized-test-results",
    ]
    assert bundle.contents[0].artifact_id == result.result_hash
    assert bundle.contents[1].artifact_id == result.test_results_hash
    assert result.result_hash == content_hash(result.hash_payload())

    with pytest.raises(
        VerificationError,
        match="normalized test results hash",
    ):
        replace(result, test_results_hash="f" * 64)


def test_verify_binds_optional_read_only_dependency_artifact() -> None:
    policy = SandboxPolicy()
    implement = _implement_result(policy)
    dependency = _dependency_execution(implement, policy)
    signer = JobSpecSigner(key_id="verify-key", signing_key=b"v" * 32)
    runtime = _FakeVerifyRuntime()

    result = asyncio.run(
        VerifyService(
            signer=signer,
            policy=policy,
            runtime=runtime,
        ).run(
            signer.sign(
                _spec(
                    implement,
                    policy,
                    dependency_result_hash=dependency.result.result_hash,
                )
            ),
            implement,
            _workspace(implement, policy),
            dependency,
        )
    )

    assert runtime.dependency_workspace == dependency.workspace
    assert result.dependency_result_hash == dependency.result.result_hash
    assert (
        result.dependency_inventory_hash
        == dependency.result.dependency_inventory_hash
    )
    assert result.result_hash == content_hash(result.hash_payload())

    stale_workspace = replace(
        dependency.workspace,
        inventory_hash="0" * 64,
    )
    with pytest.raises(VerificationError, match="dependency provenance"):
        asyncio.run(
            VerifyService(
                signer=signer,
                policy=policy,
                runtime=_FakeVerifyRuntime(),
            ).run(
                signer.sign(
                    _spec(
                        implement,
                        policy,
                        dependency_result_hash=dependency.result.result_hash,
                    )
                ),
                implement,
                _workspace(implement, policy),
                replace(dependency, workspace=stale_workspace),
            )
        )


def test_verify_records_test_failure_without_treating_it_as_integrity_error() -> None:
    policy = SandboxPolicy()
    implement = _implement_result(policy)
    signer = JobSpecSigner(key_id="verify-key", signing_key=b"v" * 32)

    result = asyncio.run(
        VerifyService(
            signer=signer,
            policy=policy,
            runtime=_FakeVerifyRuntime(
                status=VerifyCommandStatus.FAILED
            ),
        ).run(
            signer.sign(_spec(implement, policy)),
            implement,
            _workspace(implement, policy),
        )
    )

    assert result.succeeded is False
    assert result.command_evidence[0].exit_code == 1


def test_verify_rejects_stale_artifact_workspace_and_inventory() -> None:
    policy = SandboxPolicy()
    implement = _implement_result(policy)
    signer = JobSpecSigner(key_id="verify-key", signing_key=b"v" * 32)
    spec = _spec(implement, policy)

    stale_spec = replace(
        spec,
        input_artifact_hashes=("8" * 64,),
    )
    with pytest.raises(VerificationError, match="artifact input"):
        asyncio.run(
            VerifyService(
                signer=signer,
                policy=policy,
                runtime=_FakeVerifyRuntime(),
            ).run(
                signer.sign(stale_spec),
                implement,
                _workspace(implement, policy),
            )
        )

    stale_workspace = replace(
        _workspace(implement, policy),
        inventory_hash="8" * 64,
    )
    with pytest.raises(VerificationError, match="Implement provenance"):
        asyncio.run(
            VerifyService(
                signer=signer,
                policy=policy,
                runtime=_FakeVerifyRuntime(),
            ).run(
                signer.sign(spec),
                implement,
                stale_workspace,
            )
        )

    with pytest.raises(VerificationError, match="inventory changed"):
        asyncio.run(
            VerifyService(
                signer=signer,
                policy=policy,
                runtime=_FakeVerifyRuntime(inventory_hash="8" * 64),
            ).run(
                signer.sign(spec),
                implement,
                _workspace(implement, policy),
            )
        )


def test_verify_evidence_rejects_inconsistent_status() -> None:
    empty_hash = hashlib.sha256(b"").hexdigest()
    with pytest.raises(VerificationError, match="do not match"):
        VerifyCommandEvidence.from_wire(
            {
                "command_id": "tests",
                "command_hash": HASH,
                "status": "passed",
                "exit_code": 1,
                "started_at": STARTED_AT,
                "completed_at": COMPLETED_AT,
                "resource_usage": _resource_usage().to_wire(),
                "stdout_hash": empty_hash,
                "stdout_bytes": 0,
                "stdout_base64": "",
                "stderr_hash": empty_hash,
                "stderr_bytes": 0,
                "stderr_base64": "",
                "raw_logs_omitted": False,
                "duration_ms": 1,
            }
        )


def test_verify_evidence_redacts_raw_logs_and_rejects_capture_tampering() -> None:
    raw = b"safe\ngithub_pat_verifycanary12345678\n"
    empty = b""
    payload = {
        "command_id": "tests",
        "command_hash": HASH,
        "status": "passed",
        "exit_code": 0,
        "started_at": STARTED_AT,
        "completed_at": COMPLETED_AT,
        "resource_usage": _resource_usage().to_wire(),
        "stdout_hash": hashlib.sha256(raw).hexdigest(),
        "stdout_bytes": len(raw),
        "stdout_base64": base64.b64encode(raw).decode("ascii"),
        "stderr_hash": hashlib.sha256(empty).hexdigest(),
        "stderr_bytes": 0,
        "stderr_base64": "",
        "raw_logs_omitted": False,
        "duration_ms": 10,
    }

    evidence = VerifyCommandEvidence.from_wire(payload)

    assert evidence.stdout_log == "safe\n[REDACTED]\n"
    assert "github_pat_" not in str(evidence.to_wire())
    with pytest.raises(VerificationError, match="does not match"):
        VerifyCommandEvidence.from_wire(
            {**payload, "stdout_hash": "f" * 64}
        )


def test_verify_evidence_truncates_sanitized_logs_and_omits_over_limit_raw() -> None:
    raw = b"x" * 20_000
    empty_hash = hashlib.sha256(b"").hexdigest()
    payload = {
        "command_id": "tests",
        "command_hash": HASH,
        "status": "passed",
        "exit_code": 0,
        "started_at": STARTED_AT,
        "completed_at": COMPLETED_AT,
        "resource_usage": _resource_usage().to_wire(),
        "stdout_hash": hashlib.sha256(raw).hexdigest(),
        "stdout_bytes": len(raw),
        "stdout_base64": base64.b64encode(raw).decode("ascii"),
        "stderr_hash": empty_hash,
        "stderr_bytes": 0,
        "stderr_base64": "",
        "raw_logs_omitted": False,
        "duration_ms": 10,
    }

    truncated = VerifyCommandEvidence.from_wire(payload)
    omitted = VerifyCommandEvidence.from_wire(
        {
            **payload,
            "status": "output_limit",
            "stdout_base64": None,
            "stderr_base64": None,
            "raw_logs_omitted": True,
        }
    )

    assert truncated.logs_truncated is True
    assert truncated.stdout_log.endswith("[TRUNCATED]")
    assert len(truncated.stdout_log) == 16_384
    assert omitted.raw_logs_omitted is True
    assert omitted.stdout_log == "[OMITTED: output limit exceeded]"


def test_docker_verify_is_fresh_read_only_networkless_and_credential_free(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy()
    implement = _implement_result(policy)
    workspace = _workspace(implement, policy)
    command_path = tmp_path / "commands.json"
    command_path.write_text("{}\n", encoding="utf-8")
    runtime = DockerVerifyRuntime(
        docker_environment={"PATH": "/usr/bin", "HOME": "/tmp/worker"}
    )
    argv = runtime.build_argv(
        workspace=workspace,
        commands_path=str(command_path),
        policy=policy,
        container_name="contribos-verify-" + "1" * 32,
    )
    joined = " ".join(argv)

    for expected in (
        "--pull never",
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges=true",
        "--user 65532:65532",
        "dst=/workspace,readonly",
        "dst=/input/commands.json,readonly",
        "PYTHONDONTWRITEBYTECODE=1",
        "GIT_CONFIG_NOSYSTEM=1",
        "core.hooksPath",
        "protocol.allow",
    ):
        assert expected in joined
    assert joined.count("type=volume") == 1
    assert "/var/run/docker.sock" not in joined
    for forbidden in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "SSH_AUTH_SOCK",
    ):
        assert forbidden not in joined

    with pytest.raises(ValueError, match="disallowed"):
        DockerVerifyRuntime(
            docker_environment={
                "PATH": "/usr/bin",
                "OPENAI_API_KEY": "api_key=verifycanaryvalue",
            }
        )


def test_docker_verify_mounts_dependency_output_read_only(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy()
    implement = _implement_result(policy)
    dependency = _dependency_execution(implement, policy)
    command_path = tmp_path / "commands.json"
    command_path.write_text("{}\n", encoding="utf-8")
    runtime = DockerVerifyRuntime(docker_environment={"PATH": "/usr/bin"})

    argv = runtime.build_argv(
        workspace=_workspace(implement, policy),
        commands_path=str(command_path),
        policy=policy,
        container_name="contribos-verify-" + "2" * 32,
        dependency_workspace=dependency.workspace,
    )
    joined = " ".join(argv)

    assert (
        f"type=volume,src={dependency.workspace.volume_name},"
        "dst=/dependencies,readonly"
    ) in joined
    assert joined.count("type=volume") == 2
    assert "--network none" in joined
