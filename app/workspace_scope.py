"""A frozen source-tree scope narrows materialization; credential guards remain."""
from __future__ import annotations
from typing import Any
from sqlalchemy.orm import Session


def selected_workspace_paths(environment: dict[str, Any], inventory: set[str], changes: set[str]) -> tuple[str, ...]:
    if environment.get("ecosystem") == "node":
        # Preserve the full installation graph, while keeping credential paths
        # outside the signed source scope. The Runner's rejection stays intact.
        denied = {".env", ".git", ".netrc", ".npmrc", ".pypirc", "auth.json",
                  "credentials", "id_dsa", "id_ed25519", "id_rsa"}
        unsafe = {p for p in inventory if any(part.lower() in denied for part in p.split("/"))}
        safe = sorted(inventory - unsafe)
        roots = set()
        for path in safe:
            parts = path.split("/")
            for length in range(1, len(parts) + 1):
                candidate = "/".join(parts[:length])
                if not any(p == candidate or p.startswith(candidate + "/") for p in unsafe):
                    roots.add(candidate)
                    break
        return tuple(sorted(roots))
    directories = environment.get("project_directories") or [environment.get("project_directory", ".")]
    if "." in directories or environment.get("status") != "supported":
        return ()
    package_roots = {directory.split("/", 1)[0] for directory in directories}
    roots = {*package_roots, *changes}
    names = {"README.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "CHANGELOG.md",
             "CONTRIBUTING.md", "pyproject.toml", "ruff.toml", ".ruff.toml", "mypy.ini", "pytest.ini", "setup.cfg", ".coveragerc"}
    roots.update(p for p in inventory if p in names)
    return tuple(sorted(p for p in roots if p in package_roots or not any(p.startswith(root + "/") for root in package_roots)))


def attempt_workspace_paths(session: Session, attempt: Any) -> tuple[str, ...]:
    from app.workbench import verified_workbench_history
    rows = verified_workbench_history(session, attempt.task_id)
    # Repair attempts retain the same PlanApproval. Find its exact authorized root.
    from app.executions import ExecutionAttemptService
    for event in rows:
        if event.kind == "automation_started":
            root = ExecutionAttemptService(session).get_verified(event.payload["attempt_id"])
            if root.plan_approval_id == attempt.plan_approval_id:
                authorization = next(e for e in rows if e.record_hash == event.payload["authorization_hash"])
                paths = authorization.payload.get("workspace_paths", ())
                if "workspace_scope_version" in authorization.payload:
                    from app.provenance import content_hash
                    if authorization.payload["workspace_scope_version"] != "source-tree-scope-v1" or content_hash(list(paths)) != authorization.payload["workspace_scope_hash"]:
                        raise ValueError("Workspace scope provenance is invalid")
                return tuple(paths)
    return ()
