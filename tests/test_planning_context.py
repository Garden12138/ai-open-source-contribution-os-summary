from __future__ import annotations

import asyncio
import io
import hashlib
import json
import os
from pathlib import Path
import tarfile

import pytest

from app.archives import RepositoryArchiveStore
from app.sandbox_worker.coding_context import CodingContextError
from app.sandbox_worker.planning_context import DockerPlanningContextRuntime
from app.sandbox_worker.specs import SandboxPolicy


def archive(tmp_path: Path, entries: dict[str, bytes]):
    path = tmp_path / "context.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
    store = RepositoryArchiveStore(tmp_path / "artifacts")
    digest = store.put_file(path)
    return store.lookup(
        digest, repository_full_name="fixture/repo", base_commit_sha="a" * 40
    )


def test_planning_inspection_has_no_network_credentials_or_writable_archive(tmp_path):
    source = archive(tmp_path, {"repo-" + "a" * 40 + "/README.md": b"Trusted fixture"})

    class Inspect(DockerPlanningContextRuntime):
        async def _run(self, argv, **_):
            assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
            assert "--read-only" in argv and "--cap-drop" in argv
            assert not any("docker.sock" in part or "ghp_" in part for part in argv)
            assert all(
                part.endswith("readonly")
                for part in argv
                if part.startswith("type=bind")
            )
            return b'{"inventory":["README.md"],"entries":[],"truncated":true}'

    runtime = Inspect(docker_environment={})
    result = asyncio.run(
        runtime.inspect(
            archive=source,
            image_digest="sha256:" + "a" * 64,
            policy=SandboxPolicy(),
            query="readme",
        )
    )
    assert result["truncated"]
    with pytest.raises((ValueError, CodingContextError)):
        asyncio.run(
            runtime.inspect(
                archive=source,
                image_digest="sha256:" + "a" * 64,
                policy=SandboxPolicy(),
                query="",
                paths=("../secret",),
            )
        )


def test_planning_omits_sensitive_file_text_without_fabricating_content_hash(tmp_path):
    source = archive(tmp_path, {"repo-" + "a" * 40 + "/README.md": b"fixture"})
    canary = "ghp_planningdocumentcanary123456789012345"
    safe = "def parse(value): return value\n"

    class Inspect(DockerPlanningContextRuntime):
        async def _run(self, argv, **kwargs):
            return json.dumps({
                "inventory": ["README.md", "src/parser.py"],
                "entries": [
                    dict(path=path, content=content, executable=False,
                         prior_hash=hashlib.sha256(content.encode()).hexdigest())
                    for path, content in [("README.md", canary), ("src/parser.py", safe)]
                ],
                "truncated": False,
            }).encode()

    result = asyncio.run(Inspect(docker_environment={}).inspect(
        archive=source, image_digest="sha256:" + "a" * 64,
        policy=SandboxPolicy(), query="parser",
    ))
    assert result["truncated"]
    assert result["inventory"] == ["README.md", "src/parser.py"]
    assert result["entries"][0] == dict(
        path="README.md", content=None, prior_hash=None, executable=None
    )
    assert result["entries"][1]["content"] == safe
    assert result["entries"][1]["prior_hash"] == hashlib.sha256(safe.encode()).hexdigest()
    assert canary not in json.dumps(result)


@pytest.mark.skipif(
    not os.getenv("CONTRIBOS_PLANNING_TEST_IMAGE"),
    reason="real planning Runner image is opt-in",
)
def test_real_planning_context_reads_without_running_repository_code(tmp_path):
    root = "repo-" + "a" * 40 + "/"
    source = archive(
        tmp_path,
        {
            root + "README.md": b"# Fixture\nPlan the parser improvement.\n",
            root
            + "src/parser.py": b"raise RuntimeError('repository code must never execute')\n",
            root + "package.json": b'{"scripts":{"prepare":"exit 99"}}',
            root + "binary.bin": b"\x00\xff",
        },
    )
    allowed = (
        "PATH",
        "HOME",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "TMPDIR",
    )
    runtime = DockerPlanningContextRuntime(
        docker_environment={
            key: os.environ[key] for key in allowed if key in os.environ
        }
    )
    result = asyncio.run(
        runtime.inspect(
            archive=source,
            image_digest=os.environ["CONTRIBOS_PLANNING_TEST_IMAGE"],
            policy=SandboxPolicy(),
            query="parser",
        )
    )
    assert {entry["path"] for entry in result["entries"]} == {
        "README.md",
        "src/parser.py",
        "package.json",
    }
    assert "repository code must never execute" in next(
        v["content"] for v in result["entries"] if v["path"] == "src/parser.py"
    )
    bad = archive(tmp_path, {root + "../escape": b"forbidden"})
    with pytest.raises(CodingContextError):
        asyncio.run(
            runtime.inspect(
                archive=bad,
                image_digest=os.environ["CONTRIBOS_PLANNING_TEST_IMAGE"],
                policy=SandboxPolicy(),
                query="",
            )
        )
    assert not (tmp_path / "escape").exists()
