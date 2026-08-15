"""Read-only Explore over an exact, authenticated repository archive."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from app.provenance import content_hash
from app.sandbox_worker.container import ReadOnlySnapshot
from app.sandbox_worker.git_safety import docker_git_safety_argv
from app.sandbox_worker.specs import (
    JobSpecSignatureError,
    JobSpecSigner,
    SandboxPolicy,
    SandboxStage,
    SignedJobSpec,
)
from app.security import ensure_no_sensitive_data


EXPLORE_RESULT_VERSION = "sandbox-explore-result-v1"
MAX_REPOSITORY_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPLORE_OUTPUT_BYTES = 1_000_000
_HASH = re.compile(r"^[0-9a-f]{64}$")
_BASE_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_ALLOWED_DOCKER_ENV = frozenset(
    {
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "HOME",
        "PATH",
        "TMPDIR",
    }
)


class ExploreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RepositoryArchive:
    repository_full_name: str
    base_commit_sha: str
    path: Path
    archive_hash: str
    size_bytes: int

    @classmethod
    def capture(
        cls,
        *,
        repository_full_name: str,
        base_commit_sha: str,
        path: Path,
        allowed_root: Path,
    ) -> "RepositoryArchive":
        _repository(repository_full_name)
        _base_sha(base_commit_sha)
        if path.is_symlink():
            raise ValueError("Repository archive cannot be a symlink")
        resolved_path = path.resolve()
        resolved_root = allowed_root.resolve()
        if (
            not resolved_path.is_file()
            or not resolved_path.is_relative_to(resolved_root)
            or "," in str(resolved_path)
        ):
            raise ValueError(
                "Repository archive must be a file inside its allowed root"
            )
        size = resolved_path.stat().st_size
        if size < 1 or size > MAX_REPOSITORY_ARCHIVE_BYTES:
            raise ValueError("Repository archive size is invalid")
        return cls(
            repository_full_name=repository_full_name,
            base_commit_sha=base_commit_sha,
            path=resolved_path,
            archive_hash=_file_hash(resolved_path),
            size_bytes=size,
        )

    def verify(self) -> None:
        if (
            self.path.is_symlink()
            or not self.path.is_file()
            or self.path.stat().st_size != self.size_bytes
            or _file_hash(self.path) != self.archive_hash
        ):
            raise ExploreError("Repository archive changed after capture")


@dataclass(frozen=True, slots=True)
class ExploreInspection:
    inventory_hash: str
    file_count: int
    total_bytes: int
    sample_paths: tuple[str, ...]
    manifest_paths: tuple[str, ...]

    @classmethod
    def from_wire(cls, value: object) -> "ExploreInspection":
        if not isinstance(value, dict) or set(value) != {
            "inventory_hash",
            "file_count",
            "total_bytes",
            "sample_paths",
            "manifest_paths",
        }:
            raise ExploreError("Explore inspection response is invalid")
        if not isinstance(value["inventory_hash"], str) or not _HASH.fullmatch(
            value["inventory_hash"]
        ):
            raise ExploreError("Explore inventory hash is invalid")
        for name in ("file_count", "total_bytes"):
            number = value[name]
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise ExploreError(f"Explore {name.replace('_', ' ')} is invalid")
        sample_paths = _path_tuple(
            value["sample_paths"],
            name="sample paths",
            maximum=200,
        )
        manifest_paths = _path_tuple(
            value["manifest_paths"],
            name="manifest paths",
            maximum=200,
        )
        inspection = cls(
            inventory_hash=value["inventory_hash"],
            file_count=value["file_count"],
            total_bytes=value["total_bytes"],
            sample_paths=sample_paths,
            manifest_paths=manifest_paths,
        )
        ensure_no_sensitive_data(
            inspection.to_wire(),
            context="Explore inspection",
        )
        return inspection

    def to_wire(self) -> dict[str, object]:
        return {
            "inventory_hash": self.inventory_hash,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "sample_paths": list(self.sample_paths),
            "manifest_paths": list(self.manifest_paths),
        }


@dataclass(frozen=True, slots=True)
class ExploreResult:
    spec_id: str
    spec_hash: str
    execution_attempt_id: str
    repository_full_name: str
    base_commit_sha: str
    repository_archive_hash: str
    runner_image_digest: str
    sandbox_policy_version: str
    sandbox_policy_hash: str
    snapshot_manifest_hash: str
    inventory_hash: str
    file_count: int
    total_bytes: int
    sample_paths: tuple[str, ...]
    manifest_paths: tuple[str, ...]
    result_hash: str
    version: str = EXPLORE_RESULT_VERSION

    def __post_init__(self) -> None:
        if self.version != EXPLORE_RESULT_VERSION:
            raise ExploreError("Explore result version is unsupported")
        for value in (
            self.spec_hash,
            self.repository_archive_hash,
            self.sandbox_policy_hash,
            self.snapshot_manifest_hash,
            self.inventory_hash,
            self.result_hash,
        ):
            if not isinstance(value, str) or not _HASH.fullmatch(value):
                raise ExploreError("Explore result hash is invalid")
        _repository(self.repository_full_name)
        _base_sha(self.base_commit_sha)
        if self.result_hash != content_hash(self.hash_payload()):
            raise ExploreError("Explore result hash does not match its payload")
        ensure_no_sensitive_data(self.to_wire(), context="Explore result")

    @classmethod
    def create(
        cls,
        *,
        spec: SignedJobSpec,
        snapshot: ReadOnlySnapshot,
        inspection: ExploreInspection,
    ) -> "ExploreResult":
        job = spec.spec
        payload = {
            "version": EXPLORE_RESULT_VERSION,
            "spec_id": job.spec_id,
            "spec_hash": spec.spec_hash,
            "execution_attempt_id": job.execution_attempt_id,
            "repository_full_name": job.repository_full_name,
            "base_commit_sha": job.base_commit_sha,
            "repository_archive_hash": job.repository_archive_hash,
            "runner_image_digest": job.runner_image_digest,
            "sandbox_policy_version": job.sandbox_policy_version,
            "sandbox_policy_hash": job.sandbox_policy_hash,
            "snapshot_manifest_hash": snapshot.manifest_hash,
            **inspection.to_wire(),
        }
        ensure_no_sensitive_data(payload, context="Explore result")
        return cls(
            spec_id=job.spec_id,
            spec_hash=spec.spec_hash,
            execution_attempt_id=job.execution_attempt_id,
            repository_full_name=job.repository_full_name,
            base_commit_sha=job.base_commit_sha,
            repository_archive_hash=job.repository_archive_hash,
            runner_image_digest=job.runner_image_digest,
            sandbox_policy_version=job.sandbox_policy_version,
            sandbox_policy_hash=job.sandbox_policy_hash,
            snapshot_manifest_hash=snapshot.manifest_hash,
            inventory_hash=inspection.inventory_hash,
            file_count=inspection.file_count,
            total_bytes=inspection.total_bytes,
            sample_paths=inspection.sample_paths,
            manifest_paths=inspection.manifest_paths,
            result_hash=content_hash(payload),
        )

    def hash_payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "spec_id": self.spec_id,
            "spec_hash": self.spec_hash,
            "execution_attempt_id": self.execution_attempt_id,
            "repository_full_name": self.repository_full_name,
            "base_commit_sha": self.base_commit_sha,
            "repository_archive_hash": self.repository_archive_hash,
            "runner_image_digest": self.runner_image_digest,
            "sandbox_policy_version": self.sandbox_policy_version,
            "sandbox_policy_hash": self.sandbox_policy_hash,
            "snapshot_manifest_hash": self.snapshot_manifest_hash,
            "inventory_hash": self.inventory_hash,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "sample_paths": list(self.sample_paths),
            "manifest_paths": list(self.manifest_paths),
        }

    def to_wire(self) -> dict[str, object]:
        return {
            **self.hash_payload(),
            "result_hash": self.result_hash,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ExploreResult":
        if not isinstance(value, dict):
            raise ExploreError("Explore result payload is invalid")
        payload = dict(value)
        result_hash = payload.pop("result_hash", None)
        if not isinstance(result_hash, str):
            result_hash = content_hash(payload)
        return cls(
            spec_id=str(payload["spec_id"]),
            spec_hash=str(payload["spec_hash"]),
            execution_attempt_id=str(payload["execution_attempt_id"]),
            repository_full_name=str(payload["repository_full_name"]),
            base_commit_sha=str(payload["base_commit_sha"]),
            repository_archive_hash=str(payload["repository_archive_hash"]),
            runner_image_digest=str(payload["runner_image_digest"]),
            sandbox_policy_version=str(payload["sandbox_policy_version"]),
            sandbox_policy_hash=str(payload["sandbox_policy_hash"]),
            snapshot_manifest_hash=str(payload["snapshot_manifest_hash"]),
            inventory_hash=str(payload["inventory_hash"]),
            file_count=int(payload["file_count"]),
            total_bytes=int(payload["total_bytes"]),
            sample_paths=tuple(payload["sample_paths"]),
            manifest_paths=tuple(payload["manifest_paths"]),
            result_hash=result_hash,
            version=str(payload.get("version", EXPLORE_RESULT_VERSION)),
        )


class ExploreRuntime(Protocol):
    async def materialize(
        self,
        *,
        archive: RepositoryArchive,
        destination: Path,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> None: ...

    async def inspect(
        self,
        *,
        snapshot: ReadOnlySnapshot,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> ExploreInspection: ...


class ExploreService:
    def __init__(
        self,
        *,
        signer: JobSpecSigner,
        policy: SandboxPolicy,
        runtime: ExploreRuntime,
    ) -> None:
        self.signer = signer
        self.policy = policy
        self.runtime = runtime

    async def run(
        self,
        signed_spec: SignedJobSpec,
        archive: RepositoryArchive,
    ) -> ExploreResult:
        try:
            spec = self.signer.verify(
                signed_spec,
                expected_policy=self.policy,
            )
        except JobSpecSignatureError as exc:
            raise ExploreError(str(exc)) from exc
        if spec.stage is not SandboxStage.EXPLORE:
            raise ExploreError("Explore requires an Explore JobSpec")
        if (
            archive.repository_full_name != spec.repository_full_name
            or archive.base_commit_sha != spec.base_commit_sha
            or archive.archive_hash != spec.repository_archive_hash
        ):
            raise ExploreError(
                "Repository archive does not match the signed JobSpec"
            )
        archive.verify()
        with tempfile.TemporaryDirectory(
            prefix="contribos-explore-"
        ) as temporary:
            root = Path(temporary)
            destination = root / "repository"
            destination.mkdir(mode=0o777)
            await self.runtime.materialize(
                archive=archive,
                destination=destination,
                image_digest=spec.runner_image_digest,
                policy=self.policy,
            )
            archive.verify()
            snapshot = ReadOnlySnapshot.capture(
                snapshot_id=f"repository:{spec.base_commit_sha}",
                source=destination,
                allowed_root=root,
            )
            inspection = await self.runtime.inspect(
                snapshot=snapshot,
                image_digest=spec.runner_image_digest,
                policy=self.policy,
            )
            snapshot.verify()
            archive.verify()
            if (
                inspection.file_count != snapshot.file_count
                or inspection.total_bytes != snapshot.total_bytes
            ):
                raise ExploreError(
                    "Explore inspection does not match the frozen snapshot"
                )
            return ExploreResult.create(
                spec=signed_spec,
                snapshot=snapshot,
                inspection=inspection,
            )


class DockerExploreRuntime:
    def __init__(
        self,
        *,
        docker_environment: Mapping[str, str],
        docker_executable: str = "docker",
        max_output_bytes: int = MAX_EXPLORE_OUTPUT_BYTES,
    ) -> None:
        unexpected = sorted(set(docker_environment) - _ALLOWED_DOCKER_ENV)
        if unexpected:
            raise ValueError(
                "Explore Docker environment contains disallowed variables: "
                + ", ".join(unexpected)
            )
        if not docker_executable or os.path.sep in docker_executable:
            raise ValueError("Explore Docker executable is invalid")
        if max_output_bytes < 1 or max_output_bytes > MAX_EXPLORE_OUTPUT_BYTES:
            raise ValueError("Explore output limit is invalid")
        self.docker_environment = dict(docker_environment)
        self.docker_executable = docker_executable
        self.max_output_bytes = max_output_bytes

    def build_materialize_argv(
        self,
        *,
        archive: RepositoryArchive,
        destination: Path,
        image_digest: str,
        policy: SandboxPolicy,
        container_name: str,
    ) -> tuple[str, ...]:
        _container_inputs(
            image_digest=image_digest,
            paths=(archive.path, destination),
            container_name=container_name,
        )
        uid = os.getuid()
        gid = os.getgid()
        if uid == 0 or gid == 0:
            raise ExploreError(
                "Repository materialization requires a non-root Worker identity"
            )
        return (
            *self._base_argv(
                image_digest=image_digest,
                policy=policy,
                container_name=container_name,
                user=f"{uid}:{gid}",
            ),
            "--mount",
            (
                f"type=bind,src={archive.path},dst=/input/repository.tar,"
                "readonly,bind-propagation=rprivate"
            ),
            "--mount",
            (
                f"type=bind,src={destination},dst=/output,"
                "bind-propagation=rprivate"
            ),
            "--entrypoint",
            "python3",
            image_digest,
            "-I",
            "-c",
            _MATERIALIZE_SCRIPT,
            archive.repository_full_name.rsplit("/", 1)[1],
            archive.base_commit_sha,
        )

    def build_inspect_argv(
        self,
        *,
        snapshot: ReadOnlySnapshot,
        image_digest: str,
        policy: SandboxPolicy,
        container_name: str,
    ) -> tuple[str, ...]:
        _container_inputs(
            image_digest=image_digest,
            paths=(snapshot.source,),
            container_name=container_name,
        )
        return (
            *self._base_argv(
                image_digest=image_digest,
                policy=policy,
                container_name=container_name,
                user=f"{policy.run_as_uid}:{policy.run_as_gid}",
            ),
            "--workdir",
            "/workspace",
            "--mount",
            (
                f"type=bind,src={snapshot.source},dst=/workspace,"
                "readonly,bind-propagation=rprivate"
            ),
            "--entrypoint",
            "python3",
            image_digest,
            "-I",
            "-c",
            _INSPECT_SCRIPT,
        )

    async def materialize(
        self,
        *,
        archive: RepositoryArchive,
        destination: Path,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> None:
        name = f"contribos-explore-materialize-{uuid4().hex}"
        stdout = await self._run(
            self.build_materialize_argv(
                archive=archive,
                destination=destination,
                image_digest=image_digest,
                policy=policy,
                container_name=name,
            ),
            container_name=name,
            timeout_seconds=policy.timeout_seconds,
        )
        if stdout.strip() != b"sandbox-materialize-v1":
            raise ExploreError(
                "Repository materializer returned an invalid response"
            )

    async def inspect(
        self,
        *,
        snapshot: ReadOnlySnapshot,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> ExploreInspection:
        name = f"contribos-explore-inspect-{uuid4().hex}"
        stdout = await self._run(
            self.build_inspect_argv(
                snapshot=snapshot,
                image_digest=image_digest,
                policy=policy,
                container_name=name,
            ),
            container_name=name,
            timeout_seconds=policy.timeout_seconds,
        )
        try:
            raw = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExploreError("Explore returned invalid JSON") from exc
        return ExploreInspection.from_wire(raw)

    def _base_argv(
        self,
        *,
        image_digest: str,
        policy: SandboxPolicy,
        container_name: str,
        user: str,
    ) -> tuple[str, ...]:
        return (
            self.docker_executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            policy.network_mode,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(policy.pids_limit),
            "--memory",
            str(policy.memory_bytes),
            "--memory-swap",
            str(policy.memory_bytes),
            "--cpus",
            str(policy.cpu_limit),
            "--user",
            user,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "LANG=C.UTF-8",
            *docker_git_safety_argv(),
        )

    async def _run(
        self,
        argv: Sequence[str],
        *,
        container_name: str,
        timeout_seconds: int,
    ) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.docker_environment,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            await self._force_remove(container_name)
            raise ExploreError("Explore container timed out") from exc
        if (
            process.returncode != 0
            or stderr
            or len(stdout) > self.max_output_bytes
        ):
            raise ExploreError(
                "Explore container failed with a redacted error"
            )
        return stdout

    async def _force_remove(self, container_name: str) -> None:
        cleanup = await asyncio.create_subprocess_exec(
            self.docker_executable,
            "rm",
            "--force",
            container_name,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.docker_environment,
        )
        try:
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        except TimeoutError:
            cleanup.kill()
            await cleanup.wait()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _repository(value: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise ValueError("Repository full name is invalid")
    ensure_no_sensitive_data(value, context="repository full name")
    return value


def _base_sha(value: str) -> str:
    if not isinstance(value, str) or not _BASE_SHA.fullmatch(value):
        raise ValueError("Repository base commit SHA is invalid")
    return value


def _path_tuple(
    value: object,
    *,
    name: str,
    maximum: int,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ExploreError(f"Explore {name} are invalid")
    try:
        result = tuple(value)
    except TypeError as exc:
        raise ExploreError(f"Explore {name} are invalid") from exc
    if (
        len(result) > maximum
        or len(result) != len(set(result))
        or tuple(sorted(result)) != result
    ):
        raise ExploreError(f"Explore {name} are invalid")
    for item in result:
        if not isinstance(item, str) or not item or len(item) > 1_000:
            raise ExploreError(f"Explore {name} are invalid")
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise ExploreError(f"Explore {name} are invalid")
    return result


def _container_inputs(
    *,
    image_digest: str,
    paths: Sequence[Path],
    container_name: str,
) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise ValueError("Explore image must be digest-pinned")
    if not re.fullmatch(
        r"contribos-explore-(?:materialize|inspect)-[0-9a-f]{32}",
        container_name,
    ):
        raise ValueError("Explore container name is invalid")
    if any("," in str(path) or not path.is_absolute() for path in paths):
        raise ValueError("Explore mount path is invalid")


_MATERIALIZE_SCRIPT = r"""
import os
import posixpath
import sys
import tarfile
from pathlib import PurePosixPath

archive = "/input/repository.tar"
output = "/output"
repository_name = sys.argv[1]
base_sha = sys.argv[2]
expected_root = f"{repository_name}-{base_sha}"
denied = {
    ".env", ".git", ".netrc", ".npmrc", ".pypirc", "auth.json",
    "credentials", "id_dsa", "id_ed25519", "id_rsa",
}

with tarfile.open(archive, mode="r:*") as bundle:
    members = bundle.getmembers()
    if not members or len(members) > 20000:
        raise SystemExit(20)
    paths = set()
    total = 0
    validated = []
    roots = set()
    for member in members:
        try:
            member.name.encode("utf-8")
        except UnicodeEncodeError:
            raise SystemExit(21)
        path = PurePosixPath(member.name)
        if (
            not member.name
            or len(member.name) > 1000
            or path.is_absolute()
            or ".." in path.parts
            or "\x00" in member.name
        ):
            raise SystemExit(22)
        roots.add(path.parts[0])
        relative = PurePosixPath(*path.parts[1:])
        if str(relative) == ".":
            continue
        if any(part.lower() in denied for part in relative.parts):
            raise SystemExit(23)
        normalized = str(relative)
        if normalized in paths:
            raise SystemExit(24)
        paths.add(normalized)
        if member.isdir():
            kind = "directory"
        elif member.isfile():
            kind = "file"
            total += member.size
            if total > 536870912:
                raise SystemExit(25)
        elif member.issym():
            kind = "symlink"
            target = member.linkname
            if (
                not target
                or len(target) > 1000
                or PurePosixPath(target).is_absolute()
            ):
                raise SystemExit(26)
            resolved = posixpath.normpath(
                posixpath.join(str(relative.parent), target)
            )
            if resolved == ".." or resolved.startswith("../"):
                raise SystemExit(27)
        else:
            raise SystemExit(28)
        validated.append((relative, member, kind))
    if roots != {expected_root}:
        raise SystemExit(29)
    for relative, member, kind in sorted(
        validated,
        key=lambda item: (len(item[0].parts), str(item[0])),
    ):
        target = os.path.join(output, *relative.parts)
        parent = os.path.dirname(target)
        os.makedirs(parent, mode=0o755, exist_ok=True)
        if kind == "directory":
            os.makedirs(target, mode=0o755, exist_ok=False)
        elif kind == "symlink":
            os.symlink(member.linkname, target)
        else:
            source = bundle.extractfile(member)
            if source is None:
                raise SystemExit(30)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(
                target,
                flags,
                0o755 if member.mode & 0o111 else 0o644,
            )
            with source, os.fdopen(descriptor, "wb") as destination:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(1048576, remaining))
                    if not chunk:
                        raise SystemExit(31)
                    destination.write(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise SystemExit(32)
print("sandbox-materialize-v1")
""".strip()


_INSPECT_SCRIPT = r"""
import hashlib
import json
import os
import stat

root = "/workspace"
entries = []
total = 0
manifest_names = {
    "cargo.toml", "go.mod", "package.json", "pnpm-lock.yaml",
    "poetry.lock", "pyproject.toml", "requirements.txt",
    "setup.cfg", "setup.py", "yarn.lock",
}
for current, directories, files in os.walk(root, topdown=True, followlinks=False):
    directories.sort()
    files.sort()
    for name in list(directories):
        absolute = os.path.join(current, name)
        if os.path.islink(absolute):
            directories.remove(name)
            relative = os.path.relpath(absolute, root)
            entries.append({
                "path": relative,
                "type": "symlink",
                "target": os.readlink(absolute),
            })
    for name in files:
        absolute = os.path.join(current, name)
        relative = os.path.relpath(absolute, root)
        metadata = os.lstat(absolute)
        if stat.S_ISLNK(metadata.st_mode):
            entries.append({
                "path": relative,
                "type": "symlink",
                "target": os.readlink(absolute),
            })
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(40)
        digest = hashlib.sha256()
        with open(absolute, "rb") as handle:
            while True:
                chunk = handle.read(1048576)
                if not chunk:
                    break
                digest.update(chunk)
        total += metadata.st_size
        entries.append({
            "path": relative,
            "type": "file",
            "size": metadata.st_size,
            "sha256": digest.hexdigest(),
            "executable": bool(metadata.st_mode & 0o111),
        })
entries.sort(key=lambda item: item["path"])
encoded = json.dumps(
    entries,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
paths = [item["path"] for item in entries]
manifests = sorted(
    path for path in paths
    if os.path.basename(path).lower() in manifest_names
)
result = {
    "inventory_hash": hashlib.sha256(encoded).hexdigest(),
    "file_count": len(entries),
    "total_bytes": total,
    "sample_paths": paths[:200],
    "manifest_paths": manifests[:200],
}
print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
""".strip()
