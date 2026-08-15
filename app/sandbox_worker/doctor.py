"""Fail-closed local runtime diagnostics for Sandbox Worker prerequisites."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.sandbox_worker.specs import (
    DEFAULT_DISK_BYTES,
    DEFAULT_MEMORY_BYTES,
    SANDBOX_POLICY_VERSION,
)
from app.security import ensure_no_sensitive_data


SANDBOX_DOCTOR_VERSION = "sandbox-doctor-v1"
MINIMUM_DOCKER_VERSION = (24, 0, 0)
MINIMUM_DOCKER_API_VERSION = (1, 43)
MINIMUM_BUILDX_VERSION = (0, 12, 0)
MINIMUM_PYTHON_VERSION = (3, 11, 0)
MINIMUM_FREE_BYTES = DEFAULT_DISK_BYTES + DEFAULT_MEMORY_BYTES
MAX_COMMAND_OUTPUT_BYTES = 1_000_000
_PINNED_IMAGE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})$"
)
_DOCKER_ENVIRONMENT = frozenset(
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


@dataclass(frozen=True, slots=True)
class DoctorCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes = b""


class DoctorCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> DoctorCommandResult: ...


class SubprocessDoctorCommandRunner:
    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        source = os.environ if environment is None else environment
        self.environment = {
            name: value
            for name, value in source.items()
            if name in _DOCKER_ENVIRONMENT
        }

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> DoctorCommandResult:
        try:
            completed = subprocess.run(
                tuple(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return DoctorCommandResult(returncode=125, stdout=b"")
        if (
            len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
        ):
            return DoctorCommandResult(returncode=125, stdout=b"")
        return DoctorCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    code: str
    detail: str

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SandboxDoctorReport:
    host_system: str
    host_architecture: str
    checks: tuple[DoctorCheck, ...]
    version: str = SANDBOX_DOCTOR_VERSION

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def to_wire(self) -> dict[str, object]:
        value = {
            "version": self.version,
            "ok": self.ok,
            "host": {
                "system": self.host_system,
                "architecture": self.host_architecture,
            },
            "checks": [check.to_wire() for check in self.checks],
        }
        ensure_no_sensitive_data(value, context="Sandbox doctor report")
        return value


class SandboxDoctor:
    def __init__(
        self,
        *,
        runner_image: str,
        docker_executable: str = "docker",
        command_runner: DoctorCommandRunner | None = None,
        workspace_path: Path | None = None,
        host_system: str | None = None,
        host_architecture: str | None = None,
        python_version: tuple[int, int, int] | None = None,
        free_bytes: int | None = None,
    ) -> None:
        if not _PINNED_IMAGE.fullmatch(runner_image):
            raise ValueError("Doctor runner image must be digest-pinned")
        if (
            not docker_executable
            or os.path.sep in docker_executable
            or (os.path.altsep and os.path.altsep in docker_executable)
        ):
            raise ValueError("Doctor Docker executable is invalid")
        self.runner_image = runner_image
        self.docker_executable = docker_executable
        self.command_runner = command_runner or SubprocessDoctorCommandRunner()
        self.workspace_path = (workspace_path or Path.cwd()).resolve()
        self.host_system = host_system or platform.system()
        self.host_architecture = host_architecture or platform.machine()
        self.python_version = python_version or tuple(sys.version_info[:3])
        self.free_bytes = free_bytes

    def run(self) -> SandboxDoctorReport:
        checks: list[DoctorCheck] = []
        checks.append(self._host_platform_check())
        checks.append(self._python_version_check())
        checks.append(self._disk_check())

        version = self._json_command(
            (
                self.docker_executable,
                "version",
                "--format",
                "{{json .}}",
            )
        )
        if version is None:
            checks.append(
                _failed(
                    "docker_runtime",
                    "docker_unavailable",
                    "Docker client or engine is unavailable",
                )
            )
            checks.extend(self._unavailable_runtime_checks())
            return SandboxDoctorReport(
                host_system=self.host_system,
                host_architecture=self.host_architecture,
                checks=tuple(checks),
            )

        checks.append(
            _passed(
                "docker_runtime",
                "docker_available",
                "Docker client and engine are reachable",
            )
        )
        checks.append(self._docker_version_check(version))

        info = self._json_command(
            (
                self.docker_executable,
                "info",
                "--format",
                "{{json .}}",
            )
        )
        if info is None:
            checks.extend(
                (
                    _failed(
                        "engine_platform",
                        "docker_info_unavailable",
                        "Docker engine information is unavailable",
                    ),
                    _failed(
                        "policy_primitives",
                        "docker_info_unavailable",
                        "Docker policy primitives cannot be verified",
                    ),
                    _failed(
                        "network_none",
                        "docker_info_unavailable",
                        "Docker network isolation cannot be verified",
                    ),
                )
            )
        else:
            checks.append(self._engine_platform_check(info))
            checks.append(self._policy_primitives_check(info))
            checks.append(self._network_check(info))

        checks.append(self._buildx_check())
        image = self._json_command(
            (
                self.docker_executable,
                "image",
                "inspect",
                "--format",
                "{{json .}}",
                self.runner_image,
            )
        )
        if image is None:
            checks.append(
                _failed(
                    "runner_image",
                    "runner_image_unavailable",
                    "Digest-pinned Sandbox Runner image is unavailable",
                )
            )
            checks.append(
                _failed(
                    "policy_probe",
                    "runner_image_unavailable",
                    "Sandbox runtime policy probe cannot run",
                )
            )
        else:
            checks.append(self._runner_image_check(image, info))
            checks.append(self._policy_probe())
        return SandboxDoctorReport(
            host_system=self.host_system,
            host_architecture=self.host_architecture,
            checks=tuple(checks),
        )

    def _host_platform_check(self) -> DoctorCheck:
        normalized = self.host_architecture.lower()
        valid = (
            self.host_system == "Darwin"
            and normalized in {"arm64", "aarch64"}
        ) or (
            self.host_system == "Linux"
            and normalized in {"x86_64", "amd64"}
        )
        if valid:
            return _passed(
                "host_platform",
                "host_platform_supported",
                f"Supported host platform {self.host_system}/{normalized}",
            )
        return _failed(
            "host_platform",
            "host_platform_unsupported",
            f"Unsupported host platform {self.host_system}/{normalized}",
        )

    def _disk_check(self) -> DoctorCheck:
        try:
            available = (
                self.free_bytes
                if self.free_bytes is not None
                else shutil.disk_usage(self.workspace_path).free
            )
        except OSError:
            return _failed(
                "disk_capacity",
                "disk_capacity_unavailable",
                "Workspace free disk capacity is unavailable",
            )
        gib = available // (1024 * 1024 * 1024)
        if available >= MINIMUM_FREE_BYTES:
            return _passed(
                "disk_capacity",
                "disk_capacity_sufficient",
                f"Workspace has {gib} GiB free",
            )
        return _failed(
            "disk_capacity",
            "disk_capacity_insufficient",
            f"Workspace has {gib} GiB free; at least 6 GiB is required",
        )

    def _python_version_check(self) -> DoctorCheck:
        version = self.python_version
        rendered = ".".join(map(str, version))
        if version >= MINIMUM_PYTHON_VERSION:
            return _passed(
                "python_version",
                "python_version_supported",
                f"Python {rendered}",
            )
        return _failed(
            "python_version",
            "python_version_unsupported",
            f"Python {rendered}; Python 3.11+ is required",
        )

    def _docker_version_check(self, value: dict[str, object]) -> DoctorCheck:
        client = value.get("Client")
        server = value.get("Server")
        if not isinstance(client, dict) or not isinstance(server, dict):
            return _failed(
                "required_versions",
                "docker_version_invalid",
                "Docker version response is invalid",
            )
        client_version = _version_tuple(client.get("Version"), parts=3)
        server_version = _version_tuple(server.get("Version"), parts=3)
        api_version = _version_tuple(server.get("ApiVersion"), parts=2)
        if (
            client_version is None
            or server_version is None
            or api_version is None
            or client_version < MINIMUM_DOCKER_VERSION
            or server_version < MINIMUM_DOCKER_VERSION
            or api_version < MINIMUM_DOCKER_API_VERSION
        ):
            return _failed(
                "required_versions",
                "docker_version_unsupported",
                "Docker 24+ and engine API 1.43+ are required",
            )
        return _passed(
            "required_versions",
            "docker_version_supported",
            (
                f"Docker client {'.'.join(map(str, client_version))}, "
                f"engine {'.'.join(map(str, server_version))}, "
                f"API {'.'.join(map(str, api_version))}"
            ),
        )

    def _engine_platform_check(self, info: dict[str, object]) -> DoctorCheck:
        operating_system = info.get("OperatingSystem")
        architecture = str(info.get("Architecture", "")).lower()
        if self.host_system == "Darwin":
            ok = (
                isinstance(operating_system, str)
                and "orbstack" in operating_system.lower()
                and architecture in {"aarch64", "arm64"}
            )
            required = "OrbStack linux/arm64"
        else:
            ok = (
                info.get("OSType") == "linux"
                and architecture in {"x86_64", "amd64"}
            )
            required = "Docker Engine linux/amd64"
        if ok:
            return _passed(
                "engine_platform",
                "engine_platform_supported",
                f"Engine platform supports {required}",
            )
        return _failed(
            "engine_platform",
            "engine_platform_unsupported",
            f"Engine must provide {required}",
        )

    def _policy_primitives_check(
        self,
        info: dict[str, object],
    ) -> DoctorCheck:
        security_options = info.get("SecurityOptions")
        runtimes = info.get("Runtimes")
        ok = (
            info.get("MemoryLimit") is True
            and info.get("CpuCfsQuota") is True
            and info.get("PidsLimit") is True
            and isinstance(security_options, list)
            and any("seccomp" in str(item) for item in security_options)
            and isinstance(runtimes, dict)
            and "runc" in runtimes
        )
        if ok:
            return _passed(
                "policy_primitives",
                "policy_primitives_supported",
                "Memory, CPU, PID, seccomp, and runc controls are available",
            )
        return _failed(
            "policy_primitives",
            "policy_primitives_missing",
            "Required memory, CPU, PID, seccomp, or runc control is missing",
        )

    def _network_check(self, info: dict[str, object]) -> DoctorCheck:
        plugins = info.get("Plugins")
        networks = plugins.get("Network") if isinstance(plugins, dict) else None
        if isinstance(networks, list) and "null" in networks:
            return _passed(
                "network_none",
                "network_none_supported",
                "Docker null network driver is available",
            )
        return _failed(
            "network_none",
            "network_none_missing",
            "Docker null network driver is unavailable",
        )

    def _buildx_check(self) -> DoctorCheck:
        result = self.command_runner.run(
            (self.docker_executable, "buildx", "version"),
            timeout_seconds=10,
        )
        if result.returncode != 0:
            return _failed(
                "buildx_version",
                "buildx_unavailable",
                "Docker buildx is unavailable",
            )
        match = re.search(rb"\bv?([0-9]+\.[0-9]+\.[0-9]+)\b", result.stdout)
        version = (
            None
            if match is None
            else _version_tuple(match.group(1).decode("ascii"), parts=3)
        )
        if version is None or version < MINIMUM_BUILDX_VERSION:
            return _failed(
                "buildx_version",
                "buildx_version_unsupported",
                "Docker buildx 0.12+ is required",
            )
        return _passed(
            "buildx_version",
            "buildx_version_supported",
            f"Docker buildx {'.'.join(map(str, version))}",
        )

    def _runner_image_check(
        self,
        image: dict[str, object],
        info: dict[str, object] | None,
    ) -> DoctorCheck:
        config = image.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        engine_arch = (
            str(info.get("Architecture", "")).lower()
            if isinstance(info, dict)
            else ""
        )
        image_arch = str(image.get("Architecture", "")).lower()
        architecture_matches = (
            image_arch in {"arm64", "aarch64"}
            and engine_arch in {"arm64", "aarch64"}
        ) or (
            image_arch in {"amd64", "x86_64"}
            and engine_arch in {"amd64", "x86_64"}
        )
        ok = (
            image.get("Os") == "linux"
            and architecture_matches
            and isinstance(config, dict)
            and config.get("User") == "65532:65532"
            and isinstance(labels, dict)
            and labels.get("io.contribos.sandbox.policy")
            == SANDBOX_POLICY_VERSION
            and labels.get("io.contribos.runner.architecture") == image_arch
            and config.get("Entrypoint") == ["/usr/bin/tini", "--"]
        )
        if ok:
            return _passed(
                "runner_image",
                "runner_image_verified",
                f"Runner image is verified for linux/{image_arch}",
            )
        return _failed(
            "runner_image",
            "runner_image_invalid",
            "Runner image platform, user, labels, or entrypoint is invalid",
        )

    def _policy_probe(self) -> DoctorCheck:
        probe = "\n".join(
            (
                "import os",
                "assert os.getuid() == 65532 and os.getgid() == 65532",
                "status = open('/proc/self/status', encoding='utf-8').read()",
                "assert 'CapEff:\\t0000000000000000' in status",
                "assert 'NoNewPrivs:\\t1' in status",
                "assert sorted(os.listdir('/sys/class/net')) == ['lo']",
                "assert os.statvfs('/').f_flag & os.ST_RDONLY",
                "open('/tmp/doctor-probe', 'w', encoding='utf-8').write('ok')",
                "print('sandbox-doctor-policy-probe-v1')",
            )
        )
        result = self.command_runner.run(
            (
                self.docker_executable,
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "16",
                "--memory",
                str(64 * 1024 * 1024),
                "--memory-swap",
                str(64 * 1024 * 1024),
                "--cpus",
                "0.1",
                "--user",
                "65532:65532",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
                "--entrypoint",
                "python3",
                self.runner_image,
                "-c",
                probe,
            ),
            timeout_seconds=30,
        )
        if (
            result.returncode == 0
            and result.stdout.strip() == b"sandbox-doctor-policy-probe-v1"
            and not result.stderr
        ):
            return _passed(
                "policy_probe",
                "policy_probe_passed",
                "Restricted SandboxPolicy runtime probe passed",
            )
        return _failed(
            "policy_probe",
            "policy_probe_failed",
            "Restricted SandboxPolicy runtime probe failed",
        )

    def _json_command(self, argv: Sequence[str]) -> dict[str, object] | None:
        result = self.command_runner.run(argv, timeout_seconds=10)
        if result.returncode != 0 or result.stderr:
            return None
        try:
            value = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _unavailable_runtime_checks() -> tuple[DoctorCheck, ...]:
        return tuple(
            _failed(name, "docker_unavailable", detail)
            for name, detail in (
                ("required_versions", "Docker versions cannot be verified"),
                ("engine_platform", "Docker engine platform cannot be verified"),
                ("policy_primitives", "Docker policy controls cannot be verified"),
                ("network_none", "Docker network isolation cannot be verified"),
                ("buildx_version", "Docker buildx cannot be verified"),
                ("runner_image", "Sandbox Runner image cannot be verified"),
                ("policy_probe", "Sandbox runtime policy probe cannot run"),
            )
        )


def _version_tuple(value: object, *, parts: int) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^([0-9]+(?:\.[0-9]+){" + str(parts - 1) + r"})", value)
    if match is None:
        return None
    return tuple(int(item) for item in match.group(1).split("."))


def _passed(name: str, code: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, ok=True, code=code, detail=detail)


def _failed(name: str, code: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, ok=False, code=code, detail=detail)
