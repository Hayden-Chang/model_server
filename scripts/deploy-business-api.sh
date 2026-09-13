#!/usr/bin/env bash
# Build and switch only business-api, automatically restoring its previous image on failure.
set -Eeuo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
candidate_sha="${1:-}"
[[ -n "$candidate_sha" ]] || { echo "usage: $0 <exact-candidate-git-sha>" >&2; exit 2; }

env_file="${MODEL_SERVER_ENV_FILE:-$repo_dir/.env}"
rollback_root="${MODEL_SERVER_ROLLBACK_ROOT:-$repo_dir/rollback-backups}"
if current_sha="$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null)"; then
  resolved_candidate="$(git -C "$repo_dir" rev-parse "${candidate_sha}^{commit}")"
  [[ "$current_sha" == "$resolved_candidate" ]] || {
    echo "candidate mismatch: checkout=$current_sha requested=$resolved_candidate" >&2
    exit 2
  }
  git -C "$repo_dir" diff --quiet
  git -C "$repo_dir" diff --cached --quiet
else
  [[ -f "$repo_dir/.candidate-source-sha" ]] || {
    echo "release archive is missing .candidate-source-sha" >&2
    exit 2
  }
  current_sha="$(<"$repo_dir/.candidate-source-sha")"
  resolved_candidate="$candidate_sha"
  [[ "$current_sha" == "$resolved_candidate" ]] || {
    echo "candidate mismatch: archive=$current_sha requested=$resolved_candidate" >&2
    exit 2
  }
fi
[[ -f "$env_file" ]] || { echo "missing production env file: $env_file" >&2; exit 2; }

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$rollback_root/business-api-$timestamp"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
cp "$env_file" "$backup_dir/env.backup"
chmod 600 "$backup_dir/env.backup"
cp "$repo_dir/docker-compose.yml" "$backup_dir/docker-compose.yml"
cp "$repo_dir/docker-compose.accounts.yml" "$backup_dir/docker-compose.accounts.yml"
cp "$script_dir/rollback-business-api.sh" "$backup_dir/rollback-business-api.sh"
chmod 700 "$backup_dir/rollback-business-api.sh"
printf '%s\n' "$current_sha" > "$backup_dir/candidate-sha"
if ! git -C "$repo_dir" status --short > "$backup_dir/candidate-status.txt" 2>/dev/null; then
  printf '%s\n' "archive release; source verified by .candidate-source-sha" \
    > "$backup_dir/candidate-status.txt"
fi

compose=(
  docker compose --project-name model-server --env-file "$env_file"
  -f "$repo_dir/docker-compose.yml"
  -f "$repo_dir/docker-compose.accounts.yml"
)
old_container="$("${compose[@]}" ps -q business-api)"
[[ -n "$old_container" ]] || { echo "business-api is not running" >&2; exit 1; }
old_image_id="$(docker inspect --format '{{.Image}}' "$old_container")"
rollback_image="model-server-business-api-rollback:${timestamp,,}"
docker tag "$old_image_id" "$rollback_image"
printf '%s\n' "$old_container" > "$backup_dir/old-container-id"
printf '%s\n' "$old_image_id" > "$backup_dir/old-image-id"
printf '%s\n' "$rollback_image" > "$backup_dir/old-image-tag"
cat > "$backup_dir/rollback-image.yml" <<EOF
services:
  business-api:
    image: $rollback_image
EOF

: > "$backup_dir/sibling-container-ids.txt"
for service in caddy time-fragment-api billing-worker litellm; do
  container_id="$("${compose[@]}" ps -q "$service")"
  if [[ -n "$container_id" ]]; then
    printf '%s %s\n' "$service" "$container_id" >> "$backup_dir/sibling-container-ids.txt"
  fi
done

switched=0
automatic_rollback() {
  status=$?
  trap - ERR INT TERM
  if [[ "$switched" -eq 1 ]]; then
    echo "deployment verification failed; restoring previous business-api image" >&2
    if ! "$backup_dir/rollback-business-api.sh" "$backup_dir"; then
      echo "AUTOMATIC ROLLBACK FAILED; run manually: $backup_dir/rollback-business-api.sh $backup_dir" >&2
    fi
  fi
  exit "$status"
}
trap automatic_rollback ERR INT TERM

echo "rollback ready: $backup_dir/rollback-business-api.sh $backup_dir"
"${compose[@]}" build business-api
"${compose[@]}" up -d --no-deps --force-recreate business-api
switched=1

new_container="$("${compose[@]}" ps -q business-api)"
[[ -n "$new_container" && "$new_container" != "$old_container" ]]
for attempt in {1..20}; do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$new_container")"
  [[ "$health" == "healthy" ]] && break
  if [[ "$attempt" -eq 20 ]]; then
    echo "new business-api did not become healthy (state: $health)" >&2
    false
  fi
  sleep 2
done

while read -r service expected_id; do
  [[ -z "$service" || -z "$expected_id" ]] && continue
  actual_id="$("${compose[@]}" ps -q "$service")"
  [[ "$actual_id" == "$expected_id" ]] || {
    echo "deployment unexpectedly changed $service ($expected_id -> ${actual_id:-missing})" >&2
    false
  }
done < "$backup_dir/sibling-container-ids.txt"

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
  "https://${PUBLIC_DOMAIN}/health/live" >/dev/null
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
  "https://${PUBLIC_DOMAIN}/health/ready" >/dev/null
PUBLIC_DOMAIN="$PUBLIC_DOMAIN" BUSINESS_API_KEY="$BUSINESS_API_KEY" ADMIN_API_KEY="$ADMIN_API_KEY" \
  "$script_dir/verify-production.sh"
python3 "$script_dir/verify-bare-title-production.py" "https://${PUBLIC_DOMAIN}"

trap - ERR INT TERM
echo "DEPLOY OK: business-api now serves $resolved_candidate"
echo "manual rollback: $backup_dir/rollback-business-api.sh $backup_dir"
