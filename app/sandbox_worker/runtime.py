from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.config import Settings


@dataclass(frozen=True, slots=True)
class StageRuntimes:
    explore: Any
    implement: Any
    verify: Any


def resolve_stage_runtimes(settings: Settings) -> StageRuntimes | None:
    """Resolve stage runtimes only inside a Worker-owned process."""
    if settings.sandbox_stage_runtime == "none":
        return None
    if settings.sandbox_stage_runtime == "fake":
        from app.sandbox_worker.fake import (
            FakeExploreRuntime,
            FakeImplementRuntime,
            FakeVerifyRuntime,
        )

        return StageRuntimes(
            explore=FakeExploreRuntime(),
            implement=FakeImplementRuntime(),
            verify=FakeVerifyRuntime(),
        )
    if settings.sandbox_stage_runtime == "docker":
        from app.sandbox_worker.explore import DockerExploreRuntime
        from app.sandbox_worker.implementation import DockerImplementRuntime
        from app.sandbox_worker.verification import DockerVerifyRuntime

        environment = {
            name: value
            for name, value in os.environ.items()
            if name
            in {
                "DOCKER_CERT_PATH",
                "DOCKER_CONFIG",
                "DOCKER_CONTEXT",
                "DOCKER_HOST",
                "DOCKER_TLS_VERIFY",
                "HOME",
                "PATH",
                "TMPDIR",
            }
        }
        return StageRuntimes(
            explore=DockerExploreRuntime(docker_environment=environment),
            implement=DockerImplementRuntime(docker_environment=environment),
            verify=DockerVerifyRuntime(docker_environment=environment),
        )
    raise ValueError(
        "SANDBOX_STAGE_RUNTIME must be 'none', 'fake', or 'docker'"
    )


__all__ = ["StageRuntimes", "resolve_stage_runtimes"]
