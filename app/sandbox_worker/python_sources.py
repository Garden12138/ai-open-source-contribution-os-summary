"""Merge frozen Python source roots for fresh, read-only verification."""
from __future__ import annotations


def build_python_source_overlay(source_paths: list[str], destination: str, workspace_root: str = "/workspace") -> str:
    """Link source files without importing code or merging conflicting modules."""
    import hashlib
    import os
    import stat

    root = os.path.realpath(workspace_root)
    if not 1 <= len(source_paths) <= 8 or len(set(source_paths)) != len(source_paths):
        raise ValueError("Invalid Python source roots")
    os.makedirs(destination, exist_ok=False)
    count = 0
    for source in source_paths:
        resolved = os.path.realpath(source)
        if (resolved != root and not resolved.startswith(root + os.sep)) or not os.path.isdir(source) or os.path.islink(source):
            raise ValueError("Python source root escapes workspace")
        for current, directories, files in os.walk(source, followlinks=False):
            directories.sort()
            files.sort()
            for name in [*directories, *files]:
                count += 1
                if count > 20_000 or os.path.islink(os.path.join(current, name)):
                    raise ValueError("Python source inventory is invalid")
            relative = os.path.relpath(current, source)
            target = destination if relative == "." else os.path.join(destination, relative)
            if os.path.lexists(target) and not os.path.isdir(target):
                raise ValueError("Python module layout conflicts")
            os.makedirs(target, exist_ok=True)
            for name in files:
                original = os.path.join(current, name)
                if not stat.S_ISREG(os.stat(original, follow_symlinks=False).st_mode):
                    raise ValueError("Python source is not a regular file")
                link = os.path.join(target, name)
                if os.path.lexists(link):
                    if not os.path.isfile(link):
                        raise ValueError("Python module layout conflicts")
                    with open(original, "rb") as left, open(link, "rb") as right:
                        if hashlib.file_digest(left, "sha256").digest() != hashlib.file_digest(right, "sha256").digest():
                            raise ValueError("Python module layout conflicts")
                else:
                    os.symlink(original, link)
    return destination
