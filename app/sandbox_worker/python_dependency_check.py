"""Validate installed versions against frozen local workspace source overrides."""
from __future__ import annotations


def check_python_dependency_versions(packages: dict, local_versions: dict, override_edges: list) -> dict:
    """Match pip's runtime checks except for explicitly frozen local source edges."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    installed = {canonicalize_name(name):value for name,value in packages.items()}
    local = {canonicalize_name(name):version for name,version in local_versions.items()}
    edges = {(canonicalize_name(source),canonicalize_name(target)) for source,target in override_edges}
    failures = []
    applied = []
    for name,(version,requirements) in installed.items():
        for declaration in requirements:
            requirement = Requirement(declaration)
            if requirement.marker and not requirement.marker.evaluate({"extra":""}):
                continue
            target = canonicalize_name(requirement.name)
            if requirement.url:
                failures.append({"package":name,"dependency":target,"reason":"url_dependency"})
                continue
            if target not in installed:
                failures.append({"package":name,"dependency":target,"reason":"missing"})
                continue
            actual = installed[target][0]
            if requirement.specifier.contains(actual,prereleases=True):
                continue
            if ((name,target) in edges and name in local and target in local
                and installed[name][0] == local[name] and actual == local[target]):
                applied.append({"package":name,"dependency":target,"specifier":str(requirement.specifier),
                                "local_version":actual})
            else:
                failures.append({"package":name,"dependency":target,"reason":"version_conflict"})
    return {"failures":failures,"workspace_source_overrides":applied}
