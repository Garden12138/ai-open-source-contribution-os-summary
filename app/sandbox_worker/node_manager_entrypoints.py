"""Versioned native manager adapters; never run package installation hooks."""


def prepare_native_manager_entrypoint(tools: str, manager: str, version: str) -> dict | None:
    """Materialize declared pnpm 12 Linux binaries from installed optional packages.

    This function is embedded in the credential-free preparation supervisor.
    Package code is not imported, hooks are not run, and no download is added.
    The receipt is included in the frozen dependency inventory.
    """
    import hashlib
    import json
    import os
    from pathlib import Path
    import platform
    import re
    import shutil
    import stat
    import tempfile

    if manager != "pnpm":
        return None
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("native_manager_version_invalid")
    if int(version.split(".")[0]) < 12:
        return None
    if not re.fullmatch(r"12\.\d+\.\d+", version):
        raise ValueError("native_manager_version_unsupported")
    architecture = {"aarch64": ("arm64", 183), "x86_64": ("x64", 62)}.get(platform.machine())
    if platform.system() != "Linux" or platform.libc_ver()[0] != "glibc" or architecture is None:
        raise ValueError("native_manager_platform_unsupported")
    root = Path(tools)
    if not root.is_absolute() or root.resolve() != root or root.is_symlink():
        raise ValueError("native_manager_root_invalid")

    def checked(relative):
        path = root
        for part in relative.split("/"):
            if part in {"", ".", ".."} or "\\" in part:
                raise ValueError("native_manager_path_invalid")
            path = path / part
            if path.is_symlink():
                raise ValueError("native_manager_symlink_denied")
        return path

    def manifest(relative):
        path = checked(relative)
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("native_manager_metadata_invalid")
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError("native_manager_duplicate_key")
                result[key] = value
            return result
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(value, dict):
            raise ValueError("native_manager_metadata_invalid")
        return value, hashlib.sha256(raw).hexdigest()

    wrapper_path = "node_modules/pnpm"
    wrapper, wrapper_hash = manifest(wrapper_path + "/package.json")
    names = ("pnpm", "pn", "pnpx", "pnx")
    native_name = "@pnpm/exe.linux-" + architecture[0]
    if (wrapper.get("name") != manager or wrapper.get("version") != version
            or wrapper.get("bin") != {name: name for name in names}
            or not isinstance(wrapper.get("optionalDependencies"), dict)
            or wrapper["optionalDependencies"].get(native_name) != version):
        raise ValueError("native_manager_declaration_mismatch")
    candidates = [prefix + "/" + native_name for prefix in (wrapper_path + "/node_modules", "node_modules")
                  if checked(prefix + "/" + native_name).exists()]
    if len(candidates) != 1:
        raise ValueError("native_manager_platform_package_unavailable")
    native_path = candidates[0]
    native, native_hash = manifest(native_path + "/package.json")
    if (native.get("name") != native_name or native.get("version") != version
            or native.get("os") != ["linux"] or native.get("cpu") != [architecture[0]]
            or native.get("libc", ["glibc"]) != ["glibc"]):
        raise ValueError("native_manager_platform_package_mismatch")
    binary = checked(native_path + "/pnpm")
    if not binary.is_file():
        raise ValueError("native_manager_binary_invalid")
    with binary.open("rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 64 <= metadata.st_size <= 256 * 1024 * 1024:
            raise ValueError("native_manager_binary_invalid")
        header = handle.read(64)
        if (header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != architecture[1]):
            raise ValueError("native_manager_binary_platform_mismatch")
        handle.seek(0)
        binary_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    targets = [wrapper_path + "/" + name for name in names]
    receipt = {"version": "native-node-manager-entrypoint-v1", "adapter": "pnpm-native-12-linux-glibc-v1",
               "manager": manager, "manager_version": version,
               "target_platform": "linux/" + {"arm64": "arm64", "x64": "amd64"}[architecture[0]],
               "source_package": native_name, "source_binary": native_path + "/pnpm",
               "binary_sha256": binary_hash, "binary_size": metadata.st_size,
               "metadata_hashes": {wrapper_path + "/package.json": wrapper_hash,
                                   native_path + "/package.json": native_hash},
               "targets": targets, "package_hooks_executed": False}
    receipt_path = checked("pnpm-native-entrypoint.json")
    if receipt_path.exists():
        if (json.loads(receipt_path.read_text()) != receipt
                or any(hashlib.sha256(checked(path).read_bytes()).hexdigest() != binary_hash for path in targets)):
            raise ValueError("native_manager_receipt_changed")
        return receipt
    # Validate every destination before replacing any placeholder. Unknown
    # packaging never falls back to a shell or an unbound download.
    for target in targets:
        path = checked(target)
        if not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("native_manager_placeholder_invalid")
        with path.open("rb") as handle:
            if target.endswith("/pnpm") and not handle.read(128).startswith(b"# pnpm's native binary replaces this file"):
                raise ValueError("native_manager_placeholder_invalid")
    for target in targets:
        descriptor, temporary = tempfile.mkstemp(prefix=".upeve-native-", dir=checked(wrapper_path))
        try:
            with os.fdopen(descriptor, "wb") as output, binary.open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
                os.fchmod(output.fileno(), 0o555)
            if hashlib.sha256(Path(temporary).read_bytes()).hexdigest() != binary_hash:
                raise ValueError("native_manager_source_changed")
            os.replace(temporary, checked(target))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    with receipt_path.open("x") as handle:
        json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
    return receipt
