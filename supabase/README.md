# DayMosaic account and sync service

This directory implements the first service from the account/cloud architecture.
It contains deployable Supabase migrations, passwordless email Auth configuration,
content-free Realtime signals, account-deletion processing, and database tests.
It does not add an iOS login screen or sync coordinator, billing, or AI routing.

## Repository ownership and existing project

This directory is the backend source of truth, moved from Time Fragment PR #117
at commit `023a31c753a9cef666473adcd0c3530e0c86eff3`. The initial import preserved
the service code, contracts, tests and four migrations byte-for-byte; its guide
added the repository handoff. Keep the existing project `tjfhfwvxkcgpswdtnhxv` in Singapore.
The migration versions applied before the repository move were `202609080001`
through `202609080004`; subsequent backend changes add new migration versions.

Link this repository to that project and inspect `supabase db push --dry-run`;
the move itself must produce no pending migrations. Do not rename or regenerate
the applied migrations. Future backend changes belong here. The App repository
continues to own client code and the original product/architecture design.
The AI Docker Compose stack has no dependency on this directory.

The migrations run against PostgreSQL 17. Local database tests use a disposable
PostgreSQL 17.7 process bound to localhost, with the Supabase Auth claims/tables
represented by test fixtures. Those tests prove SQL behavior, privileges and
concurrency; they do **not** prove hosted Auth, email delivery, PostgREST, Realtime
transport, or production deployment.

## Run the database checks

From this directory, using Node 24 LTS or later:

```sh
npm ci
npm run test:sync
```

Only this service's new tests run. The runner initializes its own temporary
database, applies the exact migrations, and tears it down. It requires permission
to bind localhost sockets. It never connects to an existing database. Protocol
fixtures are also checked by AJV, independently of the SQL validator.

If changing a JSON Schema, regenerate its migration before deployment:

```sh
node scripts/build-contract.mjs
node scripts/build-contract.mjs --check
```

Once a migration is deployed, add a new migration; do not rewrite applied files.
The contract generator rejects keywords unsupported by the restricted SQL
validator. V1 only is supported: no previous Cloud DTO has shipped, and there are
no supported schema migration edges yet. Adding v2 requires its own checkpointed,
CAS-protected migration and fixtures, rather than writing v2 with an ordinary op.

## Local Supabase and hosted deployment

With Docker and the Supabase CLI installed, run from the repository root:

```sh
supabase start
supabase db reset --local
```

This applies the migrations to the **local** Supabase project. Auth email is
captured in its local Inbucket mailbox. Cloud deployment requires a selected
Supabase project and securely configured CLI credentials:

```sh
supabase link --project-ref "$DAYMOSAIC_SUPABASE_PROJECT_REF"
supabase db push --dry-run
supabase db push
```

Select and inspect the target project before pushing. The migration owns only
`sync_private`, `public.sync_changes`, and its named RPCs; it does not change
another service's tables or Auth users. Do not expose `sync_private` through
the Data API or add its state tables to the Realtime publication.

`config.toml` configures the local stack. Linking/pushing the database does not
apply hosted Auth settings. On the selected hosted project separately configure:

- Email provider enabled, signups enabled, anonymous sign-in and manual identity
  linking disabled; no social providers are required.
- Six-digit OTP, 10-minute expiry, at least 60-second resend interval, appropriate
  Auth/IP rate limits and CAPTCHA for public signup.
- Both confirmation and magic-link templates use `templates/otp.html`; the email
  contains `{{ .Token }}`, not a login link.
- Production SMTP with the verified sending domain. Default Supabase email is
  insufficient for production delivery.
- Database backup policy, region, statement timeouts, capacity alerts and scheduled
  execution of the maintenance/deletion worker under server-only credentials.

Before public rollout, confirm the operational limits below and the privacy
policy, including the physical lifetime of deleted data in database backups.
The selected project/SMTP and live verification evidence must be recorded before
calling this service deployed.

On the verified CLI version 2.117.0, `supabase config diff` previews declared
hosted settings and `supabase config push` applies them. Inspect the diff first:
the local file also declares a development site URL. Do not push that URL over a
production redirect configuration. Free projects using the default email provider
reject template modifications; configure custom SMTP before pushing the OTP
templates. Until then, a separate minimal config can apply the Auth parameters
without the template sections, but real emailed OTP login remains unfinished.

After applying migrations to a local/staging Supabase instance, set
`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and the server-only
`SUPABASE_SERVICE_ROLE_KEY`, then run:

```sh
SYNC_TEST_ALLOW_WRITE=1 npm run test:hosted
```

This creates fresh temporary Auth users, obtains admin-generated OTPs, verifies
them through the real Auth API, refreshes a session and exercises PostgREST
isolation. Two sessions for one user register different devices, exchange a state
update, receive its content-free Realtime WebSocket notification, and verify
duplicate and stale-revision behavior. The same check runs the deletion worker
against only its own temporary deletion job, verifies Auth-user removal, and
cleans up the remaining test users. A completed anonymous deletion receipt stays
under the normal 30-day retention rule. No email is sent: SMTP delivery requires
a separate target-environment check. Running the worker in this test does not
configure a hosted worker schedule.
The template `ci/account-sync.yml.template` includes a `supabase-api` job that
starts a fresh Docker-backed local Supabase stack and runs this check with
temporary credentials. Activate it at `.github/workflows/account-sync.yml`
using GitHub credentials with workflow permission. It is not an active CI check.

## Auth and session binding

Clients use a publishable key, request a code with `signInWithOtp({email})`, and
exchange it with `verifyOtp({email,token,type:'email'})`. Refresh tokens stay in
secure client storage. No data space is created by login alone.

Every normal RPC derives the user from `auth.uid()`, checks a confirmed,
non-anonymous Auth user and a live `auth.sessions` row. Device registration binds
the signed JWT's `session_id` to an installation ID. Supplying another device ID
with an unregistered session does not grant access. A new login can rebind its
installation, invalidating the old session's sync access; it must reconcile and
acknowledge the current state before submitting. A revoked or expired device must
register again and perform first-sync reconciliation.

Authenticated sessions without an available device slot can inspect account
metadata, list devices and revoke an old device. They cannot pull business state,
export checkpoints or read Realtime signals.

## RPC contract

Call these through `/rest/v1/rpc/<name>` with the user JWT. Parameter names below
are the exact PostgREST JSON keys. UUIDs are lowercase; state dates and timestamps
follow the versioned JSON Schemas.

| RPC | Parameters | Result |
| --- | --- | --- |
| `sync_account_status` | none | `uninitialized`, `ready` metadata, or `deletionPending`; no business state |
| `register_sync_device` | `p_device_id,p_platform,p_app_version,p_supported_schema_version` | installation/session binding; maximum five active devices |
| `list_sync_devices` | none | own device metadata, without session IDs |
| `revoke_sync_device` | `p_device_id` | immediately denies that binding's sync access |
| `initialize_sync_state` | `p_device_id,p_initialization_id,p_state` | empty state revision 0, otherwise 1; generation 1 and stable space ID |
| `pull_sync_state` | `p_device_id,p_operation_ids?,p_control_request_ids?` | current state/hash/lineage and requested accepted-ID receipts |
| `acknowledge_sync_state` | `p_device_id,p_generation,p_revision,p_state_hash` | records confirmation of the exact current cloud state |
| `commit_sync_state` | `p_operation,p_result_state` | `accepted` or original `duplicate` receipt; duplicate includes current revision/generation |
| `replace_sync_state` | `p_device_id,p_replace_id,p_expected_generation,p_expected_revision,p_state` | atomic safety snapshot + new generation/revision; requires user-confirmed first-sync choice |
| `export_sync_checkpoint` | `p_device_id,p_checkpoint_id` | own protocol recovery snapshot; caller already knows the checkpoint ID |
| `request_account_deletion` | `p_deletion_request_id,p_receipt` | persistent `pending` job; requires OTP authentication within 10 minutes |
| `account_deletion_status` | `p_receipt` | `pending/dataDeleted/completed/unknown`; also available to anon after Auth deletion |

Persist request IDs and immutable attempt inputs before sending. Retry the same
request after uncertainty, or query its accepted receipt. Never turn a timeout
into a new operation ID. A successful replacement does not count as device
acknowledgement: the client must persist/reconcile the response and call
`acknowledge_sync_state` before further uploads.

Business operations send the complete candidate resulting from applying **only
the outbox head** to the last acknowledged cloud state. The server validates the
complete write set's before fingerprints, supplied read-set fingerprints, DTO
structure and CAS. Client-specific schedule algorithms and deterministic replay
remain the client's responsibility.

`preconditions.entityFingerprints` keys are `<collection>/<uuid>`, with the
canonical entity hash, or `null` when the ID must not exist. Every changed/new/
deleted entity requires an entry. `readSet` selects collections, optionally
filtered by `ids`, `dates`, or `taskIDs`; filters intersect, selectors union.
The read-set hash is the canonical hash of an object containing:

- each entity selector's canonical JSON text as a key with value `[]` (retains empty
  selections);
- each selected `<collection>/<uuid>` as a key with the complete canonical entity;
- `activeFocusSessionID` as a key when that scalar is selected.

The JSON schemas define all v1 operation payloads. Unknown fields are rejected
in this first deployed contract so private device fields cannot leak unnoticed.
Schema updates must explicitly handle any new field.

`operation-examples.json` contains a wire-shape example for every operation kind;
its placeholder fingerprints are not executable commit attempts. Clients must
compute fingerprints and results from their actual acknowledged state.

The original draft predated all-day/unscheduled/continuation occurrence fields and
segmented external events. V1 includes their platform-neutral forms, while still
excluding raw calendar IDs, permissions and derived placement status. The golden
fixture preserves outside-day and insufficient-duration schedules, and historical
focus sessions may reference deleted schedules. Cross-task overlap is allowed;
within-task overlapping segments and broken strong references are rejected.

SQL errors use stable messages, without state contents: `authRequired`,
`reauthRequired`, `deviceRequired`, `deviceLimitReached`, `sessionAlreadyBound`,
`firstSyncRequired`, `revisionConflict`, `generationConflict`, `schemaTooNew`,
`payloadInvalid`, `preconditionRequired`, `preconditionFailed`,
`stateHashMismatch`, `operationIDReused`, `requestIDReused`, `rateLimited`,
`resourceLimit`, `checkpointUnavailable`, `deletionPending`,
`deletionUnavailable`, and `authDeletionIncomplete`.
PostgREST encodes raised database exceptions as errors; clients must branch on the
stable message, not retry every HTTP 400. Transport failure is not proof that a
transaction failed.

## Realtime

Subscribe to `postgres_changes`, schema `public`, table `sync_changes`, filtering
by the signed-in user's ID. Supply the JWT to Realtime and update it after token
refresh. RLS checks the active session binding for each visible row. Events
contain only space/generation/revision/time metadata. Fetch the state by RPC;
also pull on foreground/network recovery because notifications may be missed.

## Account deletion and maintenance

After explicit destructive confirmation, the client generates a stable UUID and
32 cryptographically random bytes encoded as 64 lowercase hex characters. It
persists that receipt before requesting deletion; the database stores only its
hash. The receipt is a bearer secret and must never be logged.

The account becomes deletion-pending in the same transaction as the job.
No new synchronization or initialization is permitted. A trusted worker then
deletes business state, calls the Auth Admin API, and marks completion only after
the Auth row is actually absent. Retrying any stage is safe. Auth failures leave
the account inaccessible and the job pending. The completed receipt contains no
user ID, email or business state; it is retained for 30 days.

Run the worker from the server, with credentials injected by its secret manager:

```sh
node scripts/deletion-worker.mjs
```

It requires `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Neither belongs in a
client or Git. The worker emits only aggregate completed/pending counts and exits
nonzero when work remains. A supervisor should retry with backoff. The server-only
RPCs are `pending_account_deletions`, `prepare_account_deletion`,
`complete_account_deletion`, `cleanup_deletion_receipts`, and
`maintain_sync_account(p_user_id)` and `maintain_sync_batch(p_limit)`.

The batch RPC handles at most 100 accounts, skips busy/deletion-pending accounts,
and rotates attempts so an account cleanup failure does not block other accounts.
Its permissions are restricted to `service_role`. The scheduled entry point is
`node scripts/background-worker.mjs deletion` or `maintenance`; fatal errors emit
only a stable code. See [systemd deployment](deploy/README.md) for independent
timers, isolated credentials, runtime installation and scheduled verification.

Maintenance never reads membership status. Safety snapshots are cleaned only
after their window and every required active device's acknowledgement (or
revocation/expiry). Operation receipts additionally require a retained safety
checkpoint covering that revision. When safe cleanup is impossible, the service
keeps evidence and returns `resourceLimit` rather than silently deleting it.

## Initial operational limits

These are explicit candidate limits for staging; confirm them with production
capacity and retention policy before rollout.

| Boundary | Limit |
| --- | --- |
| Active devices / lease | 5 / 90 days |
| State / operation envelope | 4 MiB / 1 MiB |
| Entities per collection / total | 10,000 / 20,000 |
| Title / note | 500 / 20,000 Unicode characters |
| Duration / schedule segment | up to 35,040 slots / at most 96 slots per segment |
| Safety checkpoints | 32 per account; at least 7 days and acknowledgements |
| Operation receipts | 50,000 per account; at least 30 days plus safe cleanup conditions |
| Control receipts | 10,000 per account; retained to preserve idempotency |
| Accepted sync requests | 120 per minute per account |
| Pull receipt lookups | 256 operation IDs / 64 control request IDs |

The database budget covers accepted requests; rejected transactions roll back
their counters. Public IP/request-body/failed-auth throttling belongs at the
Supabase gateway/Auth configuration. Do not treat this SQL counter as a complete
anti-abuse perimeter.

## References

- [Supabase passwordless email Auth](https://supabase.com/docs/guides/auth/auth-email-passwordless)
- [Database function privileges](https://supabase.com/docs/guides/database/functions)
- [RLS](https://supabase.com/docs/guides/database/postgres/row-level-security)
- [Realtime Postgres Changes](https://supabase.com/docs/guides/realtime/postgres-changes)
- [Custom SMTP](https://supabase.com/docs/guides/auth/auth-smtp)
