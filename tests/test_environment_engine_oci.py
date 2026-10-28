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
def test_real_python_probe_rejects_source_shadow_without_importing_it(tmp_path):
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
    configuration = {"ecosystem": "python", "target_platform": "linux/arm64", "commands": [{
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
            assert result["executables"][0]["reason_code"] == "entrypoint_module_unavailable_or_shadowed"
            if os.getenv("UPEVE_SECURITY_EVIDENCE"):
                Path(os.environ["UPEVE_SECURITY_EVIDENCE"]).write_text(json.dumps({
                    "classification": "controlled_actual_OCI_security_probe", "native": False,
                    "image": image, "policy_hash": policy.policy_hash, "result": result,
                    "command_evidence": evidence.to_wire(), "source_unchanged": True}, indent=2) + "\n")
        finally:
            await runtime.destroy(workspace)
    asyncio.run(run())
