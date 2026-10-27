"""Static verification policy; environment preparation is a separate action."""
from __future__ import annotations
from pathlib import PurePosixPath
from typing import Any, Sequence


def _package_script(argv: list[str], directory: PurePosixPath) -> tuple[PurePosixPath, str] | None:
    """Resolve literal manager directory options without executing the command."""
    manager = PurePosixPath(argv[0]).name
    options = {"bun":{"--cwd"}, "yarn":{"--cwd"}, "npm":{"--prefix"}, "pnpm":{"--dir", "-C"}}
    allowed = options.get(manager)
    if allowed is None:
        return None
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in allowed and index + 1 < len(argv):
            directory = directory / argv[index + 1]
            index += 2
        elif any(argument.startswith(option + "=") for option in allowed):
            directory = directory / argument.split("=", 1)[1]
            index += 1
        else:
            break
    if index + 1 < len(argv) and argv[index] == "run" and not argv[index + 1].startswith("-"):
        return directory, argv[index + 1]
    return None


def verification_command_error(commands: Sequence[Any], inventory: set[str], new_paths: set[str], evidence: dict | None = None) -> dict | None:
    for command in commands:
        argv = list(command.argv)
        executable = PurePosixPath(argv[0]).name
        tool = argv[2] if executable in {"python", "python3"} and len(argv) > 2 and argv[1] == "-m" else executable
        install = ((tool in {"pip", "pip3", "uv", "poetry", "pdm", "npm", "pnpm", "yarn", "bun"}
                    and any(a in {"install", "ci", "sync", "add", "update", "upgrade"} for a in argv[1:]))
                   or tool in {"tox", "tox-uv"}
                   or (tool == "uv" and "run" in argv[1:]))
        if install:
            return {"reason_code":"planning_output_invalid", "validation_types":["value_error"],
                    "fields":["commands_to_run.argv"], "constraint":"dependency_preparation_separate",
                    "command_ids":[command.command_id],
                    "remediation":"Remove installation/environment-management commands from verification. The Sandbox Worker separately resolves and builds frozen dependencies. Use direct offline pytest/ruff/mypy commands."}
        directory_arguments = []
        if executable == "bun" and "run" in argv[1:]:
            run_index = argv.index("run", 1)
            directory_arguments = argv[1:run_index]
            for argument in argv[run_index + 1:]:
                if not argument.startswith("-"):
                    break  # Arguments after the script name belong to that script.
                directory_arguments.append(argument)
        if any(argument == "--cwd" or argument.startswith("--cwd=") for argument in directory_arguments):
            return {"reason_code":"planning_output_invalid", "validation_types":["value_error"],
                    "fields":["commands_to_run.argv", "commands_to_run.working_directory"],
                    "constraint":"bun_script_directory_requires_working_directory",
                    "command_ids":[command.command_id],
                    "remediation":"Use argv [bun, run, <declared script>] with the exact package directory in working_directory. Bun script directory flags have version-dependent dispatch behavior; help with exit zero does not prove script execution. Do not silently rewrite an approved command."}
        directory = PurePosixPath(command.working_directory)
        script = _package_script(argv, directory)
        if evidence and script:
            import json
            script_directory, script_name = script
            manifest = str(script_directory / "package.json")
            source = next((e.get("content") for e in evidence.get("entries",[]) if e["path"]==manifest),None)
            if isinstance(source,str):
                try:
                    package=json.loads(source)
                except ValueError:
                    package={}
                scripts=package.get("scripts",{}) if isinstance(package,dict) else {}
                if script_name not in scripts and str(script_directory/script_name) not in inventory:
                    return {"reason_code":"planning_output_invalid", "validation_types":["value_error"],
                        "fields":["commands_to_run.argv"], "constraint":"project_script_missing",
                        "script":script_name,
                        "command_ids":[command.command_id], "paths":[manifest],
                        "remediation":"The named script is absent from the inspected package.json. Use an actual declared script at its exact working_directory; do not save a speculative command or promise a runtime fallback."}
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
