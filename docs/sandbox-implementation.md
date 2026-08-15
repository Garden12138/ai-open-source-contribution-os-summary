# Plan-bound implementation workspace

P5-T07 applies a structured `implementation-change-set-v1` inside one opaque,
disposable Docker named volume. The API, Provider, and model never receive the
volume name or Docker access.

## ChangeSet contract

Every ChangeSet binds the approved PlanVersion ID, content hash, and record
hash. Each unique operation is either:

- `write`: repository-relative path, expected prior SHA-256 (or `null` only for
  a new file), UTF-8 content, content hash, and executable flag;
- `delete`: repository-relative path and mandatory expected prior SHA-256.

Traversal, `.git`, duplicate paths, credential-like content, oversized files or
sets, and malformed operations fail before Docker starts. The signed Implement
JobSpec must bind exactly two ordered predecessor artifacts: the verified
Explore result hash and ChangeSet hash. Change paths must be a subset of the
approved JobSpec paths, and the Implement stage rejects repository commands.

This structured boundary is provider-neutral. A future Implementer Provider may
propose a ChangeSet, but model output does not authorize it and cannot select a
different plan, base, policy, image, artifact, path, or command.

## Disposable workspace

The Worker creates one randomly named, labelled Docker volume and initializes
its empty root before repository materialization. This trusted empty-volume
initialization runs before any repository content exists. The repository
archive is then reverified and expanded by the same fixed, networkless,
fail-closed materializer used by Explore, this time directly into the volume as
UID/GID 65532.

The apply container has exactly:

- the named volume as its only writable mount at `/workspace`;
- the canonical ChangeSet wrapper as one read-only mount;
- a small tmpfs;
- the digest-pinned Runner image and fixed Python applicator argv.

It has no network, model or credentials, Docker socket, host home, SSH agent, or
shell-built command. Root is read-only, all capabilities are dropped,
no-new-privileges applies, and CPU/memory/PID/time limits come from the accepted
SandboxPolicy.

The applicator revalidates the complete ChangeSet hash and approved path set
inside the container. It rejects symlink path components, stale/missing prior
hashes, unexpected existing targets, malformed UTF-8 writes, and no-op or
out-of-band changes. It inventories every regular file and symlink before and
after applying operations, then requires the observed changed-path set to equal
the ChangeSet paths exactly.

`sandbox-implement-result-v2` binds the Explore result, ChangeSet, exact
repository/base/archive, approved plan, image, policy, complete before/after
inventory entries and their hashes/counts/bytes, changed paths, deterministic
unified diff, and raw SHA-256 diff hash. The Worker independently rebuilds both
inventory hashes and the observed changed-path set, verifies the diff hash, and
rejects malformed, unsorted, duplicate, oversized, or credential-bearing
evidence. The opaque workspace handle is retained only inside the Worker for
fresh-container Verify; it is not part of the public result.

Any materialization, apply, validation, timeout, or provenance failure triggers
immediate volume deletion. Successful workspaces are destroyed after artifact
finalization under P5-T22.

## Acceptance

Default tests use a deterministic fake runtime and cover plan/artifact/path
binding, stale hashes, traversal, secret canaries, full-tree change equality,
inventory/diff tampering, cleanup-on-failure, and exact mount/runtime arguments.
Real OrbStack acceptance uses the production volume/materialize/apply path,
verifies two approved changes, complete sorted inventories, deterministic diff
and its hash, and deletes the volume in `finally`:

```text
CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1
CONTRIBOS_SANDBOX_RUNNER_IMAGE=sha256:<local-arm64-image-config-digest>
python -m pytest -q \
  tests/test_sandbox_implementation.py::\
test_real_plan_bound_implementation_in_disposable_volume
```
