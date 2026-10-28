# ADR: one durable journal, staged tool jobs, provider-neutral adapter

Status: accepted for branch implementation; real native journey gate remains open.
Baseline: `27802bb5`; specification RC-01–RC-07.

## Decision

Extend the existing runtime with `app/agent_v2`: a single Workbench journal,
CAS checkpoints, a typed tool registry and separate leased model/tool jobs.
Preserve the original planner and exact domain approvals, fresh Verify,
independent Review and isolated Publisher. SDKs are optional executors behind
the same platform authority, never a second business state store.

This changes the execution mechanism: a model requests an individual source
read or exact patch, sees its actual receipt, runs approved checks, reads the
failure and edits again. The model no longer constructs the final multi-file
ChangeSet transaction. A trusted bridge derives that compatibility artifact
from actual sandbox snapshots. Prompt wording alone cannot provide this loop.

## Actual probes

Probes ran in `/tmp/arv2-sdk-spike`, with no credentials, networked model or
repository-code execution. Candidate packages were not added to this project.

| Candidate | Measured behavior | Integration consequence |
| --- | --- | --- |
| Existing journal + new loop | Durable multi-tool jobs, CAS, pause/restart, charged budgets and ambiguous tool outcome stop in `test_agent_v2_*` | Reuses the established authority and worker lease contract |
| LangGraph 1.2.18 / checkpoint-sqlite 3.1.2 | A graph reads, interrupts, closes SQLite, recompiles and resumes from that database | Useful engine; its checkpoint/interrupt authority still needs a Workbench CAS and domain-approval adapter |
| OpenAI Agents SDK 0.24.0 | Fixture Model makes three calls and two tools; SQLiteSession is reopened and its history recovered; tracing disabled | Tool runner works, but message history is not our immutable authorization/checkpoint transaction |
| Codex CLI 0.164.0 / app-server | Fresh HOME/CODEX_HOME, no credentials: initialize JSON-RPC handshake succeeds | Native model/tool execution was not exercised; filesystem/shell policy and thread state need a separately isolated harness adapter |

Exact probe classifications/versions are retained in
[framework-spikes.json](../acceptance/agent-runtime-v2/framework-spikes.json)
and [codex-protocol-spike.json](../acceptance/agent-runtime-v2/codex-protocol-spike.json).
These are technical probes, not comparative live-model performance results.
The selected implementation's real Docker probe repeatedly edits the same
file, observes an actual assertion failure, repairs it and passes fresh tests.
That is a controlled sandbox result, not a native autonomous contribution.

## Alternatives and boundaries

LangGraph would introduce graph/checkpointer serialization, replay and another
transaction boundary without removing the need for our approvals, worker RPC,
artifact hashes or idempotent tool receipts. Agents SDK similarly requires
adapters for our gateway, leases, exact approval and immutable journal; its
session memory alone does not establish exactly-once execution. Both remain
viable executor implementations if future evidence justifies their added cost.

Codex SDK/app-server is a credible separate coding harness. It is not selected
as the initial orchestrator: native CLI tools cannot be handed a Docker socket,
GitHub credentials or the host checkout. An adapter must preserve the limited
Sandbox Gateway and journal authority. xcli has the same requirement.

Only the explicitly configured structured-tools adapter is implemented here.
Text-only providers cannot edit. Native function-call support requires its own
capability/contract acceptance; it is not guessed from JSON-shaped prose.

Rollout defaults off. Existing v1 jobs keep their parser and immutable inputs.
Rollback drains v2 and restores matched code/database/artifacts; it never
rewrites historical events or moves active v2 jobs into a v1 parser.

## Sources checked for this decision

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence): checkpoint/store responsibilities and fault-tolerant threads.
- [Agents SDK running agents](https://openai.github.io/openai-agents-python/running_agents/): tool loop, turn budgets and state handling.
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk): programmatic thread lifecycle.
- [Codex app-server](https://learn.chatgpt.com/docs/app-server): client integration, approvals and streamed events.

The measured integration-cost judgment is specific to this repository, not a
claim that these frameworks lack durable execution.
