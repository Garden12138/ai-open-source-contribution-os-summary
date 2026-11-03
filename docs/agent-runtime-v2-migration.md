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

## Forward revision 0039 and environment contracts

`0039_environment_contracts` adds a guard for versioned, exact expected-Issue
reproduction declarations in command JSON, without rewriting old rows. New
signed dependency plan v6 and baseline v2 artifacts are opt-in and pinned to new
plan bindings. Disabling that engine blocks its execution. Old approvals and
preflight hashes retain their parser. Restore matched pre-upgrade code/database/
artifacts for rollback, preserving later usage and failures. Initialize fresh
schemas serially before starting roles. Actual upgrade/backup/hash preservation
and unresolved acceptance are documented in [environment-engine-v1.md](environment-engine-v1.md).

## Forward revision 0041: frozen native model functions

New Runtime v2 plans with a bound DeepSeek implementation profile pin
`agent-native-tools-v1` and a hash of the restricted function schemas, trusted
instruction, four-call limit and versioned prompt projection. Exact execution preview/API confirmation includes
`model_transport_hash`; authorization and TaskSpec retain the same contract.
`0041_native_model_tools` guards new plan/authorization event JSON and rejects
removing the transport from an authorization. It changes no historical rows.
An absent field retains the exact legacy TaskSpec wire/hash and structured-result
adapter, including existing DeepSeek sessions. Rebinding requires a child plan
and fresh confirmation; existing approvals cannot switch transport in place.

Native calls still become the existing typed Decision and restricted tool Jobs.
Function registration adds no shell, network, command arguments or approval
authority. Finish requests still require actual edit/check evidence and independent
Review. Model profiles, limits and old unknown reservations are unchanged.

Drain writers, take a matched SQLite/artifact/code backup, and initialize 0041
serially before starting roles. Upgrade/restore tests preserve existing events,
plans, approvals, attempts and reviews and check integrity/foreign keys. To recover,
stop writers and restore the matched backup; preserve subsequent evidence and usage
separately. Never remove a frozen transport or clear budgets for rollback. The
isolated deployment does not update formal services or their data.

The schema-reference/history-index projection changes that contract digest and
therefore requires a newly bound child plan; it cannot reinterpret an old native
approval. Existing SQL0041/hash guards and context-artifact lists already cover
the unchanged domain shape. The full paged index is versioned advisory evidence,
not a second workflow store or new execution authority. Older structured TaskSpec
and prompt wire remain exact. Drain before rolling code back; preserve the newer
artifacts, failures and usage, and let incompatible native hashes block instead
of stripping their binding or clearing a checkpoint.

The bounded complete-task-input promotion changes the native projection digest
again. It uses the existing task artifact, context issuance and SQL0041 shape;
no new migration is needed. Older native payloads remain readable and stale for
new execution. Incomplete historical maintenance records are retained unchanged
and counted conservatively as `legacy_unverified`; they cannot establish a
zero-maintenance success.

The `native-batch-feedback-v3` projection freezes a new transport digest for
complete latest-batch observation and bounded receipt pages. It uses existing
ToolResult artifacts, journal sequences and context issuance; no SQL shape or
migration changes. Existing native hashes remain readable but stale for execution;
rebinding requires a child Plan and fresh exact confirmation. Legacy structured
prompt wire is unchanged. Projection changes only newly delivered inline bytes;
stored receipts and their pagination remain exact. A smaller inline source page
explicitly retains the original receipt reference and original next offset, while
its displayed continuation points to the first undelivered UTF-8 byte. Preserve
new artifacts, failures and cumulative usage before draining or rolling back;
never replay the old failed session or remove its frozen transport.

The package proxy's successful public-address cache changes only ephemeral
transport state. No SQL shape, existing serialized policy/recipe, API or stored
receipt changes; no migration is needed. The bounded 30-second cache is empty on
restart, removes expired entries before refresh and never supplies failed/private
or stale destinations. Source freezes and exact execution approvals must be
reissued for the updated implementation; an old preparation with its single
attempt consumed cannot be reset or resumed through an unrelated node_tools
proof. Drain work before deployment/rollback and retain all failed native and
independent diagnostic artifacts. A controlled same-recipe pass does not restore
the native job, replace its failure or establish a successful native journey.

Keep every live business SQLite connection in one kernel/filesystem lock domain.
The isolated macOS host-process/Linux Publisher bind-mount arrangement exposed
different open WAL inodes and an invalid combined view. Recovery must quiesce
writers, preserve raw DB/WAL/SHM evidence, independently verify any recovery
candidate and every available hash chain, and retain missing/uncheckpointed tails
as unknown. Never delete a live WAL or silently retry a submitted execution.
The recovered isolated business roles share a Linux Docker named volume; the
credential-only model Gateway stays separate. Only the trusted Sandbox Worker
has the Docker socket, and only Publisher mounts its private credential volume.
Recent connection events absent from a verified candidate are not fabricated;
a preserved credential can be re-bound inside Publisher through the existing
sealed API and a new real read-only account probe. See
[SQLite's WAL requirements](https://sqlite.org/wal.html).
