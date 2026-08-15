from __future__ import annotations

import re
from pathlib import Path


DOCKERFILE = (
    Path(__file__).resolve().parents[1]
    / "containers"
    / "sandbox-runner"
    / "Dockerfile"
)


def test_runner_uses_one_digest_pinned_multiarch_dockerfile() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8")

    from_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("FROM ")
    ]
    assert from_lines == [
        "FROM node:22-bookworm-slim@sha256:"
        "f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46"
    ]
    assert "ARG TARGETARCH" in source
    assert "amd64|arm64" in source
    assert 'io.contribos.runner.architecture="${TARGETARCH}"' in source
    assert 'io.contribos.sandbox.policy="${SANDBOX_POLICY_VERSION}"' in source
    assert re.search(r"(?m)^USER 65532:65532$", source)


def test_runner_contains_python_typescript_and_checkout_tools_without_secrets() -> None:
    source = DOCKERFILE.read_text(encoding="utf-8")

    for required in (
        "ca-certificates",
        "git",
        "python3",
        "python3-venv",
        "node --version",
        "npm --version",
        "tini",
    ):
        assert required in source
    for forbidden in (
        "ARG GITHUB_TOKEN",
        "ARG GH_TOKEN",
        "ARG API_KEY",
        "COPY .env",
        "docker.sock",
        "SSH_AUTH_SOCK",
        "--privileged",
    ):
        assert forbidden not in source

    dockerignore = DOCKERFILE.with_name(".dockerignore").read_text(
        encoding="utf-8"
    )
    assert dockerignore.splitlines() == ["*", "!Dockerfile"]
