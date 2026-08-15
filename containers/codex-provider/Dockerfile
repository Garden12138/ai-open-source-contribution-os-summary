FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46

ARG CODEX_VERSION=0.146.0-alpha.3.1

RUN npm install --global --omit=dev "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && codex --version

LABEL org.opencontainers.image.title="ContribOS Codex Provider"
LABEL org.opencontainers.image.version="${CODEX_VERSION}"
LABEL org.opencontainers.image.description="Pinned Codex CLI for isolated read-only analysis"

ENTRYPOINT ["codex"]
