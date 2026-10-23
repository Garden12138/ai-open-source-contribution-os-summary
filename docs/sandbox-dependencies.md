# Isolated dependency preparation

P5-T17 treats package installation as an optional, separate trust boundary. It
never relaxes the default `network=none` rule for Explore, Implement, or Verify.
The step is used only when an approved execution needs dependencies that are not
already present in the digest-pinned Runner image.

## Signed input and immutable output

`dependency-preparation-plan-v1` binds the execution attempt and exact
Implement result, frozen workspace inventory, ecosystem, lockfile paths and
content hashes, Runner digest, SandboxPolicy hash, package-proxy policy hash,
and disk budget. The orchestrator authenticates that canonical plan with
`dependency-plan-hmac-sha256-v1` before the Worker starts Docker.

The legacy v1 inputs are:

- Python: one hash lockfile; pip uses `--require-hashes`,
  `--only-binary=:all:`, isolated configuration, and no cache.
- Node: exact `package.json` plus `package-lock.json`; npm uses `ci`,
  `--ignore-scripts`, no audit, and no funding request.

The repository volume is mounted read-only. A distinct labelled dependency
volume is the only writable package output and is never returned as a public
path. The result records its complete file/symlink inventory hash, file count,
byte count, plan hash, proxy policy hash, image, policy, Implement result, and
overall result hash. A failed or timed-out preparation destroys that volume.

## Network boundary

The untrusted preparation container is attached only to a Docker
`--internal` network. It has no default public egress and receives no GitHub,
Provider/model, SSH, Docker, host-home, task, or application credential.
Its only configured HTTP(S) endpoint is `http://dependency-proxy:8080`.

The fixed trusted proxy is dual-homed: one side reaches the public package
service, and the other is the internal Worker network. Its versioned allowlist
is not supplied by repository content:

- Python: `pypi.org`, `files.pythonhosted.org`;
- Node: `registry.npmjs.org`.

The proxy rejects every other hostname and port, discards non-global DNS
answers (including loopback, private, link-local, and metadata addresses),
strips authentication/cookie headers from plain HTTP requests, bounds
connections and transferred bytes, and carries no application credentials.
The Worker waits for a local proxy readiness probe before preparation begins
and destroys the proxy container and internal network afterward.

Every package-manager child also receives the exact
[`sandbox-git-safety-v1`](sandbox-git-safety.md) environment. VCS dependencies,
submodules, hooks, credential helpers, LFS filters, SSH, and every Git transport
therefore remain disabled even while the allowlisted HTTP package proxy exists.

## Verify consumption

Verify may bind one optional dependency result after the exact Implement result
in its signed JobSpec. Migration `0018_dependency_verify_inputs` permits only
that ordered one-or-two-input shape in the durable stage chain.

The fresh, networkless Verify container mounts both the Implement workspace and
dependency volume read-only. Before and after every signed command, it
recomputes the dependency inventory and requires the preparation result's exact
hash. It exposes only fixed local paths for the Python venv or Node modules.
Any replaced dependency result, volume, image, policy, inventory, or input order
fails before successful result creation.

## Acceptance

Deterministic tests verify the fixed allowlists, signed plans, credential-free
environments, internal-network topology, one separate writable output,
Python/npm safety flags, cleanup, ordered JobSpec inputs, stale dependency
rejection, and read-only Verify mounts.

Real macOS arm64 + OrbStack acceptance against the digest-pinned Runner proves:

- a preparation container on the internal network cannot connect directly to a
  public IP;
- a metadata-IP request through the proxy is rejected with HTTP 403;
- the fixed PyPI endpoint is reachable only through the proxy;
- an empty hash lock creates a real isolated venv without credentials;
- the resulting volume is read-only and inventory-stable in a fresh
  `network=none` Verify container;
- all temporary volumes, networks, and containers are removed.

Native Linux amd64 + Docker Engine acceptance remains part of P5-G06 and is not
claimed by this macOS result.

## Project-derived Node environments (2026-10-22)

`node-workspace-preparation-v1` selects the installation root from the approved
source paths and original lockfile. It inspects every package manifest under
that root, `packageManager`, `engines.node`, `.node-version`/`.nvmrc`,
`pnpm-workspace.yaml` and the Yarn linker declaration. Exact manager versions
are frozen; absent `packageManager` uses the versioned platform default
(npm 10.9.4, Yarn 1.22.22, pnpm 10.11.0). Missing evidence requires a read,
while conflicting managers, unsafe declarations, missing locks, more than 64
manifests or unrelated installation roots fail explicitly. Mixed Python/Node
changes retain their existing blocker. Repository configuration cannot change
the fixed package proxy or supply credentials.

`workspace-dependency-plan-v5` extends the existing signed plan with the Node
recipe, package roots, lifecycle declaration, original manifest hashes and an
optional digest-pinned compiler image. `workspace-dependency-result-v3` seals
the actual Node/manager version, runtime path, original recipe, installed
manifest hashes, actual execution image and offline-build completion. These
are additive artifact versions: legacy v1–v4 plans and v1/v2 results serialize
identically, preserving their hashes. No SQL schema migration is needed.

Network acquisition installs only through `registry.npmjs.org`, with repository
lifecycle scripts disabled. If the base runtime does not satisfy the project
constraints, the worker freezes a compatible platform-specific Node binary
from that same registry. npm, Yarn Classic, modern Yarn/PnP and pnpm retain
their original lockfile semantics. Only the public Yarn npm-mirror URL is
normalized in the private installation copy. A second credential-free
`network=none` container runs dependency builds. Compiler-image use is explicit
in the signed recipe and result; it does not bypass the source Runner identity
or image/hash checks.

Baseline and post-change Verify each create a fresh bounded source view in
`/execution`, mount frozen dependencies read-only and probe the exact runtime
and manager before lifecycle initialization. Workspace links point at the
current source view. ESM, package-local modules and Yarn loaders therefore see
the current modification. Source-file hashes are checked after initialization
and every test; caches/generated files stay outside the contribution diff.
The source view retains `nosuid,nodev,noexec`; native dependency binaries execute
from the frozen read-only dependency volume. Repository-generated executables
in writable scratch and arbitrary browser/service downloads are not admitted by
this recipe. External test resources still need their separate versioned
startup/readiness/reset/cleanup contract.

The baseline, Verify and independent Review bind the same preparation result.
The review prompt includes the exact environment spec and runtime receipt;
queued reviews also freeze the baseline/environment binding. Human acceptance
and read-only publication preview retain the existing exact Verify/Review
chain. Preparation success alone never counts as passed tests or publication
authorization.

A disposal journal entry or a trusted pre-command dependency-inventory probe
can invalidate one exact allocation. The latter appends
`dependency_workspace_invalidated`, rather than pretending cleanup succeeded.
Existing bounded recovery rebuilds the same frozen recipe, creates a new
baseline and runs a fresh Verify/Review against the preserved implementation.
It does not repeat code generation or reuse old test/review evidence. Ordinary
command failures and post-command tampering cannot trigger this probe result.

Node dependency entries are canonically sorted by their own relative path before
hashing in both preparation and Verify. A workspace symlink can be broken during
preparation and resolve to a directory after the fresh source view exists;
`os.walk` must not change the hash merely by moving it between file/directory
groups. Link target strings and regular-file bytes remain part of the hash.
Legacy non-Node inventory ordering is unchanged. A real post-command mismatch
still rejects all test evidence and produces a persisted
`environment_baseline_integrity_failed` diagnostic rather than a generic error.

Trusted maintenance can resume a failed initial preparation after a proven
runtime fix, or after an isolated successful acquisition/version probe matches
the exact frozen manager. No model/API route accepts maintenance proofs. Each
replacement keeps the failed Job and allocations, preserves the exact approval,
revalidates source/context/model/policy/stop state, and uses new allocation keys.
There are at most two replacements; replay returns the same Job. A successful
availability probe does not diagnose an earlier unknown transport failure.
Npm network, certificate, write-permission, memory and integrity failures retain
fixed reason codes without exporting raw package output. Preparation must still
complete a real baseline before code generation.

The fresh Node view uses a private `node_modules` directory with package entries
linked to the read-only bundle. Vite's `.vite-temp`, `.vite` and `.cache` outputs
stay private to that view. `TMPDIR`/`TMP`/`TEMP`, `XDG_CACHE_HOME` and `HOME` use
fresh directories in the existing bounded execution tmpfs; the small `/tmp`
mount remains available for supervisor logs. No resource ceiling or network
policy is increased. Cache startup errors and ENOSPC are environment failures,
not evidence that the approved source requires a code repair.

An internal `workspace-preparation-job-v2` can establish a new baseline with an
unchanged, still-live bundle after a trusted readiness probe. It checks the
original approval, stage, prior baseline, source/result/spec hashes, runtime
source proof and allocation; a disposed bundle or failed/mismatched probe is
rejected. The Worker never invokes acquisition in this mode. A new allocation
reference binds the original allocation hash, and the new immutable baseline
includes its runtime proof and reused reference. Verify still rehashes the
read-only bundle before/after execution and Review binds the new baseline.
The old tests, jobs, baseline and approval remain intact. This is an additive
job/artifact contract with no SQL schema change; drain these jobs before rolling
back workers that do not implement the contract.

Explore/Implement/Verify stage workers renew their live lease every ten seconds
while retaining the original stage timeout and retry ceilings. Completion uses
elapsed current time and refreshes the Job state before atomic artifact
finalization. Cancellation, lost ownership or renewal failure cancels execution;
the Docker process group and the exact task container are cleaned up. A late
result from an expired run cannot become successful evidence. Stage
reconciliation also handles a generic Job reaper having recorded expiry first,
so the Job and its pending/running stage converge before a bounded retry.

Upgrade/recovery: back up SQLite together with the content-addressed artifacts,
deploy all API/provider/sandbox services from the same revision, and retain
allocation journals during cleanup. Before rollback to a version without v5/v3
support, stop new execution, drain/cancel new jobs, and dispose their labelled
resources. Keep new artifacts/history for audit; do not edit hashes, approvals,
or recreate the database to make older workers accept them. Plans created
against the old capability fingerprint require a child version and fresh
execution confirmation.

## Workspace preparation and baseline (2026-10-10)

New `sandbox-workflow-v2` authorizations prepare the selected package and run a
baseline **after Explore, before coding/Implement**. A persistent
`execution_prepare` Job owns materialization, dependency preparation and baseline
capture, with an 1,800-second aggregate timeout and normal lease/cancellation
checks. Verification still runs in a fresh, credential-free, networkless container.

`workspace-dependency-plan-v2` binds the real Explore result as
`source_result_hash`, frozen inventory, selected project directory, manifest hashes,
Runner, sandbox/proxy policy and budget. `workspace-dependency-result-v2` preserves
that source kind/hash. It never fabricates an Implement result. Existing v1 wire
payloads and hashes are unchanged. New Verify checks the Explore source against
the actual Implement result and consumes the same prepared dependency volume;
changes to frozen dependency manifests block verification as `environment_stale`.

Python preparation supports a selected nested `pyproject.toml`, package test
requirements and the nearest ancestor's dev requirements. Simple declarative
local dependencies whose package-directory names match dependency names are
discovered recursively (at most six), frozen and built in dependency order.
Dynamic dependencies use the separately versioned adapter below. Ambiguous local
paths, cycles, unsupported requirements options/URLs and unavailable binary
dependencies fail explicitly. Other workspace
layouts and full TypeScript workspace coverage are not claimed by this slice.

The restricted proxy is unchanged. Preparation downloads only binary upstream
packages, creates a version/SHA-256 lock and wheelhouse, installs that lock offline,
and builds local package wheels without build isolation or index resolution.
Untrusted build backends run in the preparation container, never on the host.
The dependency inventory includes the lock, downloaded/local wheels, venv and
source-path configuration. Source precedence ensures baseline and post-change
Verify import the actual workspace code, rather than an old installed wheel.

Multi-package Python preparation uses `python-workspace-preparation-v3` with
`environment_layout=per-package-v1`. It selects at most four package roots from
the approved change paths and freezes their ordered root list and manifest union
in the signed dependency plan. There are at most six additional local projects,
eight total projects, sixteen manifests, and the existing shared disk/time/model
budgets. Mixed Python/Node changes remain unsupported.

Each selected package receives a separate venv, wheel/hash lock, local dependency
closure and source-path receipt under `output/environments/<index>`. Its own test
requirements and ancestor development requirements apply only to that environment;
another selected package's requirement file does not leak into it. One package
can retain `packaging<24` while another uses `packaging>=24`. Conflicts inside either
environment still fail. Distribution names must be unique. Runtime, selected
optional/development/build and frozen requirements-file constraints are checked
after installation, including local wheel versions; the existing explicit
workspace-source overrides remain exact.

`package-environments.json` records the per-package recipes inside the hashed
dependency inventory. The immutable baseline and Verify commands bind every
working directory to its corresponding environment. All commands must run under
a selected package root, and each root needs an existing direct pytest baseline
command; new-file-only tests and collection-only commands cannot qualify. Every
baseline command is recorded before v3 enters code generation. New exact
authorizations bind `baseline-feedback-v1` in admission and approval hashes:
failed baselines first enter a persistent read-only diagnosis. Complete ordinary
code/test failures may enter the existing coding path only when the diagnosed
repair stays inside approved files with unchanged dependencies/commands and all
frozen inputs still match. Environment/incomplete evidence, outside-scope repairs
and legacy approvals instead prepare a child plan for fresh confirmation. Post-change tests
and independent review remain required for all selected packages.

Fresh network-none Verify builds bounded symlink-only Python source overlays on
tmpfs. A common regular package such as `openinference.instrumentation` therefore
sees the modified instrumentor and its local dependencies together, instead of
importing a baseline wheel. Source roots stay read-only; escaping paths, input
symlinks and conflicting module files reject. Each environment has its own
overlay; no source is copied back into the repository or dependency volume.
The whole dependency inventory is checked before and after all commands.

Legacy v1/v2 preparation wire shapes/hashes are unchanged. No SQL schema is added:
v3 is an additive signed-recipe/artifact version. Upgrade API/coordinator/Sandbox
Worker images together, retain old immutable records, and require new exact
execution confirmation for changed capability/recipe inputs. Before rollback,
stop or drain v3 jobs and preserve their bundle, baseline and journal; restore
matching images, then re-plan/reconfirm. Never reinterpret a v3 recipe as v2 or
restore an old approval by editing its hashes.

Plans editing a selected Python workspace's frozen manifest are not executable
with the existing environment, including a pyproject change confined to mypy
settings: current Verify binds the entire manifest hash, not only dependencies.
Admission reports `environment_manifest_change_unsupported` before confirmation.
Automatic baseline replanning rejects these edits with actionable feedback and
retains the original approved contribution paths/commands (not additions from a
rejected intermediate draft). Supporting manifest overlays or a changed recipe
requires its own explicit adapter; no silent old-environment reuse is permitted.

Migration `0037_baseline_feedback` permits automatic executing-to-planning only
through a succeeded diagnosis job and matching append-only planner/baseline/
authorization/artifact receipts at the exact execution-state predecessor. It
does not grant execution consent. New diagnosis/planning jobs must be drained
before rollback, followed by restoration of the complete pre-upgrade SQLite and
artifact backup with matching code. Historical failures and approvals are never
edited. Dependency cleanup retains volumes while diagnosis consumes them and
idempotently releases the abandoned attempt when it enters read-only replanning.

`execution-baseline-v1` records exact commands, plan hash, real Explore result,
environment spec/result hashes, sanitized command evidence and outcome.
Both stdout and stderr feed outcome classification and the native execution
details. Historical summaries that omitted stdout are projected from their
unchanged, hash-verified inspection artifact; this does not replace the original
receipt, change a failure into a pass or grant recovery/implementation consent.
Environment errors stop before code generation; code/test failures remain visible
baseline evidence and do not manufacture a passing baseline. Post-change Verify
must pass, and independent review receives the immutable baseline evidence.
New regression tests may need separate reproduction against the original code;
an empty selection is not proof of reproduction. Full real-task acceptance remains
WD-G03, and cancellation/crash recovery is not claimed beyond tested boundaries.

Migration `0034_execution_baselines` adds reference guards for baseline receipts,
preserving all prior records. Stop writers and back up SQLite plus artifacts before
upgrade; restore the matching database/artifact backup and old code together if
recovery is necessary. Never run old workers against active new preparation jobs.

## Selected source trees and verification commands

Nested-package execution freezes `source-tree-scope-v1` roots plus their hash in
the exact execution authorization. Nonempty scopes use `sandbox-job-spec-v2`;
legacy v1 wire and hashes remain unchanged. Explore, preparation and Implement
materialize the same roots from the original hash-verified archive. Full archive
path structure and symlink escapes are validated even outside the selected
scope. Credential-name refusal remains inside the scope; unrelated sibling trees
are absent rather than executed or rewritten. This prevents a Python task from
failing merely because an unrelated JavaScript tree contains `.npmrc`.

Verification commands reject dependency-install/environment-manager actions
and nonexistent explicit pytest file targets before saving or approving a plan.
Environment resolution/build is a separate preparation action. Baseline selection
`existing-target-commands-v1` runs unchanged approved commands, excluding commands
for approved new files until those files exist after Implement. It never silently
changes argv or manufactures pre-change failures.

Verify resolves Python against the child's frozen venv PATH, and supplies source
roots to both PYTHONPATH and MYPYPATH. It disables mypy cache writes in every
fresh verification (`MYPY_CACHE_DIR=/dev/null`), retaining the bounded log tmpfs;
Ruff scratch stays under bounded `/tmp`. This avoids typing-cache growth consuming
the same small filesystem as test logs without adding writable host mounts.

Code-basis refresh is a persistent archive/context operation and can be run
without invoking a model. A new context invalidates the prior plan binding;
manual save creates a child version and a deterministic non-execution receipt.

Migration 0035 adds scope authorization guards without changing prior records.
Before upgrade stop/drain writers and back up SQLite/artifacts. Downgrade requires
matching old code and backup; never reinterpret a scoped v2 job as unscoped v1.

## Dynamic Python metadata and admission (2026-10-14)

`execution-capabilities-v2` checks frozen manifest contents and hashes before
execution confirmation and again before the worker contacts GitHub or creates an
attempt. The UI, API and preparer share the Python metadata/requirement validator.
Missing manifests trigger read-only planning reads; known unsupported inputs
cannot create an approval, execution or job. The authorization binds the report
hash and capability version. A successful check means the adapter accepts the
inputs, not that installation or tests have passed.

For dynamic dependencies, `workspace-dependency-plan-v3` additionally signs
`metadata_strategy=wheel-metadata-v1`. The existing v1/v2 wire data and hashes
remain unchanged. A safe explicit backend and bounded relative backend paths are
required. The preparer acquires declared build tools as wheels through the fixed
proxy, freezes their SHA-256 lock and installs it offline. It builds local project
wheels without index lookup or build isolation, inside the existing credential-free
resource-bounded sandbox. Repository hooks never run on the host.

Each generated wheel must contain exactly one bounded METADATA entry with the
expected project name. Requires-Dist declarations reject URLs, pip flags and local
paths; environment/selected-test-extra markers are evaluated with the created
venv's requirement parser. The dependency artifact retains `project-metadata.json`
with the metadata hash and declarations. The combined runtime/test requirements
are resolved wheel-only through the same proxy, hash-locked and installed offline.
The selected project's already-built wheel supplies package metadata and entry
points; baseline and Verify still import the actual frozen/modified source.
Unavailable upstream wheels fail explicitly; there is no source-build fallback.

`coding-context-v2` separately raises the full-file limit to 700,000 bytes while
retaining the 1,500,000-byte aggregate budget, original hashes, sensitive-content
filter and exact text edit checks. Legacy v1 retains its 256,000-byte limit.
Automatic planning stays at 256,000 bytes; explicit reads share a 700,000-byte
total budget. Reading a large file therefore does not silently authorize an
unbounded implementation context.

Real macOS/OrbStack acceptance covers large-file planning and coding capture with
matching complete hashes, plus static and dynamic Python packages through metadata
preparation, frozen dependencies, failing original-code baseline and passing
modified-source offline verification. The dynamic fixture also verifies discovered
conditional dependencies and the retained metadata receipt. This is not Salt
execution or native Linux acceptance. Fresh Salt plan approval remains required.

These wire protocol additions use existing immutable artifacts/events and require
no SQL schema change. Before rollback drain/stop new v3 jobs and retain database
and artifact backups; use matching code for any pending protocol version. Never
reinterpret v3 as v2, mutate an approved plan or reuse an authorization whose
capability/context inputs have changed.

## Optional upstream source builds (2026-10-14)

After the user authorized the [source-build extension](dependency-source-build-proposal.md),
`execution-capabilities-v3` can select signed `workspace-dependency-plan-v4` /
`source-wheel-v1` when a digest-pinned `DEPENDENCY_BUILDER_IMAGE` is configured.
The API, coordinator and Sandbox Worker receive the same non-secret setting.
Existing v1/v2/v3 recipes remain wheel-only and cannot request a compiler.

Wheel-first resolution may acquire a missing package's bounded tar sdist from the
existing fixed PyPI proxy, select a version satisfying the actual requirements,
verify its upstream SHA-256 and validate all members. Declared build tools are
downloaded as wheels, hash-locked and installed offline into a separate tool venv.
The networked acquisition container never invokes the upstream source backend.

An independent compiler container uses `network=none` and only one read-only
volume subpath containing that source and tool venv. It has no repository, writable
volume, host directory, Docker socket, model or application credential. Compilation
runs on bounded tmpfs. Validated wheel bytes (at most 32 MB) cross bounded IPC to a
trusted offline importer; source hash, build lock hash, wheel hash and compiler
digest remain bound in the dependency artifact/spec. Baseline and Verify still
consume one read-only frozen dependency result with source precedence.

Source builds are limited to eight per preparation and share one aggregate Runner
timeout. New preparation scratch is the minimum of 512 MiB, half the memory limit
and a quarter of the disk budget, retaining noexec/nosuid/nodev. Legacy defaults
are unchanged. Stream output is bounded while reading, not only after completion.
Failure destroys the dependency volume and preserves a redacted structured receipt
with the exact phase/package, preparation spec, plan and Explore hashes. Known
deterministic failures block the same frozen environment across plan rewording;
changed image/policy/inputs require a new authorization. Transient failures are
not claimed to prove permanent unavailability.

The actual Salt frozen archive passed preparation including native `timelib`,
then its unchanged two pytest targets passed **7 tests**. Separate static,
dynamic and native-source real macOS/OrbStack fixtures passed **3 cases** through
original-code baseline and modified-source verification. These are environment
diagnostics/fixtures, not a new approved Salt implementation or review, nor native
Linux acceptance. SQL storage is unchanged; drain new v4 jobs before rollback,
retain matching database/artifacts/code and never reinterpret v4 as legacy v3.
