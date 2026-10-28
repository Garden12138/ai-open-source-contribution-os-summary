# Runtime v2 migration and recovery

The existing SQLite/Workbench journal and content-addressed ArtifactStore remain
the durable authority. v2 events are explicitly versioned. Existing task events,
PlanVersions, approvals, ChangeSet proposals and artifact hashes are retained.
Active v1 jobs retain their original parsers and budgets; activation is permitted
only at an idle, explicit execution boundary. The rollout switch defaults off.

Before an isolated upgrade, stop writers and use SQLite's online backup API;
copy the matching artifact tree and record code revision, migration revision,
row counts and event hashes. Upgrade the copy, verify integrity/foreign keys,
replay its historical chains, then exercise cancellation and restart there.
Never point this branch at production storage.

Forward migrations are additive; no historical migration is edited. A downgrade
requires draining v2 jobs and restoring the matched pre-upgrade database,
artifacts and code into an isolated instance. Preserve later failures and usage
before restoring; do not delete events, reset budgets or reinterpret approval.
An unresolved tool outcome is reconciled using its call/input/receipt hashes,
not blindly repeated. Lost provider replies remain consumed calls.

## Forward revision 0038 and runtime pinning

Node dependency results may additionally contain the versioned
`node-environment-result-v3` receipt for exact data-cache mappings. This is an
additive artifact protocol, not a database schema or approval rewrite. v1/v2
results remain readable and are not retrofitted with cache links. A new offline
preparation freezes each allowed data link in its inventory; a new verifier
checks the receipt and creates a fresh, bounded cache without writing into that
inventory. Old failed baselines remain failed evidence. Rollback to code without
v3 support requires draining v3 tasks first; older verifiers must reject v3 rather
than silently omit its cache binding. Rebuild a fresh frozen environment under
the original exact authorization when permitted, otherwise obtain a new exact
plan approval. Never edit an old environment receipt or reset its history.

`0038_agent_runtime_v2` adds a trigger for versioned v2 session/checkpoint events,
requiring orchestrator identity, an existing task-local session and exact
checkpoint predecessor hash. Migration registry checksums and all old rows stay
intact. The migration test uses an actual v0037 database, SQLite online backup,
forward upgrade, original event-hash comparison, stale-CAS failure injection,
restored backup upgrade, integrity and foreign-key checks.

New execution authorization freezes `agent_runtime_version`. An old authorization
without this field stays v1, including when the rollout switch is enabled.
An active v2 authorization with the switch off blocks; it never falls back to
v1. Drain v2 before rollback. Do not edit old authorizations to switch engines.
The default SDK/tool limits attach to the immutable session spec. Repairs under
the same approval inherit snapshot, cumulative usage and the original deadline.

Vendor tool-call IDs are scoped to a model response. The platform derives the
execution ID from the immutable model Job ID and vendor ID, preserving both the
original decision and the namespaced tool receipt. This prevents an ordinary
call-ID reuse from becoming an accidental global replay or input conflict.

New planning requests also record `agent_runtime_version` at creation. v2 source
indexing/search/line paging and reduced prompt limits apply only to those
requests; old pending v1 planning jobs retain their original context policy.
Source artifacts remain complete and immutable. Approval is still required
after planning and before dependency preparation, baseline or implementation.

## Isolated operator sequence

Use a fresh database/artifact directory and non-conflicting loopback port. Set
`AGENT_RUNTIME_V2_ENABLED=1`, `SANDBOX_STAGE_RUNTIME=docker`, a digest-pinned
`WORKBENCH_RUNNER_IMAGE` and a private sandbox job signing key. Keep
`PUBLISHER_MODE=none`. Start `serve`, `worker`, `provider-worker` and
`sandbox-worker` as separate roles. Model keys belong only in the credential
Gateway; Provider uses its scoped gateway signing mechanism. Supply a declared
read-only discovery token only to API/Orchestrator. Sandbox must receive none
of these credentials; Docker capability belongs only to its trusted supervisor.
Do not source a broad production `.env` into every role.

Open the native task and inspect the full generated plan, exact source/base,
write paths, all argv/cwds and current model binding before human confirmation.
Do not approve from a stale saved screenshot. The native exact execution
control creates authorization; a chat “continue” does not. Preparation and
baseline run after that boundary. If Review passes, inspect actual diff/test/
review artifacts and obtain separate final human acceptance. Publisher preview
and any external write retain their separate exact account/action approvals.

On shutdown, pause/cancel through the task control and drain in-flight work.
On restart, use the same matched database, artifacts, code, runner and private
signing configuration. An unacknowledged tool start blocks for reconciliation;
never clear that event or lease to force a second execution. Preserve failed
native samples before repairing platform code; freeze a new sample afterward.
