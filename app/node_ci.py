"""Literal inputs of known toolchain setup actions; never execute YAML or shell."""
from __future__ import annotations

import re
from app.environment_engine.source import FrozenSource, ResolutionError

VERSION = "node-ci-toolchains-v1"
STEP = re.compile(r"^( *)-\s+(?:name|uses|run|id|if):")
ACTION = re.compile(r"(?:pnpm/action-setup|actions/setup-node)@(?:v[0-9]+(?:\.[0-9]+)*|[a-f0-9]{40})$")
RANGE = re.compile(r"[0-9vVxX.*<>=~^| +\-]{1,160}$")


def ci_toolchains(evidence: dict, project_directory: str, manager: str, *, manager_from_manifest: bool = False) -> dict:
    """Return one consistent literal CI range per tool, with frozen provenance.

    A matrix/expression, alternative job versions, flow mapping or YAML indirection
    is a declared unresolved boundary, never permission to choose latest.
    """
    source = FrozenSource(evidence)
    prefix = "" if project_directory == "." else project_directory + "/"
    declarations = []
    paths = sorted(p for p in source.inventory if p.startswith(prefix + ".github/workflows/") and p.endswith((".yml", ".yaml")))
    try:
        for path in paths:
            text = source.read(path, VERSION)
            if "\t" in text:
                raise ValueError("node_ci_toolchain_yaml_unsupported")
            lines = text.splitlines()
            starts = [(index, len(match.group(1))) for index, line in enumerate(lines) if (match := STEP.match(line))]
            for begin, indent in starts:
                end = next((n for n in range(begin + 1, len(lines))
                    if lines[n].strip() and not lines[n].lstrip().startswith("#")
                    and len(lines[n]) - len(lines[n].lstrip()) <= indent), len(lines))
                uses = []
                for index in range(begin, end):
                    line = lines[index]
                    if index == begin:
                        line = " " * (indent + 2) + line[indent + 2:]
                    match = re.fullmatch(r" {" + str(indent + 2) + r"}uses:\s*([^\s#]+)\s*(?:#.*)?", line)
                    if match:
                        action_value = match.group(1)
                        if action_value[:1] in {"'", '"'} and action_value[-1:] == action_value[:1]:
                            action_value = action_value[1:-1]
                        uses.append(action_value)
                known = [value for value in uses if value.startswith(("pnpm/action-setup@", "actions/setup-node@"))]
                if not known:
                    continue
                if len(uses) != 1 or not ACTION.fullmatch(known[0]):
                    raise ValueError("node_ci_toolchain_action_unsupported")
                action = known[0]
                expected = "version" if action.startswith("pnpm/") else "node-version"
                tool = "pnpm" if expected == "version" else "node"
                if tool not in {manager, "node"}:
                    continue
                with_lines = [n for n in range(begin + 1, end) if re.match(r" {" + str(indent + 2) + r"}with:", lines[n])]
                if len(with_lines) > 1:
                    raise ValueError("node_ci_toolchain_yaml_unsupported")
                if not with_lines:
                    if tool == "pnpm" and not manager_from_manifest:
                        raise ValueError("node_ci_toolchain_version_missing")
                    continue
                start = with_lines[0]
                if not re.fullmatch(r"\s*with:\s*(?:#.*)?", lines[start]):
                    raise ValueError("node_ci_toolchain_yaml_unsupported")
                stop = next((n for n in range(start + 1, end) if lines[n].strip() and not lines[n].lstrip().startswith("#")
                    and len(lines[n]) - len(lines[n].lstrip()) <= indent + 2), end)
                values = []
                for index in range(start + 1, stop):
                    match = re.fullmatch(r" {" + str(indent + 4) + r"}([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*", lines[index])
                    if not match:
                        if re.search(r"(?:<<|[&*!])", lines[index]):
                            raise ValueError("node_ci_toolchain_yaml_unsupported")
                        continue
                    key, raw = match.groups()
                    if key not in {expected, "node-version-file" if tool == "node" else expected}:
                        continue
                    value = re.sub(r"\s+#.*$", "", raw).strip()
                    value_source = None
                    if value.startswith(("'", '"')):
                        if len(value) < 2 or value[-1] != value[0] or value[0] in value[1:-1]:
                            raise ValueError("node_ci_toolchain_dynamic")
                        value = value[1:-1]
                    if key == "node-version-file":
                        from app.environment_engine.source import relative_path
                        target = relative_path(prefix + "__ci__", value)
                        value = source.read(target, VERSION).strip()
                        value_source = source.refs[target].model_dump()
                    if not RANGE.fullmatch(value) or "${" in value or not re.search(r"[0-9xX*]", value):
                        raise ValueError("node_ci_toolchain_dynamic")
                    if "||" in value:
                        raise ValueError("node_ci_toolchain_ambiguous")
                    values.append(value)
                    declarations.append({"tool": tool, "range": value, "action": action, "input": key,
                        "path": path, "line": index + 1, "sha256": source.refs[path].sha256,
                        **({"value_source": value_source} if value_source else {})})
                if len(values) > 1:
                    raise ValueError("node_ci_toolchain_ambiguous")
                if not values and tool == "pnpm" and not manager_from_manifest:
                    raise ValueError("node_ci_toolchain_version_missing")
                if len(declarations) > 32:
                    raise ValueError("node_ci_toolchain_budget")
    except ResolutionError as exc:
        raise ValueError("node_ci_toolchain_source_unavailable") from exc
    grouped = {tool: sorted({d["range"] for d in declarations if d["tool"] == tool}) for tool in (manager, "node")}
    if any(len(values) > 1 for values in grouped.values()):
        raise ValueError("node_ci_toolchain_ambiguous")
    return {"version": VERSION, "manager": grouped[manager], "runtime": grouped["node"],
            "declarations": declarations, "sources": [ref.model_dump() for ref in source.refs.values()]}
