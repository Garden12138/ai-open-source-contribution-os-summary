# UPEVE v1 acceptance ledger

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
