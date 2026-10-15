FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
      ca-certificates git python3 python3-venv python3-dev build-essential tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 65532 contribos \
    && useradd --system --uid 65532 --gid 65532 --home-dir /nonexistent \
      --shell /usr/sbin/nologin contribos

LABEL io.contribos.dependency-builder="source-wheel-v1" \
      io.contribos.sandbox.policy="sandbox-policy-v1"
ENV HOME=/tmp LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "--version"]
