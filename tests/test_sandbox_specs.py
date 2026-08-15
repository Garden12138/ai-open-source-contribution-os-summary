from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.sandbox_worker import (
    JOB_SPEC_SIGNATURE_VERSION,
    JOB_SPEC_VERSION,
    SANDBOX_POLICY_VERSION,
    JobSpec,
    JobSpecSignatureError,
    JobSpecSigner,
    SandboxCommand,
    SandboxPolicy,
    SandboxSpecError,
    SandboxStage,
    SandboxWorkerRequest,
    SignedJobSpec,
)


HASH = "1" * 64
IMAGE = "sha256:" + "2" * 64


def _spec(
    policy: SandboxPolicy,
    *,
    stage: SandboxStage = SandboxStage.EXPLORE,
) -> JobSpec:
    artifacts: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    commands: tuple[SandboxCommand, ...] = ()
    if stage is SandboxStage.IMPLEMENT:
        artifacts = ("3" * 64,)
        paths = ("app/example.py", "tests/test_example.py")
    elif stage is SandboxStage.VERIFY:
        artifacts = ("4" * 64,)
        commands = (
            SandboxCommand(
                command_id="focused-tests",
                argv=("python", "-m", "pytest", "-q"),
            ),
        )
    return JobSpec(
        spec_id=f"spec-{stage.value}-1",
        execution_attempt_id="attempt-1",
        correlation_id="execution-correlation-1",
        stage=stage,
        repository_full_name="fixture/contribution",
        base_commit_sha="a" * 40,
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
        approved_state_version_id="state-approved-1",
        approved_state_record_hash=HASH,
        provider_contract_hash=HASH,
        repository_archive_hash="6" * 64,
        runner_image_digest=IMAGE,
        sandbox_policy_version=policy.version,
        sandbox_policy_hash=policy.policy_hash,
        input_artifact_hashes=artifacts,
        allowed_change_paths=paths,
        commands=commands,
    )


def test_sandbox_policy_v1_is_hashed_bounded_and_fail_closed() -> None:
    policy = SandboxPolicy()

    assert policy.version == SANDBOX_POLICY_VERSION
    assert policy.cpu_limit == 2.0
    assert policy.memory_bytes == 2 * 1024 * 1024 * 1024
    assert policy.pids_limit == 256
    assert policy.timeout_seconds == 600
    assert policy.network_mode == "none"
    assert policy.read_only_root is True
    assert policy.writable_workspace_stages == ("implement",)
    assert policy.cap_drop == ("ALL",)
    assert policy.no_new_privileges is True
    assert policy.mount_docker_socket is False
    assert policy.mount_host_home is False
    assert policy.mount_ssh_agent is False
    assert policy.inject_credentials is False
    assert policy.allow_model_access is False
    assert policy.git_safety_policy_version == "sandbox-git-safety-v1"
    assert len(policy.policy_hash) == 64
    assert SandboxPolicy.from_wire(policy.to_wire()) == policy
    assert SandboxPolicy().policy_hash == policy.policy_hash

    with pytest.raises(SandboxSpecError, match="version"):
        replace(policy, version="sandbox-policy-v2")
    with pytest.raises(SandboxSpecError, match="weakens"):
        replace(policy, mount_docker_socket=True)
    with pytest.raises(SandboxSpecError, match="weakens"):
        replace(policy, network_mode="bridge")
    with pytest.raises(SandboxSpecError, match="git_safety_policy_version"):
        replace(policy, git_safety_policy_version="sandbox-git-safety-v0")
    with pytest.raises(SandboxSpecError, match="memory"):
        replace(policy, memory_bytes=policy.memory_bytes + 1)
    with pytest.raises(SandboxSpecError, match="timeout"):
        replace(policy, timeout_seconds=601)
    with pytest.raises(SandboxSpecError, match="fields"):
        SandboxPolicy.from_wire({**policy.to_wire(), "unexpected": True})


@pytest.mark.parametrize(
    "stage",
    (
        SandboxStage.EXPLORE,
        SandboxStage.IMPLEMENT,
        SandboxStage.VERIFY,
    ),
)
def test_job_spec_hash_and_signature_are_deterministic_and_round_trip(
    stage: SandboxStage,
) -> None:
    policy = SandboxPolicy()
    spec = _spec(policy, stage=stage)
    signer = JobSpecSigner(
        key_id="sandbox-signing-2026-07",
        signing_key=b"s" * 32,
    )

    signed = signer.sign(spec)
    decoded = SignedJobSpec.from_bytes(signed.to_bytes())

    assert spec.version == JOB_SPEC_VERSION
    assert signed.signature_version == JOB_SPEC_SIGNATURE_VERSION
    assert signed.spec_hash == spec.spec_hash
    assert signer.sign(spec) == signed
    assert signer.verify(decoded, expected_policy=policy) == spec
    assert b"s" * 32 not in signed.to_bytes()
    assert "signing_key" not in signed.to_bytes().decode("utf-8")
    request = SandboxWorkerRequest(
        request_id=f"request-{stage.value}-1",
        correlation_id=spec.correlation_id,
        operation="validate_job_spec",
        payload={"signed_job_spec": signed.to_wire()},
    )
    assert len(request.to_bytes()) < 65_536


def test_job_spec_signature_rejects_payload_signature_key_and_policy_changes() -> None:
    policy = SandboxPolicy()
    spec = _spec(policy)
    signer = JobSpecSigner(key_id="active-key", signing_key=b"a" * 32)
    signed = signer.sign(spec)

    changed_spec = replace(spec, base_commit_sha="b" * 40)
    changed_payload = replace(
        signed,
        spec=changed_spec,
        spec_hash=changed_spec.spec_hash,
    )
    with pytest.raises(JobSpecSignatureError, match="signature"):
        signer.verify(changed_payload, expected_policy=policy)

    changed_signature = replace(signed, signature="f" * 64)
    with pytest.raises(JobSpecSignatureError, match="signature"):
        signer.verify(changed_signature, expected_policy=policy)

    unknown_key = replace(signed, key_id="retired-key")
    with pytest.raises(JobSpecSignatureError, match="unknown"):
        signer.verify(unknown_key, expected_policy=policy)

    reduced_policy = replace(policy, cpu_limit=1.0)
    with pytest.raises(JobSpecSignatureError, match="SandboxPolicy"):
        signer.verify(signed, expected_policy=reduced_policy)

    with pytest.raises(JobSpecSignatureError, match="hash"):
        replace(signed, spec_hash="e" * 64)


def test_job_spec_rejects_unsafe_stage_inputs_paths_commands_and_secrets() -> None:
    policy = SandboxPolicy()
    explore = _spec(policy)

    with pytest.raises(SandboxSpecError, match="Explore"):
        replace(explore, allowed_change_paths=("app/example.py",))
    with pytest.raises(SandboxSpecError, match="Implement"):
        replace(explore, stage=SandboxStage.IMPLEMENT)
    with pytest.raises(SandboxSpecError, match="Verify"):
        replace(
            explore,
            stage=SandboxStage.VERIFY,
            input_artifact_hashes=("3" * 64,),
        )
    verify = _spec(policy, stage=SandboxStage.VERIFY)
    dependency_verify = replace(
        verify,
        input_artifact_hashes=(
            verify.input_artifact_hashes[0],
            "5" * 64,
        ),
    )
    assert len(dependency_verify.input_artifact_hashes) == 2
    with pytest.raises(SandboxSpecError, match="Verify"):
        replace(
            dependency_verify,
            input_artifact_hashes=(
                *dependency_verify.input_artifact_hashes,
                "6" * 64,
            ),
        )
    implement = _spec(policy, stage=SandboxStage.IMPLEMENT)
    with pytest.raises(SandboxSpecError, match="allowed change path"):
        replace(
            implement,
            allowed_change_paths=("../escape.py",),
        )
    with pytest.raises(ValueError, match="Credential-like"):
        SandboxCommand(
            command_id="secret",
            argv=("tool", "api_key=supersecretvalue"),
        )
    with pytest.raises(ValueError, match="32 bytes"):
        JobSpecSigner(key_id="short", signing_key=b"short")


def test_signed_job_spec_parser_rejects_extra_fields_and_tampering() -> None:
    policy = SandboxPolicy()
    signed = JobSpecSigner(
        key_id="active-key",
        signing_key=b"k" * 32,
    ).sign(_spec(policy))
    raw = json.loads(signed.to_bytes())
    raw["unexpected"] = True
    with pytest.raises(SandboxSpecError, match="fields"):
        SignedJobSpec.from_bytes(json.dumps(raw).encode("utf-8"))

    raw = json.loads(signed.to_bytes())
    raw["spec"]["base_commit_sha"] = "c" * 40
    with pytest.raises(JobSpecSignatureError, match="hash"):
        SignedJobSpec.from_bytes(json.dumps(raw).encode("utf-8"))
