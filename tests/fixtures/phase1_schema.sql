PRAGMA foreign_keys=ON;

CREATE TABLE repositories (
    id INTEGER NOT NULL,
    github_id BIGINT,
    full_name VARCHAR(255) NOT NULL,
    description TEXT,
    html_url VARCHAR(500),
    language VARCHAR(80),
    license_spdx VARCHAR(80),
    stars INTEGER NOT NULL,
    forks INTEGER NOT NULL,
    open_issues INTEGER NOT NULL,
    archived BOOLEAN NOT NULL,
    disabled BOOLEAN NOT NULL,
    default_branch VARCHAR(255),
    topics JSON NOT NULL,
    pushed_at DATETIME,
    has_contributing_guide BOOLEAN NOT NULL,
    health_percentage INTEGER,
    sync_error TEXT,
    last_synced_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (github_id)
);

CREATE UNIQUE INDEX ix_repositories_full_name
    ON repositories (full_name);
CREATE INDEX ix_repositories_language
    ON repositories (language);

CREATE TABLE opportunities (
    id INTEGER NOT NULL,
    github_issue_id BIGINT NOT NULL,
    repository_id INTEGER NOT NULL,
    issue_number INTEGER NOT NULL,
    title VARCHAR(500) NOT NULL,
    body TEXT NOT NULL,
    html_url VARCHAR(500) NOT NULL,
    state VARCHAR(30) NOT NULL,
    labels JSON NOT NULL,
    comments_count INTEGER NOT NULL,
    assignees_count INTEGER NOT NULL,
    author_association VARCHAR(40),
    source_queries JSON NOT NULL,
    issue_created_at DATETIME NOT NULL,
    issue_updated_at DATETIME NOT NULL,
    first_seen_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    eligible BOOLEAN NOT NULL,
    filter_reasons JSON NOT NULL,
    score_total FLOAT NOT NULL,
    score_components JSON NOT NULL,
    risk_penalty FLOAT NOT NULL,
    risk_reasons JSON NOT NULL,
    has_bounty BOOLEAN NOT NULL,
    bounty_amount_usd FLOAT,
    is_strategic BOOLEAN NOT NULL,
    is_tech_match BOOLEAN NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_repository_issue
        UNIQUE (repository_id, issue_number),
    FOREIGN KEY(repository_id)
        REFERENCES repositories (id) ON DELETE CASCADE,
    UNIQUE (html_url)
);

CREATE UNIQUE INDEX ix_opportunities_github_issue_id
    ON opportunities (github_issue_id);
CREATE INDEX ix_opportunities_repository_id
    ON opportunities (repository_id);
CREATE INDEX ix_opportunities_eligible
    ON opportunities (eligible);
CREATE INDEX ix_opportunities_score_total
    ON opportunities (score_total);
CREATE INDEX ix_opportunities_has_bounty
    ON opportunities (has_bounty);
CREATE INDEX ix_opportunities_is_strategic
    ON opportunities (is_strategic);
CREATE INDEX ix_opportunities_is_tech_match
    ON opportunities (is_tech_match);
CREATE INDEX ix_opportunity_eligible_score
    ON opportunities (eligible, score_total);

CREATE TABLE daily_picks (
    id INTEGER NOT NULL,
    selection_date DATE NOT NULL,
    rank INTEGER NOT NULL,
    opportunity_id INTEGER NOT NULL,
    selection_reason VARCHAR(40) NOT NULL,
    score_snapshot FLOAT NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_daily_pick_rank
        UNIQUE (selection_date, rank),
    CONSTRAINT uq_daily_pick_opportunity
        UNIQUE (selection_date, opportunity_id),
    FOREIGN KEY(opportunity_id)
        REFERENCES opportunities (id) ON DELETE CASCADE
);

CREATE INDEX ix_daily_picks_selection_date
    ON daily_picks (selection_date);
CREATE INDEX ix_daily_picks_opportunity_id
    ON daily_picks (opportunity_id);

CREATE TABLE scan_runs (
    id VARCHAR(36) NOT NULL,
    status VARCHAR(30) NOT NULL,
    queries JSON NOT NULL,
    candidate_count INTEGER NOT NULL,
    eligible_count INTEGER NOT NULL,
    selected_count INTEGER NOT NULL,
    repository_count INTEGER NOT NULL,
    error_message TEXT,
    rate_limit_remaining INTEGER,
    rate_limit_reset_at DATETIME,
    started_at DATETIME NOT NULL,
    completed_at DATETIME,
    PRIMARY KEY (id)
);

CREATE INDEX ix_scan_runs_status
    ON scan_runs (status);

INSERT INTO repositories (
    id, github_id, full_name, description, html_url, language, license_spdx,
    stars, forks, open_issues, archived, disabled, default_branch, topics,
    pushed_at, has_contributing_guide, health_percentage, sync_error,
    last_synced_at, created_at, updated_at
) VALUES (
    1, 1001, 'fixture/phase1', 'Frozen Phase 1 repository',
    'https://github.com/fixture/phase1', 'Python', 'MIT',
    1234, 55, 8, 0, 0, 'main', '["ai","mcp"]',
    '2026-07-16 00:00:00', 1, 88, NULL,
    '2026-07-17 08:00:00', '2026-07-17 08:00:00',
    '2026-07-17 08:00:00'
);

INSERT INTO opportunities (
    id, github_issue_id, repository_id, issue_number, title, body, html_url,
    state, labels, comments_count, assignees_count, author_association,
    source_queries, issue_created_at, issue_updated_at, first_seen_at,
    last_seen_at, eligible, filter_reasons, score_total, score_components,
    risk_penalty, risk_reasons, has_bounty, bounty_amount_usd, is_strategic,
    is_tech_match
) VALUES (
    1, 2001, 1, 7, 'Phase 1 fixture issue',
    'A sufficiently detailed frozen Issue body used by migration tests.',
    'https://github.com/fixture/phase1/issues/7', 'open',
    '["help wanted"]', 1, 0, 'MEMBER', '["fixture-query"]',
    '2026-07-01 00:00:00', '2026-07-16 00:00:00',
    '2026-07-17 08:00:00', '2026-07-17 08:00:00',
    1, '[]', 81.5, '{"project_impact":75.0}', 0.0, '[]',
    0, NULL, 1, 1
);

INSERT INTO scan_runs (
    id, status, queries, candidate_count, eligible_count, selected_count,
    repository_count, error_message, rate_limit_remaining, rate_limit_reset_at,
    started_at, completed_at
) VALUES (
    '00000000-0000-0000-0000-000000000001', 'completed',
    '["fixture-query"]', 1, 1, 1, 1, NULL, 4999,
    '2026-07-17 09:00:00', '2026-07-17 08:00:00',
    '2026-07-17 08:01:00'
);

INSERT INTO daily_picks (
    id, selection_date, rank, opportunity_id, selection_reason,
    score_snapshot, created_at
) VALUES (
    1, '2026-07-17', 1, 1, 'strategic', 81.5,
    '2026-07-17 08:01:00'
);
