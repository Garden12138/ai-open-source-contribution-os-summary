"""PEP declarations and recursive pip files, interpreted as data only."""
from __future__ import annotations

import re
import shlex
import tomllib
from packaging.requirements import Requirement, InvalidRequirement
from packaging.utils import canonicalize_name
from app.environment_engine.contracts import RequirementDeclaration
from app.environment_engine.source import FrozenSource, ResolutionError, relative_path

VERSION = "python-requirements-v1"


def declaration(text: str, source: FrozenSource, path: str, line: int | None, constraint: bool = False) -> RequirementDeclaration:
    if len(text) > 2000 or any(c in text for c in ("\x00", "\n", "\r", "\\", "@")) or text.startswith("-"):
        raise ResolutionError("requirements_unsafe_directive", (path,))
    try:
        req = Requirement(text)
    except InvalidRequirement:
        raise ResolutionError("requirements_invalid", (path,)) from None
    if req.url or constraint and (req.extras or req.marker):
        raise ResolutionError("requirements_unsafe_directive", (path,))
    return RequirementDeclaration(requirement=str(req), distribution=canonicalize_name(req.name),
        constraint=constraint, source=source.refs[path], line=line)


def requirements(source: FrozenSource, roots: list[str], *, constraint_roots: list[str] | None = None) -> list[RequirementDeclaration]:
    result: list[RequirementDeclaration] = []
    visited: set[tuple[str, bool]] = set()
    def visit(path: str, constrained: bool, stack: tuple[str, ...]) -> None:
        if path in stack:
            raise ResolutionError("recursive_include_cycle", (*stack, path))
        if len(stack) >= 8:
            raise ResolutionError("requirements_depth_limit", (path,))
        if (path, constrained) in visited:
            return
        text = source.read(path, VERSION)
        visited.add((path, constrained))
        for number, raw in enumerate(text.splitlines(), 1):
            text = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
            if not text or text.startswith("#"):
                continue
            include = re.fullmatch(r"(-r|-c|--requirement|--constraint)(?:\s+|=)([^\s]+)", text)
            if include:
                child = relative_path(path, include[2])
                visit(child, constrained or include[1] in {"-c", "--constraint"}, (*stack, path))
            else:
                result.append(declaration(text, source, path, number, constrained))
            if len(result) > 300:
                raise ResolutionError("requirements_count_limit", (path,))
    for path in roots:
        visit(path, False, ())
    for path in constraint_roots or []:
        visit(path, True, ())
    check_conflicts(result)
    return result


def check_conflicts(values: list[RequirementDeclaration]) -> None:
    """Prove obvious contradictions early; pip remains the actual resolver."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version, InvalidVersion
    grouped: dict[str, list[RequirementDeclaration]] = {}
    installed = {d.distribution for d in values if not d.constraint}
    for value in values:
        if value.distribution in installed:
            grouped.setdefault(value.distribution, []).append(value)
    for declarations in grouped.values():
        # Markers are evaluated by pip on the actual target. Do not evaluate
        # them against the host and erase target-specific declarations.
        specs = [Requirement(d.requirement).specifier for d in declarations
                 if Requirement(d.requirement).marker is None]
        combined = SpecifierSet(",".join(str(s) for s in specs if str(s)))
        pins = [s.version for s in combined if s.operator in {"==", "==="} and "*" not in s.version]
        if pins:
            try:
                conflict = any(not combined.contains(Version(p)) for p in pins)
            except InvalidVersion:
                conflict = True
            if conflict:
                raise ResolutionError("dependency_version_conflict", tuple(d.source.path for d in declarations))


def pep621(source: FrozenSource, path: str) -> list[RequirementDeclaration]:
    try:
        metadata = tomllib.loads(source.read(path, "python-pep621-v1"))
    except tomllib.TOMLDecodeError:
        raise ResolutionError("project_metadata_invalid", (path,)) from None
    project = metadata.get("project", {})
    if not isinstance(project, dict):
        raise ResolutionError("project_metadata_invalid", (path,))
    if set(project.get("dynamic", [])) & {"dependencies", "optional-dependencies"}:
        raise ResolutionError("python_dynamic_metadata_requires_freeze", (path,))
    result: list[RequirementDeclaration] = []
    def add(values: object) -> None:
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ResolutionError("dependency_groups_unsupported", (path,))
        result.extend(declaration(v, source, path, None) for v in values)
    add(project.get("dependencies", []))
    add(metadata.get("build-system", {}).get("requires", []))
    extras = project.get("optional-dependencies", {})
    if not isinstance(extras, dict):
        raise ResolutionError("project_metadata_invalid", (path,))
    for name in ("test", "tests", "dev", "instruments", "type-check"):
        add(extras.get(name, []))
    groups = metadata.get("dependency-groups", {})
    if not isinstance(groups, dict):
        raise ResolutionError("dependency_groups_unsupported", (path,))
    def group(name: str, stack: tuple[str, ...]) -> None:
        if name in stack or len(stack) >= 8:
            raise ResolutionError("recursive_include_cycle", (path,))
        values = groups.get(name)
        if not isinstance(values, list):
            raise ResolutionError("dependency_groups_unsupported", (path,))
        for item in values:
            if isinstance(item, dict) and set(item) == {"include-group"} and isinstance(item["include-group"], str):
                group(item["include-group"], (*stack, name))
            elif isinstance(item, str):
                add([item])
            else:
                raise ResolutionError("dependency_groups_unsupported", (path,))
    for name in ("dev", "test", "tests"):
        if name in groups:
            group(name, ())
    if len(result) > 300:
        raise ResolutionError("requirements_count_limit", (path,))
    check_conflicts(result)
    return result


def declared_pip_inputs(source: FrozenSource, directory: str) -> tuple[list[str], list[str], list[RequirementDeclaration]]:
    """Literal pip declarations in Make/CI. Never evaluate shell or YAML.

    Quoted PEP 508 specifiers are data; unquoted redirections, evaluation,
    alternate indexes, editable/local installs and ambiguous cwd are gaps.
    """
    prefix = "" if directory == "." else directory + "/"
    candidates = sorted(p for p in source.inventory if p == prefix + "Makefile"
        or p.startswith(prefix + ".github/workflows/") and p.endswith((".yml", ".yaml")))
    roots: list[str] = []
    constraints: list[str] = []
    declarations: list[RequirementDeclaration] = []
    for path in candidates:
        document = source.read(path, "literal-pip-declaration-v2")
        if "working-directory:" in document and re.search(r"\bpip\b.*\binstall\b", document):
            raise ResolutionError("unsupported_dynamic_declaration", (path,))
        for number, raw in enumerate(document.splitlines(), 1):
            line = raw.strip().removeprefix("- run:").removeprefix("run:").strip()
            # A literal pip line is evidence, never an execution request.
            if not re.match(r"(?:python3? -m pip|pip3?) install\b", line):
                continue
            if any(c in line for c in ("$", "`", "\\")):
                raise ResolutionError("unsupported_dynamic_declaration", (path,))
            try:
                lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>")
                lexer.whitespace_split = True
                tokens = list(lexer)
            except ValueError:
                raise ResolutionError("unsupported_dynamic_declaration", (path,)) from None
            if any(token and all(c in ";&|<>" for c in token) for token in tokens):
                raise ResolutionError("unsupported_dynamic_declaration", (path,))
            position = 4 if tokens[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]) else 2
            while position < len(tokens):
                token = tokens[position]
                position += 1
                if token in {"--upgrade", "-U"}:
                    continue  # The prepared venv is fresh, never an in-place upgrade.
                if token in {"-e", "--editable"}:
                    if position >= len(tokens) or tokens[position] != ".":
                        raise ResolutionError("unsupported_pip_declaration", (path,))
                    token = tokens[position]
                    position += 1
                if token == ".":
                    # The frozen local project is already built/installed by
                    # workspace preparation. This selects no external package,
                    # and never executes an editable install to discover data.
                    if not path.endswith("Makefile") and directory != ".":
                        raise ResolutionError("unsupported_pip_declaration", (path,))
                    continue
                include = re.fullmatch(r"(-r|-c|--requirement|--constraint)(?:=(.+))?", token)
                if include:
                    name = include[2]
                    if name is None:
                        if position >= len(tokens):
                            raise ResolutionError("requirements_invalid", (path,))
                        name = tokens[position]
                        position += 1
                    # A CI step defaults to repository root, even for a
                    # selected nested Python project. Make runs at its root.
                    parent = (prefix if path.endswith("Makefile") else "") + "__root__"
                    target = constraints if include[1] in {"-c", "--constraint"} else roots
                    target.append(relative_path(parent, name))
                elif token.startswith("-") or token.startswith("."):
                    raise ResolutionError("unsupported_pip_declaration", (path,))
                else:
                    declarations.append(declaration(token, source, path, number))
                if len(declarations) + len(roots) + len(constraints) > 300:
                    raise ResolutionError("requirements_count_limit", (path,))
    return list(dict.fromkeys(roots)), list(dict.fromkeys(constraints)), declarations


def declared_pip_roots(source: FrozenSource, directory: str) -> list[str]:
    return declared_pip_inputs(source, directory)[0]
