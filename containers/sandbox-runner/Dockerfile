FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46

ARG TARGETARCH
ARG RUNNER_VERSION=sandbox-runner-v1
ARG SANDBOX_POLICY_VERSION=sandbox-policy-v1

RUN case "${TARGETARCH}" in \
      amd64|arm64) ;; \
      *) echo "unsupported target architecture" >&2; exit 1 ;; \
    esac \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
      ca-certificates \
      git \
      python3 \
      python3-venv \
      tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 65532 contribos \
    && useradd \
      --system \
      --uid 65532 \
      --gid 65532 \
      --home-dir /nonexistent \
      --shell /usr/sbin/nologin \
      contribos \
    && python3 --version \
    && node --version \
    && npm --version \
    && git --version \
    && tini --version

LABEL org.opencontainers.image.title="ContribOS Sandbox Runner" \
      org.opencontainers.image.version="${RUNNER_VERSION}" \
      org.opencontainers.image.description="Multi-architecture untrusted repository runner" \
      io.contribos.runner.architecture="${TARGETARCH}" \
      io.contribos.sandbox.policy="${SANDBOX_POLICY_VERSION}"

ENV HOME=/tmp/contribos-home \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /workspace
USER 65532:65532

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "--version"]
