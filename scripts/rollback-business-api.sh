#!/usr/bin/env bash
# Restore only business-api to the immutable image retained by deploy-business-api.sh.
set -euo pipefail

backup_dir="${1:-}"
if [[ -z "$backup_dir" || ! -d "$backup_dir" ]]; then
  echo "usage: $0 <rollback-backups/business-api-<timestamp>>" >&2
  exit 2
fi
backup_dir="$(cd "$backup_dir" && pwd)"

for file in env.backup docker-compose.yml docker-compose.accounts.yml rollback-image.yml old-image-tag sibling-container-ids.txt; do
  [[ -f "$backup_dir/$file" ]] || { echo "missing rollback file: $backup_dir/$file" >&2; exit 2; }
done

rollback_image="$(<"$backup_dir/old-image-tag")"
compose=(
  docker compose --project-name model-server --env-file "$backup_dir/env.backup"
  -f "$backup_dir/docker-compose.yml"
  -f "$backup_dir/docker-compose.accounts.yml"
  -f "$backup_dir/rollback-image.yml"
)

docker image inspect "$rollback_image" >/dev/null
"${compose[@]}" up -d --no-deps --no-build --force-recreate business-api

container_id="$("${compose[@]}" ps -q business-api)"
[[ -n "$container_id" ]] || { echo "rollback did not create business-api" >&2; exit 1; }
for attempt in {1..20}; do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
  [[ "$health" == "healthy" ]] && break
  if [[ "$attempt" -eq 20 ]]; then
    echo "rolled-back business-api did not become healthy (state: $health)" >&2
    exit 1
  fi
  sleep 2
done

while read -r service expected_id; do
  [[ -z "$service" || -z "$expected_id" ]] && continue
  actual_id="$("${compose[@]}" ps -q "$service")"
  [[ "$actual_id" == "$expected_id" ]] || {
    echo "rollback unexpectedly changed $service ($expected_id -> ${actual_id:-missing})" >&2
    exit 1
  }
done < "$backup_dir/sibling-container-ids.txt"

set -a
# shellcheck disable=SC1090
source "$backup_dir/env.backup"
set +a
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
  "https://${PUBLIC_DOMAIN}/health/live" >/dev/null
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
  "https://${PUBLIC_DOMAIN}/health/ready" >/dev/null

echo "ROLLBACK OK: business-api restored from $rollback_image"
echo "rollback material retained at $backup_dir"
