"""Classify read proposals against frozen evidence without guessing replacements."""

from __future__ import annotations

from app.plans import _repository_path


def planning_read_paths(paths: list[str], evidence: dict) -> tuple[list[str], dict | None]:
    """Keep admissible reads and return bounded, safe feedback for rejected paths."""
    known = set(evidence["inventory"])
    unavailable = {entry["path"] for entry in evidence["entries"]
                   if entry.get("content") is None}
    accepted, missing, blocked = [], [], []
    unsafe = 0
    for path in dict.fromkeys(paths):
        try:
            _repository_path(path, "Read path")
        except ValueError:
            unsafe += 1
            continue
        if path not in known:
            missing.append(path)
        elif path in unavailable:
            blocked.append(path)
        else:
            accepted.append(path)
    if not (missing or blocked or unsafe):
        return accepted, None
    return accepted, {
        "reason_code": "planning_read_invalid",
        "fields": ["read_paths"],
        "constraint": "read_paths_unavailable",
        "missing_paths": missing[:32],
        "unavailable_paths": blocked[:32],
        "unsafe_path_count": unsafe,
        "remediation": (
            "Only accepted read_paths were scheduled. Use exact paths from inventory; "
            "never guess replacements. Explicit content=null entries are unavailable, "
            "including sensitive files: do not request them again or modify them. "
            "Use inspected safe fixtures or a separate new regression test file. "
            "Preserve the user's answered choices. If an unavailable file is "
            "indispensable, ask a material clarification instead of repeating its read."
        ),
    }
