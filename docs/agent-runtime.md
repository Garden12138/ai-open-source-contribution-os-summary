# Conversational task runtime

This Phase 5 extension implements the user-approved conversation model: progress
questions, detail questions, references to old results, and subsequent commands
can interleave without replacing the actual task or granting authorization.

## State and coordination

`app/agent_runtime.py` is the persistent coordination entry. Existing plan,
execution, review and Publisher services remain the domain authorities. They
retain immutable inputs, exact confirmation, short transactions and CAS checks.

Four independent facts are maintained:

| Fact | Durable source | Meaning |
| --- | --- | --- |
| Current task | Existing domain state and verified artifacts | What actually happened and which inputs are current |
| Dialogue focus | `agent_focus_changed` | Which displayed evidence a follow-up refers to |
| Current request | Original `conversation_received`, optional bound replay | What this turn asks to read or do |
| Authorization | Existing exact plan/acceptance/Publisher confirmations | Which effects are permitted |

A focus never grants a capability. Reading historical evidence does not select
that historical version for execution. A progress interlude preserves the prior
evidence focus. A bare “继续” advances the actual task: running jobs continue;
an eligible failed understanding turn resumes its original request; an
unapproved plan still requires the existing exact confirmation UI.

`agent_turn_classified` distinguishes `observe` and `command`. An observed turn
releases the mutation barrier while its answer is being prepared and cannot
later escalate into a command. Unclassified turns and commands retain the
barrier. Pending commands do not prevent later questions from being understood
or answered. Stops use the existing cancellation journal immediately. Writes
remain serialized by the domain services, with independent exact confirmations.

The Provider process has one foreground lane for conversation/execution
questions and one background lane for planning, coding and review. Each lane
executes at most one model job at once. Separate processes still enforce job
leases; this does not authorize multiple workers for the same lease.

## Bounded model and coding loops

New runtime conversations use `conversation-reasoning-v4`: four calls, six
explicit reads and one format correction per logical turn. A command whose
basis changes before dispatch is reobserved using the remaining original
four-call budget. Failed turns retain their requests and all incurred usage;
continuing never increases or resets the lifetime planning/dialogue allocation.

Before invocation, `app/agent_context.py` admits the actual prompt plus twice
the serialized schema and a wire reserve. The conservative bound is 16,000 UTF-8
bytes/tokens; output is capped at 4,096 and at the remaining lifetime allocation.
The provider and upstream adapter both preserve this smaller output limit.
Older dialogue and memory are indexed into source-bound immutable artifacts.
Large evidence is explicitly paged, with issued references for the exact
original. Required requests and rules remain exact. A request that still cannot
fit is denied before a model-call receipt is created. An admitted checkpoint
describes the exact prompt sent, not the earlier compression draft.

Read steps may discard only presentation. The v4 parser recognizes the known
provider encoding `references:""` on an otherwise valid read-only step as an
empty presentation list. It never coerces actions, tool arguments, non-empty
citations or final responses. A successful read clears obsolete correction
instructions, including after restart. Normalization is recorded separately
from any displayed response or workflow effect.

Runtime execution goes directly from captured source to a ChangeSet proposal;
a separate narrative coding turn is no longer required. The model may request
one to four exact source paths already captured by Sandbox Worker. Each read
has its own invocation, immutable artifact, receipt and child job, and remains
bound to the current editing view, lease and authorization. A proposal lineage
has at most six calls including corrections. Existing paths, prior hashes,
atomic rejection, cumulative repairs and the two-code-repair limit remain in
force. Source reads do not execute commands or access arbitrary host paths.
Each invocation receives its exact remaining call count. Complete source already
in context should be reused; the final admissible call requires a proposal and
disables another read. Exhaustion is a distinct call-budget error, with no retry
or implicit allowance increase. Historical usage and failed records remain;
additional calls require an explicit budget decision.

User-directed Phase 5 recovery on 2026-10-26 adds one maintenance-only exception:
an exhausted lineage can receive one exact 6 → 8 decision. The decision binds
the task, original job/payload, execution/plan hashes, coding context,
conversation, failed job, observed usage/revision and instruction hash. The
service has no HTTP or model-tool entry point. It verifies the still-current
execution authority, appends a maintenance-attributed grant and retries in one
transaction. Replays return the same job; different second grants, stale inputs,
busy/stopped tasks and invalid records fail closed. Defaults and other lineages
remain at six; prior calls and approvals are never rewritten.

Bounded cached helper evidence is retained across read steps in the same
lineage/editing view (64 KB of content, at most 32 references). Its immutable
read receipts and hashes are checked again. Editable files still come from
`coding_context`; helper evidence grants no edit permission. This avoids losing
an earlier helper when a later read returns a different file.

No SQL schema changes are needed: older journals retain the default policy and
new grant events are explicitly versioned. Before applying a grant, back up an
idle database and its artifacts plus the running service sources. Rollback must
drain writers and preserve later receipts before restoring that matched set;
older workers must not consume jobs resumed under the extended policy.

## Failure handling and scope

Coding ProviderRunError failures retain their validated safe code/message,
instead of becoming `nvidia_coding_failed`. An append-only
`coding-provider-failure-v1` event commits atomically with the failed Job and
binds its attempt number, payload hash and retryability. Recovery considers only
matching orchestrator evidence. Truncation maps to `budget/revise_output_budget`
and is not offered as an immediate retry; provider-declared non-retryable
failures likewise remain non-retryable. Logs contain the validated error code
and exception type, never raw exception text or provider response data. Older
generic failures without that evidence remain unknown; a later attempt cannot
rewrite their diagnosis.

The trusted local correction entry also supports v2 direct proposals without
chat turns; exact context/conversation/approval bindings still apply. A separate
maintenance-only edit/append correction derives its input exclusively from the
verified failed AgentInvocation and rejection artifact. An adjacent exact edit
followed by plain append can become the existing append-with-edits form only
after proving identical output bytes, prior hashes and file mode. An explicit
mode equal to the frozen mode and an empty append edit list are identity fields;
actual mode changes, other duplicate combinations, nonadjacent pairs, unknown
fields, stale inputs, secrets or inexact anchors are rejected. The normal model
validator continues to reject duplicate paths.

Normalization proof, original/normalized hashes and the existing correction
receipt commit together. No code string is authored, model call added, retry
counter reset, plan changed, or failed receipt overwritten. There is no HTTP or
model-tool entry point for this maintenance helper. It records maintenance
attribution and cannot satisfy autonomous acceptance. Real sandbox execution,
all approved checks and fresh independent review still follow.

`worker_runtime_incomplete` means the trusted code-generation service lacks an
application component. It requires `platform/rebuild_runtime`, does not consume
a code-repair round, and is not offered as a blind retry. The worker uses a fixed
redacted message; missing-module exception text and paths are never persisted or
logged. Deployment builds statically check internal imports, including lazy
imports, before installing the application. A verified full-runtime replacement
may resume the same failed job under its still-current approval and remaining
budget; earlier failures and usage remain recorded.

Blockers include a deterministic recovery category and action: protocol,
transport, budget, environment, code, interrupted lease, unknown external
outcome or platform. This is metadata for the existing verified recovery
interfaces, not an unrestricted model retry tool. An unknown external write
requires reconciliation; environment recovery does not consume a code repair.

The chosen policy preserves issue scope when the pre-change baseline has
ordinary test failures. The baseline remains available to coding and independent
review. Every mandatory post-change check must still pass before acceptance.
If remaining failures match the original command and both output hashes
exactly, the runtime stops for a plan revision instead of spending code repairs
on unchanged failures. Test-discovery directory failures are environment
blockers. No check is removed or converted into a pass.

The real Python sample exposed a related prerequisite: setuptools dynamic
requirement files must participate in local-package discovery. Preparation now
reads bounded `.txt`/`.lock` declarations inside the selected project, freezes
their hashes, validates requirements and discovers local dependencies before
resolution. Absolute/traversal paths, excessive files and unsafe requirements
fail closed. Build metadata is still evaluated only in the isolated preparation
container; wheel metadata remains authoritative.

Trusted proposal correction and runtime rebaseline helpers append
`agent_maintenance_recorded`. Observations expose `maintenance_count` and
`autonomous_eligible`. Historical operator receipts are counted without
rewriting them. Maintenance-assisted work cannot count as independent
completion merely because a later review passes.
`autonomous_eligible` only excludes recorded assistance; it is not a success
counter or proof that unrecorded historical work was autonomous. Historical
claims still require the audited provenance and real acceptance evidence.

## API and native UI

Existing endpoints and response fields remain. Message submission optionally
accepts the pair `reply_to_message_id` / `reply_to_message_hash`; both are required
together and must identify a displayed result from the same task. Changing the
message or reference under an existing idempotency key is rejected.

`conversation.observation` provides the typed runtime version, domain revision,
task state, focus, foreground/background jobs, blocking message IDs and
maintenance status. `pending_count` includes all unanswered turns;
`blocking_count` counts turns that prevent a new mutation. Read-only questions
therefore do not unnecessarily disable valid confirmation controls.

The native UI provides “询问这条结果” and “取消引用”. Draft text and its reply
target survive refresh. A failed submission retries with the same complete
request and key. Explicit clarification answers remain bound to their original
question batch across progress/read-only interludes and do not authorize
execution.

## Event migration and recovery

There is no SQL model change or destructive migration. This is an additive,
versioned event migration. New tasks get `agent_runtime_enabled` at creation.
Existing tasks opt in on the next API message only when idle and without
pending messages. Existing queued/running v2/v3 jobs retain their payloads,
limits, leases and approvals. Their supported parsers remain available.

Before deployment, an idle SQLite online backup and a full artifact copy were
created at `/data/backups/agent-runtime-20261024T074816Z`, together with the
original event hashes and artifact manifest. The backup contains 1,793 events
and 874 artifacts and passed SQLite integrity and foreign-key checks.

To roll back, stop the local workers/API/Publisher, preserve the failed-state
database and newly produced artifacts, restore the backup database and its
artifact tree as a matched pair, deploy the prior application image, then
recheck integrity and service health before resuming. Never downgrade active v4
jobs in place or delete later receipts to force an old parser to accept them.
Keep the newer runtime if preserving post-upgrade work is required. Restore
does not grant new execution or external-action consent.

## Verification and limits

The deterministic suite covers nonlinear dialogue, progress without model
calls, observed-turn barriers, stale command reobservation, original-request
resume, restart/focus persistence, reply-source rejection, clarification
interludes, direct coding/source receipts, exact checkpoint admission, large
evidence paging, maintenance attribution and dynamic path restrictions.
Browser tests cover refresh/retry/idempotency, reference binding, enabled
confirmations during observations and desktop/mobile layouts.

Six controlled archives completed real configured-model coding, real OCI
preparation/baseline/implementation/verification and independent model review:
npm with TypeScript, Yarn Classic, Yarn modern, pnpm, declarative Python with a
local package, and dynamic Python with a local package. No operator patch or
fabricated coding/review response was used in passing runs. Fixture discovery
and the approved fixture plan are identified explicitly in the evidence; these
are not full real-GitHub acceptance. Initial failed harness runs are retained:
the first Python command had the wrong working directory, and the dynamic case
exposed missing local dependency discovery. Both were corrected and rerun.

Native Linux amd64/Docker Engine acceptance (P5-G06), dedicated real-GitHub
independent-task acceptance (UXR-G01) and ENV-G01 remain open. Controlled
archives, an approved fixture plan and browser mocks cannot close those gates.
The task runtime improves bounded autonomy within supported capabilities; it
does not claim that arbitrary repositories can now complete independently.

The historical task that previously incurred 16,478 input tokens was restored
from the backup into a separate database/artifact copy. Its original request and
305 original event hashes were preserved. The new conservative admission bound
is 14,818, with no added model call or production task mutation. Post-deployment
checks preserve all seven production task chains, 1,793 event hashes and 874
artifact hashes, with zero database/artifact runtime-secret or canary matches.
All six services match the final 19 critical source-file hashes. A separate
real-model progress/source question completed in two calls, read the entire
small source file and left background work and authorization unchanged.
