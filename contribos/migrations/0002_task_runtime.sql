ALTER TABLE tasks ADD COLUMN runtime_managed INTEGER NOT NULL DEFAULT 0 CHECK(runtime_managed IN (0, 1));
ALTER TABLE tasks ADD COLUMN delete_requested INTEGER NOT NULL DEFAULT 0 CHECK(delete_requested IN (0, 1));
ALTER TABLE tasks ADD COLUMN recap_pending INTEGER NOT NULL DEFAULT 0 CHECK(recap_pending IN (0, 1));
ALTER TABLE turns ADD COLUMN prompt_text TEXT;
ALTER TABLE turns ADD COLUMN queue_priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE turns ADD COLUMN retry_at TEXT;
ALTER TABLE turns ADD COLUMN continuation_used INTEGER NOT NULL DEFAULT 0 CHECK(continuation_used IN (0, 1));

UPDATE turns
SET status = 'interrupted', completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE status = 'running' AND rowid NOT IN (
    SELECT MAX(rowid) FROM turns WHERE status = 'running' GROUP BY task_id
);
CREATE UNIQUE INDEX turns_one_running_per_task ON turns(task_id) WHERE status = 'running';
