"""Registry-driven frozen command/dependency graph. No account/project rules."""
from __future__ import annotations

from typing import Protocol
from app.environment_engine.contracts import (CommandNode, EnvironmentBinding, Gap,
    VerificationEnvironmentContract)
from app.environment_engine.source import FrozenSource, ResolutionError
from app.environment_engine.python import pep621, requirements, declared_pip_inputs, check_conflicts
from app.provenance import content_hash


class ManifestResolver(Protocol):
    version: str
    def resolve(self, source: FrozenSource, selected: dict, commands: list[dict]) -> tuple[list, list[CommandNode], dict]: ...


# These are tool/distribution capabilities, never automatic package requests.
# A matching declared distribution AND installed entry_points are both required.
PYTHON_TOOLS = {"pytest": "pytest", "ruff": "ruff", "mypy": "mypy", "stubtest": "mypy",
    "coverage": "coverage", "sphinx-build": "sphinx", "flake8": "flake8", "black": "black"}


class PythonResolver:
    version = "python-toolchain-v2"

    def resolve(self, source: FrozenSource, selected: dict, commands: list[dict]) -> tuple[list, list[CommandNode], dict]:
        if selected.get("project_directories"):
            raise ResolutionError("python_independent_contracts_required")
        declarations = []
        roots = [p for p in selected["manifests"] if p.endswith((".txt", ".lock"))]
        for path in selected["manifests"]:
            if path.endswith("pyproject.toml"):
                declarations.extend(pep621(source, path))
        declared_roots, constraints, inline = declared_pip_inputs(source, selected.get("project_directory", "."))
        roots.extend(declared_roots)
        declarations.extend(inline)
        declarations.extend(requirements(source, list(dict.fromkeys(roots)), constraint_roots=constraints))
        if len(declarations) > 300:
            raise ResolutionError("requirements_count_limit")
        check_conflicts(declarations)
        installed = {d.distribution for d in declarations if not d.constraint}
        nodes = []
        for cmd in commands:
            argv = tuple(cmd["argv"])
            executable = argv[0]
            module = argv[2] if executable in {"python", "python3"} and len(argv) > 2 and argv[1] == "-m" else None
            if executable in {"python", "python3"} and module is None:
                raise ResolutionError("verification_command_semantics_unresolved")
            tool = module.split(".")[0] if module else executable
            distribution = PYTHON_TOOLS.get(tool)
            if tool not in {"python", "python3", "unittest", "compileall"} and (not distribution or distribution not in installed):
                raise ResolutionError("tool_distribution_unresolved")
            refs = tuple(dict.fromkeys(d.source for d in declarations if d.distribution == distribution))
            nodes.append(CommandNode(**{**cmd, "argv": argv}, required_executable=executable,
                required_module=module, distributions=(distribution,) if distribution else (), source_refs=refs))
        return declarations, nodes, {"requirements": [d.requirement for d in declarations if not d.constraint],
            "constraints": [d.requirement for d in declarations if d.constraint],
            "requirements_roots": list(dict.fromkeys(roots))}


class NodeResolver:
    version = "node-toolchain-v1"

    def resolve(self, source: FrozenSource, selected: dict, commands: list[dict]) -> tuple[list, list[CommandNode], dict]:
        from app.environment_engine.node import script_graph
        nodes = []
        units = set()
        for cmd in commands:
            graph = script_graph(source, tuple(cmd["argv"]), cmd["working_directory"])
            units.update(graph["package_directories"])
            nodes.append(CommandNode(**{**cmd, "argv": tuple(cmd["argv"])},
                required_executable=cmd["argv"][0], source_refs=tuple(source.refs.values()), chain=tuple(graph["chain"]),
                entrypoints=tuple(graph["entrypoints"])))
        roots = set(selected.get("node_environment", {}).get("package_roots", []))
        independent = sorted(units - roots)
        # Each independent install must be bound to an actual lock at its root.
        for directory in independent:
            prefix = "" if directory == "." else directory + "/"
            locks = [prefix + p for p in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock") if prefix + p in source.inventory]
            if len(locks) != 1:
                raise ResolutionError("node_independent_lock_unresolved", (prefix + "package.json",))
            source.read(locks[0], self.version)
        return [], nodes, {"independent_directories": independent}


REGISTRY: dict[str, ManifestResolver] = {"python": PythonResolver(), "node": NodeResolver()}


def resolve_contract(binding: EnvironmentBinding, commands: list[dict], selected: dict, evidence: dict) -> VerificationEnvironmentContract:
    source = FrozenSource(evidence)
    nodes, declarations, preparation, gaps = [], [], {}, []
    adapter = REGISTRY.get(selected.get("ecosystem", ""))
    try:
        if adapter is None:
            raise ResolutionError("ecosystem_unsupported")
        # Every selected manifest participates, including locks/config not parsed
        # by this adapter. Hash mismatch is never a missing-evidence retry.
        for path in selected["manifests"]:
            source.read(path, adapter.version)
        declarations, nodes, preparation = adapter.resolve(source, selected, commands)
    except ResolutionError as exc:
        gaps.append(Gap(reason_code=exc.code, paths=exc.paths))
    except (ValueError, TypeError, KeyError):
        gaps.append(Gap(reason_code="environment_schema_invalid"))
    if not nodes:
        nodes = [CommandNode(**{**c, "argv": tuple(c["argv"])}, required_executable=c["argv"][0], resolution="unsupported") for c in commands]
    return VerificationEnvironmentContract(binding=binding, commands=tuple(nodes),
        declarations=tuple(declarations), evidence=tuple(source.refs.values()), preparation=preparation,
        resolver_versions={"engine": "upeve-v1", "adapter": adapter.version if adapter else "unsupported"},
        unresolved=tuple(gaps), admission="unverified" if gaps and gaps[0].reason_code == "environment_evidence_missing" else "unsupported" if gaps else "static_admitted")


def command_hash(commands: list[dict]) -> str:
    return content_hash(commands)
