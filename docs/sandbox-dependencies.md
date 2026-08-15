# Isolated dependency preparation

P5-T17 treats package installation as an optional, separate trust boundary. It
never relaxes the default `network=none` rule for Explore, Implement, or Verify.
The step is used only when an approved execution needs dependencies that are not
already present in the digest-pinned Runner image.

## Signed input and immutable output

`dependency-preparation-plan-v1` binds the execution attempt and exact
Implement result, frozen workspace inventory, ecosystem, lockfile paths and
content hashes, Runner digest, SandboxPolicy hash, package-proxy policy hash,
and disk budget. The orchestrator authenticates that canonical plan with
`dependency-plan-hmac-sha256-v1` before the Worker starts Docker.

Only these inputs are accepted:

- Python: one hash lockfile; pip uses `--require-hashes`,
  `--only-binary=:all:`, isolated configuration, and no cache.
- Node: exact `package.json` plus `package-lock.json`; npm uses `ci`,
  `--ignore-scripts`, no audit, and no funding request.

The repository volume is mounted read-only. A distinct labelled dependency
volume is the only writable package output and is never returned as a public
path. The result records its complete file/symlink inventory hash, file count,
byte count, plan hash, proxy policy hash, image, policy, Implement result, and
overall result hash. A failed or timed-out preparation destroys that volume.

## Network boundary

The untrusted preparation container is attached only to a Docker
`--internal` network. It has no default public egress and receives no GitHub,
Provider/model, SSH, Docker, host-home, task, or application credential.
Its only configured HTTP(S) endpoint is `http://dependency-proxy:8080`.

The fixed trusted proxy is dual-homed: one side reaches the public package
service, and the other is the internal Worker network. Its versioned allowlist
is not supplied by repository content:

- Python: `pypi.org`, `files.pythonhosted.org`;
- Node: `registry.npmjs.org`.

The proxy rejects every other hostname and port, discards non-global DNS
answers (including loopback, private, link-local, and metadata addresses),
strips authentication/cookie headers from plain HTTP requests, bounds
connections and transferred bytes, and carries no application credentials.
The Worker waits for a local proxy readiness probe before preparation begins
and destroys the proxy container and internal network afterward.

Every package-manager child also receives the exact
[`sandbox-git-safety-v1`](sandbox-git-safety.md) environment. VCS dependencies,
submodules, hooks, credential helpers, LFS filters, SSH, and every Git transport
therefore remain disabled even while the allowlisted HTTP package proxy exists.

## Verify consumption

Verify may bind one optional dependency result after the exact Implement result
in its signed JobSpec. Migration `0018_dependency_verify_inputs` permits only
that ordered one-or-two-input shape in the durable stage chain.

The fresh, networkless Verify container mounts both the Implement workspace and
dependency volume read-only. Before and after every signed command, it
recomputes the dependency inventory and requires the preparation result's exact
hash. It exposes only fixed local paths for the Python venv or Node modules.
Any replaced dependency result, volume, image, policy, inventory, or input order
fails before successful result creation.

## Acceptance

Deterministic tests verify the fixed allowlists, signed plans, credential-free
environments, internal-network topology, one separate writable output,
Python/npm safety flags, cleanup, ordered JobSpec inputs, stale dependency
rejection, and read-only Verify mounts.

Real macOS arm64 + OrbStack acceptance against the digest-pinned Runner proves:

- a preparation container on the internal network cannot connect directly to a
  public IP;
- a metadata-IP request through the proxy is rejected with HTTP 403;
- the fixed PyPI endpoint is reachable only through the proxy;
- an empty hash lock creates a real isolated venv without credentials;
- the resulting volume is read-only and inventory-stable in a fresh
  `network=none` Verify container;
- all temporary volumes, networks, and containers are removed.

Native Linux amd64 + Docker Engine acceptance remains part of P5-G06 and is not
claimed by this macOS result.
