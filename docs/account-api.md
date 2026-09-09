# Account-aware AI API

The optional `time-fragment-api` service accepts either the existing installation
token or a Supabase account access token. It resolves the account through
`ai_account_identity`, reserves quota in Postgres, and calls the private planner.
The existing planner, correction loop and candidate-plan contract are reused.
An AI result never writes a user's cloud state or applies a plan to the App.

## Public contract

| Route | Credential | Behavior |
| --- | --- | --- |
| `POST /api/auth/guest` | installation ID | Existing guest token response; a previously claimed guest requires account login. |
| `POST /api/plan/parse` | guest or account | Existing V2 input/output; one quota reservation per principal and request ID. |
| `GET /api/account/quota` | guest or account | `supportCode`, `limit`, `used`, `remaining`. |
| `POST /api/account/claim-guest` | account | Body `{"guest_token":"…"}`; verify both credentials and merge free usage by maximum. |
| `GET/POST /api/development/membership` | allowlisted guest | Existing development toggle and 50/day Shanghai quota; not a paid subscription. |
| `/admin/time-fragment/quotas/...` | admin key | Existing status/reset routes now use the Postgres ledger. Reset waits for active attempts to finish. |

Formal accounts share 50 lifetime free calls across devices. Guest limits retain
the existing configured value. Claim takes `max(accountUsed, guestUsed)`, copies
completed request receipts, and permanently marks the installation as claimed.
Retrying the same claim to the same account is safe. Another account cannot claim
that installation, even after deletion of the first account. Development
membership does not become a paid account entitlement during claim.

The Supabase Data API verifies the JWT signature. `ai_account_identity` additionally
checks that the confirmed, non-anonymous user and its session still exist and that
account deletion is not pending. The server-only quota RPC rechecks that account
and session before each operation. No caller-supplied account ID is trusted.
Grants deny `anon` and `authenticated` access to the mutation RPC and all private
tables/helpers; the tables also have RLS enabled. Account deletion cascades account
quota data; the guest claim tombstone retains no reference to a deleted user.

References: [Supabase API security](https://supabase.com/docs/guides/api/securing-your-api),
the existing `sync_private.current_user_id` session contract, and the App's
`docs/account-cloud-sync-design.md` architecture decision.

## Charging and retries

Reservation and the request receipt are one Postgres transaction, serialized per
principal. The body hash binds an ID to its original payload. Internal transport
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

## Deployment and quota cutover

The base Compose file remains compatible with the currently deployed guest API.
Activating `docker-compose.accounts.yml` changes public routing and disables the
old guest/planning handlers on the model service. Internal planning requires a
separate 60-second, audience- and body-bound HMAC credential. Caddy denies the
internal path, and neither Python service publishes a host port. The generic
business-key pipeline route also refuses Time Fragment pipelines in internal mode.
Other generic pipelines and the observability dashboard remain available.

1. Apply migration `202609090006_ai_quota.sql` after the five account/sync migrations.
   It is additive. The new ledger starts closed until legacy import is completed.
2. Configure `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, the legacy JWT
   `SUPABASE_SERVICE_ROLE_KEY`, and an independent random
   `PLANNING_INTERNAL_SECRET` in the server's protected environment. Preserve
   `TIME_FRAGMENT_TOKEN_SECRET`, guest limit and development allowlist. Do not
   print `docker compose config` with real credentials; use `config --quiet`.
3. Build the two Python services and validate the merged Compose configuration:

   ```sh
   docker compose -f docker-compose.yml -f docker-compose.accounts.yml config --quiet
   docker compose -f docker-compose.yml -f docker-compose.accounts.yml build business-api time-fragment-api
   ```

4. Schedule a short maintenance window and pause the existing AI probe. Stop
   public traffic and the legacy planner, then make a consistent, permission-
   restricted backup of the `model-server-usage` SQLite volume. Keep its WAL with
   the database, or create the snapshot with SQLite's backup API. The old planner
   must remain stopped until the new API becomes the only public AI writer.
5. Run the importer against the offline snapshot with the new service environment.
   The service image already carries the app and the overlay mounts the legacy
   volume read-only at `/var/lib/model-server`:

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
Do not roll back to the old SQLite quota authority after the new API has accepted
traffic: its counters are now stale. Keep the new API/ledger during a planner
rollback, or take the service offline and perform a separately reviewed reverse
migration. Never run both public quota authorities concurrently.

## Validation

New database cases are in `supabase/tests/ai-quota.test.mjs`; API, internal-service,
legacy-export and private-content cases are in `business_api/tests/test_account_api.py`.
`test_account_routing.py` starts a verified Caddy 2.11.4 binary on temporary local
ports to exercise the actual matchers. Set `CADDY_BINARY` to enable that case.
These tests use synthetic users/content and do not send mail or invoke a paid model.

The precise final-head impact mapping and test results belong in the PR evidence.
Local PostgreSQL role tests prove SQL authorization, not the hosted JWT verifier;
production cutover requires separate hosted Auth/HTTP verification. App account UI,
cloud-sync integration, Apple/Google purchase validation, paid memberships, and
inbox placement remain separate work.
