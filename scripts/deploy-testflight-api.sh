#!/usr/bin/env bash
# Run on the server from a staged git archive, after migrations 024 and 025.
# This stages the sandbox API only; production/worker cutover is a separate gate.
set -euo pipefail
if [[ $# -ne 1 || ! "$1" =~ ^[a-f0-9]{40}$ ]]; then
  echo "usage: $0 <source-commit-sha> (run from the matching extracted release)" >&2
  exit 2
fi
RELEASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_ROOT="${MODEL_SERVER_ROOT:-/opt/model_server}"
cd "$RELEASE_DIR"
test -s docker-compose.accounts.yml
test -s "$SERVER_ROOT/.env"
test -d "$SERVER_ROOT/.secrets"
# Require an archive manifest written by the staging command, not a copied .git
# pointer that still refers to the developer's worktree on another machine.
[[ "$(cat .release-sha)" == "$1" ]]

# Fail before build/recreate if the hosted database has not crossed the gate.
SCHEMA_VERSION="$(docker exec model-server-time-fragment-api-1 python -c '
import os, httpx
key=os.environ["SUPABASE_SERVICE_ROLE_KEY"]
r=httpx.post(os.environ["SUPABASE_URL"].rstrip("/")+"/rest/v1/rpc/billing_environment_schema",
    headers={"apikey":key,"Authorization":"Bearer "+key},json={},timeout=20)
r.raise_for_status()
print(r.json())
')"
if [[ "$SCHEMA_VERSION" != 25 ]]; then
  echo "billing environment schema 25 is required before deployment" >&2
  exit 1
fi
if [[ ! -e .secrets ]]; then ln -s "$SERVER_ROOT/.secrets" .secrets; fi
[[ "$(cd .secrets && pwd -P)" == "$(cd "$SERVER_ROOT/.secrets" && pwd -P)" ]]

BACKUP_DIR="$SERVER_ROOT/rollback-backups/testflight-$(date +%Y%m%d%H%M%S)-${1:0:8}"
(umask 077; mkdir -p "$BACKUP_DIR")
echo "Recovery backup: $BACKUP_DIR"
printf '%s\n' "$1" > "$BACKUP_DIR/new-source-sha"
cp "$SERVER_ROOT/.env" "$BACKUP_DIR/env.backup"
cp "$SERVER_ROOT/Caddyfile.accounts" "$BACKUP_DIR/Caddyfile.accounts"
chmod 600 "$BACKUP_DIR/env.backup"
# Contains environment values; retain privately for manual recovery, never log it.
(umask 077; docker inspect model-server-testflight-api-1 > "$BACKUP_DIR/previous-container.json" 2>/dev/null) || true
OLD_IMAGE="$(docker inspect --format '{{.Image}}' model-server-testflight-api-1 2>/dev/null || true)"
if [[ -n "$OLD_IMAGE" ]]; then
  docker image tag "$OLD_IMAGE" "model-server-testflight-api:rollback-${1:0:8}"
  printf '%s\n' "$OLD_IMAGE" > "$BACKUP_DIR/previous-image-id"
fi
compose=(docker compose --project-name model-server --env-file "$SERVER_ROOT/.env"
  -f "$RELEASE_DIR/docker-compose.yml" -f "$RELEASE_DIR/docker-compose.accounts.yml")
"${compose[@]}" config --quiet
"${compose[@]}" build testflight-api
"${compose[@]}" up -d --no-deps --wait testflight-api
# Verify the running container before touching the public proxy.
docker exec model-server-testflight-api-1 python -c '
import os, httpx
assert os.environ["APPLE_ENVIRONMENT"]=="sandbox"
r=httpx.get("http://127.0.0.1:8000/health/ready",timeout=20)
r.raise_for_status()
'
cp Caddyfile.accounts "$SERVER_ROOT/Caddyfile.accounts"
docker exec model-server-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
curl --fail --silent --show-error --max-time 30 --dump-header "$BACKUP_DIR/health-headers.txt" \
  https://staging.api.keeline.xyz/health/ready > "$BACKUP_DIR/health.json"
if ! grep -qi '^x-apple-billing-environment: sandbox' "$BACKUP_DIR/health-headers.txt"; then
  echo "public sandbox environment header missing; inspect $BACKUP_DIR" >&2
  exit 1
fi
echo "TestFlight API ready at source $1; backup: $BACKUP_DIR"
echo "Production and billing workers were not recreated. Apple purchase/restore validation remains required."
