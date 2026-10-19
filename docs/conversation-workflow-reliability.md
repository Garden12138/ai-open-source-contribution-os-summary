# Conversation workflow reliability

Date: 2026-10-17. Scope: UXR-T01/T03/T04/T05 regression in the current Phase 5.

The existing hflow conversation completed implementation, four verification
commands (204 project tests), independent review and exact human acceptance.
Its next operation, preparing PR text/preview, failed before dispatch. This was
an orchestration/protocol failure, not a failed project test or missing approval.

## Root causes supported by saved evidence

| Boundary | Failure | Design correction |
| --- | --- | --- |
| Model schema → application validator | The four-field JSON schema permitted reads plus references, but an invisible validator forbade it. Both actual responses used this shape. | Separate read/final semantics in a pure, versioned protocol and publish the actual state/budget constraints in the output schema. |
| Validation → correction | Both rejection artifacts only reported `value_error` with an empty location; the model repeated the same error. | Structured semantic reason codes, actionable instructions and durable restoration of correction feedback after restart. |
| Evidence → dialogue | Current verified outcomes were represented as availability booleans while recent history included old failures. | Include authoritative current verification/acceptance facts and bootstrap ready-state task evidence; historical failures cannot erase current receipts. |
| Acceptance → next job | The exact human button generated ordinary “准备发布” text, which required another model to select the obvious next step. | Atomically record acceptance and a typed continuation; dispatch the existing persistent requirements → document → preview chain without a conversational model call. |
| Failure → user result | Both the generic Job loop and conversation coordinator emitted a final failure. | The serialized conversation coordinator owns its final message receipt. Job evidence remains intact. |
| Acceptance failure → recovery | Accepted work could have no usable next control once a conversation turn died. | An explicit binding-checked read-only continue endpoint/control; missing accounts lead to settings. Retries preserve their request key and remain bounded by existing document/job budgets. |
| Tests → user journey | Tests often supplied already-valid model decisions or began after dispatch. | Replay the actual failed shape and exercise 15 user turns plus one internal continuation through plan revisions, approval, implementation/review, acceptance, preview and an explicitly confirmed fake Draft publication. |

## Protocol and authority

`conversation-reasoning-v3` retains the four wire fields but gives each step one
meaning. A read step must use `action=respond`; it cannot dispatch a workflow.
Reply/citations attached to otherwise valid read requests are treated solely as
unused presentation, stored as evidence and never shown as verified claims.
Unknown fields/tools, workflow+read combinations, secret output, unsupported
references, malformed JSON and out-of-scope reads remain rejected. A final step
has no reads and can cite only observed evidence. The last call's schema forbids
further reads. Existing two-call/three-read/task token/model budgets remain.

`conversation-reasoning-v2` queued work retains strict v2 parsing and its consumed
budget. Restart restores rejection feedback/tool receipts; no old invocation or
failed Job is reset. Normalization cannot turn text into an action or approval.

The outer event envelope stays `conversation-v1`. Continuations use the separate
`continuation_version=accepted-publication-v1`; domain versions must not replace
the stream version. Commands bind the exact acceptance record/binding, task,
plan and message and are serialized through the existing durable message queue.
Internal commands belong to the orchestrator and are excluded from user dialogue;
existing source-linked internal messages are hidden only in projection, never
rewritten or deleted from the journal.
Stop, pending messages, busy work, changed acceptance, account verification and
the underlying publication service's hashes are rechecked. The native UI and
`POST /publication/continue` exchange only the current acceptance binding.

The upgrade reconciler only recovers a button-generated “准备发布” request whose
reasoning Job failed validation under a still-current human acceptance. It adds
a source/hash-bound recovery marker and a new typed request, leaving every old
event, Job attempt and model call unchanged. A subsequent user message, stop or
changed execution prevents automatic recovery. It does not reinterpret arbitrary
chat or recover external writes. Each accepted request is recovered at most once.

Actual Fork/Push/Draft PR creation, ready state, metadata changes and CLA signing
still require their own existing exact one-time confirmations. Preparation and
the upgrade recovery cannot create those confirmations.

## Verification and rollback

The deterministic journey uses fake model/sandbox/GitHub adapters. It proves
local contracts and interruption/replay behavior, not native Docker parity or
real publication. Real task recovery is verified separately after deployment;
human publication confirmation is left pending. UXR-G01 and native Linux P5-G06
remain open.

No persistent model/table change is introduced. Upgrading keeps v2 Jobs readable;
new v3 Jobs and continuation events have explicit versions. Before rollback,
stop intake, drain or safely stop new Jobs/continuations, and retain the SQLite
snapshot plus all content-addressed artifacts. Restore matching application and
data only after reconciling any external writes. An old service cannot process
v3 or continuation events and must not resume those tasks. Never delete the
original failed turn, reset its counters, or rewrite approvals to make rollback
appear successful. Final counts, real artifact hashes and deployment evidence
are recorded in `docs/tasks.md` after verification.
