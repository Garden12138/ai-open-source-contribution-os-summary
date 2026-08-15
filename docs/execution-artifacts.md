# Atomic execution artifact finalization

P5-T21 makes content-addressed evidence a prerequisite for a successful
`sandbox_stage` Job. A result hash alone is no longer sufficient at the
orchestrator boundary.

## Canonical artifact bundles

`ExecutionArtifactBundle.from_result` accepts only a verified typed Explore,
Implement, or Verify result. It serializes the result's hash payload with
canonical JSON, so the `stage-result` Artifact ID is exactly the immutable stage
result hash.

The exact ordered roles are:

| Stage | Required roles |
| --- | --- |
| Explore | `stage-result` |
| Implement | `stage-result`, `file-inventory`, `unified-diff` |
| Verify | `stage-result`, `normalized-test-results` |

The Implement diff Artifact ID is exactly `diff_hash`. The Verify normalized
test Artifact ID is exactly `test_results_hash`. The inventory artifact
contains both complete sorted inventories, their hashes/counts/bytes, and the
exact changed paths. Every artifact is credential-scanned before filesystem or
database persistence.

## Commit protocol

Successful completion uses this order:

1. Validate the typed bundle against the immutable StageRun stage and result
   hash.
2. Write each missing content file to a temporary file in its final directory,
   flush and `fsync` it, atomically replace the digest path, then `fsync` the
   directory.
3. Re-read every final file and verify its byte count and SHA-256.
4. In the still-running Job's SQLite transaction, insert immutable Artifact
   metadata, exact Job links, one manifest, and its ordered entries.
5. Flush the manifest and links.
6. Mark the Job succeeded with the exact result and manifest hashes, append the
   succeeded StageVersion, and commit all database changes together.

Migration `0019_execution_artifact_manifests` adds immutable manifests and
entries plus a database trigger that rejects a `sandbox_stage` success unless
the complete stage-specific role set and Job links already exist. Thus no
observable successful Job can lack its artifact manifest.

## Failure and recovery

Filesystem and SQLite cannot form one physical transaction. The protocol
therefore fails safely at each boundary:

- failure before atomic file replacement leaves no final file or metadata;
- a corrupt pre-existing digest path fails hash verification;
- failure before the SQLite commit rolls back Artifact metadata, links,
  manifest, Job success, and Stage success together;
- a crash after file replacement but before database commit may leave only an
  unreferenced digest-named file;
- retry re-verifies such a file byte-for-byte and adopts it into a new database
  transaction instead of rewriting or trusting it;
- failure after the database commit is a replay: the exact bundle and every
  stored file are reverified before returning the existing terminal Stage.

Hash-addressed orphan deletion is intentionally deferred to the explicit
reconciliation work in P9-T05. Operators must back up and restore the SQLite
database and Artifact root together.

## Read boundary

Execution evidence is exposed only through execution-scoped read APIs:

- `GET /api/v1/executions/{id}` returns the verified attempt, complete
  append-only stage history, bounded StageRun/Job status, and finalized
  manifests;
- `GET /api/v1/executions/{id}/artifacts` returns the same ordered manifest
  metadata;
- `GET /api/v1/executions/{id}/artifacts/{artifact_id}` returns content only
  when the Artifact is linked through a manifest owned by that exact execution.

Each read reconstructs and checks the manifest hash, expected stage role order,
Job link, successful Job result, matching succeeded StageVersion, storage key,
file size, and SHA-256. Content is hashed again after the in-memory read and
credential-scanned before it is returned. Responses never expose the Artifact
root, storage key, idempotency key, lease owner, or Worker-private workspace
reference. Content responses use `nosniff`, a restrictive CSP, attachment
disposition, and the immutable SHA-256 ETag.

For final Verify success, the same database transaction also creates a durable
workspace-disposal intent. Actual Docker removal occurs only after the Artifact
commit through the narrow Worker cleanup boundary described in
[workspace-disposal.md](workspace-disposal.md).
