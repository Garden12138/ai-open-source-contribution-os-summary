# Durable execution state

Phase 5 persists execution orchestration independently from Sandbox Worker
processes and Docker containers. A service restart therefore cannot erase or
silently advance an Explore, Implement, or Verify stage.

## Immutable records

`ExecutionAttempt` freezes one authorized execution root:

- exact task, analysis, snapshot, approved PlanVersion, and PlanApproval IDs and
  hashes;
- repository full name, approved base commit, and repository archive hash;
- runner image digest and SandboxPolicy version/hash;
- approving and executing task-state hashes;
- the distinct `start_execution` actor/action and observed approval fingerprint.

Creation atomically appends the task transition from `plan_approved` to
`executing`, the first `explore/pending` stage record, and an
`execution.started` audit event. A partial commit is not a valid attempt.

`ExecutionStageVersion` is an append-only per-attempt hash chain. Every version
contains:

- a compare-and-swap sequence and predecessor hash;
- stage, status, and machine-readable reason code;
- the authenticated JobSpec hash and ordered predecessor hashes when started;
- a result hash when evidence exists;
- an opaque disposable-workspace identity and inventory hash where Implement
  and Verify need them.

SQLite triggers reject update, delete, missing predecessors, illegal stage
edges, and provenance that does not match the immutable attempt root.

## Legal Phase 5 transitions

```text
explore/pending
  → explore/running
  → explore/succeeded
  → implement/pending
  → implement/running
  → implement/succeeded
  → verify/pending
  → verify/running
  → verify/succeeded
```

A running stage may instead append `failed`, `cancelled`, or `timed_out`.
A pending stage may append one of those explicit terminal outcomes when its
queued/leased Worker Job is cancelled or lost before execution starts. Failed
or timed-out stages may append a same-stage `pending` retry; cancelled stages
cannot retry.

Implement becomes `running` only after its disposable workspace identity is
durably recorded. Its successful terminal record adds the full inventory hash.
Verify receives that same identity and inventory and may not replace either.
Its ordered inputs contain the exact Implement result first and may contain one
signed dependency-preparation result second. No other count or order is legal.
The opaque workspace reference is Worker-private and must not be returned by a
public API.

## Restart procedure

1. Open the database and verify the `ExecutionAttempt` hash, start audit event,
   and complete stage hash chain.
2. Read the highest stage sequence.
3. For `pending`, rebuild the deterministic JobSpec from the immutable attempt
   and ordered input hashes, sign it, then use compare-and-swap to append
   `running`.
4. For a terminal stage, either advance from a verified success or stop.
5. For `running`, do not rerun or report success automatically. Reconcile its
   immutable ExecutionStageRun with the durable Job lease. An expired final
   lease appends `timed_out`; an acknowledged cancellation appends `cancelled`.

## Stage Jobs and retry

Each pending stage is scheduled through one immutable `ExecutionStageRun`
linked to one durable `sandbox_stage` Job. The link freezes the pending stage
ID/hash, authenticated JobSpec hash, ordered inputs, timeout, run number, and
maximum run count. Linked Job inputs are immutable even while lease/state fields
change.

- A run Job always has `max_attempts=1`; the Job service never blindly replays
  an interrupted writable workspace.
- A Worker lease and heartbeat have a bounded expiry.
- Job start and stage `running`, as well as Job terminal state and stage
  terminal state, commit in one SQLite transaction.
- A success first requires the exact content-addressed artifact bundle. Artifact
  metadata/links, its immutable manifest, Job success, and Stage success commit
  together; the database rejects a success without the complete stage roles.
- Queued cancellation atomically cancels both records. Running cancellation
  sets a durable request and prevents success until the Worker acknowledges it
  or its lease expires.
- Worker loss becomes an explicit `timed_out/worker_lease_expired` stage after
  restart reconciliation.
- Retry creates a new pending stage, new Job, and new workspace identity where
  Implement is writable. The immutable budget is one to three runs per stage.

Repeated calls with the same idempotency key and exact inputs return the same
record. Reusing a key with different approval, image, policy, input, result,
workspace, or transition data fails closed.

Atomic file finalization, manifest roles, rollback orphans, and replay
verification are specified in
[execution-artifacts.md](execution-artifacts.md).
Post-Verify volume cleanup has its own immutable, bounded, restart-safe state
chain documented in [workspace-disposal.md](workspace-disposal.md).
