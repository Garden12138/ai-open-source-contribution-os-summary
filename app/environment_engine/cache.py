"""Select a tool-version capability from declarations, never repository names."""
from __future__ import annotations

import tomllib
from packaging.requirements import Requirement
from app.environment_engine.source import FrozenSource, ResolutionError
from app.environment_engine.contracts import RequirementDeclaration
from app.sandbox_worker.python_cache import MYPY_CACHE_CAPABILITY


def python_cache_policy(source: FrozenSource, declarations: list[RequirementDeclaration], commands: list[dict]) -> dict | None:
    affected = [c for c in commands if c["argv"][0] == "stubtest" or c["argv"][:3] in (
        ["python", "-m", "mypy.stubtest"], ["python3", "-m", "mypy.stubtest"])]
    if not affected:
        return None
    selected = [Requirement(d.requirement) for d in declarations if d.distribution == "mypy"]
    if not selected or not any(all(not r.marker and r.specifier.contains(v) for r in selected)
                               for v in MYPY_CACHE_CAPABILITY["distribution_versions"]):
        return None
    if any(c["working_directory"] != "." for c in affected):
        raise ResolutionError("tool_data_cache_scope_unsupported")
    if any(p == ".mypy_cache" or p.startswith(".mypy_cache/") for p in source.inventory):
        raise ResolutionError("tool_data_cache_source_conflict", (".mypy_cache",))
    for path in tuple(source.refs):
        if path.endswith("pyproject.toml"):
            metadata = tomllib.loads(source.read(path, "python-data-cache-v1"))
            if metadata.get("tool", {}).get("mypy", {}).get("cache_dir", ".mypy_cache") != ".mypy_cache":
                raise ResolutionError("tool_data_cache_scope_unsupported", (path,))
    return dict(MYPY_CACHE_CAPABILITY)
