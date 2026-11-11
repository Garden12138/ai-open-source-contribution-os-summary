# Shared GitHub connection

In the native settings page, **GitHub 连接** accepts one Classic personal access
token with `public_repo` for public repository discovery, source ingestion and
later Draft PR publication. This is the user's explicit 2026-10-29 choice;
the earlier separate read-only-token-file request is superseded.
[GitHub scope definitions](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)
describe `public_repo` as read/write access to public repositories. No other scope
is needed for this workflow. Saving a token authorizes no repository mutation.

The browser seals credentials using the existing one-time Publisher grant and
RSA-OAEP/AES-GCM envelope. Only the independent Publisher decrypts and stores
the token in its private encrypted vault. API and coordinator never receive the
GitHub bearer credential. The Publisher's existing durable account probe,
CAS/account rotation, CSRF/origin protection and exact publication confirmations
remain authoritative. Models and Sandboxes get neither token nor read key.

## Read capability

Set `GITHUB_READ_PROXY_URL=http://publisher:8002` on API and coordinator, and
configure an independent `GITHUB_READ_PROXY_KEY` (at least 32 random bytes as
hex) on API, coordinator and Publisher. This key must differ from
`PUBLISHER_MANAGEMENT_KEY`. Do not configure either capability on Provider or
Sandbox Worker. Compose scopes these fields to the appropriate roles.

When enabled, `GitHubClient` sends signed read envelopes to Publisher; it never
falls back to a local GitHub token or anonymous calls on proxy failure. Without
the option, existing discovery-token configuration remains compatible. The
settings response exposes `discovery_shared`, never credentials; metadata reports
the connected account's actual readiness.

The Publisher route `/internal/github-read` accepts only versioned, bounded
public GET requests: issue search, repository metadata, community metadata,
commit lookup and a commit-pinned archive redirect. Search appends `is:public`.
Repository reads independently require `private:false`; mutation endpoints,
arbitrary URLs/methods/headers, path tricks, private repositories and changed
accounts fail closed. Concurrency, response size, time and pagination are bounded.
Only rate-limit/ETag/redirect headers return. GitHub credentials stay in Publisher;
the existing archive validator downloads from allowlisted codeload hosts without
Authorization. No new SQL model or business migration is introduced.

## Recovery and deployment

Disabling the read proxy and restarting API/coordinator restores the existing
separate discovery-token configuration. It does not change plans, approvals,
account events or historical results. Disconnect/reconnect uses the existing
Publisher UI and invalidates account-bound publication previews as before.

The current isolated trial uses port 18090, model Gateway 18091 and a separate
nonroot, read-only-root Publisher container on 18092. Because the macOS host can
access Docker, Publisher is not run as an unrestricted host process. Its two
mounts contain only isolated business data/artifacts and its own private vault;
no Docker socket, host HOME, model credential or Sandbox signing key is mounted.
The API's publication mode remains `none` for this UPEVE trial.

The isolated database was copied with SQLite backup while five owned roles were
stopped and no jobs were active. Every table count and all 57 workbench event
IDs/hashes matched; the original trial database/artifacts are retained. Current
trial storage is `/tmp/upeve-isolated-20261029-v2/business/`. Production services,
the older 18080 trial and historical native databases were not migrated.

Component tests use deterministic GitHub responses. Browser tests exercise real
encryption and API flow with a fake account probe. They do not prove real GitHub
authentication or native contribution success. The user subsequently saved the
connection: actual account probe and public repository/commit/archive reads now
pass. The reported scope is `repo`; the relay still permits public GETs only.
Native MiniMax planning has separate failed/incomplete results in the
[acceptance ledger](maintenance/HISTORY_INDEX.md); access readiness
does not satisfy the contribution gate.
