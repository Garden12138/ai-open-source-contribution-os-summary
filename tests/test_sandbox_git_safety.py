from __future__ import annotations

import pytest

from app.sandbox_worker.git_safety import (
    GIT_SAFETY_ENVIRONMENT,
    GIT_SAFETY_POLICY_VERSION,
    docker_git_safety_argv,
    select_git_safety_environment,
)


def test_git_safety_policy_is_exact_complete_and_fail_closed() -> None:
    environment = dict(GIT_SAFETY_ENVIRONMENT)
    count = int(environment["GIT_CONFIG_COUNT"])
    command_config = {
        environment[f"GIT_CONFIG_KEY_{index}"]: environment[
            f"GIT_CONFIG_VALUE_{index}"
        ]
        for index in range(count)
    }

    assert GIT_SAFETY_POLICY_VERSION == "sandbox-git-safety-v1"
    assert len(environment) == len(GIT_SAFETY_ENVIRONMENT)
    assert command_config["core.hooksPath"] == "/dev/null"
    assert command_config["core.fsmonitor"] == "false"
    assert command_config["credential.helper"] == ""
    assert command_config["credential.interactive"] == "false"
    assert command_config["protocol.allow"] == "never"
    assert command_config["submodule.recurse"] == "false"
    assert command_config["filter.lfs.process"] == ""
    assert command_config["filter.lfs.smudge"] == ""
    assert command_config["filter.lfs.clean"] == ""
    assert command_config["filter.lfs.required"] == "false"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_ASKPASS"] == "/bin/false"
    assert environment["GIT_SSH_COMMAND"] == "/bin/false"
    assert environment["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert select_git_safety_environment(environment) == environment

    changed = dict(environment)
    changed["GIT_CONFIG_NOSYSTEM"] = "0"
    with pytest.raises(ValueError, match="missing or changed"):
        select_git_safety_environment(changed)

    argv = docker_git_safety_argv()
    assert argv.count("--env") == len(GIT_SAFETY_ENVIRONMENT)
    assert "GIT_CONFIG_NOSYSTEM=1" in argv
    assert "GIT_CONFIG_GLOBAL=/dev/null" in argv
    assert "GIT_CONFIG_VALUE_0=/dev/null" in argv
