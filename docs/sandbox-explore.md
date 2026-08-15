# Read-only repository Explore

P5-T06 implements Explore as a Sandbox Worker-owned two-container operation. It
never checks out or executes repository content on the host.

## Exact input

An Explore JobSpec now binds `repository_archive_hash` in addition to the
repository full name and approved base commit SHA. The worker accepts only a
regular, non-symlink archive inside its configured artifact root, computes its
SHA-256 before execution, and verifies the same size/hash after materialization
and again after inspection.

The archive producer is the read-only GitHub acquisition boundary, not the
untrusted repository and not the Sandbox Worker. It must request the exact
approved commit and persist the raw response as a content-addressed artifact
before the orchestrator signs the JobSpec. GitHub credentials are never sent to
the Worker. The materializer also requires the archive's single top-level name
to be `<repository-name>-<exact-base-SHA>`. This metadata check complements,
but does not replace, the trusted acquisition record and signed archive hash.

`POST /api/v1/plan-versions/{id}/archives` enqueues a durable
`repository_archive` Job. The orchestrator calls
`GET /repos/{owner}/{repo}/tarball/{sha}` with the read-only token, then
follows a single HTTPS redirect to an allowlisted archive host
(`codeload.github.com` by default) **without** `Authorization`. The raw gzip
stream is size-capped, written atomically, and stored as
`{artifact_root}/repository-archives/{aa}/{sha256}`. The host never extracts
the archive or runs repository code. The execution worker later looks up that
hash locally; a missing file still fail-closes as
`repository_archive_unavailable`.

`SANDBOX_STAGE_RUNTIME=fake` lets the CLI worker complete Explore without
Docker by writing a synthetic snapshot and never opening the archive. This
is an offline state-machine path, not a substitute for the production
materializer. After Explore succeeds, a ChangeSet must be accepted before
Implement is scheduled.

## Materialization container

The first container receives only:

- the raw archive as one read-only bind mount;
- one empty disposable output directory as its only writable bind mount;
- the digest-pinned Sandbox Runner image;
- a fixed trusted Python materializer passed as argv.

It has no network, credentials, model access, shell interpolation, Docker
socket, host home, or SSH agent. Root is read-only; capabilities are dropped;
no-new-privileges and SandboxPolicy CPU/memory/PID/time limits apply. It runs as
the non-root Worker UID/GID so the disposable tree remains removable.

The materializer manually validates every tar entry before writing anything. It
limits files and bytes, requires one exact commit-root prefix, rejects absolute
or parent paths, duplicates, special files, hard links, escaping symlinks,
`.git`, and credential-bearing filenames, and writes files with only controlled
regular/executable modes. Repository hooks, filters, submodules, LFS, build
files, and tests are never invoked.

## Explore container

The Worker freezes and hashes the materialized tree, then starts a second
container with that one tree mounted read-only at `/workspace`. The container
again has `network=none`, read-only root, non-root UID/GID 65532, dropped
capabilities, no-new-privileges, resource limits, and only a small tmpfs. A
fixed built-in inspector walks without following symlink directories, hashes
regular file content, and emits only bounded metadata:

- deterministic inventory hash;
- file count and byte total;
- up to 200 sorted paths;
- up to 200 recognized Python/TypeScript/build manifest paths.

No repository file content enters the result. The Worker verifies the frozen
tree and raw archive again, cross-checks the container's count/byte total, and
returns `sandbox-explore-result-v1`, whose result hash binds the signed JobSpec,
exact base/archive, image, policy, snapshot manifest, and inventory.

## Acceptance

The default suite uses a deterministic fake runtime and tests signature,
policy, stage, archive, tamper, path, environment, mount, and inventory
failures. The opt-in OrbStack acceptance creates a real trusted Git commit,
generates `git archive` from that exact SHA, and passes it through both
production container commands:

```text
CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1
CONTRIBOS_SANDBOX_RUNNER_IMAGE=sha256:<local-arm64-image-config-digest>
python -m pytest -q \
  tests/test_sandbox_explore.py::test_real_read_only_explore_on_exact_archive
```
