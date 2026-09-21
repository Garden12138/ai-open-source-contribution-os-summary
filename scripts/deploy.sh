#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_FILE="docs/deployment/compose.yaml"

if [ -f .env ]; then
  echo "==> Deploying with .env configuration..."
  docker compose --env-file .env -f "$COMPOSE_FILE" up -d --build
else
  echo "==> Deploying with default configuration..."
  docker compose -f "$COMPOSE_FILE" up -d --build
fi

echo "==> Service status:"
if [ -f .env ]; then
  docker compose --env-file .env -f "$COMPOSE_FILE" ps
else
  docker compose -f "$COMPOSE_FILE" ps
fi
