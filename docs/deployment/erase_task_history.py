"""Credential-free maintenance: preview or erase an exact set of deleted tasks.

Run in a no-network maintenance container with only contribos-local-data mounted
at /data. Stop all writers before --apply. This action intentionally removes
matching backups; it must not be wrapped in the normal pre-deployment backup flow.
"""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from app.task_erasure import _backup_files, _selection, _snapshot, erase_task


def retained_fingerprints(connection: sqlite3.Connection) -> dict[str, str]:
    checks = {
        "opportunities": "SELECT * FROM opportunities ORDER BY id",
        "analyses": "SELECT * FROM analysis_versions ORDER BY id",
        "model_settings": "SELECT * FROM model_config_versions WHERE scope NOT LIKE 'task:%' ORDER BY id",
        "preferences": "SELECT * FROM user_preference_versions ORDER BY id",
    }
    return {name: hashlib.sha256(json.dumps([tuple(row) for row in connection.execute(sql)], sort_keys=True).encode()).hexdigest()
            for name, sql in checks.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--task-set-hash")
    args = parser.parse_args()
    path = Path("/data/contribos.db")
    connection = sqlite3.connect(path.as_uri() + ("?mode=rw" if args.apply else "?mode=ro"), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("SELECT 1 FROM jobs WHERE state IN ('queued','leased','running') LIMIT 1").fetchone():
            raise SystemExit("Active jobs prevent offline erasure")
        task_ids = [row[0] for row in connection.execute("""SELECT task_id FROM task_visibility_versions v
            WHERE state='deleted' AND sequence=(SELECT max(sequence) FROM task_visibility_versions n WHERE n.task_id=v.task_id)
            ORDER BY task_id""")]
        fingerprint = hashlib.sha256(json.dumps(task_ids).encode()).hexdigest()
        before = retained_fingerprints(connection)
        rows, links = _snapshot(connection)
        selected = {table: set() for table in rows}
        backup_paths = set()
        for task_id in task_ids:
            for table, keys in _selection(rows, links, task_id, None).items():
                selected[table].update(keys)
            backup_paths.update(path.parent for path in _backup_files(Path("/data"), task_id))
        summary = {"task_count": len(task_ids), "task_set_hash": fingerprint,
                   "records": {table: len(keys) for table, keys in selected.items() if keys},
                   "matching_backups": len(backup_paths)}
        if not args.apply:
            print(json.dumps(summary))
            return
        if not args.task_set_hash or args.task_set_hash != fingerprint:
            raise SystemExit("Task set differs from the reviewed preview")
        for task_id in task_ids:
            erase_task(connection, Path("/data/artifacts"), task_id)
        assert retained_fingerprints(connection) == before, "Unrelated records changed"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        dump = "\n".join(connection.iterdump())
        assert not any(task_id in dump for task_id in task_ids), "Task references remain"
        # The maintenance container is intentionally read-only at its root; do
        # not require VACUUM (which needs a temporary database file). Logical
        # deletion and secure_delete are complete; a later writable maintenance
        # window may compact the file if desired.
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        summary.update({"applied": True, "retained_records_unchanged": True,
                        "remaining_tasks": connection.execute("SELECT count(*) FROM contribution_tasks").fetchone()[0],
                        "remaining_audits": connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
                        "integrity": "ok"})
        print(json.dumps(summary))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
