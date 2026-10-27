"""Exercise script dispatch with sentinels, not merely exit-code-zero help."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import io
import json
import os
import tarfile

import pytest

from app.provenance import content_hash
from app.sandbox_worker.dependencies import DependencyPlanSigner, DependencyProxyPolicy, DockerDependencyRuntime, DockerDependencyProxyRuntime
from app.sandbox_worker.explore import RepositoryArchive
from app.sandbox_worker.implementation import DockerImplementRuntime
from app.sandbox_worker.specs import SandboxPolicy, SandboxCommand
from app.sandbox_worker.verification import DockerVerifyRuntime
from tests.test_node_environments import dependency_plan, node_files


@pytest.mark.skipif(os.getenv("CONTRIBOS_RUN_DOCKER_ACCEPTANCE") != "1", reason="real credential-free Bun dispatch probe")
def test_real_bun_script_dispatch_requires_execution_sentinels(tmp_path):
    files = node_files("npm", "10.9.4")
    files.pop("package-lock.json")
    root = json.loads(files["package.json"])
    root["packageManager"] = "bun@1.4.4"
    root["scripts"].update(probe="echo root-executed", fail="exit 13")
    files["package.json"] = json.dumps(root)
    child = json.loads(files["packages/lib/package.json"])
    child["scripts"] = {"probe":"echo child-executed", "fail":"exit 17"}
    files["packages/lib/package.json"] = json.dumps(child)
    archive_path = tmp_path / "repository.tar.gz"
    with tarfile.open(archive_path, "w:gz") as bundle:
        for path, text in files.items():
            entry = tarfile.TarInfo("fixture-" + "a"*40 + "/" + path)
            entry.size, entry.mode = len(text.encode()), 0o644
            bundle.addfile(entry, io.BytesIO(text.encode()))
    archive = RepositoryArchive(repository_full_name="fixture/fixture", base_commit_sha="a"*40,
        archive_hash=hashlib.sha256(archive_path.read_bytes()).hexdigest(), path=archive_path, size_bytes=archive_path.stat().st_size)
    inventory = [{"path":p,"type":"file","size":len(text.encode()),"sha256":hashlib.sha256(text.encode()).hexdigest(),
        "executable":False} for p,text in sorted(files.items())]
    inventory_hash = content_hash(inventory)
    image = os.environ["CONTRIBOS_SANDBOX_RUNNER_IMAGE"]
    plan = dependency_plan(files, image, inventory_hash)
    environment = {k:os.environ[k] for k in ("HOME","PATH","TMPDIR","DOCKER_HOST","DOCKER_CONTEXT","DOCKER_CERT_PATH","DOCKER_CONFIG","DOCKER_TLS_VERIFY") if k in os.environ}
    materializer = DockerImplementRuntime(docker_environment=environment)
    dependencies = DockerDependencyRuntime(docker_environment=environment)
    proxy = DockerDependencyProxyRuntime(docker_environment=environment)
    verifier = DockerVerifyRuntime(docker_environment=environment)
    signer = DependencyPlanSigner(key_id="bun-dispatch", signing_key=b"n"*32)
    policy, proxy_policy = SandboxPolicy(), DependencyProxyPolicy.for_ecosystem("node")

    async def run():
        workspace = await materializer.create_workspace(image_digest=image, policy=policy)
        lease = dependency = None
        try:
            await materializer.materialize(workspace=workspace, archive=archive, policy=policy)
            lease = await proxy.start(image_digest=image, policy=proxy_policy)
            dependency = await dependencies.prepare(signed_plan=signer.sign(plan), signer=signer,
                repository_volume=workspace.volume_name, proxy_policy=proxy_policy, proxy_lease=lease, sandbox_policy=policy)
            await proxy.stop(lease); lease = None
            commands = []
            for script in ("probe", "fail"):
                for label, argv, cwd in [
                    ("prefix", ("bun","--cwd","packages/lib","run",script), "."),
                    ("equals", ("bun","--cwd=packages/lib","run",script), "."),
                    ("post-run", ("bun","run","--cwd","packages/lib",script), "."),
                    ("typed-cwd", ("bun","run",script), "packages/lib")]:
                    commands.append(SandboxCommand(command_id=label+"-"+script, argv=argv, working_directory=cwd))
            result = await verifier.run(workspace=replace(workspace, inventory_hash=inventory_hash), commands=tuple(commands),
                policy=policy, dependency_workspace=dependency.workspace)
            observed = {e.command_id:{"exit_code":e.exit_code,"child_executed":"child-executed" in e.stdout_log.splitlines(),
                "help_only":"Usage: bun run [flags] <file or script>" in e.stdout_log,
                "stdout_hash":e.stdout_hash,"stderr_hash":e.stderr_hash} for e in result.command_evidence}
            assert observed["typed-cwd-probe"]["child_executed"]
            assert observed["typed-cwd-probe"]["exit_code"] == 0
            assert observed["typed-cwd-fail"]["exit_code"] == 17
            assert observed["prefix-fail"]["exit_code"] == 0
            assert observed["prefix-fail"]["help_only"]
            assert not observed["prefix-probe"]["child_executed"]
            from app.execution_outcome import classify_verification
            diagnostic = classify_verification({"succeeded": True,
                "test_results": [{"outcome":"passed","exit_code":0}],
                "command_evidence":[e.to_wire() for e in result.command_evidence if e.command_id == "prefix-fail"]})
            assert diagnostic["status"] == "environment_blocked"
            assert diagnostic["passed"] == 0
            assert result.before_inventory_hash == result.after_inventory_hash == inventory_hash
            (tmp_path / "bun-dispatch.json").write_text(json.dumps({"classification":"controlled_cli_dispatch_diagnostic",
                "manager":"bun","version":"1.4.4","commands":observed,"production_mutations":0,"model_calls":0},indent=2))
            print(json.dumps(observed))
        finally:
            if lease: await proxy.stop(lease, ignore_missing=True)
            if dependency: await dependencies.destroy(dependency.workspace, ignore_missing=True)
            await materializer.destroy(workspace)
    asyncio.run(run())
