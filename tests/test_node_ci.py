"""Frozen CI declarations constrain new UPEVE recipes without executing YAML."""
import hashlib
import json

import pytest

from app.node_ci import ci_toolchains
from app.execution_environment import environment_plan
from app.execution_preflight import inspect_environment
from app.environment_engine.resolver import resolve_contract
from app.environment_engine.contracts import EnvironmentBinding
from app.environment_engine.contracts import VerificationEnvironmentContract
from app.provenance import content_hash
from app.sandbox_worker.specs import SandboxPolicy


CI = """name: Test
jobs:
  verify:
    steps:
      - name: Select manager
        uses: pnpm/action-setup@v4
        with:
          version: '10'
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - run: pnpm test
"""


def evidence(files):
    return {"inventory": sorted(files), "entries": [{"path": path, "content": text,
        "prior_hash": hashlib.sha256(text.encode()).hexdigest()} for path, text in files.items()]}


def test_action_inputs_are_scoped_and_provenanced():
    files = {".github/workflows/check.yml": CI}
    result = ci_toolchains(evidence(files), ".", "pnpm")
    assert result["manager"] == ["10"] and result["runtime"] == ["22"]
    assert [(d["tool"], d["line"]) for d in result["declarations"]] == [("pnpm", 8), ("node", 11)]
    assert all(d["sha256"] == hashlib.sha256(CI.encode()).hexdigest() for d in result["declarations"])
    # An unrelated action's version never becomes a toolchain declaration.
    unrelated = CI.replace('pnpm/action-setup@v4', 'fixture/other-action@v4')
    assert ci_toolchains(evidence({".github/workflows/a.yml": unrelated}), ".", "pnpm")["manager"] == []


@pytest.mark.parametrize("value", ["${{ matrix.pnpm }}", "latest", "|", "[10, 11]"])
def test_dynamic_or_nonliteral_version_is_an_explicit_boundary(value):
    files = {".github/workflows/a.yml": CI.replace("'10'", value)}
    with pytest.raises(ValueError, match="node_ci_toolchain_dynamic"):
        ci_toolchains(evidence(files), ".", "pnpm")


@pytest.mark.parametrize("change", ["flow", "anchor", "duplicate", "jobs", "action"])
def test_ambiguous_or_indirect_yaml_is_not_reinterpreted(change):
    text = CI
    files = {}
    if change == "flow": text = text.replace("with:\n          version: '10'", "with: {version: 10}")
    elif change == "anchor": text = text.replace("version: '10'", "<<: *toolchain")
    elif change == "duplicate": text = text.replace("version: '10'", "version: '10'\n          version: '11'")
    elif change == "jobs": files[".github/workflows/b.yml"] = CI.replace("'10'", "'11'")
    else: text = text.replace("pnpm/action-setup@v4", "pnpm/action-setup@unverified")
    files[".github/workflows/a.yml"] = text
    with pytest.raises(ValueError, match="node_ci_toolchain_"):
        ci_toolchains(evidence(files), ".", "pnpm")


def test_sources_and_runtime_version_files_are_hash_bound():
    text = CI.replace("node-version: 22", "node-version-file: tooling/node.txt")
    files = {".github/workflows/a.yml": text, "tooling/node.txt": "22.17.0\n"}
    result = ci_toolchains(evidence(files), ".", "pnpm")
    assert result["runtime"] == ["22.17.0"]
    assert {ref["path"] for ref in result["sources"]} == set(files)
    broken = evidence(files); broken["entries"][0]["prior_hash"] = "0" * 64
    with pytest.raises(ValueError, match="source_unavailable"):
        ci_toolchains(broken, ".", "pnpm")
    escaped = evidence({".github/workflows/a.yml": text.replace("tooling/node.txt", "../escape")})
    with pytest.raises(ValueError, match="source_unavailable"):
        ci_toolchains(escaped, ".", "pnpm")


def test_only_new_UPEVE_recipes_consume_CI_constraints_and_freeze_provenance():
    files = {"package.json": json.dumps({"private": True, "scripts": {"test": "node test.cjs"}}),
        "test.cjs": "console.log('test')", "pnpm-lock.yaml": "lockfileVersion: '9.0'\n", ".github/workflows/a.yml": CI}
    source = evidence(files)
    selection = environment_plan(list(files), change_paths=["test.cjs"])
    legacy = inspect_environment(selection, source)
    result = inspect_environment(selection, source, engine_version="upeve-v1")
    assert legacy["node_environment"]["manager_constraint"] == ">=1.0.0"
    assert legacy["node_environment"]["runtime_requirement"] == ""
    assert result["status"] == "supported"
    assert result["node_environment"]["manager_constraint"] == ">=1.0.0 10"
    assert result["node_environment"]["runtime_requirement"] == "22"
    command = {"command_id": "tests", "argv": ["pnpm", "test"], "working_directory": "."}
    binding = EnvironmentBinding(task_id="fixture", snapshot_hash="a" * 64, source_archive_hash="b" * 64,
        base_commit_sha="a" * 40, plan_version_hash="c" * 64, approved_commands_hash=content_hash([command]),
        target_platform="linux/arm64", runner_image_digest="sha256:" + "d" * 64, sandbox_policy_hash=SandboxPolicy().policy_hash)
    contract = resolve_contract(binding, [command], result, source)
    assert contract.admission == "static_admitted"
    assert contract.resolver_versions["adapter"] == "node-toolchain-v4"
    assert contract.preparation["toolchain_declarations"] == result["ci_toolchains"]["declarations"]
    tampered = contract.model_dump(mode="json")
    tampered["preparation"]["toolchain_declarations"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="ci_source_missing"):
        VerificationEnvironmentContract.model_validate_json(json.dumps(tampered))


def test_quoted_action_and_literal_ranges_are_bound_without_running_scripts():
    text = CI.replace("pnpm/action-setup@v4", '"pnpm/action-setup@v4"').replace("node-version: 22", 'node-version: ">=20 <23"')
    result = ci_toolchains(evidence({".github/workflows/a.yml": text}), ".", "pnpm")
    assert result["manager"] == ["10"] and result["runtime"] == [">=20 <23"]
    # A shell's lookalike lines cannot provide action input provenance.
    fake = "jobs:\n  x:\n    steps:\n      - run: |\n          uses: pnpm/action-setup@v4\n          with:\n            version: 99\n"
    assert not ci_toolchains(evidence({".github/workflows/a.yml": fake}), ".", "pnpm")["declarations"]


def test_old_contract_versions_keep_their_exact_wire_hash():
    from tests.test_environment_engine_resolution import contract
    value = contract({"package.json": json.dumps({"scripts": {"test": "node test.cjs"}}),
                      "test.cjs": "console.log('test')"}, [{"command_id": "tests", "argv": ["npm", "test"], "working_directory": "."}], "node")
    for version in ("node-toolchain-v1", "node-toolchain-v2", "node-toolchain-v3"):
        wire = value.model_dump(mode="json")
        wire["resolver_versions"]["adapter"] = version
        parsed = VerificationEnvironmentContract.model_validate_json(json.dumps(wire))
        assert parsed.model_dump(mode="json") == wire
        assert parsed.contract_hash == content_hash(wire)


def test_setup_without_version_requires_an_actual_manifest_declaration():
    text = CI.replace("        with:\n          version: '10'\n", "")
    data = evidence({".github/workflows/a.yml": text})
    with pytest.raises(ValueError, match="version_missing"):
        ci_toolchains(data, ".", "pnpm")
    assert ci_toolchains(data, ".", "pnpm", manager_from_manifest=True)["manager"] == []


def test_disjunctive_CI_versions_are_an_ambiguous_boundary():
    with pytest.raises(ValueError, match="ambiguous"):
        ci_toolchains(evidence({".github/workflows/a.yml": CI.replace("'10'", "'10 || 11'")}), ".", "pnpm")
