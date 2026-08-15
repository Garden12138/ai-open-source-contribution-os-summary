# Disposable workspace destruction

P5-T22 destroys the opaque Implement workspace only after the final Verify
artifacts are durably finalized. Cleanup is durable and retryable without
giving the API/orchestrator direct Docker access.

## Ordering

When Verify succeeds, the orchestrator transaction performs:

1. finalize and verify the complete Verify Artifact bundle;
2. insert its immutable Artifact manifest;
3. create one immutable `execution-workspace-disposal-v1` root and initial
   `pending/artifacts_finalized` state bound to that exact manifest, Verify
   StageRun, ExecutionAttempt, workspace identity/inventory, Runner image, and
   SandboxPolicy;
4. commit the Artifact rows, disposal intent, Job success, and Stage success
   together.

No disposal intent can point at an Explore/Implement run, another attempt,
another manifest, or a different workspace. The opaque volume reference remains
Worker-private and must not be exposed by public APIs.

## Trust boundary

The orchestrator owns the append-only disposal state and calls only the narrow
`WorkspaceDestroyClient` interface. A separately configured Sandbox Worker
implementation owns Docker and receives only the validated `DisposableWorkspace`
identity. It receives no GitHub/model credential, business database access,
repository command, Docker socket forwarding, or arbitrary target.

`DockerImplementRuntime.destroy` accepts only the exact
`contribos-workspace-<32 hex>` namespace. It first performs an exact filtered
lookup, removes that one volume, then treats a confirmed absent volume as
success. This makes retry safe when deletion succeeded but acknowledgement was
lost.

## Durable state and recovery

Disposal versions are append-only:

```text
pending → running → succeeded
                  ↘ failed → pending → running …
```

Only the claiming cleanup Worker may finish its `running` state. An orchestrator
restart never guesses success: it records a known lost Worker as
`failed/cleanup_worker_lost`, then schedules a new pending attempt. Runtime
errors are reduced to the fixed `workspace_destroy_failed` reason and never
persist raw Docker output.

At most three `running` attempts are allowed. After exhaustion the disposal
remains explicitly failed for operator repair; retry budgets are not weakened
and the volume is not silently declared absent. Exact success replay performs no
second external call.

Implement materialization/apply failures continue to destroy their just-created
workspace immediately. A successful Implement workspace remains available
read-only through Verify and becomes eligible for this durable cleanup only
after Verify Artifact finalization.

