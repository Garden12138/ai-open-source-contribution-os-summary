"""Project-derived Node environments; inspection never runs repository code."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any
from app.environment_resolution import LOCK_OWNERS, package_manager_declaration

NODE_PREPARATION = "node-workspace-preparation-v1"
LOCKS = LOCK_OWNERS
CONFIGS = (".nvmrc", ".node-version", ".bun-version", ".tool-versions", ".yarnrc.yml", "pnpm-workspace.yaml", "bunfig.toml")


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
        locked = next((p for p in candidates if any(str(p / name) in paths for name in LOCKS)), candidates[-1])
        roots.add(str(locked))
    if not roots:
        return None
    if len(roots) != 1:
        return {"status": "unsupported", "manifests": [], "reason_code": "node_workspace_roots_conflict",
                "reason": "修改涉及互不关联的 Node 安装根目录，需要分别冻结执行环境。"}
    directory = roots.pop()
    prefix = "" if directory == "." else directory + "/"
    locks = [prefix + name for name in LOCKS if prefix + name in paths]
    packages = sorted(p for p in paths if p.startswith(prefix) and PurePosixPath(p).name == "package.json"
                      and "node_modules" not in PurePosixPath(p).parts)
    workflows = sorted(p for p in paths if p.startswith(prefix+".github/workflows/") and p.endswith((".yml",".yaml")))
    manifests = sorted(set(packages + locks + workflows + [prefix + c for c in CONFIGS if prefix + c in paths]))
    if len(manifests) > 64:
        return {"status": "unsupported", "manifests": [], "reason_code": "node_manifest_budget",
                "reason": "Node 工作区超过 64 项清单预算，需要缩小执行范围。"}
    return {"status": "supported", "ecosystem": "node", "mode": "workspace-v2",
            "preparation_version": NODE_PREPARATION, "project_directory": directory,
            "manifests": manifests, "lockfiles": locks, "change_paths":list(changes),
            "reason": "按项目声明选择运行时和包管理器，冻结完整工作区依赖；独立离线构建后，先运行修改前测试，再实施及离线验证。"}


def validate_node_recipe(value: object) -> dict:
    """The signed recipe is also validated inside the credential-free Runner."""
    import re
    from pathlib import PurePosixPath
    keys = {"version", "manager", "manager_version", "runtime_requirement", "runtime_pin",
            "project_directory", "package_roots", "linker", "workspace_lifecycle", "registry"}
    if not isinstance(value, dict):
        raise ValueError("Invalid Node environment")
    modern_recipe = value.get("version") == "node-environment-v2"
    if modern_recipe:
        keys |= {"manager_constraint", "lockfile", "lock_strategy"}
    if set(value) != keys or value["version"] not in {"node-environment-v1", "node-environment-v2"}:
        raise ValueError("Invalid Node environment")
    if (value["manager"] not in ({"npm", "yarn", "pnpm", "bun"} if modern_recipe else {"npm", "yarn", "pnpm"})
            or not isinstance(value["manager_version"], str)
            or not (modern_recipe and value["manager_version"] == "" or re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[A-Za-z0-9.-]{1,50})?", value["manager_version"]))
            or value["registry"] != "https://registry.npmjs.org"
            or value["linker"] not in {"node-modules", "pnp"}):
        raise ValueError("Invalid Node manager")
    if modern_recipe and (not isinstance(value["manager_constraint"], str)
            or not re.fullmatch(r"[0-9vVxX.*<>=~^| +\-]{1,160}",value["manager_constraint"])
            or value["lock_strategy"] not in {"existing", "resolve_private"}
            or value["lockfile"] is not None and (not isinstance(value["lockfile"],str)
                or value["lockfile"].startswith("/") or ".." in PurePosixPath(value["lockfile"]).parts
                or "\\" in value["lockfile"])):
        raise ValueError("Invalid Node resolution")
    if modern_recipe:
        lock=value["lockfile"]
        if (value["lock_strategy"]=="existing") != (lock is not None):
            raise ValueError("Invalid Node lock strategy")
        names={"npm":{"package-lock.json","npm-shrinkwrap.json"}, "yarn":{"yarn.lock"},
               "pnpm":{"pnpm-lock.yaml"}, "bun":{"bun.lock","bun.lockb"}}
        if lock and (PurePosixPath(lock).name not in names[value["manager"]]
                or str(PurePosixPath(lock).parent)!=value["project_directory"]):
            raise ValueError("Invalid Node lock binding")
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
        hint = package_manager_declaration(declared)[0] if declared else "pnpm" if prefix+"pnpm-workspace.yaml" in entries else "npm"
        workspace_paths = _workspace_packages(parsed, root, prefix, entries, hint)
        if len(workspace_paths) == 1:
            # An unrelated parent package is not an installation root. Narrow
            # to the closest package when no parent workspace binds the change.
            nested = sorted((p for p in parsed if p != prefix+"package.json"), key=len, reverse=True)
            requested = selected.get("change_paths", [])
            candidate = next((p for p in nested if any(c.startswith(p.removesuffix("package.json")) for c in requested)),None)
            if candidate and not selected.get("lockfiles"):
                directory = candidate.removesuffix("/package.json")
                narrowed = {**selected,"project_directory":directory,
                    "manifests":[p for p in selected["manifests"] if p.startswith(directory+"/")]}
                return inspect_node_environment(narrowed,evidence)
        locks = selected["lockfiles"]
        if declared:
            manager, version = package_manager_declaration(declared)
            if manager not in {"npm", "yarn", "pnpm", "bun"}:
                raise ValueError("node_manager_capability_missing")
            if locks and not any(LOCKS[PurePosixPath(p).name] == manager for p in locks):
                raise ValueError("node_manager_lock_conflict")
        elif len(locks) == 1:
            manager = LOCKS[PurePosixPath(locks[0]).name]
            version = ">=1.0.0"
        elif not locks:
            scripts = root.get("scripts", {})
            if isinstance(scripts,dict) and any(isinstance(v,str) and re.search(r"\bbun\b",v) for v in scripts.values()):
                manager, version = "bun", ">=1.0.0"
            else:
                manager, version = "npm", ">=10.0.0"
        else:
            raise ValueError("node_manager_ambiguous")
        engines = root.get("engines", {})
        if not isinstance(engines, dict):
            raise ValueError("node_runtime_invalid")
        pin = next((entries[prefix + name]["content"].strip() for name in (".node-version", ".nvmrc")
                    if prefix + name in entries), "")
        constraints = [version]
        if manager in engines:
            constraints.append(engines[manager])
        if manager == "bun" and prefix + ".bun-version" in entries:
            exact = entries[prefix + ".bun-version"]["content"].strip()
            package_manager_declaration("bun@"+exact)
            constraints.append(exact)
        tool_versions=entries.get(prefix+".tool-versions",{}).get("content","")
        matches=re.findall(r"(?m)^"+re.escape(manager)+r"\s+(\d+\.\d+\.\d+)\s*$",tool_versions)
        constraints.extend(matches)
        ci_pins=set()
        for path,entry in entries.items():
            if path.startswith(prefix+".github/workflows/") and path.endswith((".yml",".yaml")):
                ci_pins.update(re.findall(r"(?m)^\s*"+re.escape(manager)+r"-version:\s*['\"]?(\d+\.\d+\.\d+)['\"]?\s*(?:#.*)?$",entry["content"]))
        if len(ci_pins)==1:
            constraints.extend(ci_pins)
        version=" ".join(dict.fromkeys(constraints))
        package_manager_declaration(manager+"@"+version)
        linker = "node-modules"
        config = entries.get(prefix + ".yarnrc.yml", {}).get("content", "")
        major = re.search(r"\d+", version)
        if manager == "yarn" and (major is None or int(major.group()) >= 2):
            match = re.search(r"(?m)^nodeLinker:\s*[\"']?([a-z-]+)", config)
            linker = match.group(1) if match else "pnp"
        workspace_paths = _workspace_packages(parsed, root, prefix, entries, manager)
        packages = sorted((p.rsplit("/", 1)[0] if "/" in p else ".") for p in workspace_paths)
        lifecycle = [{"directory": (p.rsplit("/", 1)[0] if "/" in p else "."), "script": name}
                     for p in workspace_paths for meta in [parsed[p]] for name in ("preinstall", "install", "postinstall", "prepare")
                     if isinstance(meta.get("scripts"), dict) and name in meta["scripts"]]
        recipe = {"version": "node-environment-v1", "manager": manager,
            "manager_version": version, "runtime_requirement": engines.get("node", ""), "runtime_pin": pin,
            "project_directory": selected["project_directory"], "package_roots": packages,
            "linker": linker, "workspace_lifecycle": lifecycle, "registry": "https://registry.npmjs.org"}
        chosen_lock = next((p for p in locks if LOCKS[PurePosixPath(p).name] == manager), None)
        if manager == "bun" or chosen_lock is None or chosen_lock.endswith("npm-shrinkwrap.json") or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?",version):
            recipe.update(version="node-environment-v2", manager_constraint=version,
                manager_version="", lockfile=chosen_lock, lock_strategy="existing" if chosen_lock else "resolve_private")
        recipe = validate_node_recipe(recipe)
    except (ValueError, TypeError, KeyError) as exc:
        code = str(exc) if str(exc).startswith("node_") else "node_manifest_invalid"
        return {**selected, "status": "unsupported", "reason_code": code,
                "reason": ("已识别项目的包管理器声明，但当前隔离准备器尚缺少对应工具驱动；不是缺少锁文件，也不需要替换包管理器。"
                    if code == "node_manager_capability_missing" else "项目的包管理器、运行时或工作区声明无法一致冻结；请检查具体配置，不会猜测版本或改写锁文件。")}
    # Only the selected manager's lock participates; unrelated lockfiles remain source data.
    return {**selected, "manifests": sorted([*required, *([chosen_lock] if chosen_lock else [])]), "manifest_hashes": hashes,
            "node_environment": recipe, "capability_version": recipe["version"]}


def validate_node_receipt(value: object, recipe: dict | None = None) -> dict:
    import re
    from pathlib import PurePosixPath
    modern = isinstance(value,dict) and value.get("version")=="node-environment-result-v2"
    keys = {"version", "recipe", "runtime_version", "runtime_bin", "manager_version", "manifest_hashes",
        "built_offline", "execution_image_digest"} | ({"native_packages"} if modern else set())
    if (not isinstance(value, dict) or set(value) != keys
            or value["version"] not in {"node-environment-result-v1","node-environment-result-v2"} or value["built_offline"] is not True):
        raise ValueError("Invalid completed Node environment")
    validate_node_recipe(value["recipe"])
    if recipe is not None and value["recipe"] != recipe:
        raise ValueError("Node environment recipe changed")
    if (not isinstance(value["runtime_version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", value["runtime_version"])
            or (value["recipe"]["version"] == "node-environment-v1" and value["manager_version"] != value["recipe"]["manager_version"])
            or not isinstance(value["manager_version"],str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?",value["manager_version"])
            or not isinstance(value["execution_image_digest"], str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["execution_image_digest"])
            or value["runtime_bin"] not in {"/usr/local/bin", "/dependencies/output/runtime/node_modules/node-linux-arm64/bin",
                                           "/dependencies/output/runtime/node_modules/node-linux-x64/bin"}
            or not isinstance(value["manifest_hashes"], dict) or len(value["manifest_hashes"]) > 64
            or any(not isinstance(p, str) or p.startswith("/") or ".." in PurePosixPath(p).parts
                   or not isinstance(h, str) or not re.fullmatch(r"[a-f0-9]{64}", h) for p, h in value["manifest_hashes"].items())):
        raise ValueError("Invalid Node environment versions or manifests")
    if modern:
        packages=value["native_packages"]
        if not isinstance(packages,list) or not 1<=len(packages)<=4:
            raise ValueError("Invalid native workspace packages")
        seen=set()
        for package in packages:
            if (not isinstance(package,dict) or set(package)!={"package","version","workspace_directory","binary_name","main","binary_hash"}
                    or not isinstance(package["package"],str) or not re.fullmatch(r"(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+-linux-(?:arm64|x64)",package["package"])
                    or package["package"] in seen or package["workspace_directory"] not in value["recipe"]["package_roots"]
                    or not isinstance(package["version"],str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?",package["version"])
                    or not isinstance(package["binary_name"],str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}",package["binary_name"])
                    or not isinstance(package["main"],str) or not re.fullmatch(re.escape(package["binary_name"])+r"\.[A-Za-z0-9_.-]+\.node",package["main"])
                    or not isinstance(package["binary_hash"],str) or not re.fullmatch(r"[a-f0-9]{64}",package["binary_hash"])):
                raise ValueError("Invalid native workspace package")
            seen.add(package["package"])
    return value
