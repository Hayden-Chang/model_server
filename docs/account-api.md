# Account-aware AI API

The optional `time-fragment-api` service serves both identities, but not on every
route. `GET /api/account/quota` accepts either a Supabase account access token
(resolved through `ai_account_identity`) or the installation token, and
`POST /api/account/claim-guest` requires the account token plus the guest token
it is asked to merge. `/api/plan/parse` and every `/billing/*` route require the
device token and refuse an account session with `401 DEVICE_REQUIRED`. Quota is
reserved in Postgres and the private planner is called with an internal HMAC
credential.
The existing planner, correction loop and candidate-plan contract are reused.
An AI result never writes a user's cloud state or applies a plan to the App.

## Public contract

| Route | Credential | Behavior |
| --- | --- | --- |
| `POST /api/auth/guest` | installation ID | Existing guest token response, including after account sign-out. |
| `POST /api/plan/parse` | guest only | Existing V2 input/output; linked identities cannot execute the same request ID twice. An account token is refused with `401 DEVICE_REQUIRED` before any quota is reserved. |
| `GET /api/account/quota` | guest or account | `supportCode`, `limit`, `used`, `remaining`. |
| `POST /api/account/claim-guest` | account | Body `{"guest_token":"…"}`; verify both credentials and merge free usage by maximum. |
| `POST /billing/claims` | device token | Body `{"provider":"apple","productId":"…","claimId":"…"}` (`claimId` optional; the server mints one). Returns `claimId`, this device's `appAccountToken` and a one-hour `expiresAt`. An account session gets `401 DEVICE_REQUIRED`. |
| `GET /billing/entitlement` | device token | Device-scoped projection: `plan`, `status`, `validUntil`, `serviceEndAt`, `entitlementRevision`, `aiQuota` (`limit`/`used`/`remaining`/`resetsAt`) and `billingSources` (`provider`/`productId`/`expiresAt`). |
| `POST /billing/apple/verify` | device token | Body `{"signedTransaction":"…","productId":"…"}` (`claimId` optional); verifies the StoreKit JWS against the pinned Apple root, re-queries the chain's authoritative status and binds this device. Returns the `/billing/entitlement` projection; `503 BILLING_NOT_CONFIGURED` without the Apple key or store reference key. |
| `POST /webhooks/apple` | Apple JWS | No bearer credential: the `signedPayload` JWS is the authentication. Verified and deduplicated by `notificationUUID` into `billing_events`; a processing failure answers `500 EVENT_RETRY_SCHEDULED` and stays for the worker to replay. |
| ~~`GET/POST /api/development/membership`~~ | — | **Not accepted by this service** (404): the development toggle exists only in the base `business-api` (`business_api/app/factory.py`, see [development-membership.md](development-membership.md)). This service's member allowance is the entitlement-driven Plus daily limit from `TIME_FRAGMENT_MEMBER_QUOTA_LIMIT` (default 30, `business_api/app/account_backend.py`); the base's 50/day is a fixed value of its SQLite quota store, not this service's limit. |
| `/admin/time-fragment/quotas/...` | admin key | Existing status/reset routes now use the Postgres ledger. Reset waits for active attempts to finish. |

The App Store release must use `APPLE_ENVIRONMENT=production`; development and
TestFlight purchases use `sandbox`. Migration `202609240024` scopes entitlement
aggregation and billing sources to the configured Apple environment. Migration
`202609250025` additionally separates daily member counters by environment, even
when both purchase chains have the same owner. Apply both migrations before
deploying the matching API, then switch
the release service's environment. A sandbox chain can remain in the database
without granting Plus to the production service. A successful device refresh
replaces any previously cached sandbox entitlement. Updated iOS builds namespace
the cache by API endpoint and ignore the old unscoped cache. Older builds can
still keep an unexpired local cache while offline; server changes cannot erase it. TestFlight archives must set
`MODEL_SERVER_API_BASE=https://staging.api.keeline.xyz`; that hostname routes to
`testflight-api`, and its companion `testflight-billing-worker` processes sandbox
events. Both keep `APPLE_ENVIRONMENT=sandbox` after the public service switches
to production.
Start the TestFlight worker only when the existing worker switches to production;
until then the existing worker handles sandbox events, so there is never a pair
of sandbox pollers racing on the same event queue.
The production archive keeps `https://api.keeline.xyz`. Configure Apple's
sandbox Server Notifications V2 URL as
`https://staging.api.keeline.xyz/webhooks/apple`; keep the production URL on
`https://api.keeline.xyz/webhooks/apple`. Do not flip the variable alone: the
older database functions aggregate purchases from both environments.
For archive-based hosts and migration/cutover gates, use
[testflight-billing-rollout.md](testflight-billing-rollout.md). The historical
`billing-deploy.sh` assumes a live Git checkout and is unsuitable for these hosts.

If the production cutover fails, restore the backed-up `.env` with
`APPLE_ENVIRONMENT=sandbox`, stop `testflight-billing-worker`, and recreate
`time-fragment-api` plus `billing-worker`. This restores the prior sandbox
service while leaving the forward migration in place. Do not run the legacy
`billing-rollback-device-principal.sh`: it reverses the older device-principal
migrations, not this environment switch.

The AI route is device-metered. Membership resolves by principal identity
(`202609170017_device_principal_billing.sql`), an account session resolves to
`account:<uuid>`, and every entitlement-writing action requires the device
principal, so that account principal cannot carry Plus. Charging it would spend the
account's lifetime free pool on behalf of a paying member.
`/api/plan/parse` therefore accepts only the guest token: an account token gets
`401` with code `DEVICE_REQUIRED` before the `reserve` action runs, so the rejected
attempt consumes no quota. The shipped client retries this route's `401` with its
guest token (the catch in `AIPlanningClient.authenticatedSend` checks only
`failure.status == 401`), so the refusal costs one extra round trip until the
companion client release stops sending the account token; if the account token is
refreshed between those two attempts the client keeps using the account token and
that request fails instead of falling back. Membership reads and the device-scoped
quota surface stay on `/billing/*`, which requires the device token by the same
rule (`billing_device`). The one exception is `GET /api/account/quota`, which
deliberately keeps reading the requesting actor's own merged free pool (below).

### Membership quota scope

The daily Plus allowance is **one counter per purchase chain**, shared by that
chain's active member devices (`billing_private.purchase_devices` rows with
`revoked_at is null`), reset at local midnight in the entitlement's
`account_timezone` (`202609170020_shared_member_quota.sql`; product decision E1).
`quota_status` renders the member `resetsAt` as the local wall clock in that zone
labelled with the zone's real UTC offset at that instant
(`202609180021_member_resets_offset.sql`), so the label is an ISO 8601 instant in
any zone — `+08:00`, `-05:00`, `+05:30` and `+08:45` all render correctly, and a
zone with DST is labelled with the offset in force on that reset day. Before that
migration the label appended a literal `+08:00`, which was right only while
entitlements kept the `Asia/Shanghai` default (`202609110014_member_quota.sql`).
It is explicitly *not* per Time Fragment account: an account is not a billing
subject, and a signed-in member's AI request is refused on `/api/plan/parse` and
retried with the device token, so the device principal is the only AI identity.
The meter row lives at the chain owner's principal
(`store_purchases.principal`, fixed at first INSERT), so the existing
three-active-device cap is also the quota-sharing boundary. Receipts,
idempotency and refunds stay per device. `ai_private.quota_status` resolves the
shared scope once, so `GET /billing/entitlement`, `apple_verify` and the quota
the AI route enforces all read the same row; `reserve` locks that row and
re-reads it before its exhaustion check, because without it two devices on one
chain can each observe `remaining = 1` and both consume it.

The free tier is **not** shared this way. Two signed-out devices of one free user
keep independent lifetime pools (decision E4). A free pool is still merged across
identities by `claim-guest`, and `free_pools` is unchanged. For a free principal
the shared scope is a no-op: the read and the write stay on that principal's own
pool.

This is a best-effort product control, not enforcement: the `device_id` is
client-asserted and a guest JWS is a copyable bearer credential. Sharing also
converts entitlement theft into an availability attack on the group — a forged or
borrowed `device_id` on a bound chain can drain the group's daily 30 and lock the
legitimate devices out.

`GET /api/account/quota` is deliberately unchanged. It is called with the
Supabase session token and its number is rendered as the *merged free allowance*
line (`本机游客 AI 额度已合并：已用 X/Y 次`), so it keeps reading the account's own
free pool and keeps returning the actor's `supportCode`; the member counter has
its own device-token-scoped surface, `/billing/entitlement`.

Accounts and guests have 30 lifetime free calls. Claim takes
`max(accountUsed, guestUsed)` and links both identities to one free pool. Signing
out preserves the remaining free balance; it does not grant another pool or
expose the account's Plus entitlement. Receipts remain on their original identity
and replay checks cover every identity in the linked group.
Retrying the same claim to the same account is safe. Another account cannot claim
that installation, even after deletion of the first account. Development
membership does not become a paid account entitlement during claim.

The Supabase Data API verifies the JWT signature. `ai_account_identity` additionally
checks that the confirmed, non-anonymous user and its session still exist and that
account deletion is not pending. The server-only quota RPC rechecks that account
and session before each operation. No caller-supplied account ID is trusted.
Grants deny `anon` and `authenticated` access to the mutation RPC and all private
tables/helpers; the tables also have RLS enabled. Account deletion cascades account
quota data; shared free consumption remains available to linked guests and the
guest claim tombstone retains no reference to a deleted user.

References: [Supabase API security](https://supabase.com/docs/guides/api/securing-your-api),
the existing `sync_private.current_user_id` session contract, and the App's
`docs/account-cloud-sync-design.md` architecture decision.

## Charging and retries

Reservation and the request receipt are one Postgres transaction, serialized per
free pool. A claim briefly takes the ledger gate exclusively to change pool
membership; no ledger transaction spans the model call. The body hash binds an ID
to its original payload. Internal transport
retries reuse the reservation attempt UUID. Concurrent duplicate requests return
`AI_REQUEST_IN_PROGRESS`; changed bodies return `AI_REQUEST_ID_CONFLICT`.
Already completed IDs return `AI_REQUEST_ALREADY_COMPLETED` without another model
call or charge, matching the legacy API. Response replay/storage is not provided.

A usable proposal consumes one call, including a proposal with validation issues.
Input rejection, infrastructure failure or a null proposal refunds the reservation.
The model's correction attempt is included in the same reservation. Refunds and
completion are fenced by attempt UUID. Lost/abandoned reservations expire after ten
minutes and are reclaimed on the next quota operation. A late old response cannot
complete a replacement attempt. Clients must retain the request ID after an
ambiguous failure; automatically creating a new ID can create a second operation.

The ledger keeps only identifiers, body hashes and quota state. Internal planning
observability retains model/token/timing metadata, with input, output and provider
error content redacted. No response cache stores a user's schedule. Existing
pre-cutover observability records retain their existing retention policy.

## Verification diagnostics and event retries

`POST /billing/apple/verify` never trusts the client's claim about subscription
state. After verifying the submitted JWS it re-queries Apple's
`/inApps/v1/subscriptions/{originalTransactionId}` endpoint. That response is a
container, not a JWS: the authoritative entries live at
`data[].lastTransactions[]`, and each entry's `signedTransactionInfo` is verified
against the pinned root before its `expiresDate` is used. The entry's `status`
code is mapped through `STATUS_MAP` in `business_api/app/billing_verify.py`:
`1=active`, `2=expired`, `3=billing_retry`, `4=revoked`, and an unknown code is
treated as `expired`. A chain Apple does not list at all is a
`422 VERIFICATION_FAILED` (`TRANSACTION_NOT_FOUND`).

The retry split follows the failure class. Transport failure, a 5xx or 429 from
Apple, and an unparsable envelope are transient: they return
`202 VERIFICATION_PENDING` with a `reason` so the client retries later. A rejected
response, an environment mismatch, a failed JWS check or a mismatched product is
terminal: `422 VERIFICATION_FAILED`, `422 ENVIRONMENT_MISMATCH` or
`422 PRODUCT_MISMATCH`. Missing Apple configuration answers
`503 BILLING_NOT_CONFIGURED` before any Apple call.

Every verification writes one INFO line comparing the submitted transaction with
the chain state Apple reports:

```text
apple verify chain=<originalTransactionId> submitted tx=<transactionId> purchaseDate=<iso> expiresDate=<iso> | apple status=<STATUS_MAP value> expiresAt=<iso>
```

`purchaseDate` and `expiresDate` come from the submitted JWS; `status` and
`expiresAt` come from Apple. When they disagree the client is handing back an old
transaction from StoreKit's queue instead of a fresh purchase, which otherwise
looks like a server fault. The line carries chain and transaction identifiers
only: no account, session, support code or credential. The application logger is
set to INFO in `business_api/app/account_main.py`, because uvicorn configures only
its own loggers and the line would otherwise be invisible in the container log;
`billing-worker` sets the same level.

Webhook deliveries and verification failures are stored in
`billing_private.billing_events` (initial `status` `received`, `attempts` 0). The
`billing-worker` container polls every `BILLING_WORKER_INTERVAL_SECONDS`, default
300 seconds (the compose default as well), replays pending events and then
reconciles active chains. Each tick selects up to 20 events with
`status in ('received','failed')` and `attempts < 8`, oldest `received_at` first —
the defaults of `process_pending_events` and of the `event_pending` action. A
replayed event is already stored, so the receive/dedupe step is skipped. `event_mark`
increments `attempts` only for a `failed` or `processed` status, so a persistently
failing event leaves the pending set after eight recorded attempts and is never
retried forever. Every failure also records `last_error_code` (the upstream code,
`PROCESSING_FAILED`, or the exception class name). A processing failure answers the
webhook with `500 EVENT_RETRY_SCHEDULED`, so Apple's own retries act as the second
net.

## Deployment and quota cutover

For an existing account-aware deployment through `202609130015_free_quota_30.sql`,
apply the forward migration `202609140016_guest_free_pool.sql`. It backfills each
account and its claimed guests into one pool using the highest existing free
usage, preserving support codes, receipts and outstanding reservation refunds.
The public API and iOS request format do not change. A code merge alone does not
activate this fix; the database migration must be applied. Do not reapply the
manual `contract_v2_rollback` or `contract_v2_reregister` recovery scripts as
forward migrations.

The device-principal and chain-shared quota migrations apply in lexical order in
the same maintenance window, before `scripts/billing-deploy.sh`:
`202609170017_device_principal_billing.sql`,
`202609170018_contract_v2_puzzle_optional.sql`,
`202609170019_billing_ai_quota_source.sql`,
`202609170020_shared_member_quota.sql`, then
`202609180021_member_resets_offset.sql` (the member `resetsAt` offset label, which
depends on the `plus_source` that `202609170020` adds).
`202609170020` depends on `202609170017`'s `purchase_devices` table and does not
re-issue `202609130015`'s three `free_limit` statements — it reports the live
`free_limit` distribution instead, and it fails closed if a member bucket row
would be re-keyed onto a chain owner. Neither `202609170020` nor `202609180021`
changes any schema or data, so neither needs a rollback file: their inverse is
restoring the previous function bodies from git. Reverting `202609170020` restores
per-device metering but not the counters — member usage written under a chain
owner cannot be split back per device, so after a revert the owner's device shows
the group's usage and its peers show 0 (each device appears to gain up to
30/day). The reverse export does not drop that usage: `ai_quota_export_legacy`
emits the chain owner's member
bucket with the group's used count. Watch `AI_DAILY_QUOTA_EXHAUSTED` after
cutover, because a shared counter exhausts earlier for multi-device members.

The base Compose file remains compatible with the currently deployed guest API.
Activating `docker-compose.accounts.yml` changes public routing and disables the
old guest/planning handlers on the model service. Internal planning requires a
separate 60-second, audience- and body-bound HMAC credential. Caddy denies the
internal path, and neither Python service publishes a host port. The generic
business-key pipeline route also refuses Time Fragment pipelines in internal mode.
Other generic pipelines and the observability dashboard remain available.

1. Apply migrations `202609090006_ai_quota.sql` and
   `202609090007_ai_quota_rollback.sql` after the five account/sync migrations.
   They are additive. The new ledger starts closed until legacy import is
   completed; the second migration adds the service-role-only export and rollback
   functions used by the emergency script.
2. Configure `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, the legacy JWT
   `SUPABASE_SERVICE_ROLE_KEY`, and an independent random
   `PLANNING_INTERNAL_SECRET` in the server's protected environment. Preserve
   `TIME_FRAGMENT_TOKEN_SECRET`, guest limit and development allowlist. Do not
   print `docker compose config` with real credentials; use `config --quiet`.
3. Build the two Python services and validate the merged Compose configuration:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.accounts.yml config --quiet
   docker compose -f docker-compose.yml -f docker-compose.accounts.yml --profile rollback \
     build business-api time-fragment-api quota-rollback
   ```

   Before the window, confirm the rollback plan and keep the script executable:

   ```sh
   scripts/rollback-account-ai-cutover.sh --dry-run
   ```

4. Schedule a short maintenance window and pause the existing AI probe. Stop
   public traffic and the legacy planner, then make a consistent, permission-
   restricted backup of the `model-server-usage` SQLite volume. Keep its WAL with
   the database, or create the snapshot with SQLite's backup API. The old planner
   must remain stopped until the new API becomes the only public AI writer.
5. Run the importer against the offline snapshot with the new service environment.
   The service image already carries the app and the overlay mounts the legacy
   volume at `/var/lib/model-server`. SQLite is in WAL mode, so the mount is
   writable for the importer and rollback one-offs even though the running API
   never writes to it:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.accounts.yml run --rm --no-deps \
     time-fragment-api python -m app.migrate_guest_quota \
     /var/lib/model-server/usage.sqlite3 --legacy-service-stopped
   ```

   It opens SQLite read-only and imports existing support codes, active free/daily
   counters, development toggles, and completed IDs from all historical buckets.
   Recent outstanding reservations block import; allow their ten-minute lease to
   expire with the legacy service stopped. Expired reservations are removed from
   imported counts. Import retries require the same snapshot hash for each
   principal. Completion closes the import gate permanently and enables readiness.
6. Start using both Compose files and verify readiness, guest/account requests,
   claim/retry behavior, and denial of public `/internal/time-fragment/plan` and
   direct legacy planner routes. Resume the probe with this service configuration.

After cutover, all commands that recreate services must use both Compose files.
Never run both public quota authorities concurrently.

## Emergency rollback

`scripts/rollback-account-ai-cutover.sh` is the supported way to switch back. It
stops the new public entrypoint, takes a consistent SQLite backup under
`rollback-backups/<timestamp>/`, reverse-exports post-cutover guest usage from
Postgres into the legacy database, closes the new ledger gate (refunding abandoned
reservations), restores the base Compose topology and verifies `/health/live`
plus guest-token issuance. The one-off `quota-rollback` profile service has a
writable legacy volume but only the two credentials the reverse export needs.

```sh
cd /opt/model_server
scripts/rollback-account-ai-cutover.sh            # availability first
scripts/rollback-account-ai-cutover.sh --strict   # abort if the reverse export fails
```

The default run still restores the legacy service when the reverse export fails,
and exits nonzero so the warning is visible; that keeps the app working with
slightly stale quota counters. `--skip-export` restores availability without
merging post-cutover usage and is only for the case where the new ledger is
unreachable and the quota drift is acceptable. `--no-reset-import` closes the gate
but keeps the imported guest rows for inspection.

After a successful rollback the new gate stays closed and imported guest rows are
removed, so a later re-cutover re-imports the updated SQLite snapshot instead of
hitting an import-hash clash. The legacy service is the only public quota
authority until a re-cutover. If the reverse export failed, do not re-cutover until
the missing usage is reconciled.

## Validation

New database cases are in `supabase/tests/ai-quota.test.mjs`; API, internal-service,
legacy-export and private-content cases are in `business_api/tests/test_account_api.py`.
Rollback export/close behavior is covered by
`business_api/tests/test_reverse_ai_quota_export.py` and the script contract by
`business_api/tests/test_rollback_script.py`.
`test_account_routing.py` starts a verified Caddy 2.11.4 binary on temporary local
ports to exercise the actual matchers. Set `CADDY_BINARY` to enable that case.
These tests use synthetic users/content and do not send mail or invoke a paid model.

The precise final-head impact mapping and test results belong in the PR evidence.
Local PostgreSQL role tests prove SQL authorization, not the hosted JWT verifier;
production cutover requires separate hosted Auth/HTTP verification. App account UI,
cloud-sync integration, Apple/Google purchase validation, paid memberships, and
inbox placement remain separate work.
