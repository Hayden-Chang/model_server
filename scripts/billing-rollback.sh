#!/usr/bin/env bash
# Roll the billing rollout back to the pre-deploy state: previous code and the
# pre-deploy quota function definitions. The additive billing tables stay in
# the hosted database (the pre-deploy code ignores them) — nothing is lost.
# Usage (root on the server): scripts/billing-rollback.sh <backup-dir>
# After the code rollback, apply the restored function definitions:
#   /tmp/rollback-functions.sql  → Supabase SQL editor or supabase db push.
set -euo pipefail
BACKUP_DIR="${1:-}"
if [[ -z "$BACKUP_DIR" || ! -d "$BACKUP_DIR" ]]; then
  echo "usage: $0 <rollback-backups/billing-<timestamp>>" >&2
  exit 1
fi
cd /opt/model_server
PREV_SHA=$(cat "$BACKUP_DIR/pre-deploy-sha")
echo "rolling back to $PREV_SHA (backup $BACKUP_DIR)"
docker compose -f docker-compose.yml -f docker-compose.accounts.yml \
  stop billing-worker time-fragment-api caddy 2>/dev/null || true
git checkout "$PREV_SHA"
# Function-level restore material (the hosted database still runs the new
# definitions; apply this file to restore the pre-deploy quota behavior).
{
  sed -n "/create function ai_private.quota_status/,/\\\$\\\$;/p" \
    "$BACKUP_DIR/rollback-functions-006.sql"
  grep -A2000 'create or replace function public.ai_quota_service' \
    "$BACKUP_DIR/rollback-functions-008.sql"
} > /tmp/rollback-functions.sql
echo "restored function SQL: /tmp/rollback-functions.sql (apply to hosted Supabase)"
docker compose -f docker-compose.yml -f docker-compose.accounts.yml \
  build business-api time-fragment-api
docker compose -f docker-compose.yml -f docker-compose.accounts.yml \
  up -d caddy time-fragment-api business-api
docker compose -f docker-compose.yml -f docker-compose.accounts.yml \
  rm -sf billing-worker 2>/dev/null || true
sleep 8
curl -fsS https://api.keeline.xyz/health/live
curl -fsS https://api.keeline.xyz/health/ready
echo "ROLLBACK CODE OK — apply /tmp/rollback-functions.sql to finish"
