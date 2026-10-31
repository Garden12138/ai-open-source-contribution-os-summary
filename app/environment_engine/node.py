"""Literal package-script edges. Dynamic installers have no execution fallback."""
from __future__ import annotations

import json
import re
import shlex
from pathlib import PurePosixPath
from app.environment_engine.source import FrozenSource, ResolutionError, relative_path

VERSION = "node-script-chain-v3"
MANAGERS = {"npm", "pnpm", "yarn", "bun"}


def script_directory(source: FrozenSource, current: str, destination: str) -> str:
    """Resolve a literal shell cwd inside the frozen archive, never on host."""
    if not destination or destination.startswith("-") or any(c in destination for c in "~*?[]{}"):
        raise ResolutionError("node_script_cwd_unsafe")
    try:
        parent = ("" if current == "." else current + "/") + "__cwd__"
        result = str(PurePosixPath(relative_path(parent, destination + "/__directory__")).parent)
    except ValueError:
        raise ResolutionError("node_script_cwd_unsafe") from None
    for index in range(1, len(PurePosixPath(result).parts) + 1):
        component = str(PurePosixPath(*PurePosixPath(result).parts[:index]))
        entry = source.entries.get(component, {})
        if entry.get("type") == "symlink" or entry.get("symlink") or entry.get("link_target"):
            raise ResolutionError("node_script_cwd_unsafe", (component,))
    if result != "." and not any(path.startswith(result + "/") for path in source.inventory):
        raise ResolutionError("node_script_cwd_missing", (result,))
    return result


def filtered_directory(source: FrozenSource, current: str, selector: str) -> str:
    """Resolve one exact pnpm workspace name, with no scope/glob fallback."""
    if (len(selector) > 214 or ".." in selector
            or not re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", selector)):
        raise ResolutionError("node_filter_unsupported")
    roots = [PurePosixPath(current), *PurePosixPath(current).parents]
    root = next((p for p in roots if str(p / "pnpm-workspace.yaml") in source.inventory), None)
    if root is None:
        raise ResolutionError("node_filter_workspace_missing")
    workspace = str(root / "pnpm-workspace.yaml")
    text = source.read(workspace, VERSION)
    keys = re.findall(r"(?m)^packages\s*:.*$", text)
    if (len(keys) != 1 or not re.fullmatch(r"packages:\s*(?:#.*)?", keys[0])
            or re.search(r"(?m)^(?:['\"]packages['\"]|<<)\s*:", text)):
        raise ResolutionError("node_workspace_declaration_invalid", (workspace,))
    prefix = "" if str(root) == "." else str(root) + "/"
    package = prefix + "package.json"
    try:
        metadata = json.loads(source.read(package, VERSION))
        if not isinstance(metadata, dict):
            raise ValueError
        from app.node_environments import _workspace_packages
        candidates = {p: None for p in source.inventory if p.startswith(prefix)
            and PurePosixPath(p).name == "package.json"
            and not {"node_modules", ".git"} & set(PurePosixPath(p).parts)}
        members = _workspace_packages(candidates, metadata, prefix, source.entries, "pnpm")
        matched = []
        for path in members:
            value = json.loads(source.read(path, VERSION))
            if not isinstance(value, dict) or value.get("name") is not None and not isinstance(value["name"], str):
                raise ValueError
            if value.get("name") == selector:
                matched.append(path)
    except ResolutionError:
        raise
    except (ValueError, TypeError):
        raise ResolutionError("node_workspace_declaration_invalid", (workspace,)) from None
    if len(matched) != 1:
        raise ResolutionError("node_filter_ambiguous" if matched else "node_filter_no_exact_match", (workspace,))
    destination = str(PurePosixPath(matched[0]).parent)
    relative = destination[len(prefix):] if prefix else destination
    return script_directory(source, str(root), relative or ".")


def script_graph(source: FrozenSource, argv: tuple[str, ...], cwd: str) -> dict:
    chain: list[str] = []
    units: set[str] = set()
    tools: set[str] = set()
    entrypoints: set[tuple[str, str]] = set()
    leaves: list[dict] = []
    def walk(args: list[str], directory: str, stack: tuple[str, ...]) -> None:
        if len(stack) > 12 or len(chain) > 100 or not args:
            raise ResolutionError("node_script_graph_limit")
        executable = args[0]
        if executable in {"cd", "pushd", "popd"}:
            raise ResolutionError("unsupported_dynamic_script")
        entrypoints.add((directory, executable))
        if executable in {"npx", "curl", "wget", "sh", "bash"} or "dlx" in args:
            raise ResolutionError("unsupported_dynamic_installer")
        if executable in MANAGERS:
            pos = 1
            flags = {"npm": {"--prefix"}, "pnpm": {"-C", "--dir"}, "yarn": {"--cwd"}, "bun": {"--cwd"}}[executable]
            if len(args) > 2 and args[pos] in flags:
                directory = script_directory(source, directory, args[2])
                pos += 2
            if executable == "pnpm" and pos < len(args) and (
                    args[pos] in {"--filter", "-F"} or args[pos].startswith("--filter=")):
                if args[pos].startswith("--filter="):
                    selector = args[pos].split("=", 1)[1]
                    pos += 1
                else:
                    if len(args) <= pos + 1:
                        raise ResolutionError("node_filter_unsupported")
                    selector = args[pos + 1]
                    pos += 2
                if len(args) != pos + 2 or args[pos] != "run" or args[pos + 1].startswith("-"):
                    raise ResolutionError("node_filter_command_unsupported")
                directory = filtered_directory(source, directory, selector)
                chain.append("pnpm#filter=" + selector + "@cwd=" + directory)
            if pos < len(args) and args[pos] == "exec" and executable == "pnpm" and len(args) > pos + 1:
                units.add(directory)
                walk(args[pos + 1:], directory, stack)
                return
            if pos < len(args) and args[pos] in {"install", "ci", "add", "update", "exec", "dlx"}:
                raise ResolutionError("unsupported_dynamic_installer")
            if pos < len(args) and args[pos] == "run":
                pos += 1
            if pos >= len(args) or args[pos].startswith("-"):
                raise ResolutionError("node_script_unresolved")
            name = args[pos]
            path = ("" if directory == "." else directory + "/") + "package.json"
            key = path + "#" + name
            if key in stack:
                raise ResolutionError("node_script_cycle", (path,))
            try:
                metadata = json.loads(source.read(path, VERSION))
                if not isinstance(metadata, dict):
                    raise ResolutionError("node_script_schema_invalid", (path,))
                scripts = metadata.get("scripts", {})
                if not isinstance(scripts, dict):
                    raise ResolutionError("node_script_schema_invalid", (path,))
                text = scripts[name]
            except ResolutionError:
                raise
            except (ValueError, TypeError, KeyError):
                raise ResolutionError("node_script_unresolved", (path,)) from None
            if not isinstance(text, str) or len(text) > 8000:
                raise ResolutionError("node_script_unresolved", (path,))
            chain.append(key)
            units.add(directory)
            # Manager pre/post scripts are part of the effective command.
            names = (["pre" + name] if executable in {"npm", "yarn"} and "pre" + name in scripts else [])
            for pre in names:
                walk([executable, "run", pre], directory, (*stack, key))
            if any(c in text for c in ("$", "`", ";", "|", ">", "<", "\n")) or "&" in text.replace("&&", ""):
                raise ResolutionError("unsupported_dynamic_script", (path,))
            script_cwd = directory
            for fragment in text.split("&&"):
                try:
                    tokens = shlex.split(fragment)
                except ValueError:
                    raise ResolutionError("unsupported_dynamic_script", (path,)) from None
                if tokens and tokens[0] == "cd":
                    if len(tokens) != 2:
                        raise ResolutionError("unsupported_dynamic_script", (path,))
                    script_cwd = script_directory(source, script_cwd, tokens[1])
                    package = ("" if script_cwd == "." else script_cwd + "/") + "package.json"
                    if package in source.inventory:
                        source.read(package, VERSION)
                        units.add(script_cwd)
                    # A shell cwd edge is not an executable or a passing check.
                    chain.append(key + "@cd=" + script_cwd)
                    if len(chain) > 100:
                        raise ResolutionError("node_script_graph_limit")
                else:
                    walk(tokens, script_cwd, (*stack, key))
            if executable in {"npm", "yarn"} and "post" + name in scripts:
                walk([executable, "run", "post" + name], directory, (*stack, key))
            return
        chain.append(directory + ":" + shlex.join(args))
        leaves.append({"argv": args, "working_directory": directory})
        tools.add(executable)
        if executable in {"node", "bun"} and any(a in {"-e", "--eval", "-p", "--print"} for a in args[1:]):
            raise ResolutionError("unsupported_dynamic_script")
        if executable in {"node", "bun"} and len(args) > 1 and not args[1].startswith("-"):
            path = relative_path(("" if directory == "." else directory + "/") + "__cwd__", args[1])
            if path not in source.inventory and path + ".js" in source.inventory:
                path += ".js"
            text = source.read(path, VERSION)
            # No partial JS interpretation: subprocess-capable wrappers require
            # a separately versioned semantic adapter. Never execute to discover.
            if re.search(r"child_process|\b(?:spawn|exec|eval|fetch)\s*\(|\b(?:install|dlx)\b|https?://", text):
                raise ResolutionError("unsupported_dynamic_installer", (path,))
    walk(list(argv), cwd, ())
    return {"chain": chain, "package_directories": sorted(units), "executables": sorted(tools),
        "entrypoints": sorted(entrypoints), "leaf_commands": leaves}
