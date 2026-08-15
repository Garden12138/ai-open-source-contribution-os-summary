"""Docker runtime implementation owned only by the Sandbox Worker domain."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import uuid4

from app.provenance import canonical_json, content_hash
from app.providers.codex_cli import CodexExecInvocation, CodexExecResult
from app.providers.contracts import ProviderRunError
from app.providers.gateway import GatewayTaskCredentialBroker


CONTAINER_POLICY_VERSION = "codex-readonly-container-v1"
GATEWAY_CONTAINER_POLICY_VERSION = "codex-gateway-container-v1"
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_SCHEMA_PATH = "/run/contribos/output-schema.json"
CONTAINER_CODEX_HOME = "/run/codex"
MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
_PINNED_IMAGE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[^\\s@]+@sha256:[0-9a-f]{64})$"
)
_INTERNAL_GATEWAY_NETWORK = re.compile(
    r"^contribos-model-gateway-[a-z0-9-]{1,48}$"
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
_DENIED_SNAPSHOT_PARTS = frozenset(
    {
        ".env",
        ".git",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "auth.json",
        "credentials",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
    }
)


@dataclass(frozen=True, slots=True)
class ReadOnlySnapshot:
    snapshot_id: str
    source: Path
    manifest_hash: str
    file_count: int
    total_bytes: int

    @classmethod
    def capture(
        cls,
        *,
        snapshot_id: str,
        source: Path,
        allowed_root: Path,
    ) -> "ReadOnlySnapshot":
        if not snapshot_id.strip() or len(snapshot_id) > 128:
            raise ValueError("Container snapshot ID is invalid")
        if source.is_symlink():
            raise ValueError("Container snapshot root cannot be a symlink")
        resolved_source = source.resolve()
        resolved_root = allowed_root.resolve()
        if (
            not resolved_source.is_dir()
            or not resolved_source.is_relative_to(resolved_root)
            or "," in str(resolved_source)
        ):
            raise ValueError(
                "Container snapshot must be a mountable directory "
                "inside its allowed root"
            )
        manifest, total_bytes = _snapshot_manifest(resolved_source)
        return cls(
            snapshot_id=snapshot_id,
            source=resolved_source,
            manifest_hash=content_hash(manifest),
            file_count=len(manifest),
            total_bytes=total_bytes,
        )

    def verify(self) -> None:
        manifest, total_bytes = _snapshot_manifest(self.source)
        if (
            len(manifest) != self.file_count
            or total_bytes != self.total_bytes
            or content_hash(manifest) != self.manifest_hash
        ):
            raise ProviderRunError(
                "codex_snapshot_changed",
                "Read-only Codex snapshot changed after it was frozen",
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class ContainerIsolationPolicy:
    version: str = CONTAINER_POLICY_VERSION
    network: str = "none"
    user: str = "65532:65532"
    pids_limit: int = 128
    memory_bytes: int = 1_073_741_824
    cpus: float = 1.0
    tmpfs_bytes: int = 67_108_864

    def __post_init__(self) -> None:
        if self.version not in {
            CONTAINER_POLICY_VERSION,
            GATEWAY_CONTAINER_POLICY_VERSION,
        }:
            raise ValueError("Container policy version is unsupported")
        if (
            self.version == CONTAINER_POLICY_VERSION
            and self.network != "none"
        ) or (
            self.version == GATEWAY_CONTAINER_POLICY_VERSION
            and not _INTERNAL_GATEWAY_NETWORK.fullmatch(self.network)
        ):
            raise ValueError("Container network does not match its policy")
        if (
            self.pids_limit < 1
            or self.memory_bytes < 16 * 1024 * 1024
            or self.cpus <= 0
            or self.tmpfs_bytes < 1024 * 1024
        ):
            raise ValueError("Container resource limits are invalid")

    def build_argv(
        self,
        *,
        docker_executable: str,
        image: str,
        snapshot: ReadOnlySnapshot,
        schema_path: Path | None,
        container_name: str,
        entrypoint: str,
        command: Sequence[str],
        inherit_task_token: bool = False,
    ) -> tuple[str, ...]:
        if not _PINNED_IMAGE.fullmatch(image):
            raise ValueError("Codex container image must be digest-pinned")
        if not re.fullmatch(
            r"contribos-(?:codex|model-gateway)-[a-z0-9-]{1,80}",
            container_name,
        ):
            raise ValueError("Codex container name is invalid")
        snapshot_mount = (
            "type=bind,"
            f"src={snapshot.source},"
            f"dst={CONTAINER_WORKSPACE},"
            "readonly,bind-propagation=rprivate"
        )
        argv: list[str] = [
            docker_executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--init",
            "--name",
            container_name,
            "--network",
            self.network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            str(self.memory_bytes),
            "--memory-swap",
            str(self.memory_bytes),
            "--cpus",
            str(self.cpus),
            "--user",
            self.user,
            "--workdir",
            CONTAINER_WORKSPACE,
            "--mount",
            snapshot_mount,
            "--tmpfs",
            (
                f"/tmp:rw,noexec,nosuid,nodev,size={self.tmpfs_bytes},"
                "mode=1777"
            ),
            "--tmpfs",
            (
                f"{CONTAINER_CODEX_HOME}:rw,noexec,nosuid,nodev,"
                f"size={self.tmpfs_bytes},mode=700,uid=65532,gid=65532"
            ),
            "--env",
            f"CODEX_HOME={CONTAINER_CODEX_HOME}",
            "--env",
            "TMPDIR=/tmp",
        ]
        if schema_path is not None:
            resolved_schema = schema_path.resolve()
            if not resolved_schema.is_file() or "," in str(resolved_schema):
                raise ValueError("Codex schema path is not mountable")
            argv.extend(
                (
                    "--mount",
                    (
                        "type=bind,"
                        f"src={resolved_schema},"
                        f"dst={CONTAINER_SCHEMA_PATH},"
                        "readonly,bind-propagation=rprivate"
                    ),
                )
            )
        if inherit_task_token:
            argv.extend(("--env", "CODEX_API_KEY"))
        argv.extend(("--entrypoint", entrypoint, image, *command))
        return tuple(argv)


class DockerCodexExecRunner:
    """Run Codex only through a hardened, digest-pinned container boundary."""

    def __init__(
        self,
        *,
        snapshot: ReadOnlySnapshot,
        image: str,
        docker_environment: Mapping[str, str],
        docker_executable: str = "docker",
        policy: ContainerIsolationPolicy | None = None,
        credential_broker: GatewayTaskCredentialBroker | None = None,
        timeout_seconds: int = 600,
        max_prompt_bytes: int = 1_000_000,
        max_output_bytes: int = 4_000_000,
    ) -> None:
        if not _PINNED_IMAGE.fullmatch(image):
            raise ValueError("Codex container image must be digest-pinned")
        unexpected = sorted(set(docker_environment) - _ALLOWED_DOCKER_ENV)
        if unexpected:
            raise ValueError(
                "Docker supervisor environment contains disallowed variables: "
                + ", ".join(unexpected)
            )
        if timeout_seconds < 1:
            raise ValueError("Codex container timeout must be at least 1 second")
        if max_prompt_bytes < 1 or max_output_bytes < 1:
            raise ValueError("Codex container byte limits must be positive")
        self.snapshot = snapshot
        self.image = image
        self.docker_environment = dict(docker_environment)
        self.docker_executable = docker_executable
        self.policy = policy or ContainerIsolationPolicy()
        if credential_broker is None:
            if (
                self.policy.version != CONTAINER_POLICY_VERSION
                or self.policy.network != "none"
            ):
                raise ValueError(
                    "Gateway container policy requires a credential broker"
                )
        elif (
            self.policy.version != GATEWAY_CONTAINER_POLICY_VERSION
            or self.policy.network != credential_broker.network
        ):
            raise ValueError(
                "Gateway credential broker does not match container policy"
            )
        self.credential_broker = credential_broker
        self.timeout_seconds = timeout_seconds
        self.max_prompt_bytes = max_prompt_bytes
        self.max_output_bytes = max_output_bytes

    def build_argv(
        self,
        *,
        model: str,
        schema_path: Path,
        container_name: str,
        gateway_base_url: str | None = None,
    ) -> tuple[str, ...]:
        gateway_config: tuple[str, ...] = ()
        if gateway_base_url is not None:
            gateway_config = (
                "--config",
                f'openai_base_url="{gateway_base_url}"',
            )
        return self.policy.build_argv(
            docker_executable=self.docker_executable,
            image=self.image,
            snapshot=self.snapshot,
            schema_path=schema_path,
            container_name=container_name,
            entrypoint="codex",
            command=(
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                model,
                *gateway_config,
                "--json",
                "--output-schema",
                CONTAINER_SCHEMA_PATH,
                "--color",
                "never",
                "--cd",
                CONTAINER_WORKSPACE,
                "-",
            ),
            inherit_task_token=gateway_base_url is not None,
        )

    async def run(self, invocation: CodexExecInvocation) -> CodexExecResult:
        if invocation.snapshot_id != self.snapshot.snapshot_id:
            raise ProviderRunError(
                "codex_snapshot_mismatch",
                "Codex invocation does not match the mounted snapshot",
                retryable=False,
            )
        self.snapshot.verify()
        prompt_bytes = invocation.prompt.encode("utf-8")
        if len(prompt_bytes) > self.max_prompt_bytes:
            raise ProviderRunError(
                "codex_prompt_limit",
                "Codex prompt exceeded the configured byte limit",
                retryable=False,
            )
        started = monotonic()
        container_name = f"contribos-codex-{uuid4().hex}"
        credential = (
            None
            if self.credential_broker is None
            else self.credential_broker.issue(invocation)
        )
        process_environment = dict(self.docker_environment)
        if credential is not None:
            await self._verify_internal_network(credential.network)
            process_environment["CODEX_API_KEY"] = credential.token
        with tempfile.TemporaryDirectory(
            prefix="contribos-container-schema-"
        ) as temp:
            schema_path = Path(temp) / "output-schema.json"
            schema_path.write_text(
                canonical_json(invocation.output_schema),
                encoding="utf-8",
            )
            schema_path.chmod(0o644)
            argv = self.build_argv(
                model=invocation.model,
                schema_path=schema_path,
                container_name=container_name,
                gateway_base_url=(
                    None if credential is None else credential.base_url
                ),
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    env=process_environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ProviderRunError(
                    "codex_container_start_failed",
                    "Codex container could not be started",
                    retryable=False,
                ) from exc
            try:
                if process.stdin is None or process.stdout is None:
                    raise ProviderRunError(
                        "codex_container_pipe_failure",
                        "Codex container pipes were not available",
                        retryable=False,
                    )
                process.stdin.write(prompt_bytes)
                await process.stdin.drain()
                process.stdin.close()
                async with asyncio.timeout(self.timeout_seconds):
                    stdout = await self._read_bounded(process.stdout)
                    return_code = await process.wait()
                self.snapshot.verify()
            except TimeoutError as exc:
                await self._terminate(process, container_name)
                raise ProviderRunError(
                    "codex_container_timeout",
                    "Codex container exceeded its execution timeout",
                    retryable=True,
                ) from exc
            except ProviderRunError:
                await self._terminate(process, container_name)
                raise
            except Exception as exc:
                await self._terminate(process, container_name)
                raise ProviderRunError(
                    "codex_container_io_failure",
                    "Codex container communication failed",
                    retryable=True,
                ) from exc
        return CodexExecResult(
            stdout=stdout.decode("utf-8", errors="strict"),
            return_code=return_code,
            duration_ms=max(0, int((monotonic() - started) * 1000)),
        )

    async def _read_bounded(self, stream: asyncio.StreamReader) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > self.max_output_bytes:
                raise ProviderRunError(
                    "codex_container_output_limit",
                    "Codex container exceeded its output byte limit",
                    retryable=False,
                )
            chunks.append(chunk)

    async def _verify_internal_network(self, network: str) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.docker_executable,
                "network",
                "inspect",
                network,
                "--format",
                "{{.Internal}}",
                env=self.docker_environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(10):
                stdout, _ = await process.communicate()
        except (OSError, TimeoutError) as exc:
            raise ProviderRunError(
                "codex_gateway_network_unavailable",
                "Internal model gateway network is unavailable",
                retryable=True,
            ) from exc
        if process.returncode != 0 or stdout.strip() != b"true":
            raise ProviderRunError(
                "codex_gateway_network_unsafe",
                "Model gateway network is not internal",
                retryable=False,
            )

    async def _terminate(
        self,
        process: asyncio.subprocess.Process,
        container_name: str,
    ) -> None:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
            await process.wait()
        cleanup = await asyncio.create_subprocess_exec(
            self.docker_executable,
            "rm",
            "--force",
            container_name,
            env=self.docker_environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await cleanup.wait()


def _snapshot_manifest(source: Path) -> tuple[list[dict[str, object]], int]:
    manifest: list[dict[str, object]] = []
    total_bytes = 0
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source)
        if any(part.lower() in _DENIED_SNAPSHOT_PARTS for part in relative.parts):
            raise ValueError(
                "Container snapshot contains a denied credential or metadata path"
            )
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or not (
                path.parent / target
            ).resolve(strict=False).is_relative_to(source):
                raise ValueError("Container snapshot symlink escapes its root")
            manifest.append(
                {
                    "path": relative.as_posix(),
                    "kind": "symlink",
                    "target": target,
                }
            )
        elif path.is_dir():
            continue
        elif path.is_file():
            size = path.stat().st_size
            total_bytes += size
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise ValueError("Container snapshot exceeds its byte limit")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            manifest.append(
                {
                    "path": relative.as_posix(),
                    "kind": "file",
                    "size": size,
                    "sha256": digest.hexdigest(),
                }
            )
        else:
            raise ValueError("Container snapshot contains a special file")
        if len(manifest) > MAX_SNAPSHOT_FILES:
            raise ValueError("Container snapshot exceeds its file limit")
    return manifest, total_bytes
