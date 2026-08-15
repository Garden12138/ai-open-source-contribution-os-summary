from __future__ import annotations

import json

import pytest

import app.cli as cli
from app.sandbox_worker.doctor import (
    MINIMUM_FREE_BYTES,
    DoctorCommandResult,
    SandboxDoctor,
    SubprocessDoctorCommandRunner,
)


IMAGE = "sha256:" + "5" * 64


class _FakeCommandRunner:
    def __init__(
        self,
        *,
        version: dict[str, object] | None = None,
        info: dict[str, object] | None = None,
        image: dict[str, object] | None = None,
        buildx: bytes = b"github.com/docker/buildx v0.33.0 fixture\n",
        probe_ok: bool = True,
    ) -> None:
        self.version = version
        self.info = info
        self.image = image
        self.buildx = buildx
        self.probe_ok = probe_ok
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv,  # type: ignore[no-untyped-def]
        *,
        timeout_seconds: int,
    ) -> DoctorCommandResult:
        command = tuple(argv)
        self.calls.append(command)
        assert 1 <= timeout_seconds <= 30
        if command[1] == "version":
            return self._json(self.version)
        if command[1] == "info":
            return self._json(self.info)
        if command[1:3] == ("buildx", "version"):
            return DoctorCommandResult(0, self.buildx)
        if command[1:3] == ("image", "inspect"):
            return self._json(self.image)
        if command[1] == "run":
            return DoctorCommandResult(
                0 if self.probe_ok else 1,
                (
                    b"sandbox-doctor-policy-probe-v1\n"
                    if self.probe_ok
                    else b""
                ),
            )
        raise AssertionError(command)

    @staticmethod
    def _json(value: dict[str, object] | None) -> DoctorCommandResult:
        if value is None:
            return DoctorCommandResult(
                1,
                b"",
                b"github_pat_doctorcanary12345678",
            )
        return DoctorCommandResult(0, json.dumps(value).encode("utf-8"))


def _version() -> dict[str, object]:
    return {
        "Client": {"Version": "29.4.0"},
        "Server": {"Version": "29.4.0", "ApiVersion": "1.54"},
    }


def _info(
    *,
    operating_system: str = "OrbStack",
    architecture: str = "aarch64",
) -> dict[str, object]:
    return {
        "OperatingSystem": operating_system,
        "OSType": "linux",
        "Architecture": architecture,
        "MemoryLimit": True,
        "CpuCfsQuota": True,
        "PidsLimit": True,
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        "Runtimes": {"runc": {"path": "runc"}},
        "Plugins": {"Network": ["bridge", "null"]},
    }


def _image(*, architecture: str = "arm64") -> dict[str, object]:
    return {
        "Id": IMAGE,
        "Os": "linux",
        "Architecture": architecture,
        "Config": {
            "User": "65532:65532",
            "Entrypoint": ["/usr/bin/tini", "--"],
            "Labels": {
                "io.contribos.runner.architecture": architecture,
                "io.contribos.sandbox.policy": "sandbox-policy-v1",
            },
        },
    }


def test_doctor_passes_all_macos_orbstack_prerequisites() -> None:
    runner = _FakeCommandRunner(
        version=_version(),
        info=_info(),
        image=_image(),
    )
    report = SandboxDoctor(
        runner_image=IMAGE,
        command_runner=runner,
        host_system="Darwin",
        host_architecture="arm64",
        free_bytes=MINIMUM_FREE_BYTES,
    ).run()

    assert report.ok is True
    assert len(report.checks) == 11
    assert all(check.ok for check in report.checks)
    assert report.to_wire()["ok"] is True

    probe = next(command for command in runner.calls if command[1] == "run")
    for expected in (
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "no-new-privileges=true",
        "--pids-limit",
        "--memory",
        "--cpus",
        "--user",
        "65532:65532",
        "--pull",
        "never",
    ):
        assert expected in probe
    assert probe[-3] == IMAGE
    assert "/var/run/docker.sock" not in " ".join(probe)


def test_doctor_passes_supported_linux_amd64_engine() -> None:
    report = SandboxDoctor(
        runner_image=IMAGE,
        command_runner=_FakeCommandRunner(
            version=_version(),
            info=_info(
                operating_system="Debian GNU/Linux",
                architecture="x86_64",
            ),
            image=_image(architecture="amd64"),
        ),
        host_system="Linux",
        host_architecture="x86_64",
        free_bytes=MINIMUM_FREE_BYTES + 1,
    ).run()

    assert report.ok is True
    assert next(
        check for check in report.checks if check.name == "engine_platform"
    ).code == "engine_platform_supported"


def test_doctor_fails_closed_without_docker_and_redacts_stderr() -> None:
    report = SandboxDoctor(
        runner_image=IMAGE,
        command_runner=_FakeCommandRunner(),
        host_system="Darwin",
        host_architecture="arm64",
        free_bytes=MINIMUM_FREE_BYTES,
    ).run()
    wire = report.to_wire()

    assert report.ok is False
    assert next(
        check for check in report.checks if check.name == "docker_runtime"
    ).code == "docker_unavailable"
    assert "github_pat_" not in json.dumps(wire)
    assert {check.name for check in report.checks} >= {
        "required_versions",
        "engine_platform",
        "policy_primitives",
        "network_none",
        "runner_image",
        "policy_probe",
    }


def test_doctor_reports_each_unsupported_prerequisite() -> None:
    version = _version()
    version["Server"] = {"Version": "23.0.0", "ApiVersion": "1.42"}
    info = _info(operating_system="Docker Desktop", architecture="amd64")
    info.update(
        {
            "MemoryLimit": False,
            "Plugins": {"Network": ["bridge"]},
        }
    )
    image = _image()
    image["Config"] = {
        "User": "0:0",
        "Entrypoint": ["/bin/sh"],
        "Labels": {},
    }
    report = SandboxDoctor(
        runner_image=IMAGE,
        command_runner=_FakeCommandRunner(
            version=version,
            info=info,
            image=image,
            buildx=b"github.com/docker/buildx v0.11.2 old\n",
            probe_ok=False,
        ),
        host_system="Darwin",
        host_architecture="x86_64",
        python_version=(3, 10, 14),
        free_bytes=MINIMUM_FREE_BYTES - 1,
    ).run()
    codes = {check.code for check in report.checks if not check.ok}

    assert report.ok is False
    assert codes >= {
        "host_platform_unsupported",
        "python_version_unsupported",
        "disk_capacity_insufficient",
        "docker_version_unsupported",
        "engine_platform_unsupported",
        "policy_primitives_missing",
        "network_none_missing",
        "buildx_version_unsupported",
        "runner_image_invalid",
        "policy_probe_failed",
    }


def test_doctor_subprocess_environment_is_an_exact_noncredential_allowlist() -> None:
    runner = SubprocessDoctorCommandRunner(
        environment={
            "PATH": "/usr/bin",
            "HOME": "/tmp/doctor-home",
            "DOCKER_CONTEXT": "orbstack",
            "GITHUB_TOKEN": "github_pat_doctorcanary12345678",
            "SSH_AUTH_SOCK": "/tmp/forbidden-agent",
            "DATABASE_URL": "sqlite:///forbidden.db",
        }
    )

    assert runner.environment == {
        "PATH": "/usr/bin",
        "HOME": "/tmp/doctor-home",
        "DOCKER_CONTEXT": "orbstack",
    }
    with pytest.raises(ValueError, match="digest-pinned"):
        SandboxDoctor(runner_image="contribos/sandbox-runner:latest")


def test_doctor_cli_outputs_json_and_does_not_load_application_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = SandboxDoctor(
        runner_image=IMAGE,
        command_runner=_FakeCommandRunner(
            version=_version(),
            info=_info(),
            image=_image(),
        ),
        host_system="Darwin",
        host_architecture="arm64",
        free_bytes=MINIMUM_FREE_BYTES,
    ).run()

    class _FakeDoctor:
        def __init__(self, *, runner_image: str) -> None:
            assert runner_image == IMAGE

        def run(self):  # type: ignore[no-untyped-def]
            return report

    monkeypatch.setattr(cli, "SandboxDoctor", _FakeDoctor)
    monkeypatch.setattr(
        cli.Settings,
        "from_env",
        classmethod(
            lambda cls: (_ for _ in ()).throw(
                AssertionError("doctor must not load application settings")
            )
        ),
    )

    assert cli.main(["doctor", "--runner-image", IMAGE]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["version"] == "sandbox-doctor-v1"
    assert output["ok"] is True
