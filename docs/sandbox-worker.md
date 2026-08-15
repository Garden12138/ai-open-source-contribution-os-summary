# Sandbox Worker boundary

Phase 5 introduces `app.sandbox_worker` as a standalone process boundary. The
API/orchestrator communicates with it only through the
`sandbox-worker-v1` request/response contract; it does not import a container
runtime into the API process.

The initial boundary deliberately supports only `probe`. Repository checkout,
container creation, execution, and artifact finalization are added by later
Phase 5 tasks and remain fail-closed until their policies and job specifications
exist.

## Docker ownership

Only the Sandbox Worker owns the Docker runtime implementation. The hardened
Phase 3 runner now lives in `app.sandbox_worker.container` and is intentionally
absent from the public `app.providers` interface. Provider code supplies typed
model invocation and short-lived task-authorization contracts; it does not
create containers or receive Docker configuration. API/orchestrator and future
Publisher code likewise do not import the container module.

The standalone process client strips `DOCKER_HOST`, `DOCKER_CONTEXT`, and every
other inherited variable before starting its protocol subprocess. Docker access
will be attached only to the separately deployed Sandbox Worker runtime when
later Phase 5 execution operations are implemented. It is never forwarded
through the worker protocol or placed in a JobSpec.

## SandboxPolicy and authenticated JobSpec

`sandbox-policy-v1` is a canonical, hash-addressed policy. Its defaults cap a
job at 2 CPU, 2 GiB memory, 256 PIDs, 10 minutes, 4 GiB disposable disk, and
4 MiB logs. It requires a non-root UID/GID, read-only root, `network=none`,
`cap-drop ALL`, `no-new-privileges`, no host PID/IPC/devices, and no Docker
socket, host home, SSH agent, credentials, or model access. Only Implement may
write `/workspace`; callers may reduce resource ceilings but cannot weaken an
isolation invariant without introducing and reviewing a new policy version.

`sandbox-job-spec-v1` binds one Explore, Implement, or Verify stage to the exact:

- execution attempt and correlation IDs;
- repository and base commit SHA;
- exact repository archive SHA-256;
- task, AnalysisVersion, Snapshot, approved PlanVersion, approval, and approved
  state IDs/hashes;
- Provider contract hash;
- digest-pinned runner image and SandboxPolicy hash;
- predecessor artifact hashes, plan-bounded writable paths, and shell-free argv
  commands.

Explore is read-only and has no predecessor artifact. Implement requires both a
predecessor artifact and explicit allowed change paths. Verify requires a
predecessor artifact and at least one command, has no allowed change paths, and
remains read-only.

The canonical JobSpec payload has its own SHA-256. An HMAC-SHA-256 envelope
(`sandbox-job-spec-hmac-sha256-v1`) binds the complete payload, hash, and a
rotation-safe key ID. Verification rejects unknown keys, modified payloads,
hash/signature mismatches, or a policy version/hash different from the Worker’s
accepted policy. Signing key material is never serialized, logged, sent in the
worker request, or exposed to the untrusted container. The signed envelope is
bounded to 60,000 bytes so it fits inside the existing 64 KiB Worker protocol.

## Process transport

`SandboxWorkerProcessClient` starts one worker subprocess per request with:

- an absolute Python executable and fixed `app.sandbox_worker` module;
- no shell;
- stdin/stdout JSON framing bounded to 64 KiB;
- a 10-second default and 60-second maximum process timeout;
- fixed errors that never expose stderr;
- an exact minimal environment (`LANG`, `PYTHONDONTWRITEBYTECODE`, and
  `PYTHONUNBUFFERED`; Python may add `LC_CTYPE`);
- no inherited database URL, GitHub/Provider credential, `HOME`, SSH agent,
  Docker configuration, or application settings.

Each response must match the request and correlation IDs. Unknown operations,
extra probe data, malformed JSON, extra fields, credential-like payloads,
timeouts, stderr output, nonzero exit, and oversized or mismatched responses are
rejected.

## Testing

The offline suite launches the real module and verifies a different process ID,
the exact protocol, minimal environment, unsupported-operation behavior, and
credential rejection. `FakeSandboxWorkerClient` implements the same typed
interface for deterministic orchestrator tests.

This probe does not claim sandbox isolation. Docker/OrbStack ownership, policy,
signed JobSpec, multi-platform image parity, malicious fixtures, and real
execution remain separate Phase 5 gates.
