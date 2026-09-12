"""Run inside a credential-free maintenance container with /data mounted."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile


def main() -> None:
    source = Path("/data/contribos.db")
    if not source.is_file():
        raise SystemExit("Existing database not found; refusing an empty backup")
    target = Path("/data/backups") / (
        "pre-studio-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    target.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(source) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("Source integrity check failed")
        if database.execute("PRAGMA foreign_key_check").fetchall():
            raise SystemExit("Source foreign key check failed")
        active = database.execute(
            "SELECT count(*) FROM jobs WHERE state IN ('queued','leased','running')"
        ).fetchone()[0]
        if active:
            raise SystemExit("Active Jobs require explicit reconciliation before upgrade")
        with sqlite3.connect(target / "contribos.db") as backup:
            database.backup(backup)
        tables = ["opportunities", "analysis_versions", "contribution_tasks", "plan_versions",
                  "execution_attempts", "review_runs", "draft_pull_requests"]
        records = {}
        for table in tables:
            rows = database.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
            records[table] = {
                "count": len(rows),
                "hash": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            }
    with tarfile.open(target / "artifacts.tar.gz", "w:gz", dereference=False) as archive:
        archive.add("/data/artifacts", arcname="artifacts")
    for path in target.iterdir():
        path.chmod(0o600)
    manifest = target / "manifest.json"
    manifest.write_text(json.dumps(records, indent=2))
    manifest.chmod(0o600)
    print(json.dumps({"backup": str(target), "records": records}))


if __name__ == "__main__":
    main()
