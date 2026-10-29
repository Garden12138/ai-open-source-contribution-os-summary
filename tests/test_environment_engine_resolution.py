"""Pure contracts; these results never count as real/native acceptance."""
import hashlib
import json

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
