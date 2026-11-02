"""Native manager provenance and fail-closed preparation boundaries."""
import ast
import hashlib
from inspect import getsource
import json
import os
from pathlib import Path
import platform
import subprocess

import pytest

from app.sandbox_worker.dependency_diagnostics import summarize_dependency_failure, validate_dependency_diagnostic
from app.sandbox_worker.dependencies import container_python_program, _PREPARE_SCRIPT
from app.sandbox_worker.node_dependencies import prepare_node_dependencies
from app.sandbox_worker.node_manager_entrypoints import prepare_native_manager_entrypoint


def installed(tmp_path, monkeypatch, architecture="arm64"):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: {"arm64": "aarch64", "x64": "x86_64"}[architecture])
    monkeypatch.setattr(platform, "libc_ver", lambda: ("glibc", "2.36"))
    tools = tmp_path / "tools"
    wrapper = tools / "node_modules/pnpm"
    native = tools / ("node_modules/@pnpm/exe.linux-" + architecture)
    wrapper.mkdir(parents=True)
    native.mkdir(parents=True)
    name = "@pnpm/exe.linux-" + architecture
    metadata = {"name": "pnpm", "version": "12.17.0", "bin": {x: x for x in ("pnpm", "pn", "pnpx", "pnx")},
                "optionalDependencies": {name: "12.17.0"}, "scripts": {"preinstall": "must never execute"}}
    (wrapper / "package.json").write_text(json.dumps(metadata))
    (native / "package.json").write_text(json.dumps({"name": name, "version": "12.17.0", "os": ["linux"], "cpu": [architecture], "libc": ["glibc"]}))
    for command in metadata["bin"]:
        (wrapper / command).write_text("# pnpm's native binary replaces this file during installation\nexit 1\n" if command == "pnpm" else "#!/bin/sh\nexec pnpm \"$@\"\n")
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (183 if architecture == "arm64" else 62).to_bytes(2, "little")
    binary = native / "pnpm"
    binary.write_bytes(bytes(header) + b"trusted fixture bytes, never executed")
    return tools, wrapper, native, binary


@pytest.mark.parametrize("architecture", ["arm64", "x64"])
def test_native_cli_is_bound_to_declared_platform_package_without_hooks(tmp_path, monkeypatch, architecture):
    tools, wrapper, native, binary = installed(tmp_path, monkeypatch, architecture)
    original = (wrapper / "package.json").read_bytes()
    receipt = prepare_native_manager_entrypoint(str(tools), "pnpm", "12.17.0")
    assert receipt["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert receipt["package_hooks_executed"] is False
    assert receipt["metadata_hashes"]["node_modules/pnpm/package.json"] == hashlib.sha256(original).hexdigest()
    for path in receipt["targets"]:
        assert (tools / path).read_bytes() == binary.read_bytes()
        assert (tools / path).stat().st_mode & 0o777 == 0o555
    assert (wrapper / "package.json").read_bytes() == original
    assert json.loads((tools / "pnpm-native-entrypoint.json").read_text()) == receipt
    assert prepare_native_manager_entrypoint(str(tools), "pnpm", "12.17.0") == receipt
    (wrapper / "pnpm").chmod(0o755)
    (wrapper / "pnpm").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="receipt_changed"):
        prepare_native_manager_entrypoint(str(tools), "pnpm", "12.17.0")


@pytest.mark.parametrize("failure", ["version", "declaration", "bin_path", "binary_arch", "binary_link", "parent_link", "target_link", "missing", "duplicate_key", "unknown_packaging"])
def test_native_cli_rejects_unproven_or_escaping_inputs_before_replacing_bins(tmp_path, monkeypatch, failure):
    tools, wrapper, native, binary = installed(tmp_path, monkeypatch)
    if failure in {"version", "declaration", "bin_path"}:
        path = wrapper / "package.json"
        value = json.loads(path.read_text())
        if failure == "version": value["version"] = "12.16.0"
        elif failure == "declaration": value["optionalDependencies"]["@pnpm/exe.linux-arm64"] = "^12.17.0"
        else: value["bin"]["pn"] = "../escape"
        path.write_text(json.dumps(value))
    elif failure == "binary_arch":
        value = bytearray(binary.read_bytes()); value[18:20] = (62).to_bytes(2, "little"); binary.write_bytes(value)
    elif failure == "binary_link":
        target = tmp_path / "outside"; target.write_bytes(binary.read_bytes()); binary.unlink(); binary.symlink_to(target)
    elif failure == "parent_link":
        target = tmp_path / "outside-package"; native.rename(target); native.symlink_to(target, target_is_directory=True)
    elif failure == "target_link":
        (wrapper / "pn").unlink(); (wrapper / "pn").symlink_to(tmp_path / "outside")
    elif failure == "missing": binary.unlink()
    elif failure == "duplicate_key":
        (native / "package.json").write_text('{"name":"@pnpm/exe.linux-arm64","name":"duplicate"}')
    else: (wrapper / "pnpm").write_text("unknown launcher")
    original = (wrapper / "pnpm").read_bytes()
    with pytest.raises((ValueError, OSError)):
        prepare_native_manager_entrypoint(str(tools), "pnpm", "12.17.0")
    assert (wrapper / "pnpm").read_bytes() == original
    assert not (tools / "pnpm-native-entrypoint.json").exists()


@pytest.mark.parametrize("failure", ["musl", "new_major", "prerelease"])
def test_unknown_native_platform_or_packaging_is_not_guessed(tmp_path, monkeypatch, failure):
    tools, _, _, _ = installed(tmp_path, monkeypatch)
    if failure == "musl": monkeypatch.setattr(platform, "libc_ver", lambda: ("musl", "1.2"))
    version = {"new_major": "13.0.0", "prerelease": "12.18.0-beta"}.get(failure, "12.17.0")
    with pytest.raises(ValueError, match="unsupported"):
        prepare_native_manager_entrypoint(str(tools), "pnpm", version)


def test_legacy_managers_do_not_read_or_rewrite_native_receipts(tmp_path):
    for manager, version in (("npm", "10.9.4"), ("yarn", "4.9.2"), ("bun", "1.3.0"), ("pnpm", "10.11.0"), ("pnpm", "11.28.4")):
        assert prepare_native_manager_entrypoint(str(tmp_path / "absent"), manager, version) is None


def test_process_spawn_errors_are_redacted_preparation_failures(tmp_path, monkeypatch, capsys):
    # Exercise the real nested supervisor at its subprocess boundary. Only the
    # trusted log path changes; no fixture or package program is executed.
    function = next(x for x in ast.walk(ast.parse(getsource(prepare_node_dependencies))) if isinstance(x, ast.FunctionDef) and x.name == "run")
    class LogPath(ast.NodeTransformer):
        def visit_Constant(self, node):
            return ast.copy_location(ast.Constant(str(tmp_path / "bounded.log")), node) if node.value == "/tmp/node-environment.log" else node
    module = ast.fix_missing_locations(LogPath().visit(ast.Module(body=[function], type_ignores=[])))
    def refused(*args, **kwargs):
        raise OSError(8, "untrusted subprocess detail must not escape")
    monkeypatch.setattr(subprocess, "Popen", refused)
    namespace = {"subprocess": subprocess, "json": json, "env": {}, "summarize_dependency_failure": summarize_dependency_failure}
    exec(compile(module, "trusted-node-supervisor", "exec"), namespace)
    with pytest.raises(SystemExit) as error:
        namespace["run"](["pnpm", "--version"], "node_tools")
    assert error.value.code == 111
    output = capsys.readouterr().out
    diagnostic = validate_dependency_diagnostic(json.loads(output))
    assert diagnostic["reason_code"] == "tool_entrypoint_unavailable"
    assert diagnostic["phase"] == "node_tools"
    assert "untrusted" not in output and "OSError" not in output


def test_large_trusted_supervisor_preserves_unicode_and_source_semantics_below_exec_limit():
    source = 'value = "真实源码"\n' + '# bounded supervisor transport\n' * 5000 + 'observed.append(value)\n'
    program = container_python_program(source)
    assert len(program.encode()) < 96000 < len(source.encode())
    namespace = {"observed": []}
    exec(program, namespace)
    assert namespace["observed"] == ["真实源码"]
    assert len(container_python_program(_PREPARE_SCRIPT).encode()) <= 96000
    assert container_python_program('observed.append("small")') == 'observed.append("small")'
    with pytest.raises(ValueError, match="source budget"):
        container_python_program("x" * 512001)
