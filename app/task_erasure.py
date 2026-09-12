"""Explicit, user-requested physical erasure of local contribution history.

Normal writes remain immutable. The eraser runs with an exclusive SQLite write
transaction, restores every delete trigger before commit, and keeps the deleted
task and durable job until file cleanup succeeds. A crash can leave missing files
on an inaccessible task; retry completes the same erasure, never restores it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import Database
from app.jobs import JobService, TERMINAL_STATES
from app.models import ContributionTask, Job
from app.planning import ContributionTaskConflictError
from app.task_visibility import TaskVisibilityService, current_visibility

ERASURE_KIND = "task_erasure"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_ROWS = 200_000


def enqueue_erasure(session: Session, task_id: str, expected_sequence: int) -> Job | None:
    if session.get(ContributionTask, task_id) is None:
        return None
    current = current_visibility(session, task_id)
    if not current or current.state != "deleted":
        TaskVisibilityService(session).change(task_id, target="deleted", expected_sequence=expected_sequence, commit=False)
    elif expected_sequence not in {current.sequence, current.sequence - 1}:
        raise ContributionTaskConflictError("任务状态已变化，请刷新后重试")
    job, _ = JobService(session).enqueue(
        kind=ERASURE_KIND, idempotency_key="erase:" + task_id,
        payload={"erase_task_id": task_id}, max_attempts=3, timeout_seconds=120,
    )
    if job.state in {"failed", "cancelled", "timed_out"} and job.attempt_count < job.max_attempts:
        job = JobService(session).retry(job.id)
    return job


def _tokens(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_tokens(item) for item in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_tokens(item) for item in value)) if value else set()
    if isinstance(value, str):
        if value.startswith(("{", "[")):
            try:
                return _tokens(json.loads(value))
            except (ValueError, RecursionError):
                pass
        return {value}
    return set()


def _quoted(identifier: str) -> str:
    if not _ID.fullmatch(identifier):
        raise ValueError("Unsupported database identifier")
    return '"' + identifier + '"'


def _snapshot(connection: sqlite3.Connection) -> tuple[dict, dict]:
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='_schema_migrations'")]
    rows, links = {}, {}
    total = 0
    for table in tables:
        count = connection.execute(f"SELECT count(*) FROM {_quoted(table)}").fetchone()[0]
        total += count
        if total > _MAX_ROWS:
            raise ValueError("History exceeds online erasure limit; use offline maintenance")
        rows[table] = {row["_erase_rowid"]: dict(row) for row in connection.execute(
            f"SELECT rowid AS _erase_rowid,* FROM {_quoted(table)}")}
        links[table] = list(connection.execute(f"PRAGMA foreign_key_list({_quoted(table)})"))
    return rows, links


def _selection(rows: dict, links: dict, task_id: str, job_id: str | None) -> dict[str, set[int]]:
    chosen = {name: set() for name in rows}
    chosen["contribution_tasks"] = {key for key, row in rows["contribution_tasks"].items() if row["id"] == task_id}
    if job_id:
        chosen["jobs"] = {key for key, row in rows["jobs"].items() if row["id"] == job_id}
    # Follow only descendants. Discovery snapshots, analysis versions, global
    # model profiles, preferences and other tasks are not erasure targets.
    for _ in range(len(rows) + 4):
        before = sum(map(len, chosen.values()))
        identifiers = {str(row["id"]) for table in rows for key, row in rows[table].items()
                       if key in chosen[table] and "id" in row}
        for table, records in rows.items():
            if table == "artifacts":
                continue
            for key, row in records.items():
                if key in chosen[table]:
                    continue
                related = any(row.get(link["from"]) is not None and any(
                    parent.get(link["to"]) == row[link["from"]]
                    for parent_key, parent in rows.get(link["table"], {}).items()
                    if parent_key in chosen.get(link["table"], set())) for link in links[table])
                if table in {"jobs", "audit_events"}:
                    related |= bool(_tokens(row) & identifiers)
                if table == "model_config_versions":
                    related |= row["scope"] == "task:" + task_id
                if related:
                    chosen[table].add(key)
        # Parent jobs of task-specific descendants also belong to this task.
        owned_jobs = {row.get(link["from"]) for table in rows for link in links[table]
                      if link["table"] == "jobs" for key, row in rows[table].items() if key in chosen[table]}
        chosen["jobs"].update(key for key, row in rows["jobs"].items() if row["id"] in owned_jobs)
        if sum(map(len, chosen.values())) == before:
            return chosen
    raise ValueError("Erasure dependency graph did not converge")


def _safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    if path == root or not path.is_relative_to(root) or ".." in Path(relative).parts:
        raise ValueError("Erasure path escapes storage")
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError("Erasure refuses symlinks")
    if path.exists() and not path.is_file():
        raise ValueError("Erasure expected a regular file")
    return path


def _backup_files(data_root: Path, task_id: str) -> list[Path]:
    root = data_root / "backups"
    if not root.exists():
        return []
    if root.is_symlink():
        raise ValueError("Backup root is a symlink")
    files = []
    for folder in sorted(root.iterdir()):
        if not re.fullmatch(r"pre-studio-\d{8}T\d{6}Z", folder.name):
            continue
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("Invalid managed backup")
        database = _safe_file(root, str(folder.relative_to(root) / "contribos.db"))
        if not database.exists():
            # Database is deleted last, so an archive without its database is
            # not an identifiable backup and must never be guessed as in scope.
            if (folder / "artifacts.tar.gz").exists():
                raise ValueError("Backup ownership cannot be established")
            relevant = True
        else:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as old:
                relevant = old.execute("SELECT 1 FROM contribution_tasks WHERE id=?", (task_id,)).fetchone() is not None
        if relevant:
            for child in sorted(folder.iterdir(), key=lambda item: item.name == "contribos.db"):
                if child.name not in {"contribos.db", "artifacts.tar.gz", "manifest.json", "contribos.db-wal", "contribos.db-shm"}:
                    raise ValueError("Unknown file in managed backup")
                files.append(_safe_file(root, str(child.relative_to(root))))
    return files


def erase_task(connection: sqlite3.Connection, artifact_root: Path, task_id: str,
               *, job_id: str | None = None, timeout_seconds: float = 110) -> dict[str, int]:
    """Physically remove a deleted task, its descendants and unshared files.

    Caller owns a dedicated connection, never an ORM Session transaction.
    Audit gaps remain detectable; surviving events are never rewritten/rehashed.
    """
    if not _ID.fullmatch(task_id):
        raise ValueError("Invalid task identifier")
    artifact_root = artifact_root.absolute()
    if artifact_root.is_symlink() or artifact_root.parent == artifact_root:
        raise ValueError("Invalid artifact root")
    deadline = time.monotonic() + timeout_seconds
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        active = connection.execute("SELECT id FROM jobs WHERE state IN ('queued','leased','running') AND kind!=? LIMIT 1", (ERASURE_KIND,)).fetchone()
        if active:
            raise ValueError("Other work is active; retry erasure when idle")
        current = connection.execute("SELECT state FROM task_visibility_versions WHERE task_id=? ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
        if current is None or current["state"] != "deleted":
            raise ValueError("Task has not been confirmed for deletion")
        rows, links = _snapshot(connection)
        chosen = _selection(rows, links, task_id, job_id)
        removed_tokens = set().union(*(_tokens(row) for table in rows for key, row in rows[table].items() if key in chosen[table]))
        kept_tokens = set().union(*(_tokens(row) for table in rows if table != "artifacts"
                                   for key, row in rows[table].items() if key not in chosen[table]))
        candidates = {token for token in removed_tokens - kept_tokens if _HASH.fullmatch(token)}
        files = []
        for key, row in rows.get("artifacts", {}).items():
            if row["id"] in candidates:
                chosen["artifacts"].add(key)
                expected = f'sha256/{row["id"][:2]}/{row["id"]}'
                if row["storage_key"] != expected:
                    raise ValueError("Invalid artifact storage key")
                files.append(_safe_file(artifact_root, expected))
        for digest in candidates:
            for prefix, suffix in (("repository-archives", ""), ("change-sets", "")):
                files.append(_safe_file(artifact_root, f"{prefix}/{digest[:2]}/{digest}{suffix}"))
        backups = _backup_files(artifact_root.parent, task_id)
        # Only DELETE triggers are suspended, inside this write transaction.
        # A rollback restores them; every original statement is restored before commit.
        triggers = [(row["name"], row["sql"]) for row in connection.execute(
            "SELECT name,sql,tbl_name FROM sqlite_master WHERE type='trigger'")
            if chosen.get(row["tbl_name"]) and re.search(r"BEFORE\s+DELETE\b", row["sql"], re.I)]
        connection.execute("PRAGMA defer_foreign_keys=ON")
        for name, _ in triggers:
            connection.execute("DROP TRIGGER " + _quoted(name))
        for table, keys in chosen.items():
            connection.executemany(f"DELETE FROM {_quoted(table)} WHERE rowid=?", [(key,) for key in keys])
        for _, statement in triggers:
            connection.execute(statement)
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("Erasure would break retained references")
        if time.monotonic() > deadline:
            raise TimeoutError("Erasure exceeded its budget")
        # Files disappear before the task/job commit. On failure their tombstone
        # and job remain durable; retry treats already missing files as success.
        for path in [*files, *backups]:
            if time.monotonic() > deadline:
                raise TimeoutError("Erasure exceeded its budget")
            path.unlink(missing_ok=True)
        for folder in {path.parent for path in backups}:
            folder.rmdir()
        connection.commit()
        # Scrub old WAL pages where no reader is pinning them. A busy checkpoint
        # does not undo committed logical deletion and is retried by maintenance.
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {table: len(keys) for table, keys in chosen.items() if keys}
    except BaseException:
        connection.rollback()
        raise


class TaskErasureWorker:
    def __init__(self, database: Database, artifact_root: str, *, worker_id: str) -> None:
        self.database, self.artifact_root, self.worker_id = database, Path(artifact_root), worker_id

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        with self.database.session() as session:
            jobs = JobService(session)
            leased = jobs.lease_next(worker_id=self.worker_id, kinds=(ERASURE_KIND,), lease_seconds=150, now=now)
            if leased is None:
                return None
            job = jobs.start(leased.id, worker_id=self.worker_id, now=now)
            jobs.heartbeat(job.id, worker_id=self.worker_id, lease_seconds=150, now=now)
            task_id, job_id = job.payload["erase_task_id"], job.id
        raw = self.database.engine.raw_connection()
        try:
            erase_task(raw.driver_connection, self.artifact_root, task_id, job_id=job_id)
            job.state = "succeeded"
            return job
        except Exception:
            with self.database.session() as session:
                return JobService(session).fail(job_id, worker_id=self.worker_id,
                    error_code="task_erasure_failed", error_message="永久删除未完成，请在其他任务结束后重试清理。", now=now)
        finally:
            raw.close()
