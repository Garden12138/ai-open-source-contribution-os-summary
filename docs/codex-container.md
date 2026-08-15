# Isolated Codex analysis container

Phase 3 introduced the isolated Codex CLI runtime. Phase 5 places its
`DockerCodexExecRunner` implementation under the standalone Sandbox Worker trust
domain (`app.sandbox_worker.container`); it does not execute the CLI or
untrusted repository code directly on the host. API, Provider, and Publisher
code neither import nor publicly re-export this runtime and never receive the
Docker socket. The Sandbox Worker receives only a materialized analysis
snapshot and provider-neutral invocation/task-authorization contracts.

The design follows the official Codex non-interactive guidance: use
`codex exec`, ephemeral sessions, explicit read-only sandboxing, ignored user
configuration, JSONL, and a JSON output schema. See
[Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

## Frozen snapshot contract

`ReadOnlySnapshot.capture()`:

- requires the source to be inside a dedicated allowed snapshot root;
- rejects a symlink root, escaping/absolute symlinks, special files, `.git`,
  `.env`, credential files, and oversized trees;
- hashes every relative path, file size/content, and symlink target into one
  manifest hash;
- verifies the manifest immediately before and after every container run.

The snapshot is mounted at `/workspace` with `readonly` and
`bind-propagation=rprivate`. A Codex invocation whose snapshot ID differs from
the mounted snapshot fails before Docker starts.

## Container policy v1

`codex-readonly-container-v1` enforces:

- a digest-pinned image reference;
- `--pull never`, so execution cannot contact a registry or consult registry
  credentials when the accepted image is missing;
- read-only root filesystem and workspace;
- `--network none`;
- UID/GID `65532:65532`;
- all Linux capabilities dropped and `no-new-privileges`;
- bounded CPU, memory, PIDs, tmpfs, prompt, output, and duration;
- ephemeral writable tmpfs only for `/tmp` and `/run/codex`;
- no host `HOME`, SSH files, Git credentials, Docker socket, or Provider
  credential in the container;
- `codex exec --ephemeral --ignore-user-config --ignore-rules
  --skip-git-repo-check --sandbox read-only --json --output-schema ...`.

The output schema is created in a short-lived host directory and mounted
read-only. The prompt crosses the boundary only through stdin. Raw stderr is
discarded, stdout is byte-bounded, and timeout cleanup force-removes only the
randomly named container created by that invocation.

## Internal model gateway policy

`codex-gateway-container-v1` is the only policy allowed to give the analysis
container network access. It requires a Docker network named
`contribos-model-gateway-*` and verifies with `docker network inspect` that the
network has `Internal=true` immediately before every run. The policy still
retains the read-only root/workspace, non-root user, capability, socket, tmpfs,
resource, and output restrictions above.

`GatewayTaskCredentialBroker` creates a signed `cgt1` authorization for one
Codex invocation. The authorization:

- expires after 60 seconds by default and can never exceed 300 seconds;
- is bound to the request, correlation, snapshot, stage, frozen input hash,
  Provider, model, and internal-gateway audience;
- permits at most 20 same-model requests so a bounded Codex tool loop can
  complete, then fails closed;
- is passed to Docker by environment-name inheritance, never as an argv value,
  and is neither persisted nor forwarded to the real model Provider.

The Sandbox Worker container receives only this task authorization as
`CODEX_API_KEY` and an internal `openai_base_url`. The real Provider credential
remains exclusively behind the gateway. `GatewayProviderCredential` has a permanently redacted
representation, and `CredentialedModelGatewayUpstream` applies it only at the
gateway-to-Provider transport call; the container task Token is not forwarded.
`create_model_gateway_app()` exposes only
`POST /v1/responses`; it bounds and validates JSON before authorization,
rejects wrong models, expired/tampered/exhausted authorizations and
credential-like content, bounds and sanitizes upstream responses, and returns
fixed redacted errors.

## Trusted policy and untrusted input framing

New analysis Jobs use `inspect-prompt-v2`, `analyze-prompt-v2`, and
`analysis-policy-v2`. `codex-prompt-envelope-v1` emits exactly three physical
records:

1. trusted policy JSON containing fixed rules, stage and version/hash metadata;
2. the exact UTF-8 byte length of the untrusted record;
3. one single-line JSON record containing the frozen repository, Issue, or
   prior-inspection data.

Newlines and Unicode line separators inside untrusted content remain escaped
inside the third JSON value. Its SHA-256 and byte length are included in the
trusted record, so content containing fake policy or framing labels cannot
create a new trusted record. The adapter accepts only the exact v2 prompt/policy
pair for new work. The retained v1 builder exists solely for deterministic
replay of already-versioned historical Jobs; mixed or unknown versions fail
closed.

The regression corpus in
`tests/fixtures/provider_prompt_injections.json` covers host-file reads, secret
disclosure, public/metadata network access, third-party writes, schema/citation
bypass, and budget bypass. Offline tests prove each string remains data and
cannot modify the model, schema, hashes, Docker argv, or external budget ledger.
The opt-in OrbStack test mounts the whole corpus and runs worst-case probes under
the same container policy; host/credential reads, root/workspace writes, Docker
socket access, and public DNS all remain unavailable.

## Image

The current image pins:

- `node:22-bookworm-slim` manifest digest
  `sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46`;
- `@openai/codex` version `0.146.0-alpha.3.1`.

`containers/codex-provider/Dockerfile` is the normal network build. It must be
built with that directory as its context so `.dockerignore` sends only the
Dockerfile.

`Dockerfile.offline` is the credential-free acceptance path. It accepts the
public main and Linux/arm64 npm tarballs, verifies their fixed SHA-512 values
inside the build, and assembles the image without npm, proxy, or network access.
Downloaded tarballs are temporary inputs and must never be committed.

## Acceptance

The default test suite validates command construction, mount policy, snapshot
hashing, stale/mismatched snapshots, denied paths, and environment allowlists
without requiring Docker.

Real macOS/OrbStack acceptance is opt-in:

```text
CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1
CONTRIBOS_CODEX_IMAGE=sha256:<local-image-id>
python -m pytest -q \
  tests/test_codex_container.py::test_real_codex_image_and_read_only_container_policy

CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1
CONTRIBOS_CODEX_IMAGE=sha256:<local-image-id>
python -m pytest -q \
  tests/test_provider_gateway.py::test_real_gateway_network_reaches_only_internal_service
```

It verifies the real Codex binary, read-only workspace/root, absent network and
Docker socket, writable ephemeral Codex home, the production runner path, and a
second policy where an analysis container can reach a temporary service on an
internal-only Docker network while public internet access remains blocked. A
credential-canary probe also starts Docker with GitHub, Provider, SSH-agent and
host-`HOME` values in the parent environment, then verifies none are visible or
mounted in the container and the Docker socket is absent. A real upstream model
response still requires a user-supplied Provider credential and is not inferred
from these isolation probes.
