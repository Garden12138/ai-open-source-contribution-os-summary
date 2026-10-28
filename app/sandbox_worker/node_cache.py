"""Narrow data-cache adapters; package code remains on its read-only volume."""
from __future__ import annotations


def prepare_node_cache_files(project: str, output: str) -> list[dict[str, str]]:
    """Called by the trusted supervisor after the offline dependency build."""
    import hashlib
    import json
    import os
    from pathlib import Path

    project_path, output_path = Path(project), Path(output)
    caches = []
    count = 0
    for directory, dirs, files in os.walk(project_path, followlinks=False):
        count += len(files) + len(dirs)
        if count > 200000:
            raise ValueError("Node cache discovery budget exceeded")
        if Path(directory).name != "vitest" or "package.json" not in files:
            continue
        package = Path(directory)
        if "node_modules" not in package.relative_to(project_path).parts:
            continue
        manifest = package / "package.json"
        if manifest.is_symlink() or manifest.stat().st_size > 1024 * 1024:
            raise ValueError("Invalid cache adapter package")
        metadata = json.loads(manifest.read_text())
        # Verified against the published v2.1.9 typecheck implementation. This
        # is a tool-version capability, independent of repository or Issue.
        if not isinstance(metadata,dict) or metadata.get("name") != "vitest" or metadata.get("version") != "2.1.9":
            continue
        chunks = package / "dist/chunks"
        if any(p.is_symlink() for p in (package / "dist", chunks)):
            raise ValueError("Node cache adapter cannot traverse links")
        candidates = sorted(chunks.glob("*.js"))
        if len(candidates) > 256:
            raise ValueError("Node cache adapter chunk budget exceeded")
        recognized = False
        for chunk in candidates:
            if chunk.is_symlink() or chunk.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Invalid cache adapter implementation")
            if b"tsconfig.tmp.tsbuildinfo" in chunk.read_bytes():
                recognized = True
        if not recognized:
            continue
        path = chunks / "tsconfig.tmp.tsbuildinfo"
        relative = str(path.relative_to(output_path))
        if path.exists() or path.is_symlink() or len(caches) >= 32:
            raise ValueError("Node cache adapter conflicts with frozen output")
        target = "/execution/cache/node/" + hashlib.sha256(relative.encode()).hexdigest() + ".tsbuildinfo"
        path.symlink_to(target)
        caches.append({"path": relative, "target": target, "adapter": "vitest-2.1.9-typecheck-v1"})
    return sorted(caches, key=lambda item: item["path"])
