"""One project-driven Node recipe, run only in credential-free containers."""
from __future__ import annotations

from app.node_environments import validate_node_recipe
from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure


def restore_unchanged_json_manifest(source_path: str, target_path: str, expected_hash: str) -> None:
    """Undo package-manager formatting only, preserving the frozen source bytes."""
    import hashlib
    import json
    from pathlib import Path

    source, target = Path(source_path), Path(target_path)
    if source.is_symlink() or target.is_symlink():
        raise ValueError("manifest_symlink_denied")
    original, actual = source.read_bytes(), target.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_hash:
        raise ValueError("manifest_source_hash_mismatch")
    if hashlib.sha256(actual).hexdigest() == expected_hash:
        return

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("manifest_duplicate_key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("manifest_invalid_constant")

    def canonical(value):
        return json.dumps(json.loads(value, object_pairs_hook=pairs,
            parse_constant=invalid_constant), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    if canonical(original) != canonical(actual):
        raise ValueError("manifest_declarations_changed")
    target.write_bytes(original)


def prepare_node_dependencies(plan: dict, environment: dict, offline: bool) -> None:
    """Network acquisition excludes scripts; a second offline container builds."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import platform
    import re
    import shutil
    import signal
    import stat
    import subprocess
    import time

    recipe = validate_node_recipe(plan["node_environment"])
    output = Path("/dependencies/output")
    project = output / "project"
    tools = output / "tools"
    root = recipe["project_directory"]
    prefix = "" if root == "." else root + "/"
    work = project / ("" if root == "." else root)
    manager = recipe["manager"]
    version = recipe["manager_version"]
    modern = manager == "yarn" and bool(version) and int(version.split(".")[0]) >= 2
    env = {**environment, "CI": "1", "HUSKY": "0", "COREPACK_ENABLE_AUTO_PIN": "0",
           "COREPACK_ENABLE_NETWORK": "0", "NPM_CONFIG_USERCONFIG": "/dev/null",
           "npm_config_userconfig": "/dev/null", "npm_config_registry": recipe["registry"],
           "npm_config_audit": "false", "npm_config_fund": "false",
           "npm_config_update_notifier": "false", "YARN_IGNORE_PATH": "1",
           "YARN_ENABLE_TELEMETRY": "0", "YARN_ENABLE_PROGRESS_BARS": "0"}
    if not offline:
        env.update(YARN_HTTP_PROXY=environment["HTTP_PROXY"], YARN_HTTPS_PROXY=environment["HTTPS_PROXY"])

    def run(argv, phase, cwd=None, capture=False):
        # Logs stay in bounded tmpfs. Only machine-readable redacted categories
        # leave this container; package output is never a business error body.
        path = "/tmp/node-environment.log"
        with open(path, "wb") as log:
            child = subprocess.Popen(argv, cwd=cwd or "/tmp", env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            started = time.monotonic()
            checked = started - 1
            while child.poll() is None:
                exhausted = False
                now = time.monotonic()
                if now - checked >= .5:
                    checked = now
                    total = count = 0
                    for directory, dirs, files in os.walk(output, followlinks=False):
                        for name in files:
                            try:
                                metadata = os.lstat(os.path.join(directory, name))
                            except FileNotFoundError:
                                continue
                            if stat.S_ISREG(metadata.st_mode): total += metadata.st_size
                            count += 1
                            if total > plan["disk_bytes"] or count > 200000:
                                exhausted = True
                                break
                        if exhausted: break
                if exhausted or now - started > 480 or os.path.getsize(path) > 4 * 1024 * 1024:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    child.wait()
                    print(json.dumps(summarize_dependency_failure(phase, 1,
                        "No space left on device" if exhausted else "Node environment budget exceeded")))
                    raise SystemExit(111)
                time.sleep(.1)
        data = Path(path).read_bytes()
        if child.returncode:
            print(json.dumps(summarize_dependency_failure(phase, child.returncode, data.decode(errors="replace"))))
            raise SystemExit(111)
        return data.decode().strip() if capture else ""

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    if not offline:
        if output.exists() and any(output.iterdir()):
            raise SystemExit(110)
        output.mkdir(exist_ok=True)
        project.mkdir()
        # Installation sees the complete workspace, not only package.json.
        # Repository manager configuration cannot inject credentials or redirect
        # the fixed proxy; its relevant declarations were inspected into recipe.
        def ignore(directory, names):
            return [name for name in names if name in {".git", "node_modules", ".npmrc", ".yarnrc", ".yarnrc.yml", "bunfig.toml"}]
        shutil.copytree("/workspace", project, dirs_exist_ok=True, symlinks=True, ignore=ignore)
        run(["npm", "install", "--prefix", str(tools), "--ignore-scripts", "--no-audit", "--no-fund",
             "--registry=" + recipe["registry"], "npm@10.9.4"], "node_tools")
        semver = str(tools / "node_modules/npm/node_modules/semver")
        selector = "const s=require(process.argv[1]);const v=JSON.parse(process.argv[2]);const ranges=JSON.parse(process.argv[3]).filter(Boolean);const a=v.filter(x=>ranges.every(r=>s.satisfies(x,r)));process.stdout.write(s.rsort(a)[0]||'');"
        arch = {"aarch64":"aarch64", "x86_64":"x64"}.get(platform.machine())
        package = "@oven/bun-linux-" + str(arch) if manager == "bun" else "@yarnpkg/cli-dist" if modern else manager
        if not version:
            if manager == "yarn" and (recipe["linker"] == "pnp" or recipe["manager_constraint"].lstrip("^~>=v ").startswith(("2", "3", "4"))):
                package = "@yarnpkg/cli-dist"
            versions = json.loads(run(["npm", "view", package, "versions", "--json", "--registry="+recipe["registry"]],
                                      "node_tools", capture=True))
            version = run(["node", "-e", selector, semver, json.dumps(versions), json.dumps([recipe["manager_constraint"]])],
                          "node_tools", capture=True)
            if not version:
                print(json.dumps(summarize_dependency_failure("node_tools",1,"No matching toolchain version")))
                raise SystemExit(111)
            modern = manager == "yarn" and int(version.split(".")[0]) >= 2
            package = "@yarnpkg/cli-dist" if modern else package
        run(["npm", "install", "--prefix", str(tools), "--ignore-scripts", "--no-audit", "--no-fund",
             "--registry="+recipe["registry"], package+"@"+version], "node_tools")
        if manager == "bun":
            executable = tools / "node_modules" / package / "bin/bun"
            if arch is None or not executable.is_file() or executable.is_symlink():
                raise SystemExit(110)
            (tools / "node_modules/.bin/bun").symlink_to(executable)
        current = run(["node", "--version"], "node_runtime", capture=True).lstrip("v")
        constraints = [recipe["runtime_requirement"], recipe["runtime_pin"]]
        actual = run(["node", "-e", selector, semver, json.dumps([current]), json.dumps(constraints)], "node_runtime", capture=True)
        runtime_bin = "/usr/local/bin"
        if not actual:
            arch = {"aarch64": "arm64", "x86_64": "x64"}.get(platform.machine())
            if arch is None:
                raise SystemExit(110)
            distribution = "node-linux-" + arch
            versions = json.loads(run(["npm", "view", distribution, "versions", "--json",
                                      "--registry=" + recipe["registry"]], "node_runtime", capture=True))
            actual = run(["node", "-e", selector, semver, json.dumps(versions), json.dumps(constraints)], "node_runtime", capture=True)
            if not actual:
                print(json.dumps(summarize_dependency_failure("node_runtime", 1, "No matching runtime version")))
                raise SystemExit(111)
            run(["npm", "install", "--prefix", str(output / "runtime"), "--ignore-scripts", "--no-audit", "--no-fund",
                 "--registry=" + recipe["registry"], distribution + "@" + actual], "node_runtime")
            runtime_bin = str(output / "runtime/node_modules" / distribution / "bin")
        env["PATH"] = str(tools / "node_modules/.bin") + ":" + runtime_bin + ":/usr/local/bin:/usr/bin:/bin"
        env["npm_config_nodedir"] = str(Path(runtime_bin).parent)
        if run(["node", "--version"], "node_runtime", capture=True).lstrip("v") != actual:
            raise SystemExit(110)
        if run([manager, "--version"], "node_tools", cwd=work, capture=True) != version:
            raise SystemExit(110)
        lock = recipe.get("lockfile") if recipe["version"] == "node-environment-v2" else next(
            m["path"] for m in plan["manifests"] if m["path"].endswith(
                {"npm":"package-lock.json","yarn":"yarn.lock","pnpm":"pnpm-lock.yaml"}[manager]))
        # The default Yarn registry is an npm mirror. Normalize only that exact
        # public alias in the private installation copy; original source stays
        # immutable and package-manager integrity checks remain enabled.
        if lock and not lock.endswith("bun.lockb"):
            text = (project / lock).read_text()
            (project / lock).write_text(text.replace("https://registry.yarnpkg.com/", "https://registry.npmjs.org/"))
        if manager == "bun":
            env.update(BUN_INSTALL_CACHE_DIR=str(output/"cache"), BUN_CONFIG_NO_CLEAR_TERMINAL="1",
                       BUN_CONFIG_SKIP_AUTO_INSTALL="1")
            (work/"bunfig.toml").write_text('[install]\nregistry="https://registry.npmjs.org"\nlinker="hoisted"\n')
        if manager == "bun":
            argv = [manager,"install","--ignore-scripts","--registry",recipe["registry"],"--linker=hoisted"]
            if lock: argv.append("--frozen-lockfile")
        elif modern:
            (work / ".yarnrc.yml").write_text("nodeLinker: " + recipe["linker"] + "\nenableGlobalCache: false\n")
            argv = [manager, "install", "--immutable", "--mode=skip-build"]
        elif manager == "yarn":
            argv = [manager, "install", "--frozen-lockfile", "--ignore-scripts", "--non-interactive",
                    "--registry", recipe["registry"], "--cache-folder", str(output / "cache")]
        elif manager == "pnpm":
            argv = [manager, "install", "--frozen-lockfile", "--ignore-scripts",
                    "--store-dir", str(output / "cache"), "--config.manage-package-manager-versions=false"]
        elif not lock:
            argv = [manager,"install","--ignore-scripts","--no-audit","--no-fund","--cache",str(output/"cache")]
        else:
            argv = [manager, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--cache", str(output / "cache")]
        if not lock:
            argv = [a for a in argv if a not in {"--immutable","--frozen-lockfile"}]
        run(argv, "node_resolve", cwd=work)
        native_packages = []
        # NAPI workspaces are source links, so their published optional platform
        # packages are not installed as registry dependencies. Acquire exact
        # same-version platform binaries separately, with scripts disabled.
        for directory in recipe["package_roots"]:
            manifest = project / ("" if directory == "." else directory) / "package.json"
            metadata = json.loads(manifest.read_text())
            napi = metadata.get("napi")
            if not isinstance(napi, dict) or not isinstance(napi.get("binaryName"), str):
                continue
            name, release, binary = metadata.get("name"), metadata.get("version"), napi["binaryName"]
            if (not isinstance(name,str) or not re.fullmatch(r"(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+",name)
                    or not isinstance(release,str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?",release)
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}",binary) or len(native_packages)>=4):
                raise SystemExit(110)
            target_arch = {"aarch64":"arm64","x86_64":"x64"}.get(platform.machine())
            if target_arch is None: raise SystemExit(110)
            package_name = name + "-linux-" + target_arch
            if (work/"node_modules"/package_name).is_dir() or (manifest.parent/"node_modules"/package_name).is_dir():
                continue
            native_root = output / "native"
            run(["npm","install","--prefix",str(native_root),"--ignore-scripts","--no-audit","--no-fund",
                 "--registry="+recipe["registry"],package_name+"@"+release],"node_native")
            package_root = native_root / "node_modules" / package_name
            leaf = json.loads((package_root/"package.json").read_text())
            main = leaf.get("main", "")
            if isinstance(main,str) and main.startswith("./"):
                main = main[2:]
            if (leaf.get("name")!=package_name or leaf.get("version")!=release
                    or leaf.get("os")!=["linux"] or leaf.get("cpu")!=[target_arch]
                    or not isinstance(main,str) or not re.fullmatch(re.escape(binary)+r"\.[A-Za-z0-9_.-]+\.node",main)
                    or (package_root/main).is_symlink() or not (package_root/main).is_file()):
                raise SystemExit(110)
            native_packages.append({"package":package_name,"version":release,"workspace_directory":directory,
                "binary_name":binary,"main":main,"binary_hash":digest(package_root/main)})
        # Private lock generation does not authorize changing the project's
        # package declarations, even inside the disposable installation copy.
        for manifest in plan["manifests"]:
            if manifest["path"].endswith("package.json"):
                try:
                    # Yarn 4 reformats package.json even with scripts disabled.
                    # Preserve exact frozen bytes only after strict semantic
                    # equality; changed declarations still fail closed.
                    restore_unchanged_json_manifest(str(Path("/workspace")/manifest["path"]),
                        str(project/manifest["path"]), manifest["content_hash"])
                except (ValueError, OSError):
                    raise SystemExit(110) from None
        hashes = {m["path"]: digest(project / m["path"]) for m in plan["manifests"]
                  if (project / m["path"]).is_file() and not m["path"].endswith((".yarnrc.yml","bunfig.toml"))}
        if not lock:
            generated = {"npm":"package-lock.json","yarn":"yarn.lock","pnpm":"pnpm-lock.yaml","bun":"bun.lock"}[manager]
            lock = prefix + generated
            if not (project/lock).is_file(): raise SystemExit(110)
            hashes[lock] = digest(project/lock)
        receipt = {"version": "node-environment-result-v2" if native_packages else "node-environment-result-v1",
            "recipe": recipe, "runtime_version": actual, "runtime_bin": runtime_bin,
            "execution_image_digest": plan.get("builder_image_digest") or plan["runner_image_digest"],
            "manager_version": version, "manifest_hashes": hashes, "built_offline": False}
        if native_packages: receipt["native_packages"] = native_packages
        (output / "node-environment.json").write_text(json.dumps(receipt))
    else:
        receipt_path = output / "node-environment.json"
        receipt = json.loads(receipt_path.read_text())
        if receipt["recipe"] != recipe or receipt["built_offline"]:
            raise SystemExit(110)
        env["PATH"] = str(tools / "node_modules/.bin") + ":" + receipt["runtime_bin"] + ":/usr/local/bin:/usr/bin:/bin"
        version = receipt["manager_version"]
        modern = manager == "yarn" and int(version.split(".")[0]) >= 2
        env["npm_config_nodedir"] = str(Path(receipt["runtime_bin"]).parent)
        env.update(YARN_ENABLE_NETWORK="0", npm_config_offline="true", npm_config_ignore_scripts="false")
        if run(["node", "--version"], "node_runtime", capture=True).lstrip("v") != receipt["runtime_version"]:
            raise SystemExit(110)
        if manager == "bun":
            env.update(BUN_INSTALL_CACHE_DIR=str(output/"cache"), BUN_CONFIG_SKIP_AUTO_INSTALL="1")
            argv = [manager,"install","--offline","--frozen-lockfile","--registry",recipe["registry"],"--linker=hoisted"]
        elif modern:
            argv = [manager, "install", "--immutable", "--immutable-cache"]
        elif manager == "yarn":
            argv = [manager, "install", "--offline", "--frozen-lockfile", "--non-interactive",
                    "--force", "--cache-folder", str(output / "cache")]
        elif manager == "pnpm":
            argv = [manager, "rebuild", "--pending", "--config.manage-package-manager-versions=false"]
        else:
            argv = [manager, "rebuild", "--offline", "--cache", str(output / "cache")]
        run(argv, "node_build", cwd=work)
        for path, expected in receipt["manifest_hashes"].items():
            if path.endswith("package.json"):
                try:
                    restore_unchanged_json_manifest(str(Path("/workspace")/path), str(project/path), expected)
                except (ValueError, OSError):
                    raise SystemExit(110) from None
        if any(digest(project / path) != expected for path, expected in receipt["manifest_hashes"].items()):
            raise SystemExit(110)
        # Workspace links must resolve to the current execution source, never
        # to the baseline source copied during dependency installation.
        for directory, dirs, files in os.walk(project, followlinks=False):
            for name in [*dirs, *files]:
                path = Path(directory) / name
                if not path.is_symlink():
                    continue
                target = os.path.normpath(str(path.parent / os.readlink(path)))
                if target.startswith(str(project) + "/"):
                    relative = os.path.relpath(target, project)
                    if "node_modules" not in Path(relative).parts:
                        path.unlink()
                        path.symlink_to("/execution/workspace/" + relative)
                        target = str(path.readlink())
                if not (target.startswith(str(output) + "/") or target.startswith("/execution/workspace/")):
                    raise SystemExit(110)
        receipt["built_offline"] = True
        receipt_path.write_text(json.dumps(receipt))
