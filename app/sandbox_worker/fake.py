"""Offline Fake Explore/Implement/Verify runtimes. They never extract archives."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.provenance import content_hash
from app.sandbox_worker.explore import ExploreInspection
from app.sandbox_worker.implementation import (
    ChangeOperationKind,
    DisposableWorkspace,
    FileInventoryEntry,
    ImplementationChangeSet,
    ImplementationInspection,
)
from app.sandbox_worker.specs import SandboxCommand, SandboxPolicy
from app.sandbox_worker.verification import (
    VerifyCommandEvidence,
    VerifyCommandStatus,
    VerifyInspection,
    VerifyResourceUsage,
)


class FakeExploreRuntime:
    async def materialize(
        self,
        *,
        archive,
        destination: Path,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> None:
        del image_digest, policy
        assert archive.archive_hash
        (destination / "src").mkdir(parents=True, exist_ok=True)
        (destination / "pyproject.toml").write_bytes(
            b"[project]\nname='fixture'\n"
        )
        (destination / "src" / "main.py").write_bytes(b"print('fixture')\n")

    async def inspect(
        self,
        *,
        snapshot,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> ExploreInspection:
        del image_digest, policy
        return ExploreInspection.from_wire(
            {
                "inventory_hash": "7" * 64,
                "file_count": snapshot.file_count,
                "total_bytes": snapshot.total_bytes,
                "sample_paths": ["pyproject.toml", "src/main.py"],
                "manifest_paths": ["pyproject.toml"],
            }
        )


class FakeImplementRuntime:
    def __init__(self) -> None:
        self.destroyed: list[str] = []

    async def create_workspace(
        self,
        *,
        image_digest: str,
        policy: SandboxPolicy,
    ) -> DisposableWorkspace:
        return DisposableWorkspace(
            workspace_id=f"workspace:{uuid4().hex[:12]}",
            volume_name="contribos-workspace-" + uuid4().hex,
            runner_image_digest=image_digest,
            sandbox_policy_hash=policy.policy_hash,
        )

    async def materialize(
        self,
        *,
        workspace: DisposableWorkspace,
        archive,
        policy: SandboxPolicy,
    ) -> None:
        del workspace, policy
        assert archive.archive_hash

    async def apply(
        self,
        *,
        workspace: DisposableWorkspace,
        change_set: ImplementationChangeSet,
        allowed_change_paths: tuple[str, ...],
        policy: SandboxPolicy,
    ) -> ImplementationInspection:
        del workspace, policy
        if not set(change_set.paths).issubset(set(allowed_change_paths)):
            raise ValueError("Fake implement paths are not approved")
        return inspection_from_change_set(change_set)

    async def destroy(self, workspace: DisposableWorkspace) -> None:
        self.destroyed.append(workspace.volume_name)


class FakeVerifyRuntime:
    async def run(
        self,
        *,
        workspace: DisposableWorkspace,
        commands: tuple[SandboxCommand, ...],
        policy: SandboxPolicy,
        dependency_workspace=None,
    ) -> VerifyInspection:
        del policy, dependency_workspace
        inventory = workspace.inventory_hash or ("0" * 64)
        started = datetime.now(timezone.utc).replace(microsecond=123456)
        completed = started + timedelta(milliseconds=5)
        empty = hashlib.sha256(b"").hexdigest()
        evidence = tuple(
            VerifyCommandEvidence(
                command_id=command.command_id,
                command_hash=content_hash(command.to_wire()),
                status=VerifyCommandStatus.PASSED,
                exit_code=0,
                started_at=_utc_stamp(started),
                completed_at=_utc_stamp(completed),
                resource_usage=VerifyResourceUsage(
                    user_cpu_ms=1,
                    system_cpu_ms=0,
                    max_rss_bytes=1024,
                    minor_page_faults=0,
                    major_page_faults=0,
                    voluntary_context_switches=0,
                    involuntary_context_switches=0,
                ),
                stdout_hash=empty,
                stdout_bytes=0,
                stdout_log="",
                stderr_hash=empty,
                stderr_bytes=0,
                stderr_log="",
                logs_truncated=False,
                raw_logs_omitted=False,
                duration_ms=5,
            )
            for command in commands
        )
        return VerifyInspection(
            before_inventory_hash=inventory,
            after_inventory_hash=inventory,
            command_evidence=evidence,
        )


def inspection_from_change_set(
    change_set: ImplementationChangeSet,
) -> ImplementationInspection:
    baseline: list[FileInventoryEntry] = []
    result: list[FileInventoryEntry] = []
    diff_parts: list[str] = []
    for operation in sorted(change_set.operations, key=lambda item: item.path):
        if operation.kind is ChangeOperationKind.WRITE:
            encoded = (operation.content or "").encode("utf-8")
            if operation.expected_prior_hash is not None:
                baseline.append(
                    FileInventoryEntry(
                        path=operation.path,
                        kind="file",
                        size=1,
                        sha256=operation.expected_prior_hash,
                        executable=False,
                    )
                )
            result.append(
                FileInventoryEntry(
                    path=operation.path,
                    kind="file",
                    size=len(encoded),
                    sha256=hashlib.sha256(encoded).hexdigest(),
                    executable=bool(operation.executable),
                )
            )
            first_line = (operation.content or "").splitlines()[:1]
            added = first_line[0] if first_line else ""
            prefix = (
                "new file mode 100644\n--- /dev/null\n"
                if operation.expected_prior_hash is None
                else f"--- a/{operation.path}\n"
            )
            diff_parts.append(
                f"diff --git a/{operation.path} b/{operation.path}\n"
                f"{prefix}"
                f"+++ b/{operation.path}\n"
                f"@@ -0,0 +1 @@\n"
                f"+{added}\n"
            )
        else:
            if operation.expected_prior_hash is None:
                raise ValueError("Fake delete requires a prior hash")
            baseline.append(
                FileInventoryEntry(
                    path=operation.path,
                    kind="file",
                    size=1,
                    sha256=operation.expected_prior_hash,
                    executable=False,
                )
            )
            diff_parts.append(
                f"diff --git a/{operation.path} b/{operation.path}\n"
                f"deleted file mode 100644\n"
                f"--- a/{operation.path}\n"
                f"+++ /dev/null\n"
            )
    baseline_tuple = tuple(baseline)
    result_tuple = tuple(result)
    unified_diff = "".join(diff_parts)
    return ImplementationInspection(
        baseline_inventory_hash=content_hash(
            [item.to_wire() for item in baseline_tuple]
        ),
        result_inventory_hash=content_hash(
            [item.to_wire() for item in result_tuple]
        ),
        baseline_file_count=len(baseline_tuple),
        result_file_count=len(result_tuple),
        baseline_total_bytes=sum(item.size or 0 for item in baseline_tuple),
        result_total_bytes=sum(item.size or 0 for item in result_tuple),
        changed_paths=change_set.paths,
        baseline_inventory=baseline_tuple,
        result_inventory=result_tuple,
        unified_diff=unified_diff,
        diff_hash=hashlib.sha256(unified_diff.encode("utf-8")).hexdigest(),
    )


def _utc_stamp(value: datetime) -> str:
    current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
