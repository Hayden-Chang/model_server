#!/usr/bin/env bash
# Emergency rollback of the device-principal billing rollout (M4): reverse
# migrations 202609170017..202609170020 in the hosted database, then restore the
# pre-deploy code. Counterpart of scripts/billing-deploy.sh, which applies the
# migrations apart from the deploy: this is the script that undoes that whole
# change.
#
# Supersedes the earlier scripts/billing-rollback.sh, which restored the pre-deploy
# quota functions from the 006/008 migrations (generations behind production's
# 016), never restored the pre-017 column names or functions, and ignored
# 202609170017-202609170020 entirely. That name now delegates here.
#
# Usage (root on the server):
#   scripts/billing-rollback-device-principal.sh <rollback-backups/billing-TS> [--dry-run]
#
# Required environment:
#   SUPABASE_DB_URL   hosted Postgres connection string with the postgres password
#                     (psql must be on PATH; the SQL editor cannot run this file).
#                     This is the DIRECT database connection, not the REST URL:
#                       postgresql://postgres:<db-password>@db.<project-ref>.supabase.co:5432/postgres
#                     It is NOT in .env or .env.example -- scripts/billing-deploy.sh
#                     never needed database access, so the operator has to supply it.
#                     Test it before you need it:
#                       psql "$SUPABASE_DB_URL" -c 'select 1'
# Optional environment:
#   MODEL_SERVER_DIR  checkout to roll back (default /opt/model_server)
#
# ORDER, AND WHAT IS NOT ATOMIC
#   [1] preflight: refuse on a dirty checkout, then read-only probe of the database
#       shape, then the reverse migration's own guard (extracted from its
#       GUARD-BEGIN/GUARD-END block, so the safety decision has one source).
#   [2] stop billing-worker, time-fragment-api and caddy: the containers that call
#       billing_service/ai_quota_service and the public entrypoint. business-api
#       does not touch the billing tables, so it keeps running.
#   [3] apply the reverse migration. It is one transaction (BEGIN/COMMIT inside the
#       file), so it applies completely or not at all.
#   [4] git checkout "$PREV_SHA", rebuild, restart, health-check.
#   Steps 3 and 4 are not atomic together, and they cannot be: the database must be
#   pre-017 before the pre-M4 containers start, and each code generation only reads
#   its own column names (M4 reads principal, pre-M4 reads user_id), so the two
#   never overlap correctly. Traffic is stopped for the whole window.
#     * if [3] fails: the database is unchanged and the containers stay stopped.
#       Fix the reported problem and re-run; nothing was half-applied.
#     * if [4] fails: the database is already pre-017 while the M4 code is still
#       checked out. Nothing is serving (the containers are stopped), so re-run this
#       script from the copy it leaves in the backup directory — the preflight sees
#       the reversed database, skips [3] and retries [4]:
#         bash <backup-dir>/billing-rollback-device-principal.sh <backup-dir>
#       Do not start the M4 containers against the reversed database.
#   This script is safe to re-run: an already-reversed database is detected and the
#   migration step is skipped.
#
# THE REVERSE IS REFUSED when a device principal owns billing data, because the
# pre-017 schema cannot represent it. Fix forward is then the only lossless option;
# the guard message lists the alternatives. A refusal changes nothing: it happens
# before the first container stop and before the first ALTER.
set -euo pipefail

DOWN_SQL_REL="supabase/migrations/202609170099_billing_device_principal_down.sql"
DOWN_SQL_COPY="billing-device-principal-down.sql"
WRAPPER_COPY="billing-rollback-device-principal.sh"
HEALTH_BASE="https://api.keeline.xyz"

usage() {
  cat <<'EOF'
Usage: scripts/billing-rollback-device-principal.sh <rollback-backups/billing-TS> [--dry-run]

  <backup-dir>   directory scripts/billing-deploy.sh created; its pre-deploy-sha is
                 the code revision this script restores
  --dry-run      run the preflight for real, then print the steps without changing
                 anything (no stop, no migration, no git, no restart)
  -h, --help     show this help

Requires SUPABASE_DB_URL in the environment and psql on PATH.
EOF
}

DRY_RUN=0
BACKUP_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    --*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -n "$BACKUP_DIR" ]]; then echo "unexpected extra argument: $1" >&2; usage >&2; exit 2; fi
      BACKUP_DIR="$1"
      ;;
  esac
  shift
done
if [[ -z "$BACKUP_DIR" || ! -d "$BACKUP_DIR" ]]; then
  echo "usage: $0 <rollback-backups/billing-<timestamp>> [--dry-run]" >&2
  exit 2
fi
PREV_SHA_FILE="$BACKUP_DIR/pre-deploy-sha"
if [[ ! -f "$PREV_SHA_FILE" ]]; then
  echo "missing $PREV_SHA_FILE — pass the backup directory scripts/billing-deploy.sh created" >&2
  exit 2
fi
PREV_SHA="$(cat "$PREV_SHA_FILE")"
if [[ ! "$PREV_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "$PREV_SHA_FILE does not contain a commit SHA: $PREV_SHA" >&2
  exit 2
fi
if [[ -z "${SUPABASE_DB_URL:-}" ]]; then
  echo "SUPABASE_DB_URL is required: export the hosted Postgres connection string" >&2
  echo "(postgresql://postgres.<ref>:<password>@<host>:5432/postgres) and re-run." >&2
  exit 2
fi
if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required on PATH (postgresql-client): the reverse migration must run" >&2
  echo "as one transaction, which the Supabase SQL editor cannot guarantee here." >&2
  exit 2
fi
APP_DIR="${MODEL_SERVER_DIR:-/opt/model_server}"
# Absolute path to this script, resolved before the cd and before the code rollback
# checks out a revision that does not contain it: the copy left in the backup
# directory is what makes a failed run re-runnable.
SELF="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")"
[[ -f "$SELF" ]] || SELF="$0"
cd "$APP_DIR" || { echo "cannot cd to $APP_DIR (set MODEL_SERVER_DIR to the checkout)" >&2; exit 2; }

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then printf '+ %s\n' "$*"; return 0; fi
  "$@"
}
run_quiet() {
  if [[ "$DRY_RUN" -eq 1 ]]; then printf '+ %s\n' "$*"; return 0; fi
  "$@" >/dev/null 2>&1 || true
}
compose() { run docker compose -f docker-compose.yml -f docker-compose.accounts.yml "$@"; }
compose_quiet() { run_quiet docker compose -f docker-compose.yml -f docker-compose.accounts.yml "$@"; }

echo "[1/5] preflight: checkout state and database shape (read-only)"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "refusing to roll back: $APP_DIR is not a git checkout (set MODEL_SERVER_DIR)" >&2
  exit 1
fi
# Untracked files are expected (scripts/billing-deploy.sh leaves rollback-backups/),
# so only tracked modifications block the checkout.
dirty="$(git status --porcelain --untracked-files=no)"
if [[ -n "$dirty" ]]; then
  {
    echo "refusing to roll back: $APP_DIR has modified tracked files, and 'git checkout $PREV_SHA' would keep or reject them:"
    printf '%s\n' "$dirty"
    echo "commit, stash or discard them first. Nothing has been stopped and nothing has changed."
  } >&2
  exit 1
fi

PROBE_SQL="select case
  when exists (select 1 from information_schema.columns where table_schema='billing_private' and table_name='store_purchases' and column_name='principal') then 'post-017'
  when exists (select 1 from information_schema.columns where table_schema='billing_private' and table_name='store_purchases' and column_name='user_id') then 'pre-017'
  else 'unknown' end"
if ! shape="$(psql "$SUPABASE_DB_URL" -X -q -t -A -v ON_ERROR_STOP=1 -c "$PROBE_SQL")"; then
  echo "cannot read the database shape through SUPABASE_DB_URL. Nothing has been changed." >&2
  exit 1
fi
shape="${shape//[[:space:]]/}"
DOWN_SQL=""
if [[ -f "$APP_DIR/$DOWN_SQL_REL" ]]; then
  DOWN_SQL="$APP_DIR/$DOWN_SQL_REL"
elif [[ -f "$BACKUP_DIR/$DOWN_SQL_COPY" ]]; then
  DOWN_SQL="$BACKUP_DIR/$DOWN_SQL_COPY"
fi

case "$shape" in
  pre-017)
    echo "  database is already on the pre-017 shape: the reverse migration is skipped"
    ;;
  post-017)
    if [[ -z "$DOWN_SQL" ]]; then
      echo "missing $DOWN_SQL_REL in $APP_DIR and no copy in $BACKUP_DIR: restore the" >&2
      echo "post-M4 checkout (or the backup copy) before rolling back. Nothing changed." >&2
      exit 1
    fi
    GUARD_FILE="$(mktemp)"
    trap 'rm -f "$GUARD_FILE"' EXIT
    sed -n '/^-- >>> GUARD-BEGIN$/,/^-- <<< GUARD-END$/p' "$DOWN_SQL" > "$GUARD_FILE"
    if ! grep -q 'DEVICE_PRINCIPAL_ROLLBACK' "$GUARD_FILE"; then
      echo "cannot find the guard block in $DOWN_SQL: refusing to run an unguarded reverse" >&2
      exit 1
    fi
    echo "  running the reverse migration's guard (read-only) from $DOWN_SQL"
    if ! guard_out="$(psql "$SUPABASE_DB_URL" -X -q -t -A -v ON_ERROR_STOP=1 -f "$GUARD_FILE" 2>&1)"; then
      {
        printf '%s\n' "$guard_out"
        echo "----------------------------------------------------------------"
        echo "The reverse migration refused to run, so this script stopped here:"
        echo "  * no container was stopped, no git command ran, nothing changed"
        echo "Options:"
        echo "  1. Fix forward: keep 202609170017 applied, keep the M4 containers and"
        echo "     fix the defect in place. This is the only lossless option once a"
        echo "     device principal owns billing rows."
        echo "  2. If that data is expendable, delete the device-principal rows as the"
        echo "     guard message describes, then re-run this script."
        echo "  3. Re-run this script once the database is account-owned again."
      } >&2
      exit 1
    fi
    printf '%s\n' "$guard_out"
    run cp "$DOWN_SQL" "$BACKUP_DIR/$DOWN_SQL_COPY"
    run cp "$SELF" "$BACKUP_DIR/$WRAPPER_COPY"
    run chmod 0755 "$BACKUP_DIR/$WRAPPER_COPY"
    DOWN_SQL="$BACKUP_DIR/$DOWN_SQL_COPY"
    ;;
  *)
    {
      echo "unexpected database shape '$shape': neither billing_private.store_purchases.principal"
      echo "nor .user_id exists. This is not the M4 schema and not the pre-017 schema."
      echo "Nothing has been changed. Inspect the billing_private schema by hand."
    } >&2
    exit 1
    ;;
esac

echo "[2/5] stop the containers that read the billing tables and the public entrypoint"
compose_quiet stop billing-worker time-fragment-api caddy

if [[ "$shape" == post-017 ]]; then
  echo "[3/5] apply the reverse migration (single transaction, guard included)"
  if ! run psql "$SUPABASE_DB_URL" -X -q -v ON_ERROR_STOP=1 -f "$DOWN_SQL"; then
    {
      echo "----------------------------------------------------------------"
      echo "DATABASE REVERSE FAILED. The migration is one transaction, so the"
      echo "database is unchanged, but the containers listed above stay stopped."
      echo "Fix the reported error and re-run this script; nothing was half-applied"
      echo "and no code was checked out."
    } >&2
    exit 1
  fi
else
  echo "[3/5] database already reversed: skipped"
fi

echo "[4/5] restore the pre-deploy code ($PREV_SHA) and rebuild"
restore_code() {
  run git checkout "$PREV_SHA" &&
  compose build business-api time-fragment-api &&
  compose up -d caddy time-fragment-api business-api &&
  compose_quiet rm -sf billing-worker &&
  run sleep 8 &&
  run curl -fsS "$HEALTH_BASE/health/live" &&
  run curl -fsS "$HEALTH_BASE/health/ready"
}
if ! restore_code; then
  {
    echo "----------------------------------------------------------------"
    echo "CODE ROLLBACK FAILED after the database was reversed."
    echo "Current state: database pre-017, containers stopped, traffic down."
    echo "This is the only non-atomic seam in the rollback, and it is safe to retry:"
    echo "  bash $BACKUP_DIR/$WRAPPER_COPY $BACKUP_DIR"
    echo "The re-run detects the already-reversed database, skips the migration and"
    echo "retries the code rollback. Do not bring the M4 containers up against it."
  } >&2
  exit 1
fi

echo "[5/5] ROLLBACK OK: database is pre-017 and $PREV_SHA is serving"
echo "backup: $BACKUP_DIR (holds $DOWN_SQL_COPY and a re-runnable copy of this script)"
