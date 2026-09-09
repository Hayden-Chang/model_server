#!/usr/bin/env sh
# Emergency rollback from the account-aware AI cutover to the legacy stack.
#
# Order matters: public traffic is stopped first, post-cutover guest usage is
# reverse-exported into the legacy SQLite database, the new ledger gate is closed
# so an accidental overlay start cannot serve stale quota, and only then does the
# legacy Compose topology take over. Availability wins over quota fidelity unless
# --strict is set: a failed reverse export is reported and the legacy stack still
# starts.
set -eu

base_compose="docker-compose.yml"
overlay_compose="docker-compose.accounts.yml"
script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"
repo_dir="$(CDPATH= cd "${script_dir}/.." && pwd)"

dry_run=0
strict=0
skip_export=0
reset_import=1

usage() {
  cat <<'EOF'
Usage: scripts/rollback-account-ai-cutover.sh [options]

  --dry-run           print the plan without changing anything
  --strict            abort before switching authorities if the reverse export fails
  --skip-export       do not reverse-export Postgres usage (leaves SQLite counters stale)
  --no-reset-import   close the new gate but keep imported guest rows for inspection
  -h, --help          show this help

Run from anywhere; the script uses the repository that contains it. The default
project name and PUBLIC_DOMAIN come from the working directory's .env.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --strict) strict=1 ;;
    --skip-export) skip_export=1 ;;
    --no-reset-import) reset_import=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

cd "$repo_dir"

run() {
  if [ "$dry_run" -eq 1 ]; then
    printf '+ %s\n' "$*"
    return 0
  fi
  "$@"
}

compose_new() {
  run docker compose -f "$base_compose" -f "$overlay_compose" "$@"
}

compose_old() {
  run docker compose -f "$base_compose" "$@"
}

compose_rollback() {
  run docker compose -f "$base_compose" -f "$overlay_compose" --profile rollback "$@"
}

run_quiet() {
  if [ "$dry_run" -eq 1 ]; then
    printf '+ %s\n' "$*"
    return 0
  fi
  "$@" >/dev/null 2>&1 || true
}

compose_new_quiet() {
  run_quiet docker compose -f "$base_compose" -f "$overlay_compose" "$@"
}

for file in "$base_compose" "$overlay_compose"; do
  [ -f "$file" ] || { echo "missing ${file} in ${repo_dir}" >&2; exit 2; }
done

if [ "$dry_run" -eq 0 ]; then
  command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 2; }
  docker compose version >/dev/null 2>&1 || { echo "docker compose v2 is required" >&2; exit 2; }
  [ -f .env ] || { echo ".env is required in ${repo_dir}" >&2; exit 2; }
fi

if [ -z "${PUBLIC_DOMAIN:-}" ] && [ -f .env ]; then
  PUBLIC_DOMAIN="$(sed -n 's/^PUBLIC_DOMAIN=//p' .env | head -n 1)"
fi
[ -n "${PUBLIC_DOMAIN:-}" ] || { echo "PUBLIC_DOMAIN is required" >&2; exit 2; }

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${ROLLBACK_BACKUP_DIR:-${repo_dir}/rollback-backups/${timestamp}}"
backup_file="${backup_dir}/usage.sqlite3"

echo "[1/6] backup legacy SQLite quota database -> ${backup_file}"
if [ "$dry_run" -eq 0 ]; then
  mkdir -p "$backup_dir"
fi
compose_rollback run --rm --no-deps -v "${backup_dir}:/backup" quota-rollback \
  python -c "import sqlite3; src=sqlite3.connect('/var/lib/model-server/usage.sqlite3',timeout=30); dst=sqlite3.connect('/backup/usage.sqlite3',timeout=30); src.backup(dst); dst.close(); src.close(); print('backup ok')"
if [ "$dry_run" -eq 0 ] && [ ! -s "$backup_file" ]; then
  echo "backup verification failed: ${backup_file}" >&2
  exit 1
fi

echo "[2/6] stop the new public entrypoint"
compose_new_quiet stop caddy time-fragment-api

echo "[3/6] reverse-export post-cutover usage into the legacy database"
export_failed=0
if [ "$skip_export" -eq 1 ]; then
  echo "  skipped by --skip-export; legacy counters may be stale"
elif ! compose_rollback run --rm --no-deps quota-rollback \
  python -m app.reverse_ai_quota_export --step export; then
  export_failed=1
  echo "  warning: reverse export failed" >&2
fi
if [ "$export_failed" -eq 1 ] && [ "$strict" -eq 1 ]; then
  echo "strict mode: aborting before the legacy authority takes over" >&2
  exit 1
fi

echo "[4/6] close the new ledger gate"
close_failed=0
close_args="--step close"
if [ "$reset_import" -eq 1 ] && [ "$skip_export" -eq 0 ] && [ "$export_failed" -eq 0 ]; then
  close_args="${close_args} --reset-import"
fi
# shellcheck disable=SC2086
if ! compose_rollback run --rm --no-deps quota-rollback \
  python -m app.reverse_ai_quota_export $close_args; then
  close_failed=1
  echo "  warning: could not close the new ledger gate" >&2
fi

echo "[5/6] restore the legacy stack"
compose_new_quiet down
compose_old up -d

echo "[6/6] verify the legacy stack"
if [ "$dry_run" -eq 1 ]; then
  printf '+ curl https://%s/health/live\n' "$PUBLIC_DOMAIN"
  printf '+ curl -X POST https://%s/api/auth/guest\n' "$PUBLIC_DOMAIN"
  echo "dry run complete; no state changed"
  exit 0
fi

health_url="https://${PUBLIC_DOMAIN}/health/live"
attempt=1
while [ "$attempt" -le 10 ]; do
  if curl --fail --silent --show-error --connect-timeout 5 --max-time 10 "$health_url" >/dev/null 2>&1; then
    break
  fi
  if [ "$attempt" -eq 10 ]; then
    echo "legacy stack health check failed: ${health_url}" >&2
    echo "backup kept at ${backup_file}" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 3
done

curl --fail --silent --show-error --connect-timeout 5 --max-time 10 \
  -X POST "https://${PUBLIC_DOMAIN}/api/auth/guest" \
  -H 'Content-Type: application/json' \
  -d "{\"device_id\":\"rollback-check-${timestamp}\"}" >/dev/null

echo "rollback complete: legacy stack is serving and the new ledger gate is closed"
echo "backup: ${backup_file}"
if [ "$export_failed" -eq 1 ] || [ "$close_failed" -eq 1 ]; then
  echo "warning: quota sync was incomplete; inspect the log above before any re-cutover" >&2
  exit 1
fi
