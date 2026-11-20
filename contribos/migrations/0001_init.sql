CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE repositories (
    full_name TEXT PRIMARY KEY NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL
);
CREATE TABLE opportunities (
    id TEXT PRIMARY KEY NOT NULL,
    repo_full_name TEXT NOT NULL REFERENCES repositories(full_name),
    issue_number INTEGER NOT NULL CHECK(issue_number > 0),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    state TEXT NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '[]',
    data_json TEXT NOT NULL DEFAULT '{}',
    score_total REAL NOT NULL DEFAULT 0,
    score_json TEXT NOT NULL DEFAULT '{}',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    filtered_out INTEGER NOT NULL DEFAULT 0 CHECK(filtered_out IN (0, 1)),
    filter_reasons_json TEXT NOT NULL DEFAULT '[]',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(repo_full_name, issue_number)
);
CREATE TABLE scan_runs (
    id TEXT PRIMARY KEY NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT
);
CREATE TABLE daily_picks (
    day TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK(rank > 0),
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id),
    bucket TEXT NOT NULL,
    PRIMARY KEY(day, rank)
);
CREATE TABLE opportunity_marks (
    opportunity_id TEXT PRIMARY KEY NOT NULL REFERENCES opportunities(id),
    mark TEXT NOT NULL CHECK(mark IN ('starred', 'ignored')),
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY NOT NULL,
    opportunity_id TEXT REFERENCES opportunities(id),
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL CHECK(issue_number > 0),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preparing'
        CHECK(status IN ('preparing', 'ready', 'published', 'closed', 'setup_failed', 'archived')),
    agent_state TEXT NOT NULL DEFAULT 'idle'
        CHECK(agent_state IN ('idle', 'queued', 'running', 'waiting_retry', 'error')),
    agent_profile TEXT NOT NULL,
    isolation TEXT NOT NULL CHECK(isolation IN ('host', 'sbx', 'container')),
    sandbox_ref TEXT,
    session_id TEXT,
    model TEXT,
    mode TEXT,
    agent_caps_json TEXT NOT NULL DEFAULT '{}',
    default_branch TEXT,
    base_sha TEXT,
    work_branch TEXT,
    issue_snapshot_json TEXT NOT NULL DEFAULT '{}',
    tokens_input INTEGER CHECK(tokens_input >= 0),
    tokens_output INTEGER CHECK(tokens_output >= 0),
    token_budget INTEGER CHECK(token_budget >= 0),
    relay_token_hash TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE turns (
    id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('user', 'continue', 'auto_retry', 'pr_draft', 'review', 'recap')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'running', 'completed', 'interrupted', 'failed')),
    client_message_id TEXT UNIQUE,
    stop_reason TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
    error_code TEXT,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE task_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    turn_id TEXT REFERENCES turns(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX task_events_task_seq ON task_events(task_id, seq);
CREATE INDEX turns_task_status ON turns(task_id, status);
CREATE TABLE task_diffs (
    task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    diff_text TEXT NOT NULL,
    files_json TEXT NOT NULL DEFAULT '[]',
    additions INTEGER NOT NULL DEFAULT 0 CHECK(additions >= 0),
    deletions INTEGER NOT NULL DEFAULT 0 CHECK(deletions >= 0),
    diff_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE reviews (
    id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    agent_profile TEXT NOT NULL,
    session_id TEXT,
    diff_hash_before TEXT NOT NULL,
    diff_hash_after TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'invalid', 'failed')),
    verdict TEXT,
    summary TEXT,
    findings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE publications (
    id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('create_pr', 'push_followup', 'update_pr', 'mark_ready')),
    preview_json TEXT NOT NULL,
    preview_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL
        CHECK(status IN ('previewed', 'confirmed', 'running', 'succeeded', 'failed', 'unknown', 'superseded')),
    steps_json TEXT NOT NULL DEFAULT '{}',
    fork_full_name TEXT,
    branch TEXT,
    commit_sha TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    finished_at TEXT
);
CREATE TABLE pull_requests (
    task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
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
CREATE TABLE settings (
    key TEXT PRIMARY KEY NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
