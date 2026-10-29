"""Controlled real-container boundary checks; never native contribution success."""
import asyncio
from dataclasses import replace
import hashlib
from inspect import getsource
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from app.environment_engine.probe import probe_entrypoints
from app.provenance import content_hash
from app.sandbox_worker.explore import RepositoryArchive
from app.sandbox_worker.implementation import DockerImplementRuntime
from app.sandbox_worker.specs import SandboxCommand, SandboxPolicy
from app.sandbox_worker.verification import DockerVerifyRuntime


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="actual offline entrypoint origin probe")
@pytest.mark.parametrize("failure", ["source_shadow", "target_platform"])
def test_real_python_probe_rejects_source_shadow_or_wrong_platform(tmp_path, failure):
    image = os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"]
    data = b'raise RuntimeError("untrusted source must not be imported during origin inspection")\n'
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        member = tarfile.TarInfo("fixture-" + "a" * 40 + "/unittest.py")
        member.size, member.mode = len(data), 0o644
        bundle.addfile(member, io.BytesIO(data))
    archive = RepositoryArchive.capture(repository_full_name="fixture/fixture", base_commit_sha="a" * 40,
        path=path, allowed_root=tmp_path)
    inventory = content_hash([{"path": "unittest.py", "type": "file", "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "executable": False}])
    target = os.getenv("CONTRIBOS_TARGET_PLATFORM", "linux/arm64")
    requested = target if failure == "source_shadow" else {"linux/arm64": "linux/amd64", "linux/amd64": "linux/arm64"}[target]
    configuration = {"ecosystem": "python", "target_platform": requested, "commands": [{
        "command_id": "unit", "argv": ["python3", "-m", "unittest"], "working_directory": ".",
        "distributions": [], "required_module": "unittest"}]}
    program = getsource(probe_entrypoints) + "\nimport json\nprint(json.dumps(probe_entrypoints(" + repr(configuration) + ")))"
    chunks = tuple(program[i:i + 1800] for i in range(0, len(program), 1800))
    env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT"}}
    runtime = DockerImplementRuntime(docker_environment=env)
    policy = SandboxPolicy()
    async def run():
        workspace = await runtime.create_workspace(image_digest=image, policy=policy)
        try:
            await runtime.materialize(workspace=workspace, archive=archive, policy=policy)
            inspection = await DockerVerifyRuntime(docker_environment=env).run(workspace=replace(workspace, inventory_hash=inventory),
                policy=policy, commands=(SandboxCommand(command_id="origin", argv=("python3", "-I", "-c",
                    "import sys;exec(''.join(sys.argv[1:]))", *chunks)),))
            assert inspection.before_inventory_hash == inspection.after_inventory_hash == inventory
            evidence = inspection.command_evidence[0]
            assert evidence.exit_code == 0
            result = json.loads(evidence.stdout_log)
            assert result["executables"][0]["status"] == "missing"
            assert result["executables"][0]["reason_code"] == (
                "entrypoint_module_unavailable_or_shadowed" if failure == "source_shadow" else "platform_binary_unavailable")
            assert result["target_platform"] == target
            if os.getenv("UPEVE_SECURITY_EVIDENCE"):
                Path(os.environ["UPEVE_SECURITY_EVIDENCE"] + "-" + failure + ".json").write_text(json.dumps({
                    "classification": "controlled_actual_OCI_security_probe", "native": False,
                    "image": image, "policy_hash": policy.policy_hash, "result": result,
                    "command_evidence": evidence.to_wire(), "source_unchanged": True}, indent=2) + "\n")
        finally:
            await runtime.destroy(workspace)
    asyncio.run(run())


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="actual offline zero-test guard")
def test_real_zero_tests_with_zero_exit_is_not_baseline_proof(tmp_path):
    from app.environment_engine.baseline import classify_baseline
    from tests.test_environment_engine_resolution import contract
    image = os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"]
    text = '[project]\nname="zero-tests-fixture"\nversion="1"\n'
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        member = tarfile.TarInfo("fixture-" + "a" * 40 + "/pyproject.toml")
        member.size, member.mode = len(text.encode()), 0o644
        bundle.addfile(member, io.BytesIO(text.encode()))
    archive = RepositoryArchive.capture(repository_full_name="fixture/fixture", base_commit_sha="a" * 40,
        path=path, allowed_root=tmp_path)
    inventory = content_hash([{"path": "pyproject.toml", "type": "file", "size": len(text.encode()),
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "executable": False}])
    command = SandboxCommand(command_id="empty", argv=("python3", "-m", "unittest", "discover", "-v"))
    value = contract({"pyproject.toml": text}, [command.to_wire()]).model_dump(mode="json")
    policy = SandboxPolicy()
    env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT"}}
    runtime = DockerImplementRuntime(docker_environment=env)
    async def run():
        workspace = await runtime.create_workspace(image_digest=image, policy=policy)
        try:
            await runtime.materialize(workspace=workspace, archive=archive, policy=policy)
            inspection = await DockerVerifyRuntime(docker_environment=env).run(workspace=replace(workspace, inventory_hash=inventory),
                policy=policy, commands=(command,))
            assert inspection.before_inventory_hash == inspection.after_inventory_hash == inventory
            evidence = [e.to_wire() for e in inspection.command_evidence]
            assert evidence[0]["exit_code"] == 0
            verdict = classify_baseline(value, "c" * 64, evidence, content_hash(evidence))
            assert verdict.outcome == "unknown"
            assert "baseline_zero_tests" in verdict.reason_codes
            if os.getenv("UPEVE_ZERO_TEST_EVIDENCE"):
                Path(os.environ["UPEVE_ZERO_TEST_EVIDENCE"]).write_text(json.dumps({
                    "classification": "controlled_actual_OCI_zero_tests_guard", "native": False,
                    "image": image, "policy_hash": policy.policy_hash, "command": command.to_wire(),
                    "evidence": evidence, "verdict": verdict.model_dump(mode="json")}, indent=2) + "\n")
        finally:
            await runtime.destroy(workspace)
    asyncio.run(run())


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="actual offline assertion/reproduction classification")
def test_real_assertion_and_passing_control_require_exact_reproduction_expectation(tmp_path):
    from app.environment_engine.baseline import classify_baseline
    from app.environment_engine.contracts import BaselineExpectation
    from tests.test_environment_engine_resolution import contract
    image = os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"]
    files = {"pyproject.toml": '[project]\nname="reproduction-fixture"\nversion="1"\n',
        "test_failure.py": 'import unittest\nclass TestFailure(unittest.TestCase):\n    def test_actual(self):\n        self.fail("frozen reproduction sentinel")\n',
        "test_control.py": 'import unittest\nclass TestControl(unittest.TestCase):\n    def test_control(self):\n        self.assertEqual(2 + 2, 4)\n'}
    path = tmp_path / "source.tar.gz"
    inventory = []
    with tarfile.open(path, "w:gz") as bundle:
        for name, text in sorted(files.items()):
            member = tarfile.TarInfo("fixture-" + "a" * 40 + "/" + name)
            member.size, member.mode = len(text.encode()), 0o644
            bundle.addfile(member, io.BytesIO(text.encode()))
            inventory.append({"path": name, "type": "file", "size": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(), "executable": False})
    inventory_hash = content_hash(inventory)
    archive = RepositoryArchive.capture(repository_full_name="fixture/fixture", base_commit_sha="a" * 40,
        path=path, allowed_root=tmp_path)
    commands = (SandboxCommand(command_id="reproduce", argv=("python3", "-m", "unittest", "-v", "test_failure.TestFailure.test_actual")),
        SandboxCommand(command_id="control", argv=("python3", "-m", "unittest", "-v", "test_control")))
    value = contract(files, [c.to_wire() for c in commands]).model_dump(mode="json")
    expectation = BaselineExpectation(issue_url="https://github.com/fixture/fixture/issues/1",
        test_node="test_failure.TestFailure.test_actual", failure_literal="AssertionError: frozen reproduction sentinel")
    policy = SandboxPolicy()
    env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT"}}
    runtime = DockerImplementRuntime(docker_environment=env)
    async def run():
        workspace = await runtime.create_workspace(image_digest=image, policy=policy)
        try:
            await runtime.materialize(workspace=workspace, archive=archive, policy=policy)
            inspection = await DockerVerifyRuntime(docker_environment=env).run(workspace=replace(workspace, inventory_hash=inventory_hash),
                policy=policy, commands=commands)
            assert inspection.before_inventory_hash == inspection.after_inventory_hash == inventory_hash
            evidence = [e.to_wire() for e in inspection.command_evidence]
            assert [e["exit_code"] for e in evidence] == [1, 0]
            ordinary = classify_baseline(value, "c" * 64, evidence, content_hash(evidence))
            assert ordinary.outcome == "preexisting_failure"
            value["expected_failures"] = {"reproduce": expectation.model_dump(mode="json")}
            expected = classify_baseline(value, "c" * 64, evidence, content_hash(evidence))
            assert expected.outcome == "expected_issue_reproduction"
            value["expected_failures"]["reproduce"]["failure_literal"] = "AssertionError: different sentinel"
            assert classify_baseline(value, "c" * 64, evidence, content_hash(evidence)).outcome == "preexisting_failure"
            if os.getenv("UPEVE_ASSERTION_EVIDENCE"):
                Path(os.environ["UPEVE_ASSERTION_EVIDENCE"]).write_text(json.dumps({
                    "classification": "controlled_actual_OCI_assertion_contract_not_human_approval", "native": False,
                    "image": image, "policy_hash": policy.policy_hash, "commands": [c.to_wire() for c in commands],
                    "expectation": expectation.model_dump(mode="json"), "command_evidence": evidence,
                    "without_expectation": ordinary.model_dump(mode="json"), "exact_expectation": expected.model_dump(mode="json")}, indent=2) + "\n")
        finally:
            await runtime.destroy(workspace)
    asyncio.run(run())


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="actual offline nested diagnostic guard")
@pytest.mark.parametrize("diagnostic", ["--help", "--version"])
def test_real_zero_exit_package_script_is_not_baseline_proof(tmp_path, diagnostic):
    from app.environment_engine.contracts import EnvironmentBinding
    from app.environment_engine.resolver import resolve_contract
    from app.environment_engine.baseline import classify_baseline
    image = os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"]
    text = json.dumps({"name": "diagnostic-fixture", "scripts": {"lint": "node " + diagnostic}})
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        member = tarfile.TarInfo("fixture-" + "a" * 40 + "/package.json")
        member.size, member.mode = len(text.encode()), 0o644
        bundle.addfile(member, io.BytesIO(text.encode()))
    archive = RepositoryArchive.capture(repository_full_name="fixture/fixture", base_commit_sha="a" * 40,
        path=path, allowed_root=tmp_path)
    digest = hashlib.sha256(text.encode()).hexdigest()
    inventory = content_hash([{"path": "package.json", "type": "file", "size": len(text.encode()),
        "sha256": digest, "executable": False}])
    command = SandboxCommand(command_id="lint", argv=("npm", "run", "lint"))
    policy = SandboxPolicy()
    binding = EnvironmentBinding(task_id="controlled", snapshot_hash="a" * 64, source_archive_hash=archive.archive_hash,
        base_commit_sha=archive.base_commit_sha, plan_version_hash="b" * 64,
        approved_commands_hash=content_hash([command.to_wire()]), target_platform="linux/arm64",
        runner_image_digest=image, sandbox_policy_hash=policy.policy_hash)
    contract = resolve_contract(binding, [command.to_wire()], {"ecosystem": "node", "manifests": ["package.json"],
        "node_environment": {"package_roots": ["."]}}, {"inventory": ["package.json"],
        "entries": [{"path": "package.json", "content": text, "prior_hash": digest}]})
    assert contract.admission == "static_admitted"
    env = {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT"}}
    runtime = DockerImplementRuntime(docker_environment=env)
    async def run():
        workspace = await runtime.create_workspace(image_digest=image, policy=policy)
        try:
            await runtime.materialize(workspace=workspace, archive=archive, policy=policy)
            inspection = await DockerVerifyRuntime(docker_environment=env).run(workspace=replace(workspace, inventory_hash=inventory),
                policy=policy, commands=(command,))
            assert inspection.before_inventory_hash == inspection.after_inventory_hash == inventory
            evidence = [e.to_wire() for e in inspection.command_evidence]
            assert evidence[0]["exit_code"] == 0
            verdict = classify_baseline(contract.model_dump(mode="json"), "c" * 64, evidence, content_hash(evidence))
            assert verdict.outcome == "unknown"
            assert "baseline_help_only" in verdict.reason_codes
            if os.getenv("UPEVE_HELP_EVIDENCE"):
                target = Path(os.environ["UPEVE_HELP_EVIDENCE"] + diagnostic + ".json")
                target.write_text(json.dumps({"classification": "controlled_actual_OCI_baseline_guard", "native": False,
                    "image": image, "policy_hash": policy.policy_hash, "command": command.to_wire(),
                    "exit_code": 0, "evidence": evidence, "verdict": verdict.model_dump(mode="json")}, indent=2) + "\n")
        finally:
            await runtime.destroy(workspace)
    asyncio.run(run())
