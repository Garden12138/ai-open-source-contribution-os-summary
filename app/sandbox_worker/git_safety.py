"""Fixed Git process policy for every untrusted repository container."""

from __future__ import annotations

from collections.abc import Mapping

from app.security import ensure_no_sensitive_data


GIT_SAFETY_POLICY_VERSION = "sandbox-git-safety-v1"
_COMMAND_CONFIG = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.sshCommand", "/bin/false"),
    ("core.gitProxy", "none"),
    ("core.askPass", "/bin/false"),
    ("core.attributesFile", "/dev/null"),
    ("core.excludesFile", "/dev/null"),
    ("credential.helper", ""),
    ("credential.interactive", "false"),
    ("credential.useHttpPath", "true"),
    ("protocol.allow", "never"),
    ("fetch.recurseSubmodules", "false"),
    ("submodule.recurse", "false"),
    ("push.recurseSubmodules", "no"),
    ("filter.lfs.process", ""),
    ("filter.lfs.smudge", ""),
    ("filter.lfs.clean", ""),
    ("filter.lfs.required", "false"),
    ("init.templateDir", "/dev/null"),
    ("http.followRedirects", "false"),
)
_BASE_ENVIRONMENT = (
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_CONFIG_GLOBAL", "/dev/null"),
    ("GIT_ATTR_NOSYSTEM", "1"),
    ("GIT_TERMINAL_PROMPT", "0"),
    ("GIT_ASKPASS", "/bin/false"),
    ("SSH_ASKPASS", "/bin/false"),
    ("GIT_SSH_COMMAND", "/bin/false"),
    ("GIT_LFS_SKIP_SMUDGE", "1"),
    ("GIT_OPTIONAL_LOCKS", "0"),
    ("GIT_DISCOVERY_ACROSS_FILESYSTEM", "0"),
    (
        "GIT_CEILING_DIRECTORIES",
        "/workspace:/dependencies:/tmp",
    ),
)
GIT_SAFETY_ENVIRONMENT = (
    *_BASE_ENVIRONMENT,
    ("GIT_CONFIG_COUNT", str(len(_COMMAND_CONFIG))),
    *tuple(
        item
        for index, (key, value) in enumerate(_COMMAND_CONFIG)
        for item in (
            (f"GIT_CONFIG_KEY_{index}", key),
            (f"GIT_CONFIG_VALUE_{index}", value),
        )
    ),
)
GIT_SAFETY_ENVIRONMENT_NAMES = tuple(
    name for name, _ in GIT_SAFETY_ENVIRONMENT
)


def docker_git_safety_argv() -> tuple[str, ...]:
    """Return exact Docker `--env` arguments without inheriting host Git state."""

    ensure_no_sensitive_data(
        dict(GIT_SAFETY_ENVIRONMENT),
        context="Git safety policy",
    )
    return tuple(
        value
        for name, setting in GIT_SAFETY_ENVIRONMENT
        for value in ("--env", f"{name}={setting}")
    )


def select_git_safety_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Fail closed unless a process received the complete exact Git policy."""

    expected = dict(GIT_SAFETY_ENVIRONMENT)
    selected = {
        name: environment.get(name)
        for name in GIT_SAFETY_ENVIRONMENT_NAMES
    }
    if selected != expected:
        raise ValueError("Git safety environment is missing or changed")
    return expected.copy()


__all__ = [
    "GIT_SAFETY_ENVIRONMENT",
    "GIT_SAFETY_ENVIRONMENT_NAMES",
    "GIT_SAFETY_POLICY_VERSION",
    "docker_git_safety_argv",
    "select_git_safety_environment",
]
