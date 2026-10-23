"""Static verification policy; environment preparation is a separate action."""
from __future__ import annotations
from pathlib import PurePosixPath
from typing import Any, Sequence


def verification_command_error(commands: Sequence[Any], inventory: set[str], new_paths: set[str]) -> dict | None:
    for command in commands:
        argv = list(command.argv)
        executable = PurePosixPath(argv[0]).name
        tool = argv[2] if executable in {"python", "python3"} and len(argv) > 2 and argv[1] == "-m" else executable
        install = ((tool in {"pip", "pip3", "uv", "poetry", "pdm", "npm", "pnpm", "yarn"}
                    and any(a in {"install", "ci", "sync", "add", "update", "upgrade"} for a in argv[1:]))
                   or tool in {"tox", "tox-uv"}
                   or (tool == "uv" and "run" in argv[1:]))
        if install:
            return {"reason_code":"planning_output_invalid", "validation_types":["value_error"],
                    "fields":["commands_to_run.argv"], "constraint":"dependency_preparation_separate",
                    "command_ids":[command.command_id],
                    "remediation":"Remove installation/environment-management commands from verification. The Sandbox Worker separately resolves and builds frozen dependencies. Use direct offline pytest/ruff/mypy commands."}
        directory = PurePosixPath(command.working_directory)
        if str(directory) != "." and not any(p.startswith(str(directory) + "/") for p in inventory):
            return {"reason_code":"planning_output_invalid", "validation_types":["value_error"],
                    "fields":["commands_to_run.working_directory"], "constraint":"command_directory_missing",
                    "command_ids":[command.command_id]}
        if tool == "pytest":
            for arg in argv[1:]:
                path = arg.split("::", 1)[0]
                if path.endswith(".py") and not path.startswith("-"):
                    target = str(directory / path)
                    if target not in inventory and target not in new_paths:
                        return {"reason_code":"planning_output_invalid", "validation_types":["value_error"],
                                "fields":["commands_to_run.argv"], "constraint":"test_path_missing",
                                "command_ids":[command.command_id], "paths":[target],
                                "remediation":"Use exact repository inventory test paths relative to working_directory, or an approved new regression test path."}
    from app.execution_environment import environment_plan
    selected = environment_plan(list(inventory), change_paths=list(new_paths))
    directories = selected.get("project_directories", [])
    if directories:
        unscoped = [command.command_id for command in commands if not any(
            root == "." or command.working_directory == root or command.working_directory.startswith(root + "/")
            for root in directories)]
        if unscoped:
            return {"reason_code": "planning_output_invalid", "validation_types": ["value_error"],
                    "fields": ["commands_to_run.working_directory"], "constraint": "package_command_unscoped",
                    "command_ids": unscoped,
                    "remediation": "Every command in a multi-package plan must run under its selected package root; the worker selects that package's independently frozen environment by working_directory."}
        existing = baseline_commands(commands, inventory, new_paths)
        covered = set()
        for command in existing:
            argv = list(command.argv)
            executable = PurePosixPath(argv[0]).name
            tool = argv[2] if executable in {"python", "python3"} and len(argv) > 2 and argv[1] == "-m" else executable
            if tool == "pytest" and not any(value in {"--collect-only", "--co"} for value in argv):
                covered.add(command.working_directory)
        missing = sorted(set(directories) - covered)
        if missing:
            return {"reason_code": "planning_output_invalid", "validation_types": ["value_error"],
                    "fields": ["commands_to_run"], "constraint": "package_baseline_missing", "paths": missing,
                    "remediation": "Each selected Python package needs a direct offline pytest command with its exact package root as working_directory and an existing test target. New-file-only tests or collection-only commands cannot provide a pre-change baseline."}
    return None


def baseline_commands(commands: Sequence[Any], inventory: set[str], new_paths: set[str]) -> tuple[Any, ...]:
    """Run unchanged approved commands; new-file tests exist only after Implement."""
    return tuple(command for command in commands if not any(
        str(PurePosixPath(command.working_directory) / arg.split("::", 1)[0]) in new_paths - inventory
        for arg in command.argv
    ))
