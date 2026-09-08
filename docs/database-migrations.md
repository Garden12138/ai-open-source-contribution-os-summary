# Database migrations

The application uses an ordered, checksummed migration registry in
`app/migrations/versions`. API startup and CLI scans both call
`Database.create_schema()`, which now means "upgrade to the latest registered
revision", not `Base.metadata.create_all()`.

## Safety properties

- Applied revisions are recorded in `_schema_migrations`.
- Every revision has a deterministic checksum. Changing an applied migration
  fails startup instead of silently changing history.
- Revision history must be ordered, unique, and contiguous.
- An unversioned database is adopted only when the baseline migration recognizes
  its complete tables, columns, primary keys, and foreign keys.
- Partial or unknown schemas fail closed.
- Every migration must include non-empty recovery instructions or the registry
  refuses to start.
- Migration code must preserve existing records. Dropping and recreating the
  database is not an upgrade strategy.

## Current revisions

### `0001_phase1_baseline`

Purpose:

- create the original repositories, opportunities, daily picks, and scan runs
  schema for a new database; or
- validate and stamp an existing Phase 1 database without rewriting its rows.

Recovery:

1. Stop all ContribOS processes before manually replacing a database file.
2. Keep an untouched copy of the pre-migration SQLite file.
3. If an empty-database migration fails, fix the reported configuration or
   filesystem problem and retry.
4. If an existing database cannot be adopted, do not edit the version table.
   Restore the untouched file and investigate the reported schema difference.
5. Verify a restored file with `PRAGMA integrity_check` before restarting.

This baseline has no destructive downgrade. Removing its version row would make
provenance ambiguous and is not a supported rollback.

### `0002_provenance`

Purpose:

- add immutable opportunity snapshots and rule-score versions;
- bind future daily picks to their exact scan, snapshot, and score;
- label pre-migration scans and picks `legacy_unverified` without fabricating
  references;
- enforce snapshot/score immutability and verified-pick consistency in SQLite.

Recovery:

1. Stop writers and preserve the original SQLite file before upgrading.
2. If the revision fails, keep the error output and restore the untouched file.
3. Do not delete migration rows, triggers, snapshot records, or score records to
   force startup.
4. Verify the restored database using `PRAGMA integrity_check`.

This revision has no destructive downgrade because removing immutable provenance
would invalidate later analysis and approval chains.

### `0003_jobs`

Purpose:

- add durable jobs with idempotency keys and canonical payload hashes;
- enforce legal queued, leased, running, and terminal state fields;
- persist leases, heartbeats, attempts, cancellation, progress, results, and
  redacted failure summaries;
- allow an expired lease to be requeued or terminated deterministically.

Recovery:

1. Stop writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table/index creation fails.
3. Never hand-edit a running job to look successful. Use lease recovery, retry,
   cancellation, or an explicit terminal failure transition.
4. Verify the restored file with `PRAGMA integrity_check`.

This additive migration has no destructive downgrade.

### `0004_artifacts_audit`

Purpose:

- add immutable content-addressed Artifact metadata and immutable Job links;
- add append-only AuditEvent records with sequence, correlation, payload hash,
  previous-event hash, and event hash;
- reject database updates/deletes to artifacts, links, and audit events.

Recovery:

1. Stop writers and preserve the SQLite file and artifact root as one recovery
   set before upgrade.
2. If migration fails, restore both members of that set.
3. Do not remove immutability triggers or edit hashes to force validation.
4. Verify SQLite with `PRAGMA integrity_check` and verify every stored artifact
   hash before resuming work.

This additive migration has no destructive downgrade.

### `0005_scan_selection_date`

Purpose:

- persist the local leaderboard date on every new ScanRun;
- let historical leaderboard responses use their own scan time and counts;
- leave unverifiable legacy scan dates null instead of guessing.

Recovery:

1. Stop writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if adding the nullable column or index fails.
3. Never backfill legacy dates from global "latest scan" data.

This additive migration has no destructive downgrade.

### `0006_provider_invocations`

Purpose:

- append one immutable accounting record for every Provider Job attempt and
  inspect/analyze stage that actually starts;
- retain successful, failed, timed-out, and cancelled attempts across Job
  retries;
- record only sanitized Provider, adapter, model, Prompt, Policy, schema,
  Token, estimated-cost, duration, and input/output hash metadata;
- reject updates and deletes so a retry cannot erase prior usage.

Recovery:

1. Stop Provider workers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table, index, or trigger creation fails.
3. Never fabricate usage rows or remove the immutability triggers to reconcile
   a failed Job.
4. Verify the restored file with `PRAGMA integrity_check` before restarting
   workers.

This additive migration has no destructive downgrade. Provider output content
does not belong in this accounting table; immutable analysis content is added
by its owning migration.

### `0007_analysis_versions`

Purpose:

- add one immutable AnalysisVersion per succeeded Provider analysis Job;
- bind the record to the exact OpportunitySnapshot, rule ScoreVersion, frozen
  input hash, and final-attempt inspect/analyze invocation records;
- retain inspection and analysis structured output, citations, input/output
  hashes, Provider/model identity, Prompt/Policy/schema versions, and sanitized
  aggregate usage;
- reject mismatched Job, provenance, invocation, usage, update, and delete
  operations with SQLite constraints and triggers.

Recovery:

1. Stop Provider workers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table, index, or trigger creation fails.
3. Do not mark a Provider Job successful, alter hashes, or disable provenance
   triggers to force an AnalysisVersion insert.
4. Verify the restored database with `PRAGMA integrity_check`.

This additive migration has no destructive downgrade. Later analysis schema
revisions create new version records; they do not rewrite these rows.

### `0008_final_score_versions`

Purpose:

- add immutable AI-calibrated FinalScoreVersion records without changing the
  deterministic rule ScoreVersion;
- bind each calibration to the exact AnalysisVersion, OpportunitySnapshot, rule
  ScoreVersion, calibration input, algorithm/schema versions, cited frozen
  evidence, and input/output hashes;
- reject mismatched provenance, duplicate conflicting calibration, updates, and
  deletes with SQLite constraints and triggers.

Recovery:

1. Stop Provider workers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table, index, or trigger creation fails.
3. Never overwrite a rule ScoreVersion, fabricate an AnalysisVersion, or disable
   immutable provenance triggers to force a calibration insert.
4. Verify the restored database with `PRAGMA integrity_check`.

This additive migration has no destructive downgrade. A changed calibration or
algorithm creates a new version; it never rewrites either a rule score or an
existing final score.

### `0009_contribution_tasks`

Purpose:

- add one immutable ContributionTask root for one exact AnalysisVersion;
- copy and hash-bind the AnalysisVersion record/output hash, frozen Snapshot
  input hash, Snapshot ID, and Opportunity ID needed by downstream planning;
- make creation naturally idempotent per AnalysisVersion while retaining the
  first bounded, credential-safe request idempotency key;
- reject mismatched provenance, updates, and deletes through SQLite constraints
  and triggers so later task state or plan records cannot rewrite their source.

Recovery:

1. Stop API and task writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table, index, or trigger creation fails.
3. Never rewrite an AnalysisVersion, fabricate a hash, or disable immutable
   provenance triggers to force a ContributionTask insert.
4. Verify the restored database with `PRAGMA integrity_check` before restarting.

This additive migration has no destructive downgrade. Task state transitions and
plan versions are separate versioned records added by their owning migrations;
they do not mutate the task's source-analysis binding.

### `0010_contribution_task_states`

Purpose:

- add append-only, hash-chained ContributionTask state versions while leaving
  each task root immutable;
- initialize every task in `planning`, including a data-preserving backfill for
  task roots created on revision `0009`;
- define the complete legal local lifecycle and require each transition to name
  the exact prior sequence and record hash;
- reject missing predecessors, forged task hashes, illegal edges, updates, and
  deletes through SQLite constraints and triggers.

Recovery:

1. Stop task writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if schema creation or initial-state backfill fails.
3. Never delete a state record, invent a predecessor hash, or weaken the legal
   transition trigger to bypass compare-and-swap.
4. Verify the restored database with `PRAGMA integrity_check` before restarting.

This additive migration has no destructive downgrade. Corrective lifecycle work
appends a new legal state version; it never rewrites task identity or history.

### `0011_plan_versions`

Purpose:

- add immutable, structured PlanVersion records for goal, acceptance criteria,
  inspection/change paths, implementation steps, tests, argv-based commands,
  risks, and maintainer questions;
- bind each initial plan to the exact immutable task root and current
  hash-verified `planning` state version;
- retain separate content and record hashes plus deterministic version numbers
  and bounded credential-safe idempotency;
- reject mismatched task/state provenance, duplicate content/version numbers,
  updates, and deletes through SQLite constraints and triggers.

Recovery:

1. Stop task and plan writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table, index, or trigger creation fails.
3. Never rewrite task/state hashes, store executable shell strings in place of
   structured argv, or disable immutable provenance triggers to force a plan.
4. Verify the restored database with `PRAGMA integrity_check` before restarting.

This additive migration has no destructive downgrade. A revised plan is a new
child version added by the next owning migration; it never rewrites an existing
PlanVersion.

### `0012_plan_revision_links`

Purpose:

- add nullable `parent_version_id` and parent record-hash provenance without
  changing existing initial PlanVersion rows;
- require every revision after version 1 to link the immediately preceding
  version of the same task and its exact immutable record hash;
- index parent links for deterministic history traversal;
- reject skipped versions, cross-task parents, stale parent hashes, and missing
  initial/revision parent semantics through a SQLite trigger.

Recovery:

1. Stop plan writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if either additive column, index, or trigger fails.
3. Never rewrite an existing plan, skip a version number, or fabricate a parent
   record hash to create a revision.
4. Verify the restored database with `PRAGMA integrity_check` before restarting.

This migration does not backfill a fictional parent: all pre-revision plans
remain version 1 roots with null parent fields.

### `0013_plan_locks`

Purpose:

- add immutable PlanLock records that freeze one current PlanVersion before any
  approval transition;
- bind the lock to the exact ContributionTask, current `planning` state,
  AnalysisVersion, Issue snapshot, repository base commit SHA, provider/model/
  prompt/policy/schema versions, and plan content/record hashes;
- retain separate provider-contract and complete lock hashes so service reads
  can revalidate every frozen input after restart;
- reject stale plans, non-current states, mismatched provenance, duplicate locks,
  updates, and deletes through SQLite constraints and triggers.

Recovery:

1. Stop plan and lock writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table, index, or trigger creation fails.
3. Never substitute a newer snapshot, base commit, provider/policy version, or
   plan hash into an existing lock; create a new plan revision and lock instead.
4. Verify the restored database with `PRAGMA integrity_check` before restarting.

This additive migration has no destructive downgrade and does not approve a
plan. Approval remains a separate compare-and-swap state transition owned by a
later migration and service boundary.

### `0014_plan_approvals`

Purpose:

- add immutable PlanApproval records as the canonical approval of one exact
  PlanLock and PlanVersion;
- bind the approving actor, lock/plan hashes, prior `planning` state hash, and
  newly appended `plan_approved` state ID/hash into one approval hash;
- create the approval and state transition in one transaction so either both
  persist or neither does;
- reject stale locks, mismatched actors/provenance, duplicate approvals,
  updates, and deletes through service validation, constraints, and triggers.

Recovery:

1. Stop approval writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table, index, or trigger creation fails.
3. Never mutate an approved PlanVersion, replace its lock, invent an actor, or
   repair a failed approval by editing its state/hash fields.
4. Verify the restored database with `PRAGMA integrity_check` before restarting.

This additive migration has no destructive downgrade. A later approval
revocation is represented by new append-only state/decision records; it never
deletes or rewrites the historical approval.

### `0015_plan_conversations`

Purpose:

- add one append-only, per-task hash chain for plan messages and machine-readable
  decisions;
- optionally bind each entry to a verified PlanVersion from the same task while
  retaining actor identity, bounded credential-safe content, and idempotency;
- make sequence, prior-entry hash, content hash, and record hash independently
  verifiable after restart;
- reject cross-task plans, broken chains, concurrent sequence reuse, updates,
  and deletes through service validation, constraints, and triggers.

Recovery:

1. Stop conversation writers and preserve the SQLite file before upgrade.
2. Restore the untouched file if table, index, or trigger creation fails.
3. Never renumber entries, rewrite messages/decisions, fabricate a predecessor
   hash, or attach an entry to a PlanVersion from another task.
4. Verify the restored database with `PRAGMA integrity_check` before restarting.

This additive migration has no destructive downgrade. Corrections and changed
decisions are new entries; prior discussion and decisions remain intact.

### `0016_execution_attempts`

Purpose:

- add immutable ExecutionAttempt roots bound to the exact approved plan,
  approval, task/analysis/snapshot provenance, repository/base/archive, Runner
  image, SandboxPolicy, actor, and distinct `start_execution` authorization;
- atomically append the task's `plan_approved → executing` transition, initial
  `explore/pending` state, and `execution.started` audit event;
- add append-only Explore/Implement/Verify stage versions with compare-and-swap
  sequence, predecessor hash, signed JobSpec hash, ordered inputs, result hash,
  and opaque workspace/inventory evidence;
- reject broken chains, illegal stage order, stale transitions, conflicting
  idempotent replays, updates, and deletes through service validation,
  constraints, and triggers.

Recovery:

1. Stop orchestrator and Sandbox Worker writers and preserve the SQLite file
   before upgrade.
2. Restore the untouched file if either table, index, or trigger creation fails.
3. Never edit a running stage to succeeded, fabricate a signed JobSpec/result
   hash, replace a workspace reference, or weaken compare-and-swap checks.
4. After restore, verify SQLite integrity plus every ExecutionAttempt, audit
   event, and complete stage hash chain before resuming.

This additive migration has no destructive downgrade. Service restart resumes
from the highest verified stage version. A still-running stage is not guessed
successful; worker-loss reconciliation and bounded retry are added by P5-T10.
See [execution-state.md](execution-state.md).

### `0017_execution_stage_runs`

Purpose:

- add immutable ExecutionStageRun records binding each pending stage to one
  leased `sandbox_stage` Job, authenticated JobSpec hash, ordered inputs,
  timeout, run number, and immutable one-to-three-run budget;
- force every linked Job to `max_attempts=1`, preventing blind replay of a
  partially modified Implement workspace;
- protect linked Job kind, idempotency key, payload/hash, timeout, and attempt
  fields from later mutation while allowing normal lease/heartbeat/state
  transitions;
- extend the stage provenance trigger for explicit pre-start
  failure/cancellation/timeout and same-stage retry after only failure or
  timeout.

Recovery:

1. Stop orchestrator and Sandbox Worker writers and preserve the SQLite file
   before upgrade.
2. Restore the untouched file if table/index creation or trigger replacement
   fails.
3. Never requeue a linked Job by editing its attempt count, replace its payload,
   turn a lost Worker into success, or increase a stage's frozen retry budget.
4. After restore, run lease reconciliation; each expired Worker becomes an
   explicit terminal stage before any new retry is scheduled.

This additive migration has no destructive downgrade. Job/stage start and
terminal transitions are atomic. Expired leases are idempotently reconciled
after restart, and each retry receives a new pending stage and Job. See
[execution-state.md](execution-state.md).

### `0018_dependency_verify_inputs`

Purpose:

- replace only the execution-stage provenance trigger so Verify may consume the
  exact Implement result followed by at most one signed dependency-preparation
  result;
- retain the existing one-input Verify path for executions that need no package
  preparation;
- require the Implement result to remain first and preserve the inherited
  workspace identity and inventory;
- continue rejecting all updates, deletes, broken predecessors, illegal
  transitions, and three-or-more-input Verify records.

Recovery:

1. Stop orchestrator and Sandbox Worker writers and preserve the SQLite file
   before upgrade.
2. Restore the untouched file if trigger replacement fails.
3. Never reorder the Implement result, add an unverified second input, or edit a
   historical Verify state to attach dependencies.
4. After restore, verify SQLite integrity and each execution stage chain before
   resuming writers.

This forward migration changes no stored rows or public credentials. The
dependency plan, volume, result, and Verify binding are described in
[sandbox-dependencies.md](sandbox-dependencies.md).

### `0019_execution_artifact_manifests`

Purpose:

- add one immutable content-addressed Artifact manifest per successful
  ExecutionStageRun;
- bind the exact Job, StageRun, ExecutionAttempt, stage, authenticated JobSpec,
  result hash, ordered artifact roles, Artifact IDs, sizes, and media types;
- require the stage-specific complete role set and exact Job links before a
  `sandbox_stage` Job may transition to `succeeded`;
- commit Artifact metadata/links, the manifest, Job success, and Stage success
  in one SQLite transaction after atomic filesystem finalization.

Recovery:

1. Stop orchestrator and Sandbox Worker writers and back up both the SQLite
   file and Artifact root before upgrade.
2. Restore both backups together if table, index, or trigger creation fails.
3. Never fabricate a manifest, alter its result/role hashes, or mark a Job
   successful by disabling the artifact trigger.
4. A digest-named file left by a rolled-back database transaction is a safe
   unreferenced candidate, not proof of success. Reverify and adopt it only
   through the normal finalizer; defer deletion to orphan reconciliation.

This additive migration does not rewrite historical Jobs or Artifact rows.
The full commit and crash-recovery protocol is documented in
[execution-artifacts.md](execution-artifacts.md).

### `0020_execution_workspace_disposals`

Purpose:

- add one immutable post-Verify disposal root bound to the exact successful
  Artifact manifest, Verify StageRun, ExecutionAttempt, workspace identity and
  inventory, Runner image, and SandboxPolicy;
- create the initial pending disposal in the same transaction as Verify
  Artifact/Job/Stage success;
- add an append-only `pending → running → succeeded|failed` state chain with
  explicit failed-to-pending retry and worker ownership;
- preserve disposal evidence across process restart without exposing Docker to
  the business-state service.

Recovery:

1. Stop cleanup coordination writers and preserve the SQLite database before
   upgrade. The migration itself never removes a volume.
2. Restore the database if table/index/trigger creation fails; leave labelled
   volumes untouched until their manifests are verified.
3. Never edit disposal status, workspace references, or hashes, and never mark
   success without the Worker confirming that the exact volume is absent.
4. Reconcile a known lost `running` cleanup as explicit failure, then retry
   within the fixed three-attempt budget.

The trust split, idempotent Docker removal, and restart procedure are documented
in [workspace-disposal.md](workspace-disposal.md).

### `0021_review_runs`

Purpose:

- add immutable `ReviewRun` rows bound to the exact plan, base SHA, archive,
  policy, Implement diff, and Verify result hashes;
- allow a later repair `ExecutionAttempt` to start from `reviewing` without
  mutating the previous attempt.

Recovery:

1. Stop review and execution writers and preserve the SQLite file.
2. Restore the backup if table or trigger replacement fails.
3. Never edit a ReviewRun or invent a repair attempt without a prior review.

### `0022_publish_intents`

Purpose:

- add immutable `PublishIntent`, `DraftPullRequest`, and
  `PublishConfirmation` records for the Fake publisher path;
- keep confirmation one-time and nonce-bound. This revision never writes to
  GitHub.

Recovery:

1. Stop publication writers and preserve the SQLite file.
2. Restore the backup on failure.
3. Never mark a confirmation without the exact nonce and `ready → draft_pr`
   state transition.

### `0023_pull_request_events`

Purpose:

- store append-only local PR observations with a stable `remote_event_id`.

Recovery:

1. Stop event writers and preserve the SQLite file.
2. Restore the backup on failure.
3. Replay the same `remote_event_id` instead of deleting or rewriting events.

### `0024_task_side_states`

Purpose:

- replace only the legal-transition trigger so `changes_requested` may
  return to `planning`;
- add an immutable `task_lifecycle_marks` table for `failed`, `rejected`,
  and `abandoned` without rebuilding the heavily referenced state table.

Recovery:

1. Stop task-state writers and preserve the SQLite file.
2. Restore the backup if trigger replacement or mark-table creation fails.
3. Never rewrite historical `to_state` values or delete a mark.

### `0025_product_experience`

Purpose:

- add immutable, versioned local preference profiles used to personalize the
  full eligible opportunity pool without changing any rule `ScoreVersion`;
- add append-only shortlist, dismissal, and reminder decisions per opportunity;
- add immutable in-app notifications with separate read receipts for new
  matches, shortlisted-opportunity changes, and due reminders;
- keep all new state local and introduce no GitHub or other third-party write.

Recovery:

1. Stop API and Worker writers and preserve the SQLite file before upgrade.
2. Restore that backup if any table, index, or immutability-trigger creation
   fails; do not drop a partially created subset while writers are running.
3. Older binaries may ignore the four additive tables after a complete upgrade,
   but must not edit or fabricate preference/disposition history.
4. After restore, run SQLite integrity checks and re-run the migration normally;
   notification dedupe keys and preference/disposition hashes make retry state
   explicit.

### `0026_review_artifact_bindings`

Purpose:

- replace only the `ReviewRun` insert provenance trigger;
- require every new Review to reference the exact `unified-diff` Artifact from
  its successful Implement manifest;
- require every new Review to reference the exact `normalized-test-results`
  Artifact from its successful Verify manifest;
- retain existing immutable Review rows. Earlier rows remain transitively bound
  through their content-addressed stage-result Artifacts and are not rewritten
  to claim the new direct binding.

Recovery:

1. Stop review and publication writers and preserve the SQLite file before
   upgrade.
2. Restore that backup if trigger replacement fails; do not disable the Review
   provenance trigger to accept a record.
3. Never rewrite an existing Review, PublishIntent, or DraftPullRequest to use
   a different hash.
4. After restore, run `PRAGMA integrity_check`, verify the execution Artifact
   manifests, and re-run the forward migration normally.

### `0027_nvidia_agent_workflows`

Purpose:

- add immutable `CodingSession`, hash-chained `CodingTurn`, `AgentInvocation`,
  and `ChangeSetProposal` records;
- bind every coding session to one exact PlanVersion, base commit, Explore result
  and content-addressed coding-context Artifact;
- bind a model-generated ChangeSet to the exact conversation hash and require a
  separate user confirmation before Implement.

Recovery:

1. Stop API, Provider Worker and Sandbox Worker; preserve SQLite and the complete
   Artifact root together.
2. Restore both backups if table/index/trigger creation fails.
3. Never invent an AgentInvocation or rewrite a turn/proposal hash to recover a
   partially completed model Job; retry the durable Job from its verified inputs.

### `0028_nvidia_review_runs`

Purpose:

- extend the database-level Review kind constraint with `nvidia_nim`;
- rebuild only `review_runs` while retaining every existing Fake Review and the
  `PublishIntent` foreign keys that reference it;
- recreate the direct diff/test Artifact provenance trigger and immutable-row
  triggers before the migration commits.

This migration is the only current SQLite table rebuild that requests foreign
keys be temporarily disabled. The migration runner applies revisions in separate
transactions, enables legacy rename behavior, runs `PRAGMA foreign_key_check`
before recording the revision, then re-enables foreign keys. A failed check rolls
back the rebuild and must never be treated as a successful migration.

Recovery:

1. Stop Review, Provider and Publisher writers; back up SQLite and Artifact root.
2. Restore the backup if the rebuild or foreign-key check fails.
3. Verify `PRAGMA integrity_check` and `PRAGMA foreign_key_check` after restore,
   then rerun the forward migration.
4. Do not bypass `ck_review_run_kind` or the Review provenance trigger to insert
   a provider result.

## Adding a migration

1. Add an immutable module under `app/migrations/versions`.
2. Give it a lexically ordered unique revision.
3. Freeze its schema/operation manifest in `signature`.
4. Implement a forward-only, data-preserving `upgrade`.
5. Add exact recovery instructions.
6. Register it in `app/migrations/versions/__init__.py`.
7. Add tests for:
   - upgrade from the previous real fixture;
   - retained row values and relationships;
   - repeat execution;
   - partial failure and recovery;
   - checksum and unknown-revision rejection.
8. Update this document before checking the roadmap task.

Never edit a registered migration after it has been applied. Corrective work is
a new revision.

### `0029_workbench`

Adds the immutable, hash-chained `workbench_events` journal and unique task
sequence/idempotency constraints. Planning evidence, execution consent, human
publication confirmation and external-write reconciliation events are appended,
never overwritten. Historical plans are preserved without fabricating code-read
provenance. Explicit `user_replan` allows a stopped execution/review to return to
planning; a newly approved plan can start a new execution sequence while repair
budgets remain approval-specific.

The migration also rebuilds `plan_versions`, preserving every row and trigger,
and scopes content uniqueness to task/parent/content so identical text can form a
new child when code evidence or planning state changes. Version numbers, hashes,
parent links and idempotency remain unique and immutable.

The migration rebuilds `draft_pull_requests` within the migration transaction to
allow distinct `fake` and `github` providers, copying all rows and restoring
indexes/immutability triggers. The migration runner disables foreign-key rename
rewrites only for this operation and checks foreign keys before completion.
The upgrade test preserves existing Draft PR hashes and confirmation references.

Recovery: stop API and every worker, including Publisher. Before upgrading, use
the SQLite backup API to take a consistent pre-upgrade backup and preserve the
matching artifact tree (do not copy only a live WAL database file). If rollback
is necessary, preserve the failed-upgrade files, restore that database/artifact
pair and the previous application, then check `PRAGMA integrity_check` and
`PRAGMA foreign_key_check`. Do not delete migration stamps or provenance rows.
After a real publish confirmation, reconcile any remote write against the saved
intent before restoring or retrying: restoring SQLite cannot undo a Fork, Push
or Draft PR. Keep the post-confirmation database and audit evidence available.
