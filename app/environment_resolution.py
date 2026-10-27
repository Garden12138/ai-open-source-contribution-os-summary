"""Project facts and toolchain capabilities; no repository code is evaluated.

Drivers implement tool semantics. Project roots, declarations, versions and
commands come from frozen repository evidence, never a per-repository template.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re


@dataclass(frozen=True)
class PackageDriver:
    name: str
    lockfiles: tuple[str, ...]
    distribution: str


PACKAGE_DRIVERS = (
    PackageDriver("npm", ("package-lock.json", "npm-shrinkwrap.json"), "npm"),
    PackageDriver("yarn", ("yarn.lock",), "yarn"),
    PackageDriver("pnpm", ("pnpm-lock.yaml",), "pnpm"),
    PackageDriver("bun", ("bun.lock", "bun.lockb"), "@oven/bun-linux-{arch}"),
)
LOCK_OWNERS = {name: driver.name for driver in PACKAGE_DRIVERS for name in driver.lockfiles}
VERSION_REQUIREMENT = re.compile(r"[0-9vVxX.*<>=~^| +\-]{1,160}")


def package_manager_declaration(value: object) -> tuple[str, str]:
    """Preserve declared ranges; exact resolution belongs to isolated acquisition."""
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("node_manager_version_invalid")
    match = re.fullmatch(r"([a-z][a-z0-9-]{0,39})@(.+?)(?:\+sha(?:224|256|512)\.[a-f0-9]+)?", value)
    if not match:
        raise ValueError("node_manager_version_invalid")
    manager, constraint = match.groups()
    if not VERSION_REQUIREMENT.fullmatch(constraint) and not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?",constraint):
        raise ValueError("node_manager_version_invalid")
    return manager, constraint


def node_install_root(paths: set[str], change: str) -> str | None:
    candidates = [p for p in PurePosixPath(change).parents if str(p / "package.json") in paths]
    if not candidates:
        return None
    return str(next((p for p in candidates if any(str(p / name) in paths for name in LOCK_OWNERS)), candidates[0]))
