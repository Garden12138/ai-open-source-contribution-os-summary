"""Enum-only import facts; never export source, identifiers or literal values."""
from __future__ import annotations

import json
import re
import tempfile
from inspect import getsource
from pathlib import Path
from uuid import uuid4

from app.provenance import canonical_json
from app.security import ensure_no_sensitive_data
from app.sandbox_worker.coding_context import CodingContextError, _CAPTURE_SCRIPT, _safe_path

VERSION = "python-entrypoints-imports-v1"
ORIGINS = ("importlib.metadata", "importlib_metadata", "pkg_resources", "opentelemetry.util._importlib_metadata", "unknown")


def validate_metadata(value: dict, paths: tuple[str, ...]) -> None:
    if (not isinstance(value, dict) or set(value) != {"version", "entries"}
            or value["version"] != VERSION or not isinstance(value["entries"], list)
            or len(value["entries"]) != len(paths)):
        raise CodingContextError("Invalid import metadata")
    for path, item in zip(paths, value["entries"]):
        if (not isinstance(item, dict) or set(item) != {"path", "source_hash", "origin"}
                or item["path"] != path or item["origin"] not in ORIGINS
                or (item["source_hash"] is not None and (not isinstance(item["source_hash"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["source_hash"])))
                or (item["source_hash"] is None and item["origin"] != "unknown")):
            raise CodingContextError("Invalid import metadata entry")


def entry_points_origin(source: str) -> str:
    """Conservatively classify a unique direct import into a fixed enum."""
    import ast
    allowed = {"importlib.metadata", "importlib_metadata", "pkg_resources", "opentelemetry.util._importlib_metadata"}
    try:
        tree = ast.parse(source)
        nodes = []
        for node in ast.walk(tree):
            nodes.append(node)
            if len(nodes) > 25000:
                return "unknown"
        imports = [(node.module, alias.asname or alias.name)
                   for node in nodes if isinstance(node, ast.ImportFrom) and node.level == 0
                   for alias in node.names if alias.name == "entry_points"]
        if not imports or any(module not in allowed for module, _ in imports):
            return "unknown"
        modules = {module for module, _ in imports}
        aliases = {alias for _, alias in imports}
        if len(modules) != 1 or any(
            (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in aliases)
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in aliases)
            or (isinstance(node, ast.arg) and node.arg in aliases) for node in nodes
        ):
            return "unknown"
        return next(iter(modules))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return "unknown"


def metadata_program() -> str:
    from app.sandbox_worker.planning_context import INSPECT_SCRIPT
    boundary = '    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))'
    assert INSPECT_SCRIPT.count(boundary) == 1
    # Reuse the hardened archive reader, replacing its stdout serialization.
    # Raw source is parsed solely inside the credential-free OCI container.
    return getsource(entry_points_origin) + "\n" + INSPECT_SCRIPT.split(boundary)[0] + r'''
    selected = {entry["path"]: entry for entry in entries}
    facts = []
    for path in request["paths"]:
        entry = selected.get(path, {})
        source = entry.get("content")
        facts.append({"path": path, "source_hash": entry.get("prior_hash"),
                      "origin": entry_points_origin(source) if isinstance(source, str) else "unknown"})
    print(json.dumps({"version":"python-entrypoints-imports-v1","entries":facts},separators=(",",":")))
'''


async def inspect_import_metadata(runtime, *, archive, image_digest: str, policy, paths: tuple[str, ...]) -> dict:
    if not 1 <= len(paths) <= 4 or len(set(paths)) != len(paths):
        raise CodingContextError("Import metadata exceeds limits")
    for path in paths:
        _safe_path(path)
        ensure_no_sensitive_data(path, context="import metadata path")
        if not path.endswith(".py"):
            raise CodingContextError("Import metadata requires Python source")
    archive.verify()
    with tempfile.TemporaryDirectory(prefix="contribos-imports-") as directory:
        staged = runtime._stage_archive(archive, Path(directory))
        request = Path(directory) / "request.json"
        request.write_text(canonical_json({"query":"", "paths":list(paths)}))
        request.chmod(0o444)
        name = "contribos-imports-" + uuid4().hex
        argv = runtime._argv(archive=staged, request_path=request, image_digest=image_digest,
                             policy=policy, container_name=name)
        argv = tuple(metadata_program() if item == _CAPTURE_SCRIPT else item for item in argv)
        raw = await runtime._run(argv, container_name=name, timeout_seconds=policy.timeout_seconds)
    archive.verify()
    if len(raw) > 8000:
        raise CodingContextError("Invalid import metadata")
    value = json.loads(raw)
    validate_metadata(value, paths)
    result = {**value, "archive_hash":archive.archive_hash, "base_sha":archive.base_commit_sha,
              "image":image_digest, "policy_hash":policy.policy_hash, "full_source_available":False}
    ensure_no_sensitive_data(result, context="import metadata")
    return result
