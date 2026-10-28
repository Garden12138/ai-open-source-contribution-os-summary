"""Fresh Node execution views over immutable source and frozen dependencies."""
from __future__ import annotations

from app.node_environments import validate_node_recipe, validate_node_receipt


def prepare_node_execution(source: str, dependencies: str, scratch: str, recipe: dict, base_environment: dict) -> tuple[str, dict, list]:
    import hashlib
    import json
    import os
    from pathlib import Path, PurePosixPath
    import shutil
    import subprocess

    recipe = validate_node_recipe(recipe)
    output = Path(dependencies)
    receipt_file = output / "node-environment.json"
    if receipt_file.is_symlink():
        raise ValueError("Invalid Node environment receipt")
    receipt = validate_node_receipt(json.loads(receipt_file.read_text()), recipe)
    if (receipt.get("version") not in {"node-environment-result-v1","node-environment-result-v2","node-environment-result-v3"} or receipt.get("recipe") != recipe
            or receipt.get("built_offline") is not True
            or recipe["version"] == "node-environment-v1" and receipt.get("manager_version") != recipe["manager_version"]):
        raise ValueError("Node environment does not match execution")
    runtime_bin = receipt.get("runtime_bin")
    if runtime_bin not in {"/usr/local/bin", str(output / "runtime/node_modules/node-linux-arm64/bin"),
                           str(output / "runtime/node_modules/node-linux-x64/bin")}:
        raise ValueError("Invalid Node runtime path")
    target = Path(scratch) / "workspace"
    if target.exists():
        raise ValueError("Node execution view is not fresh")
    originals = []
    size = 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("Node source links require explicit materialization")
        for name in files:
            path = Path(directory) / name
            size += path.stat().st_size
            if size > 512 * 1024 * 1024 or len(originals) >= 50000:
                raise ValueError("Node execution source budget exceeded")
            originals.append((str(path.relative_to(source)), hashlib.sha256(path.read_bytes()).hexdigest()))
    shutil.copytree(source, target)
    for cache in receipt.get("cache_files", []):
        frozen = output / cache["path"]
        if not frozen.is_symlink() or os.readlink(frozen) != cache["target"]:
            raise ValueError("Frozen Node cache binding changed")
        # Only the exact receipt-bound data file becomes writable. Original
        # dependency code and launchers stay read-only; scratch stays noexec.
        temporary = Path(scratch) / Path(cache["target"]).relative_to("/execution")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb"):
            pass
    project = output / "project"
    for package in recipe["package_roots"]:
        relative = "" if package == "." else package
        modules = project / relative / "node_modules"
        destination = target / relative / "node_modules"
        if modules.is_dir():
            if destination.exists() or destination.is_symlink():
                raise ValueError("Tracked Node modules cannot be replaced")
            # Tools such as Vite write node_modules/.vite-temp and .vite caches.
            # Keep packages linked to the frozen read-only volume while making
            # their containing directory private to this fresh execution view.
            destination.mkdir(parents=True)
            for count, entry in enumerate(sorted(modules.iterdir(), key=lambda p: p.name)):
                if count >= 10000:
                    raise ValueError("Node module view exceeds package budget")
                if entry.name in {".vite", ".vite-temp", ".cache"}:
                    continue
                if entry.name.startswith("@") and entry.is_dir():
                    # A native platform package may share this scope. Keep its
                    # parent private so adding a link cannot write into the
                    # frozen dependency volume through a scope-directory link.
                    scope = destination / entry.name
                    scope.mkdir()
                    for scoped_count, package in enumerate(sorted(entry.iterdir(), key=lambda p: p.name)):
                        if scoped_count >= 10000:
                            raise ValueError("Node scope view exceeds package budget")
                        (scope / package.name).symlink_to(package)
                else:
                    (destination / entry.name).symlink_to(entry)
    prefix = "" if recipe["project_directory"] == "." else recipe["project_directory"]
    for native in receipt.get("native_packages", []):
        package = output / "native/node_modules" / native["package"]
        binary = package / native["main"]
        if binary.is_symlink() or not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest()!=native["binary_hash"]:
            raise ValueError("Frozen native package changed")
        destination = target / prefix / "node_modules" / native["package"]
        if destination.exists() or destination.is_symlink():
            raise ValueError("Native package conflicts with declared dependencies")
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.symlink_to(package)
        workspace = target / native["workspace_directory"]
        metadata = json.loads((workspace / "package.json").read_text())
        if (metadata.get("name") != native["package"].rsplit("-linux-", 1)[0]
                or metadata.get("version") != native["version"]
                or metadata.get("napi", {}).get("binaryName") != native["binary_name"]):
            raise ValueError("Native workspace no longer matches frozen package")
        # Source-workspace loaders can load beside their declared entrypoint
        # instead of resolving an optional platform package through node_modules.
        # Supply the same frozen binary in the disposable view only.
        main = metadata.get("main")
        if main is not None:
            if not isinstance(main, str):
                raise ValueError("Invalid native workspace entrypoint")
            main = main.removeprefix("./")
            if (not main or PurePosixPath(main).is_absolute()
                    or any(p in {"", ".", "..", "node_modules", ".git"} for p in main.split("/"))
                    or "\\" in main or PurePosixPath(main).suffix not in {".js", ".cjs", ".mjs"}
                    or not (workspace / main).is_file()):
                raise ValueError("Invalid native workspace entrypoint")
            local_binary = workspace / PurePosixPath(main).parent / native["main"]
            if local_binary.exists() or local_binary.is_symlink():
                raise ValueError("Native binary conflicts with frozen source")
            local_binary.symlink_to(binary)
    for name in (".pnp.cjs", ".pnp.loader.mjs", ".pnp.data.json"):
        prepared = project / prefix / name
        destination = target / prefix / name
        if prepared.is_file():
            if destination.exists() and destination.read_bytes() != prepared.read_bytes():
                raise ValueError("Tracked Node loader differs from frozen installation")
            shutil.copyfile(prepared, destination)
    for name in ("cache", "unplugged", "install-state.gz"):
        prepared = project / prefix / ".yarn" / name
        destination = target / prefix / ".yarn" / name
        if prepared.exists():
            if destination.exists():
                if prepared.is_dir() and destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(prepared)
    tools = str(output / "tools/node_modules/.bin")
    modules_bin = str(target / prefix / "node_modules/.bin")
    env = {"PATH": tools + ":" + runtime_bin + ":" + modules_bin + ":/usr/local/bin:/usr/bin:/bin",
           "NODE_PATH": str(target / prefix / "node_modules"), "CI": "1", "HUSKY": "0",
           "COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_AUTO_PIN": "0", "YARN_IGNORE_PATH": "1",
           "YARN_ENABLE_NETWORK": "0", "YARN_ENABLE_TELEMETRY": "0", "YARN_ENABLE_GLOBAL_CACHE": "0",
           "npm_config_offline": "true", "NPM_CONFIG_USERCONFIG": "/dev/null",
           "npm_config_userconfig": "/dev/null", "npm_config_audit": "false", "npm_config_fund": "false"}
    env["npm_config_nodedir"] = str(Path(runtime_bin).parent)
    if recipe["manager"] == "bun":
        env.update(BUN_CONFIG_SKIP_AUTO_INSTALL="1", BUN_INSTALL_CACHE_DIR=str(output / "cache"))
    pnp = target / prefix / ".pnp.cjs"
    if recipe["linker"] == "pnp":
        if not pnp.is_file():
            raise ValueError("Frozen Yarn loader is missing")
        # This executes only in the fresh networkless verification container.
        env["NODE_OPTIONS"] = "--require=" + str(pnp)
        loader = target / prefix / ".pnp.loader.mjs"
        if loader.is_file():
            env["NODE_OPTIONS"] += " --experimental-loader=" + str(loader)
    child_environment = {"HOME": "/tmp", "LANG": "C.UTF-8", **base_environment, **env}
    # /tmp is reserved for bounded supervisor logs. Test runners and package
    # managers use the existing larger per-run tmpfs for temporary/cache data.
    for name in ("tmp", "cache", "home"):
        (Path(scratch) / name).mkdir(exist_ok=True)
    env.update(TMPDIR=str(Path(scratch) / "tmp"), TMP=str(Path(scratch) / "tmp"),
        TEMP=str(Path(scratch) / "tmp"), XDG_CACHE_HOME=str(Path(scratch) / "cache"),
        HOME=str(Path(scratch) / "home"))
    child_environment.update(env)
    for executable, expected in (("node", "v" + receipt["runtime_version"]),
                                 (recipe["manager"], receipt["manager_version"])):
        with open("/tmp/node-runtime-probe.log", "wb") as log:
            probe = subprocess.run([executable, "--version"], env=child_environment,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=20, check=False)
        with open("/tmp/node-runtime-probe.log", "rb") as log:
            actual = log.read(256).decode(errors="replace").strip()
        if probe.returncode or actual != expected:
            raise ValueError("Execution runtime differs from frozen environment")
    for item in recipe["workspace_lifecycle"]:
        directory = target / ("" if item["directory"] == "." else item["directory"])
        with open("/tmp/node-workspace-build.log", "wb") as log:
            completed = subprocess.run([recipe["manager"], "run", item["script"]], cwd=directory,
                env=child_environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                timeout=120, check=False)
        if completed.returncode:
            raise ValueError("Offline workspace initialization failed")
    return str(target), env, originals


def verify_node_source(directory: str, originals: list) -> None:
    import hashlib
    from pathlib import Path
    for relative, expected in originals:
        path = Path(directory) / relative
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Tests changed frozen workspace source")
