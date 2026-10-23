"""Project-derived Node environments; inspection never runs repository code."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any

NODE_PREPARATION = "node-workspace-preparation-v1"
DEFAULT_MANAGERS = {"npm": "10.9.4", "yarn": "1.22.22", "pnpm": "10.11.0"}
LOCKS = {"package-lock.json": "npm", "yarn.lock": "yarn", "pnpm-lock.yaml": "pnpm"}
CONFIGS = (".nvmrc", ".node-version", ".yarnrc.yml", "pnpm-workspace.yaml")


def _workspace_packages(parsed: dict, root: dict, prefix: str, entries: dict, manager: str) -> list[str]:
    """Select declared workspaces; unrelated examples must not run lifecycle hooks."""
    import fnmatch
    declarations = root.get("workspaces", [])
    if isinstance(declarations, dict):
        declarations = declarations.get("packages", [])
    pnpm = entries.get(prefix + "pnpm-workspace.yaml", {}).get("content")
    if manager == "pnpm" and pnpm is not None:
        declarations = []
        in_packages = False
        for line in pnpm.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if re.fullmatch(r"packages:\s*(?:#.*)?", line):
                in_packages = True
            elif in_packages and line[:1].isspace():
                match = re.fullmatch(r"\s+-\s+(?:'([^']+)'|\"([^\"]+)\"|([^#\s]+))\s*(?:#.*)?", line)
                if not match:
                    raise ValueError("node_workspace_declaration_invalid")
                declarations.append(next(v for v in match.groups() if v is not None))
            elif not line[:1].isspace():
                in_packages = False
    if (not isinstance(declarations, list) or len(declarations) > 64
            or any(not isinstance(p, str) or not p or len(p) > 200 or p.startswith("/")
                   or ".." in p.split("/") or "\\" in p for p in declarations)):
        raise ValueError("node_workspace_declaration_invalid")
    def matches(path: str, pattern: str) -> bool:
        from functools import cache
        parts, globs = path.split("/"), pattern.removeprefix("./").rstrip("/").split("/")
        @cache
        def walk(i: int, j: int) -> bool:
            if j == len(globs): return i == len(parts)
            if globs[j] == "**":
                return walk(i, j + 1) or (i < len(parts) and walk(i + 1, j))
            return i < len(parts) and fnmatch.fnmatchcase(parts[i], globs[j]) and walk(i + 1, j + 1)
        return walk(0, 0)
    included = [p for p in declarations if not p.startswith("!")]
    excluded = [p[1:] for p in declarations if p.startswith("!")]
    packages = [prefix + "package.json"]
    for path in sorted(parsed):
        relative = path[len(prefix):].removesuffix("/package.json")
        if path != packages[0] and any(matches(relative, p) for p in included) and not any(matches(relative, p) for p in excluded):
            packages.append(path)
    return packages


def node_environment_plan(paths: set[str], changes: list[str]) -> dict[str, Any] | None:
    roots = set()
    for path in changes:
        parents = list(PurePosixPath(path).parents)
        candidates = [p for p in parents if str(p / "package.json") in paths]
        if not candidates:
            continue
        if PurePosixPath(path).suffix not in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
            python = next((p for p in parents if str(p / "pyproject.toml") in paths), None)
            if python is not None and parents.index(python) < parents.index(candidates[0]):
                continue
        locked = next((p for p in candidates if any(str(p / name) in paths for name in LOCKS)), candidates[0])
        roots.add(str(locked))
    if not roots:
        return None
    if len(roots) != 1:
        return {"status": "unsupported", "manifests": [], "reason_code": "node_workspace_roots_conflict",
                "reason": "修改涉及互不关联的 Node 安装根目录，需要分别冻结执行环境。"}
    directory = roots.pop()
    prefix = "" if directory == "." else directory + "/"
    locks = [prefix + name for name in LOCKS if prefix + name in paths]
    if not locks:
        return {"status": "unsupported", "manifests": [], "reason_code": "node_lockfile_missing",
                "reason": "项目没有可冻结的 npm/Yarn/pnpm 锁文件；不会擅自改写依赖版本。"}
    packages = sorted(p for p in paths if p.startswith(prefix) and PurePosixPath(p).name == "package.json"
                      and "node_modules" not in PurePosixPath(p).parts)
    manifests = sorted(set(packages + locks + [prefix + c for c in CONFIGS if prefix + c in paths]))
    if len(manifests) > 64:
        return {"status": "unsupported", "manifests": [], "reason_code": "node_manifest_budget",
                "reason": "Node 工作区超过 64 项清单预算，需要缩小执行范围。"}
    return {"status": "supported", "ecosystem": "node", "mode": "workspace-v2",
            "preparation_version": NODE_PREPARATION, "project_directory": directory,
            "manifests": manifests, "lockfiles": locks,
            "reason": "按项目声明选择 Node 和 npm/Yarn/pnpm，冻结完整工作区依赖；独立离线构建后，先运行修改前测试，再实施及离线验证。"}


def validate_node_recipe(value: object) -> dict:
    """The signed recipe is also validated inside the credential-free Runner."""
    import re
    from pathlib import PurePosixPath
    keys = {"version", "manager", "manager_version", "runtime_requirement", "runtime_pin",
            "project_directory", "package_roots", "linker", "workspace_lifecycle", "registry"}
    if not isinstance(value, dict) or set(value) != keys or value["version"] != "node-environment-v1":
        raise ValueError("Invalid Node environment")
    if (value["manager"] not in {"npm", "yarn", "pnpm"}
            or not isinstance(value["manager_version"], str)
            or not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[A-Za-z0-9.-]{1,50})?", value["manager_version"])
            or value["registry"] != "https://registry.npmjs.org"
            or value["linker"] not in {"node-modules", "pnp"}):
        raise ValueError("Invalid Node manager")
    for key in ("runtime_requirement", "runtime_pin"):
        text = value[key]
        if not isinstance(text, str) or len(text) > 160 or not re.fullmatch(r"[0-9vVxX.*<>=~^| +\-]*", text):
            raise ValueError("Invalid Node runtime constraint")
    def safe(path):
        return (isinstance(path, str) and path and len(path) <= 500 and not path.startswith("/")
                and "\\" not in path and not set(PurePosixPath(path).parts) & {"..", ".git", "node_modules"}
                and not any(ord(c) < 32 for c in path))
    roots = value["package_roots"]
    if (not safe(value["project_directory"]) or not isinstance(roots, list) or not 1 <= len(roots) <= 60
            or roots != sorted(set(roots)) or any(not safe(p) for p in roots)
            or value["project_directory"] not in roots
            or any(value["project_directory"] != "." and p != value["project_directory"]
                   and not p.startswith(value["project_directory"] + "/") for p in roots)):
        raise ValueError("Invalid Node workspace roots")
    lifecycle = value["workspace_lifecycle"]
    if (not isinstance(lifecycle, list) or len(lifecycle) > 240
            or any(not isinstance(item, dict) or set(item) != {"directory", "script"}
                   or item["directory"] not in roots
                   or item["script"] not in {"preinstall", "install", "postinstall", "prepare"} for item in lifecycle)):
        raise ValueError("Invalid Node lifecycle")
    return value


def inspect_node_environment(selected: dict, evidence: dict) -> dict:
    entries = {e["path"]: e for e in evidence["entries"]}
    required = [p for p in selected["manifests"] if PurePosixPath(p).name not in LOCKS]
    missing = [p for p in required if p not in entries]
    if missing:
        return {**selected, "status": "pending_read", "required_paths": missing,
                "reason": "需要补读工作区清单和运行时配置，再冻结环境方案。"}
    hashes = {}
    parsed = {}
    try:
        for path in required:
            entry = entries[path]
            text = entry.get("content")
            if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != entry.get("prior_hash"):
                raise ValueError("node_manifest_unavailable")
            hashes[path] = entry["prior_hash"]
            if path.endswith("package.json"):
                parsed[path] = json.loads(text)
                if not isinstance(parsed[path], dict):
                    raise ValueError("node_manifest_invalid")
        prefix = "" if selected["project_directory"] == "." else selected["project_directory"] + "/"
        root = parsed[prefix + "package.json"]
        declared = root.get("packageManager")
        locks = selected["lockfiles"]
        if declared:
            match = re.fullmatch(r"(npm|yarn|pnpm)@(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)(?:\+sha(?:224|256|512)\.[a-f0-9]+)?", declared)
            if not match:
                raise ValueError("node_manager_version_invalid")
            manager, version = match.groups()
            if not any(LOCKS[PurePosixPath(p).name] == manager for p in locks):
                raise ValueError("node_manager_lock_conflict")
        elif len(locks) == 1:
            manager = LOCKS[PurePosixPath(locks[0]).name]
            version = DEFAULT_MANAGERS[manager]
        else:
            raise ValueError("node_manager_ambiguous")
        engines = root.get("engines", {})
        if not isinstance(engines, dict):
            raise ValueError("node_runtime_invalid")
        pin = next((entries[prefix + name]["content"].strip() for name in (".node-version", ".nvmrc")
                    if prefix + name in entries), "")
        linker = "node-modules"
        config = entries.get(prefix + ".yarnrc.yml", {}).get("content", "")
        if manager == "yarn" and int(version.split(".")[0]) >= 2:
            match = re.search(r"(?m)^nodeLinker:\s*[\"']?([a-z-]+)", config)
            linker = match.group(1) if match else "pnp"
        workspace_paths = _workspace_packages(parsed, root, prefix, entries, manager)
        packages = sorted((p.rsplit("/", 1)[0] if "/" in p else ".") for p in workspace_paths)
        lifecycle = [{"directory": (p.rsplit("/", 1)[0] if "/" in p else "."), "script": name}
                     for p in workspace_paths for meta in [parsed[p]] for name in ("preinstall", "install", "postinstall", "prepare")
                     if isinstance(meta.get("scripts"), dict) and name in meta["scripts"]]
        recipe = validate_node_recipe({"version": "node-environment-v1", "manager": manager,
            "manager_version": version, "runtime_requirement": engines.get("node", ""), "runtime_pin": pin,
            "project_directory": selected["project_directory"], "package_roots": packages,
            "linker": linker, "workspace_lifecycle": lifecycle, "registry": "https://registry.npmjs.org"})
    except (ValueError, TypeError, KeyError) as exc:
        code = str(exc) if str(exc).startswith("node_") else "node_manifest_invalid"
        return {**selected, "status": "unsupported", "reason_code": code,
                "reason": "项目的包管理器、运行时或工作区声明无法一致冻结；请检查具体配置，不会猜测版本或改写锁文件。"}
    # Only the selected manager's lock participates; unrelated lockfiles remain source data.
    chosen_lock = next(p for p in locks if LOCKS[PurePosixPath(p).name] == manager)
    return {**selected, "manifests": sorted([*required, chosen_lock]), "manifest_hashes": hashes,
            "node_environment": recipe, "capability_version": "node-environment-v1"}


def validate_node_receipt(value: object, recipe: dict | None = None) -> dict:
    if (not isinstance(value, dict) or set(value) != {"version", "recipe", "runtime_version", "runtime_bin",
            "manager_version", "manifest_hashes", "built_offline", "execution_image_digest"}
            or value["version"] != "node-environment-result-v1" or value["built_offline"] is not True):
        raise ValueError("Invalid completed Node environment")
    validate_node_recipe(value["recipe"])
    if recipe is not None and value["recipe"] != recipe:
        raise ValueError("Node environment recipe changed")
    if (not isinstance(value["runtime_version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", value["runtime_version"])
            or value["manager_version"] != value["recipe"]["manager_version"]
            or not isinstance(value["execution_image_digest"], str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["execution_image_digest"])
            or value["runtime_bin"] not in {"/usr/local/bin", "/dependencies/output/runtime/node_modules/node-linux-arm64/bin",
                                           "/dependencies/output/runtime/node_modules/node-linux-x64/bin"}
            or not isinstance(value["manifest_hashes"], dict) or len(value["manifest_hashes"]) > 64
            or any(not isinstance(p, str) or p.startswith("/") or ".." in PurePosixPath(p).parts
                   or not isinstance(h, str) or not re.fullmatch(r"[a-f0-9]{64}", h) for p, h in value["manifest_hashes"].items())):
        raise ValueError("Invalid Node environment versions or manifests")
    return value
