"""A frozen source-tree scope narrows materialization; credential guards remain."""
from __future__ import annotations
from typing import Any
from sqlalchemy.orm import Session


def selected_workspace_paths(environment: dict[str, Any], inventory: set[str], changes: set[str]) -> tuple[str, ...]:
    directory = environment.get("project_directory", ".")
    if directory == "." or environment.get("status") != "supported":
        return ()
    root = directory.split("/", 1)[0]
    roots = {root, *changes}
    names = {"README.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "CHANGELOG.md",
             "CONTRIBUTING.md", "pyproject.toml", "ruff.toml", ".ruff.toml", "mypy.ini", "pytest.ini", "setup.cfg", ".coveragerc"}
    roots.update(p for p in inventory if p in names)
    return tuple(sorted(p for p in roots if p == root or not p.startswith(root + "/")))


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
