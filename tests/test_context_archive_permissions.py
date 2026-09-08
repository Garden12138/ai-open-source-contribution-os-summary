from __future__ import annotations

import asyncio
import json
import shutil
import stat
from pathlib import Path

import pytest

from app.archives import RepositoryArchiveStore
from app.sandbox_worker.coding_context import (
    CodingContextError,
    DockerCodingContextRuntime,
)
from app.sandbox_worker.planning_context import DockerPlanningContextRuntime
from app.sandbox_worker.specs import SandboxPolicy


def stored_archive(tmp_path: Path):
    source = tmp_path / "private-archive"
    source.write_bytes(b"frozen archive input")
    store = RepositoryArchiveStore(tmp_path / "artifacts")
    digest = store.put_file(source)
    archive = store.lookup(
        digest, repository_full_name="fixture/repo", base_commit_sha="a" * 40
    )
    assert stat.S_IMODE(archive.path.stat().st_mode) == 0o600
    return archive


async def run_context(runtime, archive, planning):
    kwargs = dict(
        archive=archive, image_digest="sha256:" + "a" * 64,
        policy=SandboxPolicy(),
    )
    if planning:
        return await runtime.inspect(**kwargs, query="readme")
    return await runtime.capture(**kwargs, paths=("README.md",))


@pytest.mark.parametrize("planning", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_private_archive_uses_readable_temporary_file_mounts(tmp_path, planning, fail):
    archive = stored_archive(tmp_path)
    base = DockerPlanningContextRuntime if planning else DockerCodingContextRuntime
    mounted = []

    class Runtime(base):
        async def _run(self, argv, **kwargs):
            for argument in argv:
                if not argument.startswith("type=bind,"):
                    continue
                assert argument.endswith(",readonly")
                source = Path(argument.split("src=", 1)[1].split(",", 1)[0])
                assert source != archive.path
                assert stat.S_IMODE(source.stat().st_mode) == 0o444
                assert stat.S_IMODE(source.parent.stat().st_mode) == 0o700
                if source.name == "repository.tar":
                    assert source.read_bytes() == archive.path.read_bytes()
                mounted.append(source)
            assert len(mounted) == 2
            assert stat.S_IMODE(archive.path.stat().st_mode) == 0o600
            if fail:
                raise CodingContextError("Fixture Runner failure")
            result = (
                {"inventory": [], "entries": [], "truncated": False}
                if planning else [dict(
                    path="README.md", prior_hash=None, content=None, executable=None
                )]
            )
            return json.dumps(result).encode()

    runtime = Runtime(docker_environment={})
    if fail:
        with pytest.raises(CodingContextError, match="Fixture Runner failure"):
            asyncio.run(run_context(runtime, archive, planning))
    else:
        asyncio.run(run_context(runtime, archive, planning))
    assert all(not path.exists() for path in mounted)
    archive.verify()
    assert stat.S_IMODE(archive.path.stat().st_mode) == 0o600


@pytest.mark.parametrize("planning", [False, True])
def test_changed_archive_copy_is_rejected_before_runner(tmp_path, monkeypatch, planning):
    archive = stored_archive(tmp_path)
    base = DockerPlanningContextRuntime if planning else DockerCodingContextRuntime
    copied = []

    def corrupt_copy(source, target):
        target.write_bytes(b"altered archive")
        copied.append(target)

    class Runtime(base):
        async def _run(self, *args, **kwargs):
            pytest.fail("Changed input must not reach Docker")

    monkeypatch.setattr(shutil, "copyfile", corrupt_copy)
    with pytest.raises(CodingContextError, match="changed while staging"):
        asyncio.run(run_context(Runtime(docker_environment={}), archive, planning))
    assert copied and all(not path.exists() for path in copied)
    archive.verify()
