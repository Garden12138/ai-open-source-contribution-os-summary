# UPEVE v1 implementation candidate

This is an opt-in candidate, not completed UPEVE acceptance. The original
specifications and [acceptance ledger](acceptance/universal-environment-v1/README.md)
remain authoritative. Original ARV2 and Phase 5 gates stay open.

## Activation and provenance

Set `AGENT_RUNTIME_V2_ENABLED=1`, `UPEVE_ENABLED=1` and explicitly select
`UPEVE_TARGET_PLATFORM=linux/arm64` or `linux/amd64` before requesting a **new**
v2 plan. A planning request and its plan binding pin the engine/platform;
revisions inherit that binding. Existing plans/approvals retain their original
parser and exact preflight hash. Disabling a pinned engine blocks execution
rather than falling back. Keep the default switches off for existing services.

`app/environment_engine` inspects frozen archive evidence and builds a
`verification-environment-contract-v1`. Its hash covers actual snapshot, source,
base, plan, exact commands/cwds, target, image/policy, declaration provenance and
parser versions. Static admission is never preparation or baseline proof.
No new task database, framework, model authority or GitHub write is introduced.

New signed `workspace-dependency-plan-v6` binds the entire contract. The existing
credential-free package proxy acquires wheels/packages, offline installation
freezes their inventory, then a separate offline Verify invocation measures
actual executable ownership, Python module origin, RECORD hashes and invocation.
Help is used only as an entrypoint probe, never as a baseline test. Every
approved command then runs unchanged. Frozen command-source files are protected
alongside manifests. A changed recipe requires a new plan/preview/authorization.

`execution-baseline-v2` retains raw command hashes, statuses, exits and log hashes,
plus separate preparation/probe artifacts and a recomputable verdict. Full,
bounded, hash-verified captures produce versioned test-count/failure summaries
before UI excerpts are truncated. Old baseline artifacts are not reinterpreted.
Admission rechecks the complete chain and exact entrypoint set; `TaskSpec` binds
its artifact hashes. Environment failures never open a coding session or charge
coding repair budget. Expected Issue reproduction is an explicit, issue-bound
PlanCommand JSON extension with an exact target, failure literal/exit and passing
control commands. Missing reproduction is `no_change`; unrelated failures,
help-only, zero tests, missing receipts and unknown evidence block coding.

The native conversation projects discovery, resolution, preparation, entrypoint,
baseline, coding and independent Review facts. Native UI/API contract tests are
separate from real model journeys.

## Supported boundary

- Python: PEP 621 static dependencies, selected dev/test extras/groups, bounded
  include-groups, literal Make/CI pip declarations (including inline package
  requirements and separate constraints with source line references), recursive relative `-r/-c`,
  PEP 508 declarations and marker preservation. Constraints never request a new
  package. Current preparation retains its 16-manifest/300-declaration budget;
  inspection has a separate 64-file/2 MiB, depth-8 bound. Unsafe paths, options,
  URLs, cycles, ambiguous working directories and dynamic metadata stop safely.
- Tool names map to **declared** distributions; actual CLI RECORD ownership and
  executable/module availability are subsequently measured. Source-shadowed test
  modules are rejected. No host-global Ruff fallback or command-name pip install.
- Node: literal manager script chains and `pnpm exec`, bounded recursive scripts,
  explicit independent tool directories with their own locks; acquisition ignores
  scripts, builds occur offline, and each independent root is frozen. Existing
  npm/pnpm/Yarn/Bun drivers and the Vitest 2.1.9 data-only cache adapter remain.
- Arbitrary subprocess-capable JS installers, npx/dlx/network wrappers, shell
  evaluation, inline Python checks with unproven semantics and unsupported
  multi-Python-package contracts remain explicit gaps. They do not become ready.

Known unresolved real regressions: current mypy 2.4.0 stubtest writes
`.mypy_cache` into read-only source; the historical Node wrapper installs tsx
under CI even after child dependencies are prepared. No source/package patch,
network exception, writable code mount, command deletion or installer-success
shim was introduced to conceal those failures. These gates remain unpassed.

## Upgrade and recovery

Forward migration `0039_environment_contracts` guards new expectation-bearing
PlanCommand JSON. It does not rewrite existing command JSON, plan hashes,
approvals, v1/v2 events or artifacts. Tests create a real v0038 database, take an
online SQLite backup, upgrade it, compare old hashes, verify integrity/foreign
keys, restore the backup into another isolated database and upgrade again.

Stop writers and back up matched database/artifacts/code before upgrading. Run
schema initialization **once, serially**, before starting role processes against
a fresh instance; simultaneous initial migration was observed to race and its
failed isolated instance is retained. Rollback drains new jobs and restores the
matched backup/code/artifacts. Never erase usage or reinterpret new contracts
with an old verifier. Do not run this candidate against production storage.

The current development instance at `http://127.0.0.1:18090` has a new database
and artifact root under `/tmp/upeve-isolated-20261029-v2`. API, coordinator and
Sandbox Worker run; Provider is separately blocked on a real configured model
Gateway. Publisher is disabled. The older trial service/data remain unchanged.
