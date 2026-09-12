#!/usr/bin/env bash
# Billing rollout (Phase A1-A6): backup, code update, build, start, verify.
# Run as root ON THE SERVER from /opt/model_server. Migrations must already be
# applied to the hosted Supabase project BEFORE this script runs.
set -euo pipefail
cd /opt/model_server
TS=$(date +%Y%m%d%H%M%S)
BACKUP_DIR="/opt/model_server/rollback-backups/billing-$TS"
mkdir -p "$BACKUP_DIR"
PREV_SHA=$(git rev-parse HEAD)
echo "$PREV_SHA" > "$BACKUP_DIR/pre-deploy-sha"
git status --short > "$BACKUP_DIR/pre-deploy-status.txt" || true
cp .env "$BACKUP_DIR/env.backup" && chmod 600 "$BACKUP_DIR/env.backup"
# Function-level rollback material: the pre-deploy quota definitions.
git show "$PREV_SHA":supabase/migrations/202609090006_ai_quota.sql \
  > "$BACKUP_DIR/rollback-functions-006.sql"
git show "$PREV_SHA":supabase/migrations/202609090008_ai_quota_safeupdate.sql \
  > "$BACKUP_DIR/rollback-functions-008.sql"
echo "backup: $BACKUP_DIR (pre-deploy SHA $PREV_SHA)"
git pull --ff-only
docker compose -f docker-compose.yml -f docker-compose.accounts.yml \
  build business-api time-fragment-api billing-worker
docker compose -f docker-compose.yml -f docker-compose.accounts.yml up -d \
  caddy time-fragment-api business-api billing-worker
sleep 8
curl -fsS https://api.keeline.xyz/health/live
curl -fsS https://api.keeline.xyz/health/ready
echo "DEPLOY OK — rollback: scripts/billing-rollback.sh $BACKUP_DIR"
