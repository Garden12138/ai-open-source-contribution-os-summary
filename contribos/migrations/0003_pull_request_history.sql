ALTER TABLE pull_requests RENAME TO task_pull_requests;
CREATE TABLE pull_requests (
    id INTEGER PRIMARY KEY,
    task_id TEXT UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    repo_full_name TEXT NOT NULL,
    number INTEGER NOT NULL CHECK(number > 0),
    url TEXT NOT NULL,
    state TEXT NOT NULL,
    draft INTEGER NOT NULL CHECK(draft IN (0, 1)),
    merged INTEGER NOT NULL CHECK(merged IN (0, 1)),
    review_decision TEXT,
    checks_state TEXT,
    comments_count INTEGER NOT NULL DEFAULT 0 CHECK(comments_count >= 0),
    head_sha TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO pull_requests (task_id,repo_full_name,number,url,state,draft,merged,review_decision,
                           checks_state,comments_count,head_sha,last_synced_at,data_json)
SELECT task_id,repo_full_name,number,url,state,draft,merged,review_decision,
       checks_state,comments_count,head_sha,last_synced_at,data_json FROM task_pull_requests;
DROP TABLE task_pull_requests;
CREATE UNIQUE INDEX imported_pull_request ON pull_requests(repo_full_name COLLATE NOCASE, number) WHERE task_id IS NULL;
