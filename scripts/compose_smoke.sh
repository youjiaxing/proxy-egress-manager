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
init_container=$(docker compose -p "$project" ps -aq init-core)
test "$(docker inspect "$init_container" --format '{{.State.ExitCode}}')" = 0
test "$(docker inspect "$init_container" --format '{{index .Config.Healthcheck.Test 0}}')" = NONE
docker compose -p "$project" exec -T proxy-egress-manager python main.py healthcheck
