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
- Node: literal manager script chains, `pnpm exec` and bounded literal `cd`
  sequences, exact typed leaf argv/cwd, bounded recursive scripts,
  explicit independent tool directories with their own locks; acquisition ignores
  scripts, builds occur offline, and each independent root is frozen. Existing
  npm/pnpm/Yarn/Bun drivers and the Vitest 2.1.9 data-only cache adapter remain.
- New `node-toolchain-v4` contracts bind literal `pnpm/action-setup` version
  and `actions/setup-node` node-version/version-file inputs to their exact
  source hash and line. These intersect manifest ranges. Dynamic, conflicting,
  indirect or unsupported declarations stop safely; CI is never executed.
  Old non-UPEVE inspection and v1/v2/v3 contracts retain their original meaning.
- pnpm12's declared native CLI can be prepared without lifecycle hooks on
  supported Linux glibc arm64/x64: validate same-version optional-package
  metadata, four known bin declarations and bounded ELF architecture, then
  materialize only those private tool entrypoints. An inventory-hashed receipt
  detects tampering. Unknown versions, platforms, links or metadata fail closed.
  Actual `--version` must succeed; `OSError` becomes a redacted typed failure,
  with no shell fallback. The old unpinned pnpm12 component passed acquisition
  after this adapter but failed its later offline build; full compatibility is
  unproven. The separate CI-pinned pnpm10/Node22 component passes all four checks.
- Arbitrary subprocess-capable JS installers, npx/dlx/network wrappers, shell
  evaluation, inline Python checks with unproven semantics and unsupported
  multi-Python-package contracts remain explicit gaps. They do not become ready.

Historical Python now passes all five original commands using a versioned,
16 MiB data-only cache for measured mypy 2.4.0/2.4.1. No dependency downgrade,
source/package patch or command change is involved. The historical Node wrapper
still installs tsx under CI after child dependencies are prepared. It remains
unsupported; no network exception or installer-success shim is provided.

`python-toolchain-v4` freezes an explicit `python-data-cache-v2` capability in
the exact environment preview. It selects only a declared mypy/stubtest at root
cwd with the default cache path; conflicting tracked cache content, custom
paths, unsupported versions and stale bindings fail closed. Preparation records
actual installed metadata in an inventory-hashed receipt; entrypoint/admission
receipts bind that record to the contract and signed plan. Old v1/v2 toolchain
contracts and the initial single-version cache candidate stay readable.

A fixed, isolated supervisor creates only an empty `.mypy_cache` mountpoint.
It uses `python -I`, has no network/credentials/model access, and executes no
repository code. Actual probes/checks mount source and dependencies read-only;
only that precise data directory has a fresh `rw,noexec,nosuid,nodev` tmpfs,
capped at 16 MiB and further limited by existing memory/disk budgets. Nonempty
or linked mountpoints reject. Logical source hashing excludes only this declared
data mount; tracked files hidden by it still cause the expected source inventory
to disagree. No source file may become writable or disappear from provenance.
Two fresh real Verify runs prove cache data does not survive into the next run.
Unknown tool versions do not inherit this capability automatically.

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
Sandbox Worker and Provider run with a separate Gateway on 18091. The
user-selected domestic DeepSeek connection is now bound to the maintained
TypeScript task: planning uses its explicit low-effort Profile; implementation
and independent Review retain the separate high-effort Profile. A normal goal
revision inherits the task's explicit planning Profile and existing Runtime v2,
never new global defaults or a silent legacy downgrade. Disabled v2 rejects.
These changes add no database schema and rewrite no historical jobs or receipts.
The isolated Publisher runs separately on18092; there is no publication consent.
The older trial service/data remain unchanged. Current deployment and real vs
controlled outcomes are recorded in the [acceptance ledger](acceptance/universal-environment-v1/README.md).

Runtime v2 provider failures now include optional `completion-response-facts-v1`
enums/booleans and `agent-provider-failure-v1` receipts. Reported token usage is
separate from the still-charged conservative reservation; absent usage is never
zero. No provider text/private reasoning, budget refund, new retry or authority
is added. Old failure events remain readable and cannot acquire missing response
facts retroactively. These are additive event fields, without SQL changes; drain
jobs and retain matching code/DB/artifacts before rollback. The maintained task
has actual native preparation/8 probes/4 Baseline passes, then a terminal model
response failure before tool execution; it has no completed contribution or Review.

Studio tool mode explicitly binds `structured-result-tool-v1` in its trusted
system instruction: the only registered result function returns the entire stage
schema; application tool names in Agent input remain JSON data. The strict
decoder continues rejecting multiple results or an unregistered function.
Optional `completion-response-facts-v2` adds a bounded call count and envelope
enum, retaining no raw names/arguments/reasoning. V1 event fields remain exact;
no schema migration or budget/retry change is made. This protocol clarification
and diagnostic detail require separate live verification; fixtures are not native
success and cannot repair or reopen a historical terminal session.
