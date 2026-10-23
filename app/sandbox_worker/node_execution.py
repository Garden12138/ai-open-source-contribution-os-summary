"""Fresh Node execution views over immutable source and frozen dependencies."""
from __future__ import annotations

from app.node_environments import validate_node_recipe


def prepare_node_execution(source: str, dependencies: str, scratch: str, recipe: dict, base_environment: dict) -> tuple[str, dict, list]:
    import hashlib
    import json
    import os
    from pathlib import Path
    import shutil
    import subprocess

    recipe = validate_node_recipe(recipe)
    output = Path(dependencies)
    receipt_file = output / "node-environment.json"
    if receipt_file.is_symlink():
        raise ValueError("Invalid Node environment receipt")
    receipt = json.loads(receipt_file.read_text())
    if (receipt.get("version") != "node-environment-result-v1" or receipt.get("recipe") != recipe
            or receipt.get("built_offline") is not True
            or receipt.get("manager_version") != recipe["manager_version"]):
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
                (destination / entry.name).symlink_to(entry)
    prefix = "" if recipe["project_directory"] == "." else recipe["project_directory"]
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
