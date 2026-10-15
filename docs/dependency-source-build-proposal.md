# Upstream source-wheel preparation proposal

Status: user authorized implementation on 2026-10-14; implemented as the optional
`workspace-dependency-plan-v4` / `source-wheel-v1` strategy. Existing v1/v2/v3
recipes remain wheel-only. This capability is required to execute Salt
task `86b6be40-2022-4975-ab04-2d56f1cc0f17` with its unchanged pytest requirements.

## Verified failure and general scope

The exact frozen dependency plan
`f1e4ccee3041ba53b93c5fc0c6f1cc4e274c709cd206c3c928d1298e5b3ab0cc`
was reproduced in the same digest-pinned Runner and SandboxPolicy. Dynamic
project metadata succeeded; runtime/test wheel resolution failed on `timelib`.
The frozen Salt base requirements include `timelib>=0.3.0` on Python 3.11.
[PyPI release files](https://pypi.org/project/timelib/#files) provide its source
archive but only a CPython 3.9/macOS arm64 wheel. The current Runner is Linux
Python 3.11 and includes no C compiler or Python development headers.

Every project requiring a dependency without a compatible wheel has the same
limitation. It cannot be fixed by generic retries, backend metadata support,
changing Salt's package name, omitting dependencies, or weakening its tests.

## Proposed explicit capability

Use a separately versioned, signed preparation strategy for upstream source
builds. Keep legacy v1/v2/v3 wheel-only recipes and hashes unchanged. Freeze the
new strategy, build image digest, compiler/Python ABI, policy and resource limits
in authorization and the preparation spec. No silent source fallback under old
approvals. A fresh plan confirmation is required for a task adopting this strategy.

1. Retain wheel-first resolution. Acquire indispensable source distributions only
   through the existing fixed PyPI/files.pythonhosted.org proxy. Validate declared
   package/version, download size, upstream SHA-256 and bounded archive structure;
   reject escaping paths/symlinks, VCS sources and alternate indexes.
2. Resolve declared build tools through the proxy as wheels, retain their exact
   SHA-256 lock and install offline. Unsupported build requirements fail explicitly.
   Retain source archives, hashes and backend declarations as immutable evidence.
3. Build sources in a separate credential-free container using a digest-pinned
   compiler/Python-header image matching the Runner's ABI. Mount source inputs
   read-only, use bounded tmpfs for all working files/output, and run offline with no model, Docker
   socket, GitHub credentials or host directories. Preserve the existing sandbox
   UID, dropped capabilities, process/time/memory/disk limits and Git safety policy.
4. Validate each built wheel's package identity, metadata and ABI. Transfer at
   most 32 MB of wheel bytes through bounded IPC to a separate trusted offline
   importer; the compiler has no writable volume or repository mount. Record source,
   build-tool, image/policy and wheel hashes. Resolve/install the combined wheel
   set offline using a hash lock. Never treat build success as test success.
5. Use the same read-only frozen dependency result for baseline and Verify. Keep
   original pytest targets. Preserve bounded structured failure evidence at every
   stage and clean scratch containers, networks and volumes after failure/cancel.

## Acceptance and recovery

Require deterministic signature/stale-input/failure/canary tests and real isolated
pure-Python and native-extension source builds. Reproduce failure with the old
strategy and verify preparation plus original Salt baseline with the new strategy.
Verify no-network builds, forbidden proxy destinations, malicious archives,
timeouts/cancellation and artifact integrity. Record macOS/OrbStack and native
Linux/Docker results separately; P5-G06 cannot be inferred from emulation.

No database schema change is implied by the proposal. Any actual persistent model
change still requires migration and recovery tests. Rollback must drain new-version
jobs and retain matching code/database/artifact backups; never reinterpret a source
recipe as a legacy wheel-only recipe. No automatic task approval or external write
is authorized by this proposal.

## Implemented bounds and local evidence

The optional `DEPENDENCY_BUILDER_IMAGE` must be a SHA-256 digest. API and Workers
freeze it, the new strategy and metadata strategy into preflight/authorization
and the signed dependency plan. Legacy approvals cannot opt into source builds.
Each preparation has at most eight source builds and one aggregate Runner timeout;
Worker cancellation/lease checks remain active. Scratch tmpfs is at most 512 MiB,
half the memory ceiling and one quarter of the disk budget. Wheel IPC has a 32 MB
binary limit, bounded reads and credential-free structured failures.

Source downloads are limited to 32 MB, expanded trees to 128 MB/20,000 entries;
absolute/parent escapes, symlinks/hardlinks and unknown request fields are rejected.
Only tar sdists and explicit build requirements available as wheels are supported;
build hooks requiring additional undeclared tools fail offline. The compiler image
is built from the same pinned Bookworm base and checks the exact Python major/minor
before building. Unsupported toolchains/ABIs do not fall back to host execution.

The actual frozen Salt source successfully prepared through this strategy and its
unchanged two pytest targets passed **7 tests** in a fresh networkless Runner.
Separate real static/dynamic/native-source fixtures passed **3 cases**, including
compiled timelib import, retained source/wheel receipts, original-code failure and
modified-source passing tests. This is environment diagnostic evidence, not a new
approved Salt execution, implementation/review result or native Linux acceptance.
