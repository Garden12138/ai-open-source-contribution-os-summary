# Fresh-container verification

P5-T08 runs Verify in a new container that never hosted Implement. The signed
Verify JobSpec binds the exact Implement result hash, may bind one optional
P5-T17 dependency-preparation result immediately after it, and includes one to
twenty shell-free `SandboxCommand` argv records. It cannot contain writable
paths.

Before execution, the Worker revalidates the Implement result hash chain and
requires exact agreement on execution attempt, repository/base/archive,
PlanVersion, image, SandboxPolicy, and the opaque workspace's frozen inventory
hash. A stale result, substituted volume, changed policy/image, or reordered
artifact input fails before Docker starts.

## Runtime

The Verify container receives only:

- the disposable workspace volume mounted read-only at `/workspace`;
- when present, the separately prepared dependency volume mounted read-only at
  `/dependencies`;
- one canonical command document mounted read-only;
- a bounded no-exec tmpfs for command stdout/stderr;
- a fixed trusted Python supervisor.

It uses `network=none`, a read-only root, UID/GID 65532, all capabilities
dropped, no-new-privileges, and SandboxPolicy CPU/memory/PID/time limits. The
container environment is rebuilt from a tiny fixed list and has no GitHub,
Provider/model, task Token, SSH agent, Docker socket, host home, or application
settings. The Docker supervisor process receives only its Docker context
allowlist; raw stderr never crosses into business results.

Every command receives the complete fixed
[`sandbox-git-safety-v1`](sandbox-git-safety.md) environment. System/global Git
configuration, hooks, credential helpers, LFS filters, submodule recursion,
interactive prompts, SSH, and Git transports remain disabled even when
repository tests invoke the `git` executable.

The supervisor recomputes the complete workspace inventory before the first
command and requires the exact Implement inventory hash. It executes argv
directly with `shell=False`, a repository-relative validated working directory,
stdin closed, a minimal environment, and the stage deadline. Output is written
only to bounded tmpfs files. Nonzero exits are normal failed test evidence.
Timeouts and output limits stop later commands with explicit machine-readable
statuses.

After commands, the supervisor rehashes the whole read-only workspace and
requires it to remain byte/mode/symlink identical. The Worker then matches every
command ID/hash and evidence count to the signed JobSpec.
`sandbox-verify-result-v2` binds the Implement result, plan/base, image/policy,
unchanged inventory hashes, full ordered signed commands, per-command
status/exit/UTC timestamps/duration/resource use/output hashes, bounded
sanitized logs, and the overall verdict. Raw output is hash/length checked
before complete-text credential redaction and truncation; over-budget output is
omitted rather than persisted. See
[execution-evidence.md](execution-evidence.md). When dependency preparation is
used, the supervisor also
rehashes its complete read-only output before and after commands, while the
result binds both the dependency result and inventory hashes. See
[sandbox-dependencies.md](sandbox-dependencies.md).

## Acceptance

Default tests cover passing and failing commands, stale artifacts/workspaces,
inventory mismatch, malformed evidence, exact read-only mounts, runtime flags,
and credential canaries. Real OrbStack acceptance creates a plan-bound
Implement volume, starts the production fresh Verify container, and runs a
probe that confirms:

- the approved new file content is visible;
- writing the workspace fails;
- a direct public-IP connection fails;
- GitHub, model, task-token, and SSH variables are absent;
- before/after inventories remain identical.

The test deletes the disposable volume in `finally` and verifies no labelled
volume remains.

The dependency acceptance additionally proves that a real hash-locked venv can
be consumed in this fresh, networkless container without changing either the
Implement workspace or dependency inventory.
