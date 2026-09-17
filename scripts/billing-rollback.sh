#!/usr/bin/env bash
# SUPERSEDED — kept only so the instruction scripts/billing-deploy.sh prints at the
# end of a deploy still resolves. It now delegates to
# scripts/billing-rollback-device-principal.sh with the same argument.
#
# The rollback this file used to perform was wrong in three ways, so its old
# behaviour is deliberately not preserved:
#   1. it rebuilt ai_private.quota_status / public.ai_quota_service from the 006/008
#      migrations, which are several generations behind production's actual last
#      migration (016 changed both bodies, 014 changed quota_status before that), so
#      it would roll the quota logic back to behaviour production has not run;
#   2. it restored the code but never the pre-017 schema, so the pre-deploy Python
#      (which reads billing_private.account_entitlements where user_id = ... and
#      calls billing_private.ensure_account(uuid)) came back to missing-column and
#      missing-function errors: the "rolled back" system was more broken than before;
#   3. it ignored 202609170017-202609170020 entirely, including the data mapping that
#      makes the pre-M4 code unable to read the migrated tables.
# The replacement reverses the database with a fail-closed guard and restores the
# pre-deploy code in one script; see its header for the order and the one seam that
# is not atomic.
set -euo pipefail

script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"

echo "scripts/billing-rollback.sh is superseded by scripts/billing-rollback-device-principal.sh." >&2
echo "The old 006/008 function restore and the code-only rollback are gone; the" >&2
echo "replacement reverses migrations 202609170017-202609170020 in the database and" >&2
echo "then restores the pre-deploy code. Delegating with the same arguments." >&2

exec "$script_dir/billing-rollback-device-principal.sh" "$@"
