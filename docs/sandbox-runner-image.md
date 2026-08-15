# Sandbox Runner image

Phase 5 uses one Dockerfile at `containers/sandbox-runner/Dockerfile` for both
required execution platforms:

- `linux/arm64` on macOS arm64 with OrbStack;
- `linux/amd64` on Linux amd64 with Docker Engine.

The image starts from the digest-pinned multi-platform
`node:22-bookworm-slim` manifest and adds Debian's Python 3, virtual-environment,
Git, CA-certificate, and `tini` packages. It therefore supplies the minimum
general runtime needed for the MVP's Python and TypeScript repositories without
including Docker, SSH, credentials, or a model client. The build accepts only
`arm64` and `amd64`, records the target architecture and SandboxPolicy version
as OCI labels, and finishes as UID/GID `65532:65532`.

Repository dependencies are not installed by this image build. Any indispensable
dependency preparation remains the separate, credential-free, allowlisted-proxy
operation defined by P5-T17. Verify remains networkless.

## Build one OCI index

From the repository root:

```text
docker buildx build \
  --platform linux/arm64,linux/amd64 \
  --provenance=false \
  --file containers/sandbox-runner/Dockerfile \
  --output type=oci,dest=/tmp/contribos-sandbox-runner-v1.oci.tar \
  containers/sandbox-runner
```

The output is one OCI image index with an arm64 manifest and an amd64 manifest
built from the same Dockerfile and base manifest digest. A local OCI archive is
acceptance evidence only; it is not committed. Publication, signing, SBOMs, and
reproducible release assembly remain Phase 9 work.

The Phase 5 acceptance build produced these immutable identifiers:

| Object | Digest |
| --- | --- |
| OCI index | `sha256:489871c92fb023c70178d6ae5a437c682814539ac2266f1e71fd3dbc19c2c7c6` |
| arm64 manifest | `sha256:88f41762093377cc27d989427ac02d333071e41fff6575a8869af9b5b47e212e` |
| arm64 config / local image ID | `sha256:5f7e99efb18aab99da3770f77d055b09861349bb1383f300c041524f08159fce` |
| amd64 manifest | `sha256:94d9cd93e021e6771072c90da3f52f518d4c8541acd54ef84d16c91de10053db` |
| amd64 config / local image ID | `sha256:b1d4632b1e0ca22eb38ed2fa0d8c9a7fd1525812e62c097cea45482c471c264b` |

Manifest digests, config digests, and local image IDs are different objects.
Every Sandbox JobSpec and `contribos doctor` invocation uses the local
platform's config digest/image ID. In particular, Linux must not reuse the
arm64 config digest merely because both variants came from the same OCI index.

## Transfer the exact amd64 image to Linux

Docker Engine's `docker image load` consumes a Docker image archive, not the
multi-platform OCI layout above. Export the already-built amd64 result from the
same Buildx cache without rebuilding any layer:

```text
docker buildx build \
  --builder contribos-multiarch \
  --platform linux/amd64 \
  --provenance=false \
  --file containers/sandbox-runner/Dockerfile \
  --output type=docker,name=contribos/sandbox-runner:p5-amd64,dest=/tmp/contribos-sandbox-runner-v1-linux-amd64.docker.tar \
  containers/sandbox-runner
```

The Phase 5 export has:

```text
archive SHA-256:
96528d17caeb5266dbf83ae403aa11a4d92ac2f3cb3453e8a51165ea6ab4c817

loaded image ID:
sha256:b1d4632b1e0ca22eb38ed2fa0d8c9a7fd1525812e62c097cea45482c471c264b
```

Copy that exact uncommitted archive to the native Linux amd64 acceptance host.
Before loading it, verify the archive SHA-256. Then run:

```text
docker image load \
  --input /path/to/contribos-sandbox-runner-v1-linux-amd64.docker.tar

docker image inspect \
  contribos/sandbox-runner:p5-amd64 \
  --format '{{.Id}} {{.Os}}/{{.Architecture}} {{.Config.User}}'
```

The inspection must return the exact image ID above, `linux/amd64`, and
`65532:65532`. A rebuilt, retagged-to-another-ID, wrong-platform, or
hash-mismatched artifact is not valid P5-G06 evidence.

## Runtime policy

The image's default user is necessary but not sufficient isolation. The Sandbox
Worker must still apply the accepted signed JobSpec and `sandbox-policy-v1`
runtime flags: read-only root, stage-specific workspace access, dropped
capabilities, no-new-privileges, resource limits, no host namespaces/devices or
secret mounts, and `network=none`.

## Doctor

Run the fail-closed prerequisite check with the local platform image's exact
config digest:

```text
contribos doctor --runner-image sha256:<64 lowercase hex characters>
```

The command emits machine-readable JSON and exits zero only when all checks
pass: supported host/engine architecture, Python 3.11+, Docker 24+/API 1.43+,
buildx 0.12+, at least 6 GiB free workspace disk, memory/CPU/PID/seccomp/runc
controls, the null network driver, exact image platform/non-root/policy labels,
and a real restricted container probe. The probe runs with no network,
read-only root, all capabilities dropped, no-new-privileges, explicit CPU,
memory and PID limits, and a small tmpfs. Raw Docker stderr is never included in
the report.
