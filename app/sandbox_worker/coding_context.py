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
from typing import TYPE_CHECKING
from uuid import uuid4

from app.provenance import canonical_json, content_hash
from app.sandbox_worker.git_safety import docker_git_safety_argv
from app.sandbox_worker.specs import SandboxPolicy
from app.security import ensure_no_sensitive_data

if TYPE_CHECKING:
    from app.sandbox_worker.explore import RepositoryArchive


CODING_CONTEXT_VERSION = "coding-context-v1"
MAX_CONTEXT_PATHS = 64
MAX_CONTEXT_FILE_BYTES = 256_000
MAX_CONTEXT_BYTES = 1_500_000
_HASH = re.compile(r"^[0-9a-f]{64}$")
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


class CodingContextError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CodingContextEntry:
    path: str
    prior_hash: str | None
    content: str | None
    executable: bool | None

    def __post_init__(self) -> None:
        _safe_path(self.path)
        if self.content is None:
            if self.prior_hash is not None or self.executable is not None:
                raise ValueError("Missing coding context entry has file metadata")
            return
        if self.prior_hash is None or not _HASH.fullmatch(self.prior_hash):
            raise ValueError("Coding context file hash is invalid")
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.prior_hash:
            raise ValueError("Coding context file hash does not match content")
        if not isinstance(self.executable, bool):
            raise ValueError("Coding context executable flag is invalid")
        if len(self.content.encode("utf-8")) > MAX_CONTEXT_FILE_BYTES:
            raise ValueError("Coding context file exceeds its byte limit")

    def to_wire(self) -> dict[str, object]:
        return {
            "path": self.path,
            "prior_hash": self.prior_hash,
            "content": self.content,
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class CodingContext:
    execution_attempt_id: str
    plan_version_id: str
    plan_content_hash: str
    plan_record_hash: str
    base_commit_sha: str
    repository_archive_hash: str
    explore_result_hash: str
    entries: tuple[CodingContextEntry, ...]
    context_hash: str
    version: str = CODING_CONTEXT_VERSION

    def __post_init__(self) -> None:
        if self.version != CODING_CONTEXT_VERSION:
            raise ValueError("Coding context version is unsupported")
        entries = tuple(self.entries)
        if (
            not entries
            or len(entries) > MAX_CONTEXT_PATHS
            or tuple(sorted(item.path for item in entries))
            != tuple(item.path for item in entries)
            or len({item.path for item in entries}) != len(entries)
        ):
            raise ValueError("Coding context entries are invalid")
        object.__setattr__(self, "entries", entries)
        for value in (
            self.plan_content_hash,
            self.plan_record_hash,
            self.repository_archive_hash,
            self.explore_result_hash,
            self.context_hash,
        ):
            if not _HASH.fullmatch(value):
                raise ValueError("Coding context hash is invalid")
        if self.context_hash != content_hash(self.hash_payload()):
            raise ValueError("Coding context hash does not match")
        encoded = canonical_json(self.to_wire()).encode("utf-8")
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ValueError("Coding context exceeds its byte limit")
        ensure_no_sensitive_data(self.to_wire(), context="coding context")

    @classmethod
    def create(
        cls,
        *,
        execution_attempt_id: str,
        plan_version_id: str,
        plan_content_hash: str,
        plan_record_hash: str,
        base_commit_sha: str,
        repository_archive_hash: str,
        explore_result_hash: str,
        entries: tuple[CodingContextEntry, ...],
    ) -> "CodingContext":
        payload = {
            "version": CODING_CONTEXT_VERSION,
            "execution_attempt_id": execution_attempt_id,
            "plan_version_id": plan_version_id,
            "plan_content_hash": plan_content_hash,
            "plan_record_hash": plan_record_hash,
            "base_commit_sha": base_commit_sha,
            "repository_archive_hash": repository_archive_hash,
            "explore_result_hash": explore_result_hash,
            "entries": [item.to_wire() for item in entries],
        }
        return cls(
            execution_attempt_id=execution_attempt_id,
            plan_version_id=plan_version_id,
            plan_content_hash=plan_content_hash,
            plan_record_hash=plan_record_hash,
            base_commit_sha=base_commit_sha,
            repository_archive_hash=repository_archive_hash,
            explore_result_hash=explore_result_hash,
            entries=entries,
            context_hash=content_hash(payload),
        )

    def hash_payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "execution_attempt_id": self.execution_attempt_id,
            "plan_version_id": self.plan_version_id,
            "plan_content_hash": self.plan_content_hash,
            "plan_record_hash": self.plan_record_hash,
            "base_commit_sha": self.base_commit_sha,
            "repository_archive_hash": self.repository_archive_hash,
            "explore_result_hash": self.explore_result_hash,
            "entries": [item.to_wire() for item in self.entries],
        }

    def to_wire(self) -> dict[str, object]:
        return {**self.hash_payload(), "context_hash": self.context_hash}

    def to_bytes(self) -> bytes:
        return (canonical_json(self.to_wire()) + "\n").encode("utf-8")

    @classmethod
    def from_wire(cls, value: object) -> "CodingContext":
        if not isinstance(value, dict):
            raise ValueError("Coding context is invalid")
        entries = value.get("entries")
        if not isinstance(entries, list):
            raise ValueError("Coding context entries are invalid")
        return cls(
            version=str(value.get("version", "")),
            execution_attempt_id=str(value.get("execution_attempt_id", "")),
            plan_version_id=str(value.get("plan_version_id", "")),
            plan_content_hash=str(value.get("plan_content_hash", "")),
            plan_record_hash=str(value.get("plan_record_hash", "")),
            base_commit_sha=str(value.get("base_commit_sha", "")),
            repository_archive_hash=str(value.get("repository_archive_hash", "")),
            explore_result_hash=str(value.get("explore_result_hash", "")),
            entries=tuple(
                CodingContextEntry(
                    path=str(item.get("path", "")),
                    prior_hash=item.get("prior_hash"),
                    content=item.get("content"),
                    executable=item.get("executable"),
                )
                for item in entries
                if isinstance(item, dict)
            ),
            context_hash=str(value.get("context_hash", "")),
        )


class DockerCodingContextRuntime:
    def __init__(
        self,
        *,
        docker_environment: Mapping[str, str],
        docker_executable: str = "docker",
    ) -> None:
        unexpected = sorted(set(docker_environment) - _ALLOWED_DOCKER_ENV)
        if unexpected:
            raise ValueError(
                "Coding context Docker environment contains disallowed variables: "
                + ", ".join(unexpected)
            )
        self.docker_environment = dict(docker_environment)
        self.docker_executable = docker_executable

    async def capture(
        self,
        *,
        archive: RepositoryArchive,
        paths: tuple[str, ...],
        image_digest: str,
        policy: SandboxPolicy,
    ) -> tuple[CodingContextEntry, ...]:
        normalized = tuple(sorted({_safe_path(path) for path in paths}))
        if not normalized or len(normalized) > MAX_CONTEXT_PATHS:
            raise CodingContextError("Coding context path set is invalid")
        archive.verify()
        request = (canonical_json({"paths": list(normalized)}) + "\n").encode()
        with tempfile.TemporaryDirectory(prefix="contribos-coding-context-") as tmp:
            request_path = Path(tmp) / "request.json"
            request_path.write_bytes(request)
            name = f"contribos-coding-context-{uuid4().hex}"
            argv = self._argv(
                archive=archive,
                request_path=request_path,
                image_digest=image_digest,
                policy=policy,
                container_name=name,
            )
            stdout = await self._run(
                argv,
                container_name=name,
                timeout_seconds=policy.timeout_seconds,
            )
        archive.verify()
        try:
            raw = json.loads(stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CodingContextError("Coding context output is invalid") from exc
        if not isinstance(raw, list) or len(raw) != len(normalized):
            raise CodingContextError("Coding context output is incomplete")
        entries = tuple(
            CodingContextEntry(
                path=str(item.get("path", "")),
                prior_hash=item.get("prior_hash"),
                content=item.get("content"),
                executable=item.get("executable"),
            )
            for item in raw
            if isinstance(item, dict)
        )
        if tuple(item.path for item in entries) != normalized:
            raise CodingContextError("Coding context paths do not match request")
        return entries

    def _argv(
        self,
        *,
        archive: RepositoryArchive,
        request_path: Path,
        image_digest: str,
        policy: SandboxPolicy,
        container_name: str,
    ) -> tuple[str, ...]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
            raise ValueError("Coding context image must be digest-pinned")
        return (
            self.docker_executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
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
            f"{policy.run_as_uid}:{policy.run_as_gid}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
            "--mount",
            f"type=bind,src={archive.path},dst=/input/repository.tar,readonly",
            "--mount",
            f"type=bind,src={request_path},dst=/input/request.json,readonly",
            "--env",
            "HOME=/tmp",
            *docker_git_safety_argv(),
            "--entrypoint",
            "python3",
            image_digest,
            "-I",
            "-c",
            _CAPTURE_SCRIPT,
            archive.repository_full_name.rsplit("/", 1)[1],
            archive.base_commit_sha,
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
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            await self._cleanup(container_name)
            raise CodingContextError("Coding context container timed out") from exc
        if process.returncode != 0 or stderr or len(stdout) > MAX_CONTEXT_BYTES:
            raise CodingContextError(
                "Coding context container failed with a redacted error"
            )
        return stdout

    async def _cleanup(self, name: str) -> None:
        process = await asyncio.create_subprocess_exec(
            self.docker_executable,
            "rm",
            "--force",
            name,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.docker_environment,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()


def _safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("Coding context path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise ValueError("Coding context path is invalid")
    return str(path)


_CAPTURE_SCRIPT = r'''
import hashlib
import json
import stat
import sys
import tarfile
from pathlib import PurePosixPath

repo = sys.argv[1]
sha = sys.argv[2]
root = f"{repo}-{sha}"
request = json.load(open("/input/request.json", encoding="utf-8"))
paths = request.get("paths")
if not isinstance(paths, list) or not paths or len(paths) > 64:
    raise SystemExit(10)
wanted = set(paths)
found = {}
with tarfile.open("/input/repository.tar", mode="r:*") as bundle:
    members = bundle.getmembers()
    if not members or len(members) > 20000:
        raise SystemExit(11)
    for member in members:
        path = PurePosixPath(member.name)
        if not path.parts or path.parts[0] != root or ".." in path.parts:
            raise SystemExit(12)
        relative = str(PurePosixPath(*path.parts[1:]))
        if relative not in wanted:
            continue
        if not member.isfile() or member.issym() or member.islnk():
            raise SystemExit(13)
        if member.size > 256000:
            raise SystemExit(14)
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(15)
        data = source.read(member.size + 1)
        if len(data) != member.size:
            raise SystemExit(16)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise SystemExit(17)
        found[relative] = {
            "path": relative,
            "prior_hash": hashlib.sha256(data).hexdigest(),
            "content": content,
            "executable": bool(member.mode & 0o111),
        }
result = [
    found.get(path, {
        "path": path,
        "prior_hash": None,
        "content": None,
        "executable": None,
    })
    for path in paths
]
encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
if len(encoded.encode("utf-8")) > 1500000:
    raise SystemExit(18)
print(encoded)
'''.strip()
