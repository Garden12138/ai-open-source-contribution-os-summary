"""Check internal imports in packaged source without executing application code."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True, order=True)
class MissingRuntimeModule:
    source: str
    module: str


def missing_runtime_modules(package_root: Path) -> tuple[MissingRuntimeModule, ...]:
    """Include imports inside functions: importing entrypoints alone misses them."""
    root = package_root.resolve()
    failures: set[MissingRuntimeModule] = set()
    for source in sorted(root.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.name)
        for node in ast.walk(tree):
            names = ([node.module] if isinstance(node, ast.ImportFrom) and not node.level
                     else [name.name for name in node.names] if isinstance(node, ast.Import) else [])
            for name in names:
                if name is None or not name.startswith("app."):
                    continue
                target = root.joinpath(*name.split(".")[1:])
                if not target.with_suffix(".py").is_file() and not (target / "__init__.py").is_file():
                    failures.add(MissingRuntimeModule(source.relative_to(root).as_posix(), name))
    return tuple(sorted(failures))


def main() -> int:
    root = Path(__file__).resolve().parent
    try:
        missing = missing_runtime_modules(root)
    except (OSError, SyntaxError, UnicodeError):
        print(json.dumps({"status":"failed", "reason_code":"worker_runtime_incomplete"}))
        return 1
    print(json.dumps({"status":"failed" if missing else "ok",
        "reason_code":"worker_runtime_incomplete" if missing else None,
        "missing_imports":[{"source":v.source, "module":v.module} for v in missing[:20]],
        "missing_count":len(missing)}))
    return int(bool(missing))


if __name__ == "__main__":
    raise SystemExit(main())
