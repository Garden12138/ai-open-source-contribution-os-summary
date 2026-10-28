"""Bounded access to already frozen archive evidence. No filesystem fallback."""
from __future__ import annotations

from hashlib import sha256
from app.environment_engine.contracts import SourceRef, safe_path


class ResolutionError(ValueError):
    def __init__(self, code: str, paths: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code, self.paths = code, paths


class FrozenSource:
    def __init__(self, evidence: dict) -> None:
        self.inventory = set(evidence.get("inventory", []))
        self.entries = {e["path"]: e for e in evidence["entries"]}
        if len(self.entries) != len(evidence["entries"]):
            raise ResolutionError("source_duplicate_path")
        self.refs: dict[str, SourceRef] = {}
        self.bytes = 0

    def read(self, path: str, parser: str) -> str:
        safe_path(path)
        if path not in self.inventory or path not in self.entries:
            raise ResolutionError("environment_evidence_missing", (path,))
        entry = self.entries[path]
        if entry.get("type") == "symlink" or entry.get("symlink") or entry.get("link_target"):
            raise ResolutionError("source_symlink_unsupported", (path,))
        text = entry.get("content")
        if not isinstance(text, str):
            raise ResolutionError("environment_evidence_missing", (path,))
        digest = sha256(text.encode()).hexdigest()
        if digest != entry.get("prior_hash"):
            raise ResolutionError("source_hash_mismatch", (path,))
        if path not in self.refs:
            self.bytes += len(text.encode())
            if len(self.refs) >= 64 or self.bytes > 2 * 1024 * 1024 or len(text.encode()) > 512 * 1024:
                raise ResolutionError("environment_evidence_budget", (path,))
            self.refs[path] = SourceRef(path=path, sha256=digest, parser=parser)
        return text


def relative_path(parent: str, name: str) -> str:
    """Permit relative includes inside the archive, including sibling directories."""
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        raise ResolutionError("requirements_path_escape", (parent,))
    parts = parent.split("/")[:-1]
    for part in name.split("/"):
        if part == "..":
            if not parts:
                raise ResolutionError("requirements_path_escape", (parent,))
            parts.pop()
        elif part not in {"", "."}:
            parts.append(part)
    path = "/".join(parts)
    try:
        return safe_path(path)
    except ValueError:
        raise ResolutionError("requirements_path_escape", (parent,)) from None
