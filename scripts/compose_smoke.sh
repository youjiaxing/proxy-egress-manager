#!/usr/bin/env bash
# Isolated deployment test. Never target an existing deployment project.
set -euo pipefail
export PEM_IMAGE="${PEM_IMAGE:-pem:ci}" PUBLIC_URL=https://pem.invalid WEB_HOST_PORT="${WEB_HOST_PORT:-18082}"
export CORE_SECRET="$(openssl rand -hex 32)" ADMIN_PASSWORD="$(openssl rand -hex 24)"
export BUSINESS_NETWORK="pem-smoke-$(openssl rand -hex 8)"
project="$BUSINESS_NETWORK"
cleanup() {
  docker compose -p "$project" down -v --remove-orphans
  docker network rm "$BUSINESS_NETWORK"
}
docker network create "$BUSINESS_NETWORK"
trap cleanup EXIT
docker compose -p "$project" up -d --no-build --wait --wait-timeout 90
docker compose -p "$project" exec -T proxy-egress-manager python main.py healthcheck
