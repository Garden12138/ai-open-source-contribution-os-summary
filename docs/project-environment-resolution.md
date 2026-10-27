# Project-derived environment preparation

This Phase 5 change follows the 2026-10-24 report for task
`65bb0e9f-e8b5-4780-8a5e-19469e5e0f46` and the user's requirement to derive
preparation from each project's actual setup rather than a fixed project template.

## Verified incident

The frozen `can1357/oh-my-pi` archive contains a root `bun.lock` (160,768 bytes,
SHA-256 `6e330e18ca780dfccd6b6161818c204155f4c2cefce482bfead3e8315406a048`),
`bunfig.toml`, and a root `package.json` declaring `bun@>=1.4`. The archive SHA-256
is `7ee697b1908313f50962e27cf98f37a1386bc2689977e32dcc3290d7ac35c8ea`.
The original resolver recognized only npm/Yarn/pnpm lock names and incorrectly
reported `node_lockfile_missing`. Recommending a new npm lockfile was wrong.

The subsequent user request to check locks exposed two additional defects:
`repository_file` with no path ignored its query and searched only previously
captured entries, so both `lock` and `package.json` requests returned the same
3,250-byte subset. The second prompt was denied at 17,307 against the unchanged
16,000 limit. A saved verification command also named `bun run typecheck`, while
the root package had no `typecheck` script. These are framework defects, not proof
that the repository lacks dependencies or that the issue cannot be implemented.

## Resolution model

The pipeline consumes frozen project facts, chooses a tool driver, resolves and
records exact tools/dependencies in isolation, and verifies against that frozen
result. `app/environment_resolution.py` defines typed driver metadata and parses
project requirements. Drivers encode package-manager semantics; they do not
contain per-repository paths, versions or dependency lists.

Discovery considers the selected source package, ancestor installation roots,
workspace declarations, the project's actual lock format, `packageManager`,
`engines`, runtime version files, `.tool-versions` and literal CI tool pins.
Conflicting or unavailable inputs remain explicit diagnostics. Multiple CI
versions do not become an impossible conjunction; the project declaration
remains the constraint when CI describes several alternatives. No CI action or
repository script is evaluated during discovery.

Existing exact npm/Yarn/pnpm recipes retain `node-environment-v1` compatibility.
`node-environment-v2` adds a manager constraint, source-lock binding and lock
strategy. Ranges such as `>=1.4` are resolved against the fixed public package
registry inside credential-free acquisition. The exact chosen version, tool
tree and dependency hashes are recorded in the immutable result. Bun uses the
official `@oven/bun-linux-*` npm platform distribution, avoiding host installation
or an install script downloaded from an arbitrary host.

With an existing lock, the manager installs against that lock. With no source
lock, the manager resolves in the disposable preparation copy and retains a
private generated lock and its hash. Original package declarations must remain
unchanged. The source checkout is not changed, no lock is proposed for commit,
and the resulting environment is reused for the baseline and post-change checks.
This rule applies to preparation, not permission to update project dependencies.

Acquisition disables lifecycle scripts and uses only the existing allowlisted
package proxy. The second container builds without network or credentials.
Fresh test views use the frozen tools and dependencies, recheck exact versions,
retain current-source workspace links and reject source mutation. Time, memory,
disk, file-count, output, model and repair limits remain in force.

Recognized but unavailable tool drivers produce an explicit capability gap.
Unavailable requested versions produce `toolchain_version_unavailable`, not a
Python-wheel error or a false missing-lock message. New tool families require
drivers that obey the same trust boundary; the model cannot grant arbitrary
network access, install system tools on the host or skip mandatory checks.

## Dialogue and plan correctness

Inventory search filters the real frozen inventory case-insensitively, reports
match counts and returns bounded pages. It separately marks whether a matched
file's content has been captured. A path's existence is therefore not confused
with having read its contents.

A source-bound `environment` read provides the current installation root,
declared manager, actual locks, unread configuration count and exact script
blockers. A planned task receives these facts before model reasoning. This
avoids confusing auxiliary tool directories with the task's installation root,
and unread metadata with missing dependencies. Baseline availability is reported
separately from whether an environment has ever started preparing.

Request admission reserves room for subsequent evidence. Long previous
assistant replies and plan descriptions become explicitly partial, source-bound
references; the current user request remains exact. The hard 16,000-input and
4,096-output limits and lifetime usage are unchanged. An input that cannot fit
still fails before another model call.

Evidence pages share the total remaining wire budget, rather than each claiming
an independent allowance. Source-bound full originals remain retrievable. A
failed read-only turn also retains its read-only classification when resumed;
the resume control is explicitly separated from the effective original request.
Its model schema exposes only `respond` and `effect=observe`, preventing
“continue checking” from becoming a request to advance the workflow.

Explicit `npm/yarn/pnpm/bun run <script>` commands are checked against an inspected
`package.json` at their exact working directory. A speculative missing script is
rejected with actionable planning feedback. Bun installation commands are also
excluded from verification; environment preparation stays separate. Stored
plans are not rewritten: a correction creates a child plan requiring its own
exact execution confirmation.

### Bun dispatch evidence correction (2026-10-26)

An actual Bun 1.4.4 sandbox probe reproduced a verification false positive:
`bun --cwd packages/tui run check:types` printed the `bun run` help menu and
exited zero without running the declared script. A controlled child-package
script that must exit 17 also returned zero with the same prefix. With
`argv=["bun", "run", "fail"]` and `working_directory="packages/lib"`, its
actual exit was 17. A separate sentinel line proves the successful probe ran;
finding the sentinel inside the help menu's script listing is insufficient.
Equals-form and post-`run` flags executed in this particular version, but the
verification policy requires an explicit typed working directory for Bun's
script-directory options so plan interpretation does not depend on flag order.
Arguments forwarded after the script name remain arguments to that script.

Plan saves, model draft validation and execution admission reject affected
commands. Baseline and post-change outcome projections independently recognize
the known help response, return `environment_blocked` with
`verification_command_not_executed`, and exclude it from the passed count.
The raw command status, exit code, logs and artifact hashes remain unchanged;
historical evidence is reinterpreted, never rewritten. This is an additive
derived verdict and needs no database migration.

The previous 5/5 baseline claim for task `65bb0e9f` was incorrect: only three
test commands demonstrably executed; two script checks did not. All three real
implementation attempts and independent blocked reviews remain immutable.
The task is held at `verification_execution_unproven`. A command correction
must be saved as a child PlanVersion and receive fresh exact execution consent.
Neither a previous approval, a generic chat continuation nor the separately
bound two-call budget extension approves that child plan.

Actual frozen-source diagnostics with typed working directories started both
scripts. The default parallel compiler was killed with exit -9 (SIGKILL); OOM
was not directly proven. Both commands passed with `--singleThreaded` under
the same 2 GiB limit. This option restricts compiler concurrency while retaining
the complete check ([official compiler documentation](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0-rc/)).
Child plan `b81f2268-af55-44c6-a971-d4268a69c121` therefore uses
`bun run check:types --singleThreaded` and `bun run check --singleThreaded`,
both with `working_directory="packages/tui"`. Its goal, option A and edit paths
are unchanged. It is maintenance-authored and unapproved. User-directed local
preparation uses the existing `user_replan` state path with explicit maintenance
attribution; no app user message, diagnosis job or human execution consent is
fabricated, and the stricter automatic baseline-diagnosis gate remains intact.
Two rejected transition attempts rolled back before this legitimate request.

Recovery uses the matched database/artifact/source backup at
`/data/backups/verification-command-policy-20261026T014915Z`. Keep every newer
failure and maintenance receipt when recovering; this procedure does not erase
or reset model usage, repair counts or signed approvals. The earlier
`execution-read-recovery-2026-10-25.json` is a historical snapshot whose exit-code
pass counts are explicitly superseded by this correction.

## Follow-up execution regression (2026-10-25)

The readonly lock replay above did not exercise task advancement. Production
message `ed01df1f-6c6d-4e3f-ab8d-61a2ec2e8399` explicitly said
`按你的下一步计划进行`, but job `3f5f776d-1544-426b-90bf-608387d7709c`
exhausted its conversation read budget and never queued a planning job. The
saved plan still contained the nonexistent root `typecheck` script. Describing
the blocker was not acceptance of the user's action request.

`planning-continuation-v1` derives the next local preparation step from the
current saved plan, context and deterministic environment/command diagnostics.
Unambiguous forward directives use the existing continue control. A blocked
plan queues preparation instead of repeating a confirmation that cannot work.
Required project metadata is collected in the existing bounded Sandbox context
job before planning; chat does not have to read each manifest in a separate
model round. The planner receives the pending saved plan and exact validation
feedback and must return a full revision or a material read/question.

An idle coordinator may recover one still-current explicit forward directive
that failed in conversation classification before dispatch. The request binds
the original local-user event ID/hash, current plan/context hashes and failed
job. Later user messages, stops, changed model/input/plan, active work and a
previous recovery prevent replay. Recovery appends orchestrator receipts and a
maintenance marker; it does not invent a human message, alter failed history,
or count this operator-assisted incident as autonomous acceptance. Reading and
planning never authorize implementation or publication.

The matched pre-recovery backup is
`/data/backups/planning-continuation-20261025T023426Z` (1,918 events, 1,100
artifacts, zero active jobs). Restore only with workers idle, preserving any
later receipts separately. There is no SQL schema change.

Production acceptance: the Sandbox read all 26 missing metadata files. Two real
planning calls (including one rejected-output repair) saved child plan
`119e2aed-976c-412e-b1c8-6cad1ce64a67`, parent
`6f241784-8976-4bd5-bd9b-30e2bfdcf452`, with the same selected option A and two
TUI paths. It uses declared package scripts `check:types`/`check`, with the
package directory supplied by Bun's `--cwd`; static script validation also
follows literal manager directory options. The deployed UI reports
`can_execute=true`, zero blockers, no active jobs and no execution authorization.
A real browser opened the current plan confirmation at desktop/mobile widths:
review and confirmation buttons were enabled and no mutation request occurred.
This is acceptance of plan continuation, not proof that this project's
dependencies, baseline or implementation have run. Those remain behind the
new exact confirmation. Detailed evidence and platform limits are recorded in
[planning-continuation-2026-10-25.json](acceptance/planning-continuation-2026-10-25.json).

## Upgrade, recovery and verification

### Native startup and supplemental source recovery (ENV-R07)

The confirmed execution of task `65bb0e9f` prepared Bun 1.4.4 but three
pre-change commands failed while loading `pi_natives.linux-arm64.node`.
The old classifier treated these startup errors as ordinary test failures;
the subsequent three model requests for `prose-gate.ts` and
`markdown-prose.ts` were rejected as proposal-format failures. Both helpers
exist in the frozen archive and are readable. The prior plan, approval,
failed jobs, rejected outputs and cumulative model usage remain evidence.

Native loader errors now produce `environment_blocked` before coding, at
coordinator, proposal admission and model invocation boundaries. Classification
uses the complete bounded command logs, while presentation keeps its short
redacted excerpts. Assertion failures retain their code-failure classification.
An older receipt is reclassified by an append-only, idempotent blocker;
historical artifacts and hashes are never rewritten.

Supplemental source requests run as persistent `coding_source_read` jobs in the
credential-free Sandbox Worker. Each step binds the original archive/base,
authorization, plan, coding conversation and editing-view hash. Captured files
come from the current editing view; uncaptured helpers come from the frozen
archive. Missing or unavailable paths return explicit diagnostics and bounded
directory candidates. Reading never adds approved edit paths. Stops, changed
inputs, unexpected paths, duplicate results and content-hash mismatches reject
the read before a successor proposal is queued. The existing six-call lineage
budget includes the previous failed calls and subsequent corrections.

For NAPI workspace packages declaring a safe `name`, exact `version` and
`napi.binaryName`, isolated acquisition can prepare an exact same-version
`<name>-linux-arm64` or `<name>-linux-x64` registry package when it is absent
from the ordinary installation. Scripts remain disabled during acquisition;
the existing fixed proxy and offline build boundary apply. This supports that
specific platform-package convention, not arbitrary native builds. Missing or
incompatible packages remain environment failures. The versioned
`node-environment-result-v2` receipt binds package identity, workspace, binary
name and binary hash; v1 receipts remain supported. Fresh execution validates
the receipt and binary again. Scope directories are private so introducing the
platform-package link cannot write through a symlink into frozen dependencies.
Source-workspace loaders may also require the binary beside their declared
JavaScript entrypoint. That location is derived from the frozen package's
`main` declaration; an additional link exists only in the disposable execution
view. Package identity is checked again, path escape and tracked-binary
replacement are rejected, and the original source tree remains unchanged.

The maintenance-only startup recovery requires the still-current exact
execution approval, original baseline hash, tested runtime source hashes and a
passing regression artifact. It creates a bounded replacement preparation job,
records maintenance attribution and resumes an eligible retained source-read
request under its original lineage. It never creates a new approval, edits a
business proposal or resets failed calls. In-flight replacement preparation owns
coordinator ticks, avoiding a second blocker from the superseded baseline.
Repeated recovery returns the same job; stopped, busy, stale and unproven
requests fail closed.

The first real model continuation after the repaired baseline returned two
operations for the same approved file. This was correctly rejected in full,
but the old validator mislabeled duplicate paths as a scope violation.
Duplicates now produce a correctable structural diagnostic with the exact path
and operation index. Feedback requires one operation per path with an ordered
edit list and all planned regression tests. Actual unapproved paths remain
non-correctable scope violations. Retrying after a trusted environment recovery
excludes only explicitly superseded failures from that same attempt; failures
from another attempt or an untrusted receipt still block. No operations are
merged, applied or authored by the maintenance process.

At the 2026-10-25 checkpoint, production consumed its final two calls on reads of source that was
already captured, leaving the original lineage at 6/6 without an applicable
proposal. The call limit was not reset or increased at that checkpoint. Admission reports the
actual remaining count, reserves the final call for a proposal rather than a
read, and refuses retries after exhaustion. Legacy exhaustion is explained by
an append-only `proposal_call_budget_exceeded` blocker instead of a misleading
proposal-size error. This is still a failed end-to-end acceptance: production
baseline and supplemental reads succeeded, but implementation, post-change
verification and independent review have not run. Additional calls need an
explicit budget decision; this incident cannot close ENV-R07 or UXR-G01.
The 2026-10-26 user-authorized two-call extension and three subsequent real
implementation/review attempts supersede that checkpoint: original root **7/8**,
ten total coding calls including two original-authority repairs, all reviews
blocked. The command-evidence correction and unapproved child plan above are
the current state. Full exact evidence is in
[verification-command-policy-2026-10-26.json](acceptance/verification-command-policy-2026-10-26.json).

The matched pre-recovery backup is
`/data/backups/coding-read-recovery-20261025T063740Z` (1,958 events and 1,133
artifacts, zero active jobs). There is no SQL migration. To roll back, first stop
writers and preserve later receipts, then restore the matched database,
artifacts and service sources together; do not run an older worker against
active v2 native receipts. Controlled real-container acceptance loads the pinned
native binary during baseline and fresh Verify and reaches independent-review
and read-only-preview contracts with fake model/GitHub responses. It does not
establish real-model completion of the original task or the native Linux gate.

No SQL schema change is required. Existing immutable recipes, attempts and
receipts are retained. New source/recipe inputs or invalid existing scripts may
make a prior confirmation stale; the system must request a corrected plan
rather than reinterpret old authorization. Rollback requires idle workers and a
matched database/artifact backup, preserving later receipts separately.

The pre-deployment backup is
`/data/backups/project-environment-20261024T153414Z`: 1,870 events and 1,072
artifacts, with SQLite integrity and foreign keys verified. Diagnostic/model
replays use separate database/artifact copies and do not update production task
messages, approvals, jobs or usage.

Real container coverage includes the four existing npm/Yarn/pnpm paths and Bun
with/without a source lock. These tests run preparation, baseline, implementation
and fresh offline verification and assert that source files stay unchanged.
They are controlled archives, not a claim that every repository or platform has
passed. Native Linux P5-G06, full real-GitHub UXR-G01 and ENV-G01 remain open.

Final real-model replay of the original task request succeeds in one call at a
conservative 13,824/16,000 input bound. It identifies the existing Bun lock and
the missing `typecheck` script. All 77 original task event hashes are unchanged;
production state and usage are not modified. Full regression: 1,319 passed,
43 skipped; final conversation/workbench/contract regression: 176 passed,
3 skipped. Seven real container preparation cases pass (four legacy manager
paths plus Bun locked/unlocked and npm unlocked). These controlled preparations
do not claim that the production repository's full build or native tests ran.
Machine-readable reports, isolated replay history and final source hashes are in
[the acceptance record](acceptance/project-environment-2026-10-25.json).

Tool semantics were checked against the official
[Bun installation documentation](https://bun.com/docs/installation),
[Bun install documentation](https://bun.com/docs/pm/cli/install) and
[lockfile documentation](https://bun.sh/docs/pm/lockfile). Security policy remains
owned by this repository rather than by instructions in upstream files.
