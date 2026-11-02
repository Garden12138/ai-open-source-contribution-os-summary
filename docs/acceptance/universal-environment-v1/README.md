# UPEVE v1 acceptance ledger

Native first-attempt freeze: `4d72db55cd56ea14edc7c70eae7187ef64175054`.
Deployed maintenance revision: `c4cfabe5e8493b9f99e5bffbe2b92356d3ed984b`.
**Acceptance incomplete.** E0–E2 pass; current task is E3 / UPEVE-E04. Historical
Python passes 5/5; historical Node remains unsupported. Latest default suite is
1771 passed / 62 skipped / 0 failed. Prior unseen cohorts retain 0/15 complete
first baselines. After the failed truncated scope revision, an approved output-only
extension permits one real DeepSeek call to save Plan3 with all eight paths and
four original commands. Read reuse now has real model-input and Plan-save
evidence. Static admission and upstream SHA match pass; exact Plan execution
approval is pending. Native Baseline/Coding/Verify/independent Review remain
not_run. Native Linux amd64 remains blocked/not_run.
[All 28 cases](acceptance-status.json) distinguish
passed, partial, failed and blocked evidence.

Current branch: `refactor/agent-runtime-v2`. Phase 5 stays current. Historical
ARV2-M5/M6, ENV-G01, UXR-G01 and P5-G06 are unchanged. No production database,
approval, upstream repository or remote branch is modified by this work.

## E0 — verified baseline

Starting revision `08be8f7`; clean worktree; HEAD and origin tracking branch
identical (0 ahead / 0 behind). Compared with local main: 151 changed files,
25,964 insertions and 75 deletions. No fetch or remote mutation.

[e0-baseline.json](e0-baseline.json) records the SHA-256 of every existing ARV2
acceptance file, original task event IDs/hashes, verified content-addressed
baseline/dependency/spec artifacts, archive hashes and relevant source contents.
The original isolated database was opened with SQLite `mode=ro` only. These are
historical failed native samples (H), never unseen successes.

Actual path: `contribution_workflow.execution_preview` → exact authorization →
Explore → `WorkspacePreparationWorker.execute` → `task_environment` →
`DependencyPreparationPlan` → proxy/download/offline build → `DockerVerifyRuntime`
→ baseline → `orchestrator.freeze_spec` → coding checks → fresh Verify → Review.

Python: `environment_plan` chooses pyproject and a few conventional filenames;
`inspect_environment` and workspace preparation do not follow Makefile pip
requirements or recursive `-r/-c`. The actual spec contains only pyproject.
Its v2 preparation program installs declared groups and its fixed test helpers,
not the missing development requirements. `ruff` / `stubtest` exited 127. The
old dependency volume was disposed; installed distribution/PATH cannot be
retrospectively measured. A fresh preparation is required to prove entrypoints.

Node: the frozen recipe includes root and child package metadata but only root
in `package_roots`. `package.json` lint invokes `scripts/run` (Node resolves
`.js`), which installs child dependencies and conditionally installs global tsx
when CI is set. Verify sets CI=1. Thus merely preparing child dependencies does
not prove offline execution. Vitest cache v3 is a separately tested component;
the original eight-command native baseline remains failed.

Platform: actual OrbStack daemon and digest-pinned runner/builder images are
available after sandbox escalation. No containers were running at inspection.
Linux amd64 endpoint remains unavailable; no emulation will count as UE-T27.

## Checkpoints

- E0: passed source/call-chain audit; old event hashes inventoried, not rewritten.
- E1–E6: pending implementation and proportionate verification.
- UE-T23/24: require fresh native plans, exact human approval and final acceptance.

## E1–E4 candidate implementation (2026-10-29)

Implemented actual code in `app/environment_engine`, the existing preparation,
Verify, planning, admission and native UI paths. See
[implementation and recovery](../../environment-engine-v1.md).

E1 contracts/readiness: passed focused API/pure/security contracts. New plans pin
engine/platform; old bindings and preflight hashes remain exact when enabled.
Static admission is displayed separately from preparation and baseline evidence.

E2 Python: `D-python-03` and stricter module-probe `D-python-04` really prepare
recursive requirements/constraints and pass **3/3** unchanged checks (unit, Ruff,
stubtest). A constraint-only package is not an installation request. Early
`D-python-01/02` failures remain; actual restricted PyPI and pip probes succeeded,
so their opaque acquisition failures are not falsely called an external network
blocker. A later trusted dependency validator packaging prerequisite was fixed
using pip's bundled parser, without adding a hidden project dependency.

H-Python: original five command IDs/argv/cwds retained. `H-python-01` rejected
Ruff because its wheel uses RECORD scripts rather than console_scripts; that
probe limitation was fixed generically. `H-python-02` preserved incomplete-log
`unknown`. `H-python-03` measures **734** and **929** unit tests, both exit 0;
format/lint exit 0; stubtest really starts and exits 1 on mypy 2.4.0's read-only
`.mypy_cache` write. Verdict **environment_blocked**. All five tool entries are
available, but the baseline gate is **not passed**; coding/review count stays 0.

E3 Node: `D-node-01` kept an npm help-probe failure. `D-node-02` uses actual
manager version invocation and passes **2/2** full checks with an independently
locked tool directory. `H-node-02` resolves original pnpm exec checks then rejects
`scripts/run.js` as `unsupported_dynamic_installer`. All eight original commands
remain in its contract. No baseline is fabricated. The E3 historical gate is
**failed/unsupported**, not missing model credentials. Further static wrapper
semantics and/or an explicitly revised environment are still required.

E4: signed contract, real entrypoint receipts, full-log summary, recomputed
verdict/admission, expected Issue/control contract and no-change stop are
implemented. Source, plan, platform, receipt or exact-command changes are rejected.
This is verified at contract/API and controlled OCI boundaries; it does **not**
prove either historical native task reached Coding or Review. Historical Node
and Python cache gates remain open. Independent safety/holdout measurements do
not declare those preceding gates complete.

## Regression/platform/deployment evidence

[checks.json](checks.json): full default regression **1549 passed, 0 failed,
54 skipped**, across four isolated pytest processes. Follow-up checks cover later
module-origin and state-projection changes. Node suite: **30 contract cases + 5
actual OCI cases**, all passed. Malicious suite: **6 actual OCI cases**, all passed.
These quantities are not contribution deliveries and repeated cases are not new
platform samples. Initial failing/obsolete test run was stopped and replaced by
the complete passing run; migration fixture expectations were updated for 0039.

Native Chromium desktop/mobile displays static/unverified/blocked facts inline,
with zero browser errors. Its API is intercepted: **component evidence only**.
[macos-doctor.json](macos-doctor.json) records actual Darwin arm64/OrbStack and
restricted runner probes. Native Linux amd64 is **not_run/blocked**: no endpoint.
[history-preservation.json](history-preservation.json) confirms the original
71 + 165 events and all original ARV2 acceptance files remain hash-identical.
[credential-scan.json](credential-scan.json) records sanitized artifact scanning.

[isolated-deployment.json](isolated-deployment.json) records a healthy new API,
coordinator and Sandbox Worker, and unchanged healthy old trial service. First
simultaneous startup raced empty-schema migration; that failed database/logs are
retained. A different isolated instance was initialized serially and started.
Real model Gateway on 18081 is unavailable; no Fake provider is used. Native
Python/TypeScript journeys remain **blocked/not_run**, also requiring future
exact human plan approval and final acceptance. No approval or business patch
has been fabricated. Linux, M5/M6, ENV-G01, UXR-G01 and P5-G06 stay open.

The additional [actual source-shadow probe](source-shadow-probe.json) rejects an
archive-provided `unittest.py` without importing it. It uses no package download
and is a controlled offline security component check. A separate full
`D-python-shadow-01` stopped at acquisition and does not count as that pass.

[Manifest errata](manifest-errata.json): early controlled H manifests labelled the
context inputs hash as snapshot hash. Their records remain unchanged and are
`legacy_unverified` as a business provenance binding; actual source/command exits
are preserved. Product admission now uses and checks the actual immutable
OpportunitySnapshot hash. Subsequent canonical H manifests read it directly
from the original read-only execution record. No historical native chain is
fabricated or retroactively upgraded.

## First frozen unseen cohort — failed, never replaced

Rules were frozen at `ef33342301d870b9527b7779e38cc2ec3085c1ac` before the
[selection protocol](holdout-protocol.json), metadata-only candidate search,
[selection](holdout-selection.json), [commit refs](holdout-refs.json), source
archive reads and [exact environment commands](holdout-plans.json). All four
first measurements used the same engine source hash. Full evidence:
[holdout-results.json](holdout-results.json).

| Original unseen sample | Actual first result | Full baseline |
| --- | --- | --- |
| U-python-1, m2cgen | Legacy setup/requirements layout unsupported; specific machine reason missing | not_run |
| U-python-2, KiCAD-MCP-Server | `tool_distribution_unresolved`; inline CI tool declaration not closed | not_run |
| U-node-1, pdfcn | Actual proxy acquisition followed by offline `node_build` failure; detailed cause unproven | not_run |
| U-node-2, avataaars-generator | Materializer refused tracked `.env` (exit 23); initial observer omitted production safe source scope | not_run |

**Initial complete-baseline success rate: 0/4.** No failure was replaced with a
friendlier project. avataaars' separately recorded scope recheck used the existing
production scope function, kept both commands and engine unchanged, then failed
actual `node_tools` preparation. That is an observer maintenance episode, not
first-run success or native delivery. No business code was patched.

The missing legacy reason was reopened after this measurement. A generic fix now
emits `environment_recipe_unsupported` for new unsupported/unknown builtin
recipes, keeps all old projections exact, carries engine state through blocked
views and forbids fallback around the new contract. Because the failure informed
this improvement, all four repositories are **D for the revised engine**. Their
original frozen U manifests/results remain unchanged. A new cohort must be
selected after another code freeze. E3/E4/native/Linux gates remain open.

Canonical H rechecks use the original execution record's actual Snapshot ID/hash.
`H-python-canonical` again measures all five entries and commands, with 4/5 exits
zero and the mypy cache environment stop. `H-node-canonical` retains eight exact
commands and rejects the dynamic installer. No original approval was reused to
perform a new business action.

## Second frozen unseen cohort and rejected experiment

Rules were frozen at `c275c55d39efcca813bcf69019130a4d82d1d948` before
[cohort 2 selection](holdout-cohort2-protocol.json). All four measurements kept
that production source hash unchanged. [Results](holdout-cohort2-results.json):

| Sample | Actual first result | Full baseline |
| --- | --- | --- |
| U2-python-1, tesserocr | `project_metadata_missing`; native OCR/build metadata outside current contract | not_run |
| U2-python-2, art | `environment_recipe_unsupported`; legacy setup layout | not_run |
| U2-node-1, dynamouse | Actual preparation failed, exit 1; detailed cause not proven | not_run |
| U2-node-2, ts-prune | `unsupported_dynamic_installer`; subprocess wrapper | not_run |

**Cumulative first complete-baseline success: 0/8**, no replacement, no model
invocations, no native deliveries. Safe unsupported results do not pass UE-T19–22.
These independent measurements do not claim the preceding E3/E4 gates completed.

A generic read-only stubtest compatibility candidate constrained a declared mypy
to 1.15.0 without changing any source/check. `D-python-profile-01` passed 3/3;
`H-python-profile-01` executed all five, but stubtest failed on missing
`_typeshed.Viewable`. **The candidate was rejected and removed from production
code**, rather than shipping a blanket downgrade. Its exact source patch,
[decision](readonly-profile-experiment.json), graph, entrypoints and failure
remain for audit. Original cache failure and approvals remain unchanged. This is
maintenance experimentation, not a native or unseen success.

Subsequent hardening rejects hidden package-script help/version/echo leaves,
requires positive execution evidence for nested test runners, rejects incomplete
command statuses and unknown adapter versions, and preserves the exact old
TaskSpec wire format when no environment evidence exists. Actual offline OCI
[help](nested-help-probe.json), [version](nested-version-probe.json) and
[source-origin](source-shadow-probe-final.json) guards passed 3/3. Focused
regression passed 64/64. Full regression is recorded separately when complete.

Model credential use was rejected by automatic approval review before execution:
starting a new Gateway with the existing NVIDIA secret and upstream required
explicit credential/destination authorization. No secret was read, no Gateway
started and no workaround attempted. The concrete [proposal](gateway-proposal.json)
is pending user authorization; plan/execution/final acceptance remain separate.

## Literal CI adapter extension and final guard checks

`python-toolchain-v2` adds declared inline Make/CI requirements, quoted PEP 508
versions and separate `-c` inputs with exact file/hash/line provenance. It never
executes a workflow to discover dependencies. Unknown shell syntax/options,
nonlocal editable installs and ambiguous working directories reject. The
already-selected frozen local project is not mistaken for an external package.
Old contract v1 remains readable; changed parser/graph hashes invalidate old
execution previews and require a new exact authorization.

`D-python-ci-01` uses a different layout with inline mypy plus requirements and
constraint files; actual preparation, entrypoints and unchanged baseline passed
3/3. `H-python-ci-parser-01` starts all five original commands; 4 exit zero,
current mypy still fails the read-only cache check. No blanket version constraint
is active. The source-shadow, wrong-target-platform, nested help/version and
zero-test guards pass **5 actual offline OCI cases**. These are controlled
boundary checks, not native or Linux amd64 acceptance. A successful negative
platform probe on arm64 does not count as running the engine on amd64.

The full baseline-guard regression at `79e039e` passed **1557**, skipped **57**,
failed **0**. The later CI extension has a separate focused boundary report in
checks.json; final broad verification is recorded separately. A command with an
incorrect test filename collected zero tests and failed before running anything;
it was corrected, not counted as verification.

## Third frozen exploratory cohort

The CI adapter was frozen at `b52a81e390d90ed69140a44b4cad89ccf7e2f47e`
before [protocol](holdout-cohort3-protocol.json), metadata search and first source
reads. Only one license-eligible Node repository was returned; no query expansion
or replacement followed. All three first measurements kept the same production
source hash. [Results](holdout-cohort3-results.json): testinfra actually reached
project build and failed (`environment_build_failed`, detailed cause unproven);
pytest-flask lacked supported project metadata; ts-xor's complete build/test
script chain was unsupported. **Cumulative first full baseline success remains
0/11**, not 0/8 with three conveniently omitted failures. The subsequent general
cache work is a new engine revision; these repositories are now known development
inputs for future selection, while their initial U records stay untouched.

## Bounded Python data cache — measured implementation

The cache primitive first failed to mount over an absent directory (exit 125).
A separate fixed supervisor creating an empty mountpoint made the precise tmpfs
mount possible; [measurement](cache-mount-probe.json) proves source `ro`, cache
`noexec`, source write EROFS and direct cache execution EACCES. This is a primitive
measurement, not native delivery or a full engine pass.

The first candidate supported only mypy 2.4.0. H runs failed its version guard;
[redacted diagnostic](cache-tool-version-diagnostic.json) preserves that failure.
The actual earlier H CI receipt and later H measurement show mypy 2.4.1. That
version was separately measured, not silently treated as compatible. Current
`python-toolchain-v4` freezes `python-data-cache-v2` with the exact two measured
versions; it never changes source requirements or installs a guessed CLI package.
Actual installed metadata, immutable dependency inventory, exact plan and
contract hashes bind the cache receipt. Unknown versions stop, not fallback.

`D-python-cache-repeat-02` (2.4.0) and `D-python-cache-241` (2.4.1) each pass all
3 commands in **two fresh actual Verify containers**. Their unit checks assert
cache starts empty, source writes fail EROFS, cache execution fails EACCES and
mount flags contain noexec. Cache data stays on the bounded 16 MiB data mount;
source/dependency volumes stay read-only. Source inventories agree before/after.
`D-python-cache-01` preceded the repeated-run harness and counts as one run only.

`H-python-cache-04` preserves the original five IDs/argv/cwds and actual snapshot
binding: test_more 734, test_all 929, format, lint and stubtest all exit 0. **5/5
historical OCI regression passed**, native Coding/Review remains 0. Original
failures, approval/event hashes and all rejected candidates remain unchanged.
A final frozen-code recheck is recorded separately; no past result is replaced.

Current complete default regression: **1567 passed, 60 skipped, 0 failed**.
Node regression on this cache implementation: **30 contracts + 5 actual OCI**,
all passed. The cache UI browser check is intercepted API/component evidence.
The extra actual assertion/control OCI check demonstrates preexisting failure
versus an exact expected-reproduction contract; it creates no human consent.
No component, fake model, plan save or cache fix counts as native delivery.

E3 H Node and therefore the full E4 historical gate remain unpassed. E5's real
cross-project success minimum is failed; native journeys wait for actual model
credential authorization plus separate exact human plan/final approval. E6 has
Mac evidence and an isolated deployment, but Linux amd64 is blocked/not_run.
These are distinct failures/blockers, not a claim that credentials explain all
unfinished engineering work.

## Final frozen-code rechecks and fourth unseen cohort

`H-python-cache-final` and `D-python-cache-final` use the exact final frozen
engine. H again passes the original five commands; D passes all three checks
in two fresh real Verify containers. [Source verification](frozen-source-verification.json)
confirms the same engine revision and source hash across H, D and all four U4
measurements. No production engine changes occurred during selection/measurement.

The [fourth protocol](holdout-cohort4-protocol.json) selected two real public
Python libraries and two TypeScript utility projects before source inspection.
[Results](holdout-cohort4-results.json):

| Sample | First outcome | Complete baseline |
| --- | --- | --- |
| U4-python-1, openapi-spec-validator | `tool_distribution_unresolved`; Poetry dev group not closed by current adapter | not_run |
| U4-python-2, colorgram.py | `pending_scope`; legacy Python root plus nested Node test package requires an explicit supported scope | not_run |
| U4-node-1, signality | `environment_evidence_budget`; full declared source graph exceeds inspection bound | not_run |
| U4-node-2, super-utils-plus | `unsupported_dynamic_installer`; JS subprocess build wrapper not statically admitted | not_run |

**Current frozen cohort 0/4; cumulative first full-baseline success 0/15.** U4
performed real OCI source inspection and static graph evaluation, but no full
preparation/baseline. Those rejections are never counted as successful baselines.
No failure is replaced or excused as missing model credentials. Generic legacy/
Poetry/JS-wrapper support and scope handling remain real engineering gaps.

Final complete default regression on the frozen code: **1570 passed, 60 skipped,
0 failed** (four XML reports and hashes in checks.json). Node regression is
30 contracts plus 5 actual OCI cases, all passed. The six real malicious cases
were rerun on the cache implementation and passed. Additional six real engine
boundary cases cover source shadowing, wrong target, help/version, zero tests
and assertion/control classification. These categories remain separate from
native journeys and are not summed as contribution deliveries.

[Final audit](final-audit.json) rechecks 74 original ARV2 files, all 71 + 165 old
events and the exact original 5 + 8 command hashes; all unchanged. Sanitized
UPEVE JSON and the retained rejected patch have zero credential findings. No
human approval, business patch, model call or third-party write was fabricated.
`compileall`, JavaScript syntax and diff whitespace checks pass. [Deployment](isolated-deployment.json)
confirms the frozen engine in the separate 18090 API/coordinator/Sandbox Worker,
SQLite integrity and healthy old trial endpoint; active jobs 0. Model Provider
is blocked on explicit credential/upstream authorization, not replaced with a
Fake. A current full native crash/restart journey and Linux amd64 are not proven.

The branch contains local stage commits only. UPEVE v1, E3–E6, original Runtime
v2 M5/M6 and Phase 5 are **not declared complete**. Next current task remains
UPEVE-E04: a supported generic Node semantic boundary/environment recipe that
preserves all required checks. A changed exact recipe requires a fresh child
plan and human authorization; Verify networking or code permissions stay closed.

## MiniMax selection follow-up

The user explicitly selected MiniMax after the NVIDIA proposal. That is accepted
as the model choice, without asking for the same confirmation again. The original
rejected NVIDIA proposal is retained as history and was never activated.
[MiniMax proposal](gateway-proposal-minimax.json) selects the repository's native
`minimax` / `MiniMax-M3` preset and fixed domestic endpoint. No NVIDIA credential
or upstream is enabled in the new launch configuration.

[Live readiness](minimax-readiness.json) confirms API 18090, Gateway 18091,
coordinator, Sandbox Worker and Provider Worker are running. Only the Gateway
will hold the upstream key; API gets its distinct management key, Provider its
short-lived-task signing key, and Sandbox neither. The old trial stays healthy.
[Actual UI check](minimax-settings-browser.json) proves the password input is
enabled, official endpoint locked, WebCrypto available and zero browser errors.
The authenticated Gateway management query succeeds. Offline configuration
contracts passed 44, with 1 opt-in skip. No live upstream model request or native
contribution success is claimed.

Historical readiness before the user saved settings (superseded by the next
checkpoint): the accessible encrypted stores, model configurations and MiniMax environment
fields contain no MiniMax key. The user must add a connection through
`http://127.0.0.1:18090/#/settings`; no key is requested in chat. Model profile
binding and actual connection testing wait for that input. This is a missing
credential, not pending model-choice consent; exact plan/execution/final approval
and all remaining E3–E6 gates remain separate.

## User-saved MiniMax connection and E3 cwd graph follow-up

The user saved the connection/Profile and four-stage defaults in the isolated
product. [Live connection evidence](minimax-live-connection.json) verifies the
immutable Profile hash and two actual succeeded Jobs: structured response in
13,863 ms and model catalog retrieval. The agent did not change the user's
Profile, read the key, or duplicate these model calls. Connection readiness is
now passed; full native contribution success remains 0.

`node-toolchain-v2` statically resolves literal shell `cd` edges in package
scripts and stores typed leaf argv/cwd separately from display strings. It does
not probe `cd` as an executable. Manager pre/post lifecycle scripts each start in
the manager's original cwd; independently locked child tools are prepared before
Verify. Hash/space directory names retain exact arguments. Escapes, linked dirs,
expansion and unknown directories fail closed. Missing frozen package evidence
continues the bounded read path, rather than being swallowed as a script error.

The new optional leaf field preserves old contract serialization. A regression
check exposed the pre-existing optional required_module=null expansion in an
early controlled D record; preserving absent-field identity restores its exact
original hash, without editing that record or promoting it to native provenance.
The first test failure remains in `/tmp/upeve-node-cwd-focused.xml`; corrected
focused and complete regression pass.

`D-node-cwd-01` passes 2/2 unchanged root checks in two fresh actual OCI Verify
runs, with pre/post scripts and nested locked dependencies under a path containing
spaces and #. [Empty cd check](node-cwd-cd-only.json) and
[nested version](node-cwd-nested-version.json) both exit 0 in real OCI and are
classified unknown, never passed. Eight actual engine boundary cases pass.
Default regression is **1584 passed, 62 skipped, 0 failed**; opt-in skips are not
OCI passes. These are controlled platform checks, not native deliveries. The
historical installer still requires a supported generic boundary/recipe; E3 and
dependent gates remain open. The earlier 0/15 holdout first outcomes stay intact.

## Fresh native planning on the frozen Node v2 engine

[Protocol](native-planning-protocol.json), [candidate metadata](native-candidate-metadata.json),
[eligibility](native-eligible-repositories.json) and [Issue selection](native-issue-selection.json)
retain metadata-only search history, exclusions and deterministic ordering.
[Targets](native-targets.json) freeze trading-ig #374 and std-env #174 before any
native message or source read. Both were open, unassigned Issues at selection;
observed branch refs are not asserted as product-verified provenance.

[Native results](native-planning-results.json) retain actual first outcomes on
isolated 18090. Python discovery produced a real Snapshot/Task; MiniMax made four
calls (13,999 input / 1,350 output tokens), classified the request observe-only,
repeated context reads and failed with `conversation_decision_invalid`. Both
rejected outputs and all 36 first-attempt events are saved. No source archive,
plan, baseline, Coding or Review was produced. TypeScript discovery completed
with metadata unavailable after GitHub HTTP403 rate-limit responses; metadata
and license filters excluded its Snapshot. This does not prove its license is
absent. No Task/model plan was created for it, and no target was replaced.

The observer initially stopped polling at 180 seconds, then incorrectly looked
for the earlier Python recommendation in the newer scan's feed. Its status is
reconciled from actual persisted Jobs/Snapshot/events without changing the
database. These are two native selection outcomes, not two extra OCI baseline
measurements; the prior 0/15 environment denominator is unchanged.

[Routing maintenance](native-routing-maintenance.json) clarifies that requesting
an investigation/plan is a command for read-only preparation, even when
implementation is forbidden. It changes no parser, output coercion, budget,
capability or exact approval boundary. The 123 focused boundary tests pass;
the complete default regression passes 1584 / 62 skipped / 0 failed. Affected targets become D for subsequent revised-runtime
measurements; original first outcomes remain frozen.

An automatic approval review could not run a later read-only localhost status
query because its service usage limit was exhausted. The action was not
executed; this was not a safety rejection. No retry/bypass was attempted.
After the reported recovery time, a fresh reviewed health query was approved:
isolated and old trial services were both healthy. That temporary review-service
block is resolved. The maintenance revision is deployed with five owned roles
restarted from zero active Jobs; isolated service health passed. Repeated GitHub anonymous limits still block repository metadata; a
dedicated read-only discovery token was requested without requesting it in chat.
[Local audit](native-readiness-audit.json) checks original file/event hashes and
sanitized evidence without reading model credentials. One public Issue's prose
matched the credential detector, was redacted with its original hash retained,
and the scan passed; the initial failing scan is also retained.

[Live routing retest](native-python-routing-retest.json) sends the unchanged
original Python request as a new message, without editing the old observe-only
classification. MiniMax uses two calls, records `effect=command` and dispatches
the real read-only planning archive workflow. Lifetime usage remains 6 calls /
20,901 input / 1,928 output tokens; no budget reset or Profile change. This is
a passed D routing component, not a native delivery. Source acquisition then
terminates `planning_archive_failed` after GitHub HTTP403; no Plan/approval/
Coding/Verify/Review exists. Both stages have no current plan to approve.
[Same Node target recheck](native-node-maintenance-rescan.json) is still excluded
after rate-limited metadata acquisition; no sample replacement.

[Historical forced-installer source proof](H-node-forced-install-boundary.json)
separates the child node_modules existence check from the later unconditional
CI global install. Merely extending wrapper parsing or preinstalling child
dependencies cannot make that branch offline. E3 stays failed under the fixed
source/CI/no-install Verify contract, with all eight commands retained.

## User-selected shared GitHub connection

The user now wants one Classic `public_repo` token for discovery/source and
later Draft PRs; the separate read-only-file request is superseded. The existing
encrypted Publisher settings form now displays **GitHub 连接** and shared-read
status. [Architecture/recovery](../../github-shared-connection.md) documents
separate token ownership and a distinct GET-only HMAC capability. No raw GitHub
token enters API/coordinator, Provider or Sandbox environments.

[Checks](shared-github-checks.json): 21 new proxy cases plus existing Publisher
contracts pass; the complete default selection is 1605 passed / 62 skipped.
An old static-asset-version assertion failed, was updated, and its entire shard
was rerun. Chromium desktop/mobile tests verify real encryption and the native
form using a deterministic fake account probe; a teardown route race was fixed
and the rerun exits 0. These are component checks, not native deliveries.

[Deployment](shared-github-deployment.json) is actual OrbStack arm64: a new
nonroot Publisher container with read-only root and two narrow mounts, no host
HOME/Docker/model credential access. Five isolated roles restarted from idle;
SQLite backup preserves every table count and 57 event IDs/hashes, and original
trial data remains available. Current storage is under `business/`; formal and
older 18080 services are untouched. Live API reports settings available and
`discovery_shared=true`; the subsequent actual account probe and authenticated
reads are recorded below. No external write or native success is inferred.

## Authenticated source access and planning context maintenance

The user saved the new connection in the native page. The actual durable account
probe succeeded; reported scope is `repo` (not the narrower minimum previously
recommended). Only Publisher holds it, and the relay still enforces public GETs.
[Resume record](authenticated-resume.json) preserves the two original targets,
original Python task/budget and actual refreshed Node Snapshot/task. Actual
repository/commit/archive access now succeeds, including an unauthenticated
codeload redirect. No credentials were read by Codex or passed to the model or
Sandbox. This resolves access readiness, not contribution acceptance.

[Measured planning failures](authenticated-planning-failures.json) retain the
Python context-ceiling stop and TypeScript conversation read-budget stop before
any code change. Explicit TypeScript planning later returned a no-bug narrative
with admitted source gaps and clarification choices; it is unverified, no Plan,
no business patch, and not a native pass.

Generic v2 compaction now indexes redundant copies of captured files, including
focus files already available by hash, and removes duplicated capture-index
metadata. Exact requested pages/current plan/rules/commands remain complete.
Read results add source_path/source_complete: only complete returned bytes that
match the captured file hash prove a complete capture; partial wrappers alone
do not mean source is missing, and query excerpts/legacy skeletons never prove a
full file. No budget or safety limit is increased; legacy checkpoints remain
unchanged. [Checks](planning-page-checks.json): 75 focused cases and complete
1607 passed / 62 skipped / 0 failed regression pass.

[Actual frozen-payload clone replay](planning-page-shape-replay.json) reproduces
the old pre-provider rejection. New code delivers the same three pages and
unchanged request at 74,131 bytes, below the original 96,000-byte hard ceiling.
The diagnostic provider deliberately stops there; no real model runs in that
check and no live database is modified. It is component evidence only.


## Real planning outcomes and preserved source-read history

[448f43f live retests](planning-page-live-retest.json) pass actual source/context
admission but produce no Plans: Python ends `minimax_completion_invalid` after
one bounded transport retry (19 lifetime calls); TypeScript ends in unverified
no-change discussion/clarification (11 lifetime calls). No baseline, code,
Review or human acceptance ran. Failed-call upstream usage remains unknown,
not assumed free. This is D evidence, preserving both original first outcomes.

Frozen [N6 protocol](native-cohort6-protocol.json) and
[metadata record](native-cohort6-selection.json) contain no eligible samples.
Frozen [N7 protocol](native-cohort7-protocol.json) and
[selection](native-cohort7-selection.json) select only one unseen Python Issue;
no TypeScript candidate meets the same bounds. Source was not read before
selection, and production code stayed frozen during its first measurement.
The existing native read/plan control invokes actual MiniMax seven times, then
stops for read-limit/material clarification with no Plan.
[First result](native-cohort7-results.json) retains all 37 event IDs/hashes.
Natural-language routing is not claimed. Cumulative native first targets are
0/3 deliveries; the separate environment first-baseline denominator stays 0/15.
Empty metadata searches are not extra failed baseline executions.

[Actual checkpoint diagnosis](read-history-diagnosis.json): eight of eleven
previously returned source responses disappear from the final prompt because
the job cache is replaced each round. The generic fix retains/deduplicates
actual tool results within the bounded v2 job lineage and through verified
same-archive capture/restart. Older text can be indexed by a retrievable hash;
metadata alone never substitutes for exact source. No model/output coercion,
extra read round, budget increase, repository exception or business fix.
Legacy jobs remain compatible, and no persistent schema migration is needed.
N7 first failure stays unchanged and subsequent tests of it are D.

[Checks](read-history-checks.json): 31 focused tests and full **1609 passed /
62 skipped / 0 failed**, compileall and diff check. These include actual worker
rounds/capture/restart with fake models and an over-target response history
roundtrip, so they are component evidence. [History audit](read-history-audit.json)
and [credential audit](read-history-security-audit.json) preserve original ARV2
74 files/71+165 events, N5 first 36 and N7 first 37 events. No credentials were
read, no third-party writes or operator business patches. E3/H Node, native
approvals/Coding/Verify/Review/final acceptance and Linux amd64 remain open.


[Deployment of b5738de](read-history-deployment.json) restarts the five owned
roles from idle; API/Gateway/Publisher and prior trial health pass. The Publisher
image is unchanged because this maintenance changes only context/planner code.
[One same-goal D retest](read-history-live-retest.json) makes five real MiniMax
calls (12 lifetime, 236,952 input / 5,775 output tokens), retains prior read
metadata/references and stays at 48,323–75,680 bytes. Source binding and limits
match the pre-retest record. It ends with material semantics/evidence questions,
no Plan; proposed reproduction claims remain unverified. No Coding, baseline,
Verify, Review, approval or delivery is inferred.


The follow-up allocation correction retains prior page text until 80 KB total
prompt size when pinned current evidence already exceeds the 48 KB soft target.
This leaves 16 KB below the unchanged 96 KB hard admission limit; oversized
requests still reject, and source compaction/model Profile/read rounds/lifetime
budgets/approval rules stay unchanged. It prevents indexing every old page in
an already over-soft-target prompt. It does not resolve the material Issue
semantics questions or prove an executed reproduction.

Latest allocation verification: 32 focused cases; full **1610 passed / 62 skipped / 0 failed**. [Checks](history-headroom-checks.json). No additional stage gate passed.


## Latest isolated checkpoint: b9bf5de

[Final verification](history-headroom-checks.json): **1610 passed / 62 skipped /
0 failed**, 32 focused cases, compileall and diff checks pass.
[Deployment](history-headroom-deployment.json) is healthy with five owned roles
restarted from idle and no formal-data/Publisher change.

[Final same-goal D model retest](history-headroom-live-retest.json) uses six real
MiniMax calls, 18 lifetime / 388,140 input / 17,764 output tokens. Prior exact
pages are inlined where they fit, otherwise referenced; maximum prompt size is
78,699 bytes below the original 96,000-byte ceiling. Frozen source and budget
limits match. Three proposed outputs fail strict `extra_forbidden`/`missing`
envelope validation, ending at the existing repair limit without a Plan. No
invented defaults, schema coercion or extra budget is used to manufacture a pass.
All Jobs completing is not Plan success. Native deliveries remain 0/3; no exact
approval/Coding/Baseline/Verify/Review/final acceptance is inferred.

[Final audit](history-headroom-audit.json) checks original ARV2 74 files and
71+165 events, N5 first 36 and N7 first 37 events, credential patterns and zero
active Jobs. E0–E2 pass; E3/H Node remains failed/unsupported, E4 partial, E5
native/generalization gates failed, E6/Linux amd64 blocked/not_run. Current
specialty task remains UPEVE-E04. No Push, PR or third-party write occurred.


## 2026-10-30: baseline field schema mismatch and diagnostic correction

The October 29 final rejection was previously described as envelope validation.
[Read-only replay](baseline-schema-replay.json) of the actual pending plan shows
the envelope was not the root cause: provider/API described
`baseline_expectation` as an arbitrary dict, but the domain required the strict
`BaselineExpectation` reproduction contract. Model `expected`/`notes` labels
were rejected; diagnostics suppressed all nested field locations as `response`,
so bounded repairs did not receive actionable feedback. First outcomes, budgets
and historical events remain unchanged. The replay uses no actual model or live
DB mutation and is not native success.

Provider/API now use the same strict existing type, with positive checks normally
omitting/nulling the field. Exact Issue URL/test-node/failure/exit-code binding
and final all-checks passing remain required. Safe diagnostics expose known
nested field prefixes only; arbitrary keys, raw values and canaries never enter
responses/events. Regression-first implementation steps are distinct from the
same checks applied to the unchanged baseline and final workspace. The native
UI's JSON commands retain their existing wire; valid legacy command hashes and
forward 0039 storage semantics are unchanged, with no new migration.

42 focused contracts pass: typed provider schema, real malformed shape, valid
wire/hash compatibility, strict exact-target refusal, worker repair accounting
and manual API/canary boundaries. [Full default regression](baseline-schema-checks.json)
is **1616 passed / 62 skipped / 0 failed**, compileall/diff checks pass. Isolated
redeploy and actual same-target D model validation follow. E3 remains the current gate;
no fake/controlled test is counted as native success.


[Deployment of 9d5094f](baseline-schema-deployment.json) is healthy and formal
data is untouched. [Actual D retest](baseline-schema-live-retest.json) makes two
real calls (20 lifetime), then fails `planning_outcome_missing` before saving or
validating a new Plan. The prior field failure is not reached, so the model
improvement is unproven. The terminal failed Job is distinct from the preceding
assistant read request, which must not be mistaken for the final outcome.

For new v2 requests only, empty outcomes now use the existing two bounded schema
repairs. No response is filled/coerced, all failure/usage receipts persist, and
unchanged lifetime budgets still apply. Repeated emptiness fails after at most
three calls; a valid subsequent proposal remains subject to all plan/security
checks. Legacy immediate rejection is preserved. 44 focused contracts and
[full regression](empty-outcome-checks.json) **1618 passed / 62 skipped / 0 failed**
pass. A same-target D measurement follows, with no native pass inferred and
no approval created.


## Frozen N8 and bounded foundation evidence

[61b559b actual N7 D retest](empty-outcome-live-retest.json) recovers one empty
response within existing repairs, then reaches material semantics questions
without a Plan. Six actual calls / 26 lifetime, no limit reset or source change.
This proves only the empty-repair component, not a Baseline Plan/schema/native
success.

[N8 protocol](native-cohort8-protocol.json) freezes code/selection rules before
[25 public Issue/repository metadata candidates](native-cohort8-selection.json).
One TypeScript Issue qualifies; bounds and no-replacement rules are retained.
[Actual first result](native-cohort8-results.json): real product Scan/Snapshot/
Task/native read-plan control, exact observed source SHA and frozen MiniMax
Profile, seven model calls, four read rounds exhausted and no Plan. Natural
language routing is not claimed. All first events remain immutable; N8 becomes
D after maintenance. Native first deliveries remain 0/4, environment baselines
remain separately 0/15. No operator business patch, approval or external write.

The first prompt indexed even tiny contribution rules/manifests, spending read
rounds to recover basic declarations. V2 now keeps exact short foundation files
as untrusted `evidence.entries`: at most 12 KB total / 6 KB per entry, selected
deterministically and only when raw content matches the original hash. It does
not promote repository instructions to system rules or expand any hard bound.
Large/oversized/legacy skeleton files remain indexed. [Clone replay](foundation-replay.json)
uses the actual frozen native capture in an isolated DB/artifact copy: all five
sampled foundation files inline at 62,094 bytes instead of four indexed at
56,285 bytes. There are zero actual model calls/live mutations; component only.
42 focused contracts and [full regression](foundation-checks.json) **1620 passed /
62 skipped / 0 failed** pass. Isolated deployment and D model measurement
follow. No additional gate is passed.


## Provider-native reasoning hypothesis, within existing limits

[Actual foundation D retest](foundation-live-retest.json) confirms five complete
short declarations in all five actual model prompts, maximum 82,784 bytes. It
still exhausts reference/read limits after five calls / 12 lifetime, no Plan.
This is a projection component improvement, not native delivery.

[Provider policy notes](minimax-adaptive-policy.json) link primary MiniMax API
docs: M3 defaults to adaptive thinking, while our forced-tool adapter disables
it. A versioned MiniMax adapter now retains the native default with identical
model/Profile, forced structured output, 8192 completion cap including thinking,
180-second timeout and lifetime limits. Total reported completion usage remains
counted; hidden reasoning is not saved/exposed. No native interleaved-thinking
transcript is introduced into provider-neutral business state. Transport no
longer replays ambiguous completion POSTs; bounded idempotent GET retries remain.
Old paired/unpaired identities/history remain distinct from the new versions.

87 focused provider/analysis-wire/NVIDIA contracts pass, including forced-tool
parameters, hidden-reasoning separation/full completion usage and single-POST
versus safe GET retry behavior. [Full regression](adaptive-checks.json) passes
**1624 / 62 skipped / 0 failed**. A bounded N8 D test follows; native improvement
is unproven and original first outcomes remain unchanged.


## Actual adaptive failure and failure-usage accounting

[Actual same-target D result](adaptive-live-retest.json) makes five additional
MiniMax calls, 17 lifetime, then fails `minimax_completion_truncated` at the frozen
8192 cap. The last job is `038eb98e-3b06-4d88-87f9-d23eda9ff214`; the preceding
assistant read request is not the terminal result. Known reported lifetime usage
is 400,325 input / 21,859 output. Last-call usage was discarded by the old error
path and remains unknown. There is no Plan, approval, baseline, Coding, Verify,
Review or final acceptance. Native first deliveries stay 0/4; the separate
original environment denominator stays 0/15. N8 remains D after maintenance.

The Provider error contract now carries validated reported usage through a
truncation/invalid completion without accepting its partial result. Planning
appends a failure receipt; terminal calls without one hold a separate allowance
at the original call bounds. Known usage and unknown reserves remain distinct in
API, native UI and budget CAS. The actual invocation's output cap is clamped to
the remaining task allowance. There is no automatic completion POST replay,
resource increase, approval or alteration of historical events. Old unknown
calls without an output bound hold the full task allowance, fail closed and
cannot be assumed free. This adds journal/response fields without a SQL schema
change; restart and rollback rules are in the architecture contract.

101 focused contracts and the offline browser component pass (desktop/mobile,
unknown usage, underfunded edits, stale/busy state, idempotent retries). They are
zero actual-model/zero real-write component checks. Full regression, preservation
audit and isolated redeployment are separate receipts; no stage gate is closed.
A 16K planning-only Profile is only a proposal requiring a new resource decision;
the saved MiniMax Profile and all defaults remain at 8192 until then. Increasing
that cap would not retroactively pass an original U attempt or prove delivery.

Final [regression receipt](budget-reservation-checks.json): **1630 passed / 62 skipped / 0 failed**. One old text assertion failed in the first run; the original failed XML is retained, its entire shard passed after the text expectation update. [Preservation audit](budget-reservation-audit.json) retains all 74 historical files, 71/165 original historical events, N5/N7/N8 first 36/37/40 events and zero execution approvals. Canary scan passes. These do not pass native/generalization gates.

[32fdeb5 隔离部署](budget-reservation-deployment.json)确认五个自有角色在零活动 Job 时重启，18090/18091/18092/18080 健康；正式数据未动。[真实任务只读观察](budget-reservation-live.json)确认已知用量与未知预留分别展示，手机页面无溢出，无新模型调用或审批。[16K 规划提案](planning-cap-proposal.md)已具体化但未应用，须由用户另行决定；当前原 Profile 和默认仍为 8192。


## User-approved task-only 16K retest and shared source hashes

The user explicitly approved the exact proposal. [Resource consent](planning-cap-approval.json)
binds its hash; [actual API/run receipt](planning-cap-live-retest.json) verifies
new Profile hash, task-only planning binding and unchanged defaults/old Profile/
other stages/timeout/task totals. Consent does not approve implementation or
publication. A launcher initially expected HTTP200 instead of normal HTTP202;
the exact existing Job was reconciled without repeating the request. The earlier
automatic tool approval timeout executed nothing; one allowed retry succeeded.

Actual result: **one** additional MiniMax call, 24,517 input / 396 output. It then
fails on context admission before the second call, no Plan. Current totals are
18 calls / 424,842 input / 22,255 output; original unknown reserves remain
450,000 input / 8,192 output. Last terminal Job is
`27c7567f-3e99-415c-b7e3-0c8ad6b2d21f`, not the preceding assistant read request.
No Baseline/Coding/Verify/Review or human execution approval exists. The authorized
single request is finished; no further actual model calls are made this round.

[Clone diagnosis](current-read-diagnosis.json) reproduces 96,836 bytes, including
32 source hashes already carried by evidence entries but repeated in the model's
reference directory. V2 now shares those hash fields by matching reference ID;
full checkpoint manifests still contain and validate every hash. All explicit
current reads, rules, inputs and plan fields stay exact. V1 uses its old wire.
[Before/after clone replay](shared-hash-replay.json) keeps all four exact read
results and all 44 full reference records unchanged, reduces the model prompt
to 94,361 bytes, and reaches an intentional diagnostic stop without a real model.
This is a component admission improvement only. The source/approval/security/
96 KB hard cap/read rounds/lifetime limits remain in force. Native effectiveness
is **unverified**, and original 0/4 native / 0/15 environment outcomes stay failed.

Final [shared-hash regression](shared-hash-checks.json): **1632 passed / 62 skipped / 0 failed**, plus 62 focused contracts. Compileall and diff checks pass. [Preservation/resource audit](shared-hash-audit.json) verifies all historical files/first events, exact approved new Profile, unchanged original/defaults/other stages, one actual call and zero execution approvals. No acceptance gate is promoted.

[918bdf5 deployment](shared-hash-deployment.json) restarts the five owned isolated roles with zero active Jobs; API/Gateway/Publisher/old-trial health passes. The approved 16K task Profile survives restart; original defaults and other stages remain unchanged. No actual model request follows the terminal retest. The [bounded follow-up proposal](planning-readonly-followup-proposal.json) is prepared but not dispatched: at most two read-only planning requests and fourteen additional model calls within the existing remaining task allowance (1,125,158 input / 69,553 output), with the same 16K/180-second per-call settings. It stops for a valid Plan, material clarification, policy/source mismatch, unrecoverable failure or exhausted budget. This broadens the prior one-request authorization only if the user explicitly agrees; it creates no execution or publication consent.


## Bounded follow-up authorized by the user

The user explicitly requests continued acceptance. [Consent](readonly-followup-approval.json) binds the exact follow-up proposal after all frozen hashes revalidate. [Request 1](readonly-followup-1.json) runs through the native product API with the unchanged goal/source/Profile and original lifetime limits; no approval for preparation, Coding or publication is inferred. At most two read-only requests and fourteen further model calls are allowed. First outcomes stay immutable, and this is D maintenance. Result pending; no native success is claimed.


## First bounded follow-up result and safe context-field repair

[Actual request 1](readonly-followup-1.json) makes five MiniMax calls and terminates `planning_output_invalid` (`too_long`, only `fields: response`), with no Plan. Maximum actual prompt is 92,432 bytes, read history is retained; source-hash sharing is empirically admitted, but no native delivery is proven. Lifetime is 23 calls / 563,657 input / 27,952 output. Original unknown reserves stay separate. No execution approval, baseline, Coding or Review.

[Schema replay](read-count-diagnostic-replay.json) proves missing allowlisted context fields hide diagnostics: a five-intent context array produces the same old safe error shape. The actual rejected raw native output is unavailable, so its exact field/count is **not proven**. This is not native response reconstruction. New diagnostics name known fields without copying values or arbitrary keys. Only v2 overlong read_context arrays gain access to the existing two schema repairs; rejected intents never execute. At zero remaining read rounds the repair demands a Plan or material question, with no additional reads. No bound or budget is increased, legacy behavior stays unchanged, and other oversized fields still fail immediately. Contract tests cover recovery, exhaustion, zero reads, canaries, receipt accounting and legacy behavior. Full checks/deployment precede request 2 under the same existing consent.

[Read-count checks](read-count-checks.json): **1638 passed / 62 skipped / 0 failed**, plus 50 focused contracts; compileall/diff checks pass. The repair/read bounds remain fixed, default tests use no real models. [Audit](read-count-audit.json) preserves historical files/events, exact resource consent and first follow-up failure. No phase gate is promoted.


## Context delivery and continuation of the same second request

[Request 2 initial failure](readonly-followup-2-context-failure.json) retains the original terminal result: three actual calls, then context admission fails before the fourth, no Plan. No third fresh request is started. The latest user “继续” requests continuation of the same frozen lineage after a generic fix.

[Actual-payload clone](delivery-replay.json) reproduces 99,905 bytes before delivery control, and admits at 94,544 after it. Only newly gathered pages are bounded before model delivery. The old source remains intact, all four returned texts are truthful UTF-8 prefixes, partial/next_offset/source_complete and continuation query/line selectors describe what was actually delivered. All existing rules and the 59 authoritative reference records remain; one fixed rule explains the new delivery metadata. Already issued evidence is never silently clipped. Previews do not write artifacts or events, and only the accepted final prompt creates a checkpoint. Mixed archive/context reads keep unseen drafts separate from actual prior reads. This clone uses a diagnostic Fake hook with zero real model calls; it is not native success.

Typed context-admission failures and the exact legacy safe error may resume only on explicit user continue, under current source/state/model checks. Round/repair counts, cache references and lifetime usage remain frozen. Tests cover pause, source/model changes, exhausted budget, UTF-8 offsets, honest incompleteness, pure previews, ordinary/mixed reads, restart and no execution consent. The next action is continuation of request 2 at round 3, with at most four remaining calls under the existing grant.

[Delivery checks](delivery-checks.json): **1650 passed / 62 skipped / 0 failed**, plus 80 focused contracts. Both ordinary and mixed-read restart paths preserve truthful partials and never promote an unseen draft to read history. Earlier fixture validation correctly rejected a continuation after a newly saved Plan changed its source binding; the final focused scenario keeps the source unchanged and checks the matching continuation. No stale guard was weakened.


## Actual same-lineage resume: admission passes, native delivery still fails

[Native continue receipt](readonly-followup-2-resume.json) proves the same failed payload/hash at read round 3 / repair 0 is resumed, no third fresh request or usage reset. [Actual request 2 journal](readonly-followup-2.json) records limited page delivery at **94,544 bytes**, four truthful prefixes and actual next offsets/selectors. The source/state/Profile/defaults remain bound and the initial failure is independently retained. This is a real admission improvement only.

The model then returns invalid context references and attempts reads at the existing round limit; both reject, with no Plan saved. Last Job `7bd27bb6-8fb0-4ca2-95be-13a32023ff81` ends `model_gateway_transport_failure`. [Safe transport observation](native-terminal-transport-observation.json) records upstream HTTP200 followed by Gateway502; the broad transport category also covers JSON/response-guard errors, so **timeout/network root cause is not proven**. Raw rejected upstream content and usage are unavailable. No ambiguous completion POST is replayed.

Two fresh follow-up requests plus continuation of request 2 use **11** actual model calls (5 + 6), inside the approved 14; there are zero active Jobs, Plans, execution approvals, code patches or native deliveries. Lifetime 29 calls have known 712,836 input / 35,617 output; two unknown calls separately reserve 900,000 input / 24,576 output. These are allowances, not invented actual spend. Remaining task allowance is 3 calls / 387,164 input / 39,807 output. No cap is increased or unknown reserve released. Original native first deliveries stay **0/4**, environment baselines separately **0/15**. Historical Node remains failed, real Linux amd64 blocked/not_run; native Baseline/Coding/Verify/Review/final acceptance remain not_run. Current task is UPEVE-E04 and no stage gate is checked.

[Final read-only observation](delivery-final-observation.json) confirms all four local role health checks, zero active Jobs/execution approvals/Plans and the actual native mobile page displaying the failure and both unknown-call reserves without overflow or mutations. Local health does not prove upstream completion success. [Final preservation/security audit](delivery-audit.json) retains all 74 historical files, 71/165 original events, N5/N7/N8 first 36/37/40 events, the initial second failure and its frozen failed payload. No raw credentials are read or found in the sanitized evidence. Code remains on the requested branch with local commits only; formal data and third-party repositories are untouched.


## Explicit real retry requested after the gateway failure

The user asks to try acceptance again. A missing model_gateway_transport_failure entry is added to the existing explicit-continue recovery allowlist; source/state/model checks, idempotency, budgets and all read/repair counters stay unchanged. This is a new user-requested attempt, not replay of an old gateway token or automatic completion POST. [Focused/targeted checks](explicit-retry-checks.json) pass **32 focused / 92 targeted**; the complete 1650-pass suite is retained as prior-version evidence, not claimed rerun. The same-lineage attempt keeps frozen round 4 / repair 1 and permits at most two calls inside the remaining allowance.

[Actual retry](explicit-model-retry.json) makes **one real MiniMax call**. Job `8b32be2d-ac2c-48b9-b5b9-2b87495f40d7` fails `model_gateway_upstream_failure` after about 1.8 seconds. The sanitized Gateway log records **HTTP 402 Payment Required** from upstream, followed by local HTTP502. This points to account billing/allowance; the exact upstream business code/body and account balance are unavailable, so balance versus plan limit is unconfirmed. [MiniMax's official error list](https://platform.minimax.io/docs/api-reference/errorcode) distinguishes insufficient balance (1008) from usage limits (2056 and other plan codes); none of those finer codes is observed here. This new 402 does not establish the cause of the older HTTP200 transport failure.

Lifetime usage is now **30 calls**, known **712,836 input / 35,617 output**. Three calls with unknown usage separately reserve **1,350,000 input / 40,960 output**. Known input plus conservative allowance is 2,062,836, leaving **zero local input allowance** under the original 2,000,000 cap. These reserves are not actual spend or MiniMax account balance; they are preserved, and no further model request or automatic budget increase occurs. No Plan is saved and no execution is approved. The original 151 events, failed payload and model/source bindings are retained.

[Final observation](explicit-retry-final-observation.json) and [preservation/security audit](explicit-retry-audit.json) record current local health, zero active Jobs, unchanged history and no new execution approvals. Native first deliveries remain **0/4**, full environment baselines **0/15**; all first outcomes and earlier consent records stay intact. E3 historical Node remains failed, E4 partial, E5 failed, Linux amd64 blocked/not_run. Native Baseline/Coding/Verify/independent Review remain not_run. No formal data or third-party repository is modified, and no acceptance gate is promoted.

## Domestic DeepSeek integration requested by the user

Source `f613dd9` adds explicit DeepSeek connections at the official `https://api.deepseek.com` endpoint, `deepseek-v4-pro` / `deepseek-flash` presets, encrypted browser credential entry, four-stage binding and exact provider-backed Review support. Thinking-mode calls use supported automatic tool choice while retaining full output/domain validation and original output/task caps. Hidden reasoning is discarded; missing usage and interrupted/non-final responses reject with known usage retained when available. Cache-hit usage is recognized. Existing providers and task/default versions remain intact. See [configuration instructions](../../studio-workbench.md#在设置页配置模型).

[Checks](deepseek-checks.json): **1676 passed / 62 skipped / 0 failed**, plus 54 focused contracts, compileall, JavaScript syntax, runtime integrity and whitespace checks. Ten historical upgrade assertions now include migration 0040; a scheduling fixture uses its injected clock for both lease and completion without changing production policy. All initial failures are separately retained in the check receipt. Browser checks caught and fixed a stale auto-filled MiniMax ID after switching connections. The task authorization identity is tested against actual bound Worker identity. The four-stage Fake journey and injected browser connection are component evidence, **not real model or native Agent success**.

[Initial isolated deployment](deepseek-deployment.json) drains all five owned host roles and Publisher, saves a consistent SQLite/Artifact backup, applies `0040_deepseek_reviews`, and verifies unchanged business counts/hashes and foreign keys. [Final deployment](deepseek-final-deployment.json) records all 234 source files, the final independent non-root/read-only Publisher image, preserved narrow mounts and four healthy local endpoints. Earlier stopped containers and the 0039 backup are retained; formal services/data and third-party repositories are untouched.

[Final audit](deepseek-audit.json) preserves the original rows of 26 immutable tables and all 74 historical files; sanitized evidence/event scans find no credential patterns. At this observation there are **zero saved DeepSeek connections/Profiles**, so real DeepSeek connection/structured-output testing is **blocked/not_run pending secure user key entry**. No raw key is read or new credential supplied by the operator. The native task remains at 30 calls / three unknown reserves / zero input allowance; changing provider does not reset budget or authorize a new execution. Native first deliveries **0/4**, environment baselines **0/15**, historical Node failure and Linux amd64 blocker are unchanged. No UPEVE gate is promoted; the current task remains UPEVE-E04.

## Real DeepSeek connection: queued test recovered and passed

The user subsequently saved a DeepSeek credential/Profile and selected it for all four global stages in the native page. The connection test appeared stuck. Read-only diagnosis found a missing Provider Worker process while API/Gateway health still returned success; the queued test and three user-cancelled predecessor tests all had attempt_count=0. There was no upstream request to diagnose from these waiting Jobs. The process exit's underlying cause is unknown because no durable exit receipt exists; the earlier HTTP health checks alone did not prove background-worker availability.

[Recovery receipt](deepseek-provider-recovery.json) starts only the missing owned role and keeps the existing queued Job/payload/max_attempts=1. [Real observation](deepseek-real-connection.json) records that same Job `089005c5-2888-41f7-866d-14e1f14b0981` succeeding with upstream **HTTP200**, **1995 ms**, `deepseek-v4-pro` and validated structured `ok=true`. All five role children and four local health endpoints are live afterwards. No replacement test is enqueued; cancelled tests remain cancelled and unattempted. No key is read by the operator, proxy/TLS/credential boundaries changed, or latest connection version silently rebound. The connection result does not retain usage; exact spend is unknown.

This is a real authenticated provider connection/structured-output pass and establishes current network availability. It is **not native Agent success**, an executed Baseline or proof of future process liveness. Global choices changed only through the user's page action. Existing native task bindings/usage/reserves stay intact: 30 calls, three unknown reserves, zero input allowance, no Plan or execution approval. Native first outcomes, Node failure, Linux blocker and all UPEVE gates remain unchanged.

## DeepSeek task switch and exact budget prerequisite

The user now explicitly asks to use DeepSeek for acceptance. [Task switch](deepseek-native-model-switch.json) appends the four-stage binding to saved Profile `d660a5ca-9904-4c5b-8dcc-b94bda8f5e4e` / record hash `bb3522b03a444b544c808217ec5ed4b4fa71253591ee8c09e3fc77c57d3a6049`, with V4 Pro, 16K output and 300s timeout. Existing source and all prior task events/Jobs remain frozen; global defaults and credentials are untouched. This remains the same D maintenance target, not a fresh U attempt.

[Preflight audit](deepseek-native-preflight-audit.json) verifies all 161 preceding events and 38 Jobs unchanged, the strict proposed request schema, and the actual native `planning_budget_exceeded` rejection. Known input 712,836 plus held input 1,350,000 exceeds the original 2M task cap; the displayed remainder is zero. No new generation or budget mutation occurs.

[Exact proposal](deepseek-native-budget-proposal.json), hash `dd328a3327c374bd59541e0dcfbf3ea5ea3151f46cf0eedd1c84ca6ca08e44a0`, permits at most one new read-only request / eight further calls under lifetime caps **38 calls / 4M input / 200K output**, retaining old use, unknown allowances, all read/repair/security boundaries and the unchanged original goal. A reviewable Plan still requires separate exact human execution approval. Budget approval is pending; the new task binding alone neither raises limits nor dispatches calls. Native first outcomes remain 0/4, environment baselines 0/15, and current task UPEVE-E04 stays open.

## Approved DeepSeek native request: routing stops before planning

[Exact approval](deepseek-native-budget-approval.json) records user “确认” against the frozen proposal, with hash `cd674d790d2fc4ae3bb7e054759171deac17afdae8df5fb68cf8d9846331f5f2`. The native API applies the task-only lifetime limits while retaining every old call and unknown reserve, then submits exactly one unchanged original-goal request. This remains D maintenance, without new unseen selection or execution consent.

[Actual native journal](deepseek-native-planning.json) records **three real calls, all upstream HTTP200**, known additional usage **11,779 input / 2,662 output**. Job `da18df58-89fd-4891-b4a1-6e82d5a82bbd` fails `conversation_tool_budget` while routing the message. Five context-reference tool results exist; no planning workflow, Plan or execution is dispatched. The exact last rejected response/read list is unavailable. [Checkpoint diagnosis](deepseek-native-read-diagnosis.json) proves five reads had used five of six slots, yet the third prompt had no remaining-read count and the reconstructed frozen schema allowed three reads. The quota error implies at least two new reads were requested, without establishing the exact count. Current network/model availability passes; this native attempt **fails**.

The request closes on that terminal failure, below its eight-call maximum. Lifetime use is **33 calls / 724,615 known input / 38,279 known output**, with unchanged unknown reserves **1,350,000 input / 40,960 output** across three calls. Remaining local capacity is **five calls / 1,925,385 input / 120,761 output**; no capacity is treated as permission for a second automatic request. No raw credentials, formal data or third-party repository is touched. Native Baseline/Coding/Verify/Review remain not_run, native first deliveries 0/4 and environment baselines 0/15 remain unchanged.

Generic platform maintenance supplies the remaining read allowance to the v4 prompt, structured-output schema and parser before any tool execution. Overlarge batches use only the existing bounded correction, without executing any rejected read; the final quota guard remains. Calls, six-read cap, one correction, continuation counters, model/lifetime limits and all authorization rules stay unchanged. Legacy defaults are retained. Focused Fake contracts verify the five-read/overlarge case, zero-read FINAL and durable restart.

[Final checks](read-budget-checks.json) on source `72cf875` pass **1682 default / 62 skipped / 0 failed**, plus **92 focused contracts**, compileall, runtime integrity and whitespace checks. The initial focused fixture used an invalid `account` tool name; it was corrected to the existing `publisher_account` enum without changing the production allowlist. [Isolated deployment](read-budget-deployment.json) restarts the five owned host roles and verifies all children/four health endpoints, unchanged budget and the entire native journal. Independent Publisher interfaces/image/credential store remain unchanged. [Preservation audit](read-budget-final-audit.json) retains all 74 historical files, 71/165 historical events, 162 pre-request events and 38 earlier Jobs, with the actual terminal failure preserved; SQLite integrity/foreign keys and sanitized evidence/event credential-pattern checks pass.

There are **zero native model calls or requests after the fix**. Its real effectiveness is **not_run**, and the failed request is never relabelled successful. Native Baseline/Coding/Verify/Review remain not_run; first native outcomes 0/4, environment baselines 0/15, historical Node failure and native Linux amd64 blocker remain unchanged. No stage gate is promoted; current task stays UPEVE-E04.

## Native continuation: read quota enforced, no Plan at the call cap

The subsequent user “继续验收” authorizes using the retained five-call allowance. [Natural-language retest](deepseek-native-read-retest.json) uses **four real DeepSeek calls** with actual read allowances **6 → 3 → 1 → 0**, exactly six tool results and no schema/quota rejection. It returns a bounded observation successfully. The model classifies this investigation as `observe` and chooses `respond`, without invoking the planner, so **natural-language planning routing remains failed**. Safe refusal to infer missing evidence is not a saved Plan or native delivery.

Using the same five-call allowance, a separately recorded [explicit native read/plan control](deepseek-explicit-plan-entry.json) uses the last call. This existing visible product entrypoint reaches the real planner and requests four context pages. Its follow-up Job `642dca84-55a1-441a-9d7b-7f5a0eade759` fails `planning_budget_exceeded` **before** another model invocation. The first Job's success means a parsed read request, not a Plan. Both native messages and their different request texts/entrypoints are retained; the explicit control is not a natural-language routing pass or a new unseen target. No host business patch, direct Plan insertion or execution consent occurs.

[Continuation audit](deepseek-continuation-audit.json) records **five actual calls**, all HTTP200, additional known **44,423 input / 8,161 output**, unchanged three-call reserves and preserved history. Lifetime use is **38 calls / 769,038 known input / 46,440 output**, with **1,350,000 input / 40,960 output** held separately. Remaining: **0 calls / 1,880,962 input / 112,600 output**. Five role children and four health endpoints pass; source still matches the prior 1682-pass regression without a new code change or suite rerun. The specific read-quota repair passes in a real request; the full native journey fails/no Plan. Native Baseline/Coding/Verify/Review, old first outcomes 0/4 and 0/15, Node failure and Linux blocker stay unchanged.

[New exact proposal](deepseek-planning-call-extension-proposal.json), hash `f3a8087173759a5084ae48f3e4dcfef9460d99a994603174b2ee0ce625fce508`, changes **only calls 38 → 50** (at most twelve additional calls). Input/output caps, known use, unknown reserves and safety limits are retained. After a new exact confirmation, one native explicit continue can use the existing budget recovery, which creates a new read-only planning request and preserves the earlier failed round rather than replaying an ambiguous completion or pretending to preserve its exact payload. Approval is pending; no budget extension or new model call is dispatched. All phase gates remain unchanged; current task UPEVE-E04 remains open.

## Approved call-only extension: direct planning recovery

[New exact approval](deepseek-planning-call-extension-approval.json), hash `8c6ddfea71e098cbdbd006688d90a1c9ba0b041cceb31f2a97787e6e093900f9`, records the user's “确认” against that frozen proposal. The native API changes only the task call cap to **50**; 4M input / 200K output and all old usage/unknown reserves are retained. [Actual recovery journal](deepseek-planning-call-extension.json) sends one explicit continue and enters the existing planning workflow, with a maximum of 12 additional calls. The existing recovery creates a new request at its normal request-local counters; the old round/failure is retained and never rewritten. The original source, exact DeepSeek Profile and tested production source hashes match. No natural-language routing pass, Plan, execution or native delivery is inferred from enqueueing the request.

[High-effort truncation observation](deepseek-high-truncation-observation.json): one real call returns HTTP200, then fails `deepseek_completion_truncated` after **180.637 seconds**, with known **49,660 input / 16,384 output**. The decoder identifies upstream `finish_reason=length`; partial output is never repaired into a Plan. The exact private-reasoning/final-content split and raw response were not retained, so it is **not proven that reasoning alone exhausted the cap**. The one-request grant stops on this terminal failure. Known usage is preserved and the three old unknown-call allowances remain unchanged.

[Final extension audit](deepseek-call-extension-audit.json) records **39 lifetime calls / 818,698 known input / 62,824 output**, reserves **1,350,000 input / 40,960 output**, and remaining **11 calls / 1,831,302 input / 96,216 output**. The prior failed round and all previous task events remain immutable. Five role children/four local health endpoints pass, and production source hashes still match the 1682-pass regression without a new code change or suite rerun. No new model call occurs after the failure. Native Baseline/Coding/Verify/Review and full acceptance remain not_run; old first outcomes and all open gates stay unchanged.

## Concrete low-effort trial preview, not applied

[Trial proposal](deepseek-planning-low-proposal.json), hash `59f6c37f175bfcbd817cb96d4b0c3e3802eb525e023625a638fb9064faee7f1d`, includes a saved but **unbound** Profile `f3687acf-ef9a-4b59-9b7f-c17ad5132382` / hash `5538d8a232185d8c2b76574665bff1f491c163b5998c2e1e3551510cf536d9fd`. It keeps DeepSeek V4 Pro, the same connection, **16K output / 300s timeout**, and proposes high → low reasoning for **only this task's planning stage**. [Official thinking-mode parameters](https://api-docs.deepseek.com/guides/thinking_mode/) support low/high effort; reducing effort might help fit a complete response within the unchanged cap, but this is an unverified hypothesis.

Global defaults, other stages, credentials, known use, unknown reserves and **50 calls / 4M input / 200K output** stay unchanged. One native read/plan request would use at most the existing eleven remaining calls after new exact profile-change confirmation; sandbox execution still needs an exact Plan approval. Zero tasks/Jobs bind the preview Profile, zero model calls use it, and no low-effort success is claimed. The saved high-effort Profile and its approved historical attempts remain intact. This parameter-change gate is pending, not permission inferred from elapsed time; current UPEVE task remains E04.

## Approved low-effort request: complete responses, prerequisite read gap

[Exact approval](deepseek-planning-low-approval.json) records user “允许” against the frozen proposal. Only this task’s planning Profile changes to low reasoning; the saved high Profile, other stages and global defaults remain intact. [Actual native result](deepseek-planning-low-retest.json) records **five real calls**, all complete without the prior output truncation, with additional known **163,409 input / 20,892 output**. The terminal draft is deferred, **not a saved Plan**; all five Jobs succeeding does not prove successful planning. Native first outcomes remain **0/4**, complete environment baselines **0/15**.

[Source-bound diagnosis](deepseek-planning-low-read-diagnosis.json) reconstructs the exact final guard: all draft inspection/change paths already have captured source, but automatic UPEVE closure requires the absent `pnpm-lock.yaml` at read round **4/4**. The prompt reports zero remaining rounds. The raw final model response was not retained, so its exact original read intent is unknown; this is not evidence of a model-only schema violation. `repair_round=0` also disproves the user-facing claim that the two automatic repairs were exhausted. The failure and original message remain immutable.

[Audit](deepseek-planning-low-audit.json) preserves all prior events, the earlier high-effort failure, original source/archive, three unknown-call reserves and usage. Lifetime **44 calls / 982,107 known input / 83,716 output**; held separately **1,350,000 input / 40,960 output**. Remaining **six calls / 1,667,893 input / 75,324 output** under unchanged **50 / 4M / 200K** limits. Five role children/four health endpoints, SQLite integrity/foreign keys and sanitized evidence checks pass. This one-request grant ends without Plan or execution approval. No further native request is inferred from the remaining allowance.

Generic maintenance prioritizes statically selected root environment declarations before the first paid planning call, preserves them under context eviction, and reports exact missing paths/read rounds separately from repair exhaustion. Archive continuations use the existing four-read-round allowance and 32-path batches; there is no new model/repair capacity or preparation/execution permission. Old or disabled UPEVE requests retain their route, and unavailable source is never repeatedly requested.

[Real OCI component receipt](environment-preload-oci.json) uses the same pinned image and policy, non-root/read-only filesystem and `--network none`. It reads the missing **67,635-byte** lock from the frozen archive, verifies SHA-256 `d5115f79ff9b073e1f88e957a49270238235c7729eb89b13f1ef2465c513596d`, then closes the draft’s static graph **in memory only**. It never executes repository code or modifies native context/Plan. This is component evidence, not native planning, preparation, Baseline or Coding success. Post-maintenance native effectiveness remains not_run; historical Node and native Linux gates remain open.

[Verified maintenance checks](environment-preload-checks.json) freeze source `9aade9356a239fe5d00c0efee63c0dbb3cd91889`: **1691 default passed / 62 skipped / 0 failed**, **27 focused passed**, compileall/runtime integrity/whitespace pass. Four failing contracts were recorded before the fix; nine cases now cover declaration preloading, unavailable files, legacy/disabled routing, read caps, eviction and accurate diagnostics. These are controlled contracts, not native deliveries. [Deployment](environment-preload-deployment.json) restarts only the five owned isolated host roles and verifies their children/four health endpoints, unchanged task binding/budget and journal. Publisher and formal services are unchanged. [Final audit](environment-preload-final-audit.json) verifies all 234 tested production files, 74 historical files, prior native events/Jobs, SQLite integrity/foreign keys and sanitized JSON.

No native call follows maintenance. [Concrete repeat proposal](deepseek-environment-preload-retest-proposal.json), hash `43190800f13da4b08ed9202f65f288bddab3d690ae2cd7166a1927d8b2008dab`, permits one new read-only native request using the existing low planning Profile and at most the remaining six calls, with unchanged source/archive, lifetime and per-call caps, known use and unknown reserves. It stops at Plan/material clarification/stale input/terminal failure/cap. It grants no execution or publication. Confirmation is pending because the preceding exact grant ended with its single terminal request; remaining capacity alone is not new consent. All phase gates remain unchanged; current task stays UPEVE-E04.

## Approved native preload retest: first saved Plan, semantic revision required

[Exact approval](deepseek-environment-preload-retest-approval.json), hash `68528eb89f387b484b122b6bde22fee003c0c6fcb322dc0bd78e298d60132864`, records user “允许” for the frozen six-call / one-request preview. [Actual native journal](deepseek-environment-preload-native-retest.json) shows the Sandbox Worker captures/hash-verifies the missing lock at sequence273 **before** the first model call at276. The first native context remains within48 entries. **Five complete DeepSeek calls** save real PlanVersion `cb2aa72a-58a6-45e2-a26f-0daf9c3ba354`, record hash `bc9791d81608a7a4b13d54d3ccb2d81c48e4a4a29483c018be669c48dccf50d9`, retaining all four exact CI checks. No manual Plan insertion or external business patch occurs.

The original Plan preflight is **blocked** (`f08109f7...3dfa38`): [per-command diagnosis](deepseek-native-filter-diagnosis.json) resolves three commands but cannot parse `pnpm --filter agent-flow run lint`. The collector’s original `plan_ready_requires_exact_execution_approval` label only reports Plan existence; it does **not** establish preflight, semantic completeness or execution readiness. Its frozen result and hash are retained. [Native audit](deepseek-environment-preload-native-audit.json) preserves earlier events/Jobs, the source/archive/model binding and unknown reserves. Known additional use is **165,216 input /23,644 output**; lifetime **49 calls /1,147,323 known input /107,360 output**, with the same three unknown-call reserves **1,350,000 /40,960**. Remaining **one call /1,502,677 input /51,680 output**, with no cap or Profile change. The request stops at Plan.

Generic maintenance adds **node-toolchain-v3 /node-script-chain-v3** for one literal, exact, uniquely named declared pnpm workspace package and explicit `run` script. It records workspace/member metadata and cwd provenance. Missing names, duplicate names, scope inference, glob/dependency/Git selectors, multiple filters, argument dropping, ambiguous YAML, missing/hash-mismatched/symlink source, installers and cycles fail closed. [pnpm10 filtering](https://pnpm.io/10.x/filtering) and [workspace declarations](https://pnpm.io/10.x/pnpm-workspace_yaml) define the underlying CLI behavior; this adapter intentionally admits a narrower subset. Existing v1/v2 contract bytes and hashes remain accepted without adding fields or rewriting evidence. [Same-Plan static recomputation](pnpm-filter-current-plan-static.json) closes the four exact original commands under v3, without changing the Plan. This is static component evidence, not actual project preparation or Baseline.

[Regression checks](pnpm-filter-checks.json) freeze engine `61cbbc0d68e212fce91d453ac260c9e287d46b0b`: **1725 default passed /62 skipped /0 failed**, plus **105 focused passed**, compileall, runtime integrity and whitespace pass. The pre-fix focused run retains23 failures/5 passes;34 new cases cover the supported subset and negative boundaries. [Real OCI component](pnpm-filter-oci.json) prepares pnpm10.11.0 via the credential-free allowlisted package proxy, stops that proxy, then runs two actual filtered checks under unchanged non-root/read-only/network-none policy. Both execute only the intended package, and frozen source inventory is unchanged. The first observer used an unused method name and failed receipt collection after command assertions; [that failed attempt](pnpm-filter-oci-observation-failure.json) is preserved, the observer corrected, and a new complete OCI run supplies the valid receipt. Neither run is a native Issue delivery.

[Operator source review](deepseek-native-plan-source-review.json) finds a distinct **blocking Plan quality defect**: transcript/hook event producers omit the shared call ID in emitted payloads. An ID in the raw transcript type does not prove its presence in web events; generating a local start ID cannot identify an out-of-order end. The saved Plan writes only four web files and defers checking upstream propagation, so a **native child Plan** must establish the producer/consumer contract and necessary regression scope before execution approval. This read-only operator review is **not an independent ReviewRun**, and no business repair is supplied by Codex.

[Isolated deployment](pnpm-filter-deployment.json) restarts only the five owned idle host roles, verifies their children/four health endpoints, unchanged budget/model/source and Plan/journal, and leaves Publisher/formal services unchanged. [Final audit](pnpm-filter-final-audit.json) retains the original blocked v2 preflight, verifies its exact wire/hash, captures current v3 static readiness separately, checks234 source hashes/74 historical files, SQLite integrity/FKs and sanitized records. Current API static admission does not resolve Plan quality or prove preparation. No native request/model call follows the saved Plan; execution approvals and third-party writes remain zero.

[Concrete child-planning preview](deepseek-native-plan-revision-proposal.json), hash `c7904ce66988f0c0fd7ffe93f4ab9dbc972b9d798f0f21ce424c745380d3e385`, proposes one existing native explicit planning request, pinned to the parent Plan hash, reviewed source and unchanged low Profile/caps, using at most the **one remaining call**. It requests verification of the producer/consumer identity chain and a child Plan retaining all four checks; it neither inserts a Plan nor authorizes implementation. If more reads require another call, the existing cap must stop it. Confirmation is pending because the prior exact one-request grant stopped at Plan. Native Baseline/Coding/Verify/independent Review remain not_run; native first outcomes0/4, environment baselines0/15, historical Node failure, Linux amd64 blocker and current UPEVE-E04 remain unchanged.

## Approved one-call child revision: context repair passes, read parameter rejected

[Exact approval](deepseek-native-plan-revision-approval.json), hash `f1dc4c408f8af61bde27c20a958aa7113d310aa9ff2a4e6ce8877a3e8cd1bbc3`, records user “允许” for the frozen native explicit planning request. [Original result](deepseek-native-plan-revision.json) fails `planning_context_limit` **before any model call**, retaining the one-call allowance and parent Plan. No child Plan or execution is produced.

[Controlled diagnosis](deepseek-plan-revision-context-diagnosis.json) uses a consistent disposable copy of the isolated business DB, retains source/Plan/state checks, omits only the already-terminal lease check in the copy, and never calls a model or writes original artifacts. It reproduces a **111,740-byte** prompt above the unchanged96KB guard. Execution status carries duplicate full static contracts and command graphs. Generic v2 projection indexes these graphs by hash, preserves exact current Plan/request/rules/current source pages and factual readiness, and keeps complete originals retrievable through task-bound references. API/business evidence and legacy prompts are unchanged. [Controlled fixed replay](deepseek-plan-revision-context-replay.json) fits the same frozen request in **93,821 bytes**, without issuing the prompt or claiming native success.

[Context checks](context-graph-checks.json) at source `f1561476c5c8572eabf8fb075790750d3ca02fa9` pass **1729 default /62 skipped /0 failed**, plus **43 focused**; four new cases retain two pre-fix failures/two passes, restart byte roundtrip, canary rejection and irreducible pinned-context refusal. Compileall/runtime integrity/diff pass. [Isolated deployment](context-graph-deployment.json) restarts only five owned idle host roles and confirms their children/four health endpoints, unchanged parent/history/budget and independent Publisher.

Because the authorized call had not been consumed, the existing product recovery continues the **same frozen planning request**, with no new root planning request. [Recovery journal](deepseek-native-plan-revision-resume.json) proves both Job payload hashes equal `9c830b5ffa12432ed0f6405498e1c9d214eb9c6205b9bc59e72bd88b67da0a9a`; the original failed Job stays failed. The user also says “继续” during recovery. **One actual model call** receives a real **93,414-byte** admitted prompt, demonstrating the context fix in the native path. [Observation](deepseek-native-plan-revision-observation.json) corroborates HTTP200 and known **33,563 input /1,453 output /15,214 ms**. The response fails `read_context.max_lines` with `less_than_equal` against160; its exact original value/raw response was not retained. No rejected read executes, and **no new child Plan** is saved. Schema rejection is the terminal failure; the simultaneously exhausted call cap is recorded separately.

Generic v2 correction now recognizes only numeric read-page bound errors and uses the **existing two correction slots** with bounds derived from `ContextRead`. It never clamps values, executes rejected reads, widens160 lines/16KB, resets read rounds, or repairs unknown/security fields through this route. A native continue may recover only a matching trusted rejection from the exact failed Job and unchanged source/model/parent; it increments the existing repair counter, keeps the original read round/request, and requires available task budget first. Legacy behavior and all stale/pause/budget checks remain. [Read-bound checks](read-bounds-checks.json) at source `f6aaa40f52b4d305e770cfe9c88a4f0a1e1a897e` pass **1743 default /62 skipped /0 failed**, **76 focused**, compileall/runtime integrity/diff. Fourteen new cases retain the earlier8-failure/5-pass run and verify no unauthorized read, no extra correction slots, zero-read limits and source-bound recovery.

[Controlled recovery preflight](read-bounds-controlled-replay.json) proves the real50-call cap refuses recovery, then simulates a64-call grant **only in a disposable copy**. The bounded correction preserves the failed Job/goal/read round and generates an unissued **94,393-byte** prompt with the same96KB ceiling. This is neither actual budget consent nor native recovery success. [Deployment](read-bounds-deployment.json) installs the fix only into the isolated host roles; Publisher/formal services/OCI programs stay unchanged. **Zero real calls follow the schema failure**.

[Final audit](native-plan-revision-final-audit.json) verifies all234 production hashes,74 historical files, source/Profile/defaults, immutable Jobs/events/parent Plan, SQLite integrity/FKs and sanitized records. Actual lifetime usage is **50 calls /1,180,886 known input /108,813 output**; the same three old unknown-call holds are **1,350,000 input /40,960 output**. Remaining **0 calls /1,469,114 input /50,227 output**. This is the local orchestration cap, **not evidence of DeepSeek account balance**. The existing Plan still needs native producer/consumer scope revision; native Baseline/Coding/Verify/independent Review remain not_run. First native outcomes0/4, complete environment baselines0/15, historical Node failure and Linux amd64 blocker remain unchanged; no stage gate is promoted.

[Concrete resource preview](deepseek-native-readonly-extension-64-proposal.json), hash `3a3d1b8602e43be87958247484b745ee309ade1833c0625509101957e598cee8`, changes **only call cap50→64** (at most14 additional calls), retaining4M/200K input/output caps, all known use/unknown holds, low Profile and16K/300s per call. After new exact confirmation, one native control resumes the existing frozen child-planning request with the trusted schema correction, original counters and all four CI checks; no new root planning request, execution or publication is granted. Stop at child Plan/material clarification/irrecoverable failure/any cap. Approval is pending; the actual cap stays50. Current specialty task remains UPEVE-E04.


## Approved64-call extension: bounds repair passes, source delivery fails (2026-11-01 local)

[Exact approval](deepseek-native-readonly-extension-64-approval.json), hash `4a094390f8aaf70288fee7b1060b79351fbd2495ee5e6626587fa2912a03f172`, applies the frozen call-only50→64 preview. [Actual native receipt](deepseek-native-readonly-extension-64.json) uses its **one recovery control/one complete DeepSeek call**. The bounded numeric correction now passes in the real path and produces four valid source reads. The next Job `475d4ded-1e95-42b6-9b4f-5701fe529d6b` fails `planning_context_limit` **before a second model call**. No child Plan is saved. [Native audit](deepseek-native-readonly-extension-64-audit.json) verifies source/parent/Profile/defaults and all earlier events/Jobs,74 historical files, SQLite integrity/FKs and sanitized records. Lifetime use **51 calls/1,214,594 known input/110,121 output**; unchanged three unknown-call holds **1,350,000 input/40,960 output**. Remaining **13 calls/1,435,406 input/48,919 output**. There is no provider-balance diagnosis.

[Controlled diagnosis](source-delivery-context-diagnosis.json) uses copies of **both DB and artifact store**, no model, and only omits the terminal lease check in the copy. Four minimum256-byte source pages still produce **98,898 bytes** above the unchanged96KB guard. The long history repeats per-proposal references in memory and its manifest. Generic v2 projection now groups older original assistant proposals by source-labelled hash references, never a generated summary. Human instructions/current Plan/exact commands/rules/current reads remain; legacy and small histories retain their contract. Original grouped proposals page back exactly after restart. [Controlled fixed replay](source-delivery-context-replay.json) fits **94,443 bytes**, with fair actual source prefixes and honest continuation offsets. These replays do not prove native success, baseline or execution.

[Checks](source-delivery-checks.json) at `f4c0886` pass **1748 default/62 skipped/0 failed**, **48 focused**, plus the final5 new cases after extracting the group-size constant. The new cases record3 pre-fix failures/2 passes. Compileall/runtime integrity/diff pass. Initial operator test/CLI invocation mistakes are retained separately in the receipt. [Isolated deployment](source-delivery-deployment.json) confirms five owned children/four health endpoints, zero active Jobs, unchanged parent/failed Jobs/events/budget, and separate Publisher. Zero model calls occur after maintenance. [Final audit](source-delivery-final-audit.json) preserves provenance and security evidence. The new history projection’s native effectiveness remains **not_run**.

[Supplemental recovery preview](deepseek-native-source-delivery-recovery-proposal.json), hash `2f0d199d9a9d636e628a0fd6abe6c8674f75b5a49b60ee823bf3ecb4024f6786`, requests at most3 recovery controls/13 model calls within the **existing64/4M/200K** caps and unchanged16K/300s per call. It resumes only the same frozen read-only request, preserving read round1/repair round1 and all four CI checks; no new root request, execution or publication. Subsequent controls are restricted to proven pre-model context-admission failure after a verified generic fix; unknown model outcomes are not retried under this grant. Stop at child Plan/material clarification/other terminal failure/stale input/any cap. Supplemental confirmation is pending because the earlier exact preview allowed one control, now used. No call-cap increase is requested. First native deliveries0/4, complete environment baselines0/15, historical Node failure, Linux amd64 blocker and all unrun native Baseline/Coding/Verify/independent Review gates remain unchanged; current task is UPEVE-E04.


## Supplemental same-request recovery: native delivery passes, schema correction exhausts (2026-11-01)

[Exact approval](deepseek-native-source-delivery-recovery-approval.json), hash `ac7d7ff492ddc03ab92f90e16b68c3a9d09eab969cb581e305224301767d6318`, binds preview2f0d199d...024f6786. [Actual native result](deepseek-native-source-delivery-recovery.json) uses one recovery control, keeps the identical frozen payload/read1/repair1, and dispatches **three real model calls**. Actual prompts **94,456/94,344/94,308 bytes** all pass the unchanged96KB guard. Two real delivery receipts admit105,905→94,456 and101,854→94,344 bytes using honest partial source pages and continuation offsets. The history projection now passes in the native path; this is not a saved child Plan, baseline or native delivery.

First response is a valid read request. Second fails `json_invalid`; its existing automatic correction fails `read_context.max_lines<=160` at repair **2/2**, read **2/4**. Rejected reads never execute; exact bad values/raw responses were not retained. The old Plan remains the only Plan and still needs producer/consumer scope revision. The preceding grant stops on this non-context terminal failure; unused context-only controls are not permission to restore schema correction slots. [Native audit](deepseek-native-source-delivery-recovery-audit.json) verifies known deltas **97,416 input/10,140 output**, lifetime **54 calls/1,312,010 input/120,261 output**, unchanged unknown holds **1,350,000/40,960**, and remaining **10 calls/1,337,990 input/38,779 output** under64/4M/200K. Parent/source/Profile/defaults, prior failed Jobs/events,74 historical files, SQLite integrity/FKs and sanitized evidence remain intact.

Generic v2 prompt guidance at `d20f8d5` now carries current schema-derived numeric page bounds and read/repair capacity on every turn, including JSON/envelope corrections. It refreshes stale earlier guidance in the prompt view without changing immutable Job payloads/hashes. Legacy behavior, enforced160 lines/16KB, four read rounds/two repairs, output/time and security limits remain. This is not claimed to fix the unretained invalid JSON response. [Checks](active-read-limits-checks.json) pass **1753 default/62 skipped/0 failed**, **42 focused**, compileall/runtime integrity/diff. Five new cases record4 pre-fix failures/1 pass. The initially too-short malformed JSON fixture normalized to an empty outcome; the corrected json_invalid fixture and its pre-fix run are retained separately.

[Controlled replay](active-read-limits-controlled-replay.json) uses copies of both DB/artifacts, no model, and only omits the terminal lease check in the copy. Unissued94,308/95,283-byte prompts expose current bounds through both error kinds; exact frozen payloads remain. Even with ten calls available, the exhausted old request still refuses recovery. This is not native success or new consent. [Deployment](active-read-limits-deployment.json) restarts only five owned idle host roles and verifies children/four health endpoints, unchanged budget/Plan/history and separate Publisher/formal services. Zero native calls follow maintenance; its real effectiveness is **not_run**. [Final audit](active-read-limits-final-audit.json) records source and evidence preservation.

[Next exact preview](deepseek-native-active-read-limits-retest-proposal.json), hash `fb611b2e070e3ce36c41b440f6affc16352e2beee35bc3bd19886031fbd2caa7`, requests at most **two new native read-only requests/ten existing calls/two context-only recovery controls**. Goal/parent/source/Profile/four CI commands and64/4M/200K caps stay fixed. New requests explicitly start their usual request-local4-read/2-repair counters; old requests/counters/failures/known use/unknown holds are preserved, never relabelled continued. A second request is allowed only after a known bounded output/evidence failure with no Plan/material question/security or unknown provider outcome and valid unchanged bindings/capacity. It stops at a child Plan/material clarification/stale input/security or unknown outcome/any cap, with no implementation/publication. Confirmation is pending because the earlier grant prohibited new root requests and ended on non-context failure. No extra model budget is requested. Native first outcomes0/4, complete environment baselines0/15, historical Node failure, native Linux amd64 blocker and unrun Baseline/Coding/Verify/independent Review stay unchanged; UPEVE-E04 remains current.


## Approved new request1: two context recoveries and real child Plan2 (2026-11-01)

[Approval](deepseek-native-active-read-limits-retest-approval.json), hash `7df9a1e0b7f886ebf06051263cdec910efbebd2ebe04b8e823d658fbeb74bf6a`, binds previewfb611b2e...fbd2caa7. [Actual native journal](deepseek-native-active-read-limits-retest-1.json) records **one new request/five actual calls/two context recoveries**, no schema/read-bound rejections, and real child Plan2. The earlier exhausted request and all usage/unknown holds remain unchanged; request-local rounds are explicitly new, not relabelled preserved.

Calls1–3 pass current read guidance, then [first frozen failure](deepseek-native-active-read-limits-retest-1-context-failure.json) cannot admit97,824 bytes even at minimum source pages. Generic v2 hard-pressure inventory indexing keeps all known/focused paths, full queryable inventory provenance and honest partial markers, while sharing per-file read hints. [Checks](inventory-headroom-checks.json) at7cfed8c pass **1757 default/62 skipped/52 focused**,3 pre-fix failures/1 pass; [copy replay](inventory-headroom-context-replay.json) fits94,236 bytes with no model/live-store writes. [Isolated deployment](inventory-headroom-deployment.json) verifies five role children/four health endpoints. [Recovery1](deepseek-native-active-read-limits-retest-1-recovery-1.json) preserves exact payload/read3/repair0 and admits an actual **94,247-byte** prompt. Its parsed response requests the last read round; [second frozen failure](deepseek-native-active-read-limits-retest-1-context-failure-2.json) is another pre-model context guard, not another paid call.

A copy replay reproduces **97,700 bytes** at minimum pages. Background projection at702bc55 pages status/snapshot/old conversation/history details by task-bound hashes, retains the first literal of repeated complete human text plus each event/hash/chronological occurrence, and preserves permission/readiness facts, current Plan/rules/commands/current pages and repository instructions. Omitted originals roundtrip exactly; no generated summary invents a decision. [Checks](background-pressure-checks.json) pass **1762 default/62 skipped/57 focused**,2 pre-fix failures/3 passes; initial fixture-pressure calibration failures are retained separately. Compileall/runtime integrity/diff pass. [Controlled replay](background-pressure-context-replay.json) fits **95,416 bytes with15,413 actual source bytes** and never calls a model or writes live DB/artifacts. [Deployment](background-pressure-deployment.json) touches only the isolated idle host roles.

[Recovery2](deepseek-native-active-read-limits-retest-1-recovery-2.json) preserves exact failed payload/read4/repair0. The actual **95,764-byte** prompt delivers15,413 source bytes with zero read requests/rounds remaining. Real call5 saves Plan2 `9a38b9e3-c0f5-42b0-9b34-2c04bdc20d57`, record hash `81c0180fa3c6c9d38c4fe4f0ff3ee4b4f5ccf39b160ec30a41bdc9a3a447e8d6`. It adds both producer files, retains consumer scope and all four exact original CI commands. The old parent stays immutable and no manual Plan insertion/business patch occurs. The grant stops at saved child; the second new request is unused. [Batch audit](deepseek-native-plan2-batch-audit.json) verifies381 events/74 old files, SQLite integrity/FKs, sanitized records, no execution approval, and **162,269 input/27,330 output** added across5 calls. Lifetime **59 calls/1,474,279 input/147,591 output**, old unknown holds **1,350,000/40,960**, remaining **5 calls/1,175,721 input/11,449 output** under64/4M/200K. Two pre-model failures stay failed with zero model calls; they are not hidden or billed as calls.

[Operator review](deepseek-native-plan2-source-review.json) is **not independent ReviewRun**. Plan2 promises two extension regression categories but has no extension/test writable path; its only test path is the web regression. Runtime freezes exactly files_likely_to_change and denies paths outside it, so these promised tests require another native child before execution approval. No actual denied patch or Coding run occurs. Static ready does not establish preparation, semantic Plan quality or native success.

Generic PlanningPlan metadata at8470ca1 clarifies that every promised source/config/new or modified test must have a concrete writable path. Existing min/max/schema/runtime permissions are unchanged. [Checks](write-scope-guidance-checks.json) pass **1762 default/62 skipped/75 focused**, compileall/runtime integrity/diff; the initial wrong focused-test filename is recorded. [Deployment](write-scope-guidance-deployment.json) preserves both Plans/history/budget and healthy isolated services. Zero calls use this new description so far; native effectiveness remains not_run.

[New-parent preview](deepseek-native-plan3-test-scope-proposal.json), hash `3bb12cfda683b58e88967d944329bbd620df18c5133ab1cfdbc19a3869f95f0e`, requests **one new read-only request/at most five existing calls**, pinned to Plan2. It retains six current paths/four commands and explicitly adds extension/test/transcript-parser.test.ts plus extension/test/hook-server.test.ts using the inspected tsx/node:test layout. No cap/Profile change, manual Plan patch, execution or publication. Confirmation is pending because the preceding exact grant stopped at child Plan and bound the old parent. [Final audit](native-plan2-final-audit.json) records source/health/provenance and this pending gate. Native first outcomes0/4, full environment baselines0/15, historical Node failure, Linux amd64 blocker and unrun Baseline/Coding/Verify/independent Review remain unchanged; UPEVE-E04 stays current. This is a real planning substep, not native end-to-end delivery.

## Plan3 scope revision fails on known truncation; read reuse awaits native verification

[Exact approval](deepseek-native-plan3-test-scope-approval.json) binds the prior
preview `3bb12cfd...69f95f0e`. The [native request](deepseek-native-plan3-test-scope.json)
on8470ca1 makes **three real DeepSeek calls**, with two valid read responses,
one real OCI source capture and no schema rejection. The last call receives a
**6,903-token** output limit because the task allowance is almost exhausted;
it fails `deepseek_completion_truncated`, with known34,414 input/6,903 output.
The raw reasoning/final-content split and provider balance are unknown. No
partial completion becomes a Plan, and no Plan3 is saved. Two old Plans remain
immutable; Plan2 still lacks promised extension test write paths and is now
context-stale after the test-source capture. The grant stops on that failure.

Lifetime **62 calls/1,572,455 input/159,040 output**, old three-call holds
**1,350,000 input/40,960 output**, unchanged limits64/4M/200K: remaining
**2 calls/1,077,545 input/0 output**. This task cap does not establish an empty
provider account. No retry clears holds or rewrites the failed read2/repair0.

Generic v2 read reuse atc4cfabe reuses only actual earlier admitted repository
pages under the same frozen task/source/Plan/state/policy/image/Profile. Issued
checkpoint/model-input hashes and consumption-time binding are checked; source
selectors, exact text and partial flags remain truthful. Explicit provenance
states zero new repository IO. Changed source/Plan/model, stop, legacy, corrupt
or unissued receipts cannot inherit pages. Budgets still gate access first;
new request-local counters never refund old usage or rewrite old rounds.

[Checks](read-reuse-checks.json) pass **1771 default/62 skipped/0 failed** and
**57 focused**, compileall/runtime integrity/diff. Two new cases fail against
the pre-fix planner; the initial unsaved-Profile fixture error is recorded and
fixed without relaxing a guard. [Copy-only replay](read-reuse-controlled-replay.json)
proves zero real output allowance still rejects a call. Simulated250K output
funding in the disposable copy admits an unissued **95,199-byte** prompt with
four verified old pages, preserving actual state/lease checks and making no
model call or native DB/artifact write. Simulated funding is not real consent.
[Deployment](read-reuse-deployment.json) restarts only five idle owned roles,
matches all236 tested source files and checks four healthy endpoints; no new
native request/call follows this maintenance. Reuse effectiveness is **not_run**.

[Audit](native-plan3-read-reuse-final-audit.json) verifies396 native events,
74 historical files, SQLite integrity/foreign keys and503 sanitized JSON files
without credential findings. [Output-only preview](deepseek-native-plan3-output-only-proposal.json),
hash `635ad29f7af7454f5f89e4c3d7736220934e312e1b2215145a61a08bb8eb3ee1`,
awaits exact resource approval: output200K→250K, unchanged calls64/input4M and
all use/holds, one new read-only Plan2 child request/at most two remaining calls.
It binds the current source and tested engine and retains eight intended paths/
four checks. The actual cap is unchanged. No execution/publication approval,
native Baseline/Coding/Verify/independent Review or new unseen success exists.
Native first deliveries0/4 and complete environment baselines0/15, historical
Node failure, Linux amd64 blocker and current UPEVE-E04 remain unchanged.

## Approved output-only extension: native Plan3 saved, execution approval pending

[Exact resource approval](deepseek-native-plan3-output-only-approval.json), hash
`c14bb91a0e081aabb94c37bf1fc4825404597756833ec4515b74a3e464f25888`,
permits only output200K→250K and one read-only child-Plan request, at most two
calls. The native budget API preserves calls64/input4M and every old known use/
unknown hold. [Actual result](deepseek-native-plan3-output-only.json) uses
**one real DeepSeek call**, **34,208 input/4,211 output**, without truncation,
schema rejection or context recovery. The actual **95,680-byte** prompt
includes exactly the four earlier issued source-page objects, verified by
checkpoint and actual model-input hashes; reuse executes zero repository IO.
Read0/repair0 are new request counters, never a refund of old failed rounds.

The native workflow saves Plan3`c4983f55-0c00-4b2a-ad86-73a45b85b27e`, record
hash`ea61d04c2a973a1aa9ff84a5984983a2d6cece904746829f8f0c3b47f9b0d00b`,
content hash`7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed`.
All six old paths and four exact command IDs/argv/cwd remain; the two extension
test paths are added and linked to concrete start/end/failure assertions using
the existing node:test style. [Operator scope review](deepseek-native-plan3-source-review.json)
finds the prior missing-test-scope blocker resolved. It is not independent
Review, real Issue reproduction or an actual test run. Source was not patched
by the operator and no Plan was manually inserted.

The grant stops at saved Plan3, leaving its second call unused. Lifetime
**63 calls/1,606,663 input/163,251 output**, old holds1,350,000/40,960 across
three unknown calls; remaining **1 call/1,043,337 input/45,789 output**.
[Audit](deepseek-native-plan3-output-only-audit.json) verifies402 journal events,
74 historical files,236 unchanged source files, SQLite integrity/foreign keys
and510 sanitized JSON files without credential findings. The tested c4cfabe
deployment remains1771 default passed/62 skipped/57 focused; no production
change requires a repeated suite or redeploy. Four health endpoints pass.

[Corrected read-only head probe](deepseek-native-plan3-head-proxy-diagnosis.json)
gets HTTP200 and the same frozen main SHA through the existing narrow proxy.
[Initial probe failure](deepseek-native-plan3-head-observation.json) is retained:
the operator supplied the proxy-key hex string where decoded bytes were
required. Correcting the probe does not change service code, settings,
credentials or permissions; it was not a network/token or native-workflow failure.

[Exact execution preview](deepseek-native-plan3-execution-proposal.json), hash
`4275a961362c86b9c2ff1011ce71ae9da310e0d268a4846dabf15d29af788aa0`,
binds Plan3/snapshot/analysis/context/models, eight write paths/four checks,
workspace/image/policy, preflight and verification contract. It proposes
restricted dependency preparation, all four pre-change checks, native Coding,
real regression failure/repair, Verify and independent Review, with existing
v2 limits24 calls/200K input/32K output/1800s and at most two code repairs.
Review remains an independent evidence-bound bounded job. Planning old usage
and unknown holds are retained; no extra cap/Profile change or publication is
included. Static ready is not prepared or baseline-verified.

Execution consent is pending; “继续” is not silently interpreted as a new Plan
approval. Native Baseline/Coding/Verify/independent Review remain not_run.
Original first deliveries0/4 and complete environment baselines0/15, historical
Node failure, Linux amd64 blocker and all open stage gates remain unchanged.

2026-11-02: [Fresh readiness](deepseek-native-plan3-execution-readiness.json)
verifies the same preview, current upstream SHA via the product GitHubClient,
402 unchanged events,74 historical files,236 tested source files, five live
roles and four healthy endpoints;512 JSON files pass the credential-pattern
scan. An initial health check finds the owned Publisher stopped cleanly with
unknown cause. [Recovery](deepseek-native-plan3-publisher-recovery.json) starts
only that same verified container with zero pending Jobs, preserving original
image/config/mounts/permissions/restart policy and native journal. No formal
service, credential, model usage or execution approval changes. Exact Plan3
execution confirmation remains pending; “继续” is not treated as approval.
