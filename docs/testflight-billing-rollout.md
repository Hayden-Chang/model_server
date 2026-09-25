# TestFlight billing environment rollout

App sign-in is not a billing identity. This rollout separates Apple's sandbox
and production entitlements and daily member counters while retaining sharing
between devices on one purchase chain inside each environment. Free lifetime
quota remains device based.

## Database gate

1. Back up the hosted database and pause API/worker writers during migration.
   Record which containers were running so only those are resumed. Do not leave
   traffic running while renaming quota buckets.
2. Apply `202609240024_billing_environment_isolation.sql` **once**, in a
   transaction, then `202609250025_billing_environment_quota.sql` (which supplies
   its own transaction). Do not rerun 024: it renames the legacy RPC bodies.
3. Migration 025 renames old `member:YYYY-MM-DD` counters to
   `member:sandbox:YYYY-MM-DD`, preserving usage, IDs, and outstanding request
   references. This is for the previous sandbox-only deployment. It refuses
   ambiguous production-owner history or an existing destination bucket; resolve
   those from actual transaction/usage history before proceeding, never delete
   counters to bypass the guard. Reapplying 025 is safe.
4. As `service_role`, call `billing_environment_schema()` and require `25`.
   Resume the previous sandbox services. API startup/readiness alone does not
   prove this migration was applied.

Do not roll back 025 by restoring old function definitions: those definitions
would look up the old period names and silently reset visible usage. Keep the
forward SQL migration when rolling back an application image. Old APIs that
omit `billingEnvironment` still default to sandbox.

## Stage a source archive

Use the reviewed commit from a clean local checkout. No server-side Git checkout
or developer worktree metadata is needed. For example, substitute the reviewed
40-character SHA in these commands:

```sh
git archive --format=tar <sha> > /tmp/model-server-release.tar
scp /tmp/model-server-release.tar root@47.120.13.5:/tmp/model-server-release.tar
```

On the server, create a **new** `/opt/model_server/releases/<sha>` directory,
extract the tar there, and write that exact SHA plus a newline to `.release-sha`.
Run its script:

```sh
/opt/model_server/releases/<sha>/scripts/deploy-testflight-api.sh <sha>
```

The script requires schema 25 before building or recreating anything, uses the
stable `/opt/model_server/.env` and signing secrets, and deploys only
`testflight-api` with `--no-deps`. It saves the prior image ID, private container
inspection, environment and Caddy configuration in a private rollback directory,
reloads the proxy, and checks the container sandbox configuration and internal readiness,
then requires the sandbox header on the public proxy response. It does not start a second sandbox worker or rebuild the production
API/planner. Do not use historical `billing-deploy.sh` on an archive-based host.

If a command fails after replacement, recover explicitly using the printed
rollback directory (also discoverable under `rollback-backups/testflight-*`):
restore its Caddy configuration and reload Caddy; tag `previous-image-id` back to
`model-server-testflight-api:latest`, then recreate **only** `testflight-api`
with the previous source/configuration recorded in `previous-container.json`
and `--no-build --no-deps`. If no prior container existed, stop/remove only the
new testflight API. Keep the SQL migration. Verify public readiness afterward.
The script does not claim automatic rollback after a partial failure.

## Beta acceptance before production

- Export/upload a new iOS archive that embeds `https://staging.api.keeline.xyz`.
  Follow Time Fragment's `docs/testflight-sandbox-endpoint.md`; the script now
  requires `DEVELOPMENT_TEAM`. Verify the archive URL, build number, and signing.
- In the installed TestFlight build, validate a signed Apple sandbox purchase,
  restore, AI usage, and notification processing. Health probes and synthetic
  SQL tests cannot establish this acceptance.
- Point Apple's sandbox notification URL to
  `https://staging.api.keeline.xyz/webhooks/apple`; retain the production URL at
  `https://api.keeline.xyz/webhooks/apple`.
- Keep `testflight-billing-worker` stopped while the existing billing worker
  still polls sandbox. After beta acceptance, use the reviewed release's Compose
  files to build the production API and both workers; back up the stable `.env`,
  change its `APPLE_ENVIRONMENT` to `production`, stop the old sandbox worker,
  then recreate only `time-fragment-api`, `billing-worker`, and
  `testflight-billing-worker` under the existing `model-server` project with
  `--no-deps`. Inspect the production API/worker container configuration and
  require `APPLE_ENVIRONMENT=production`; the production proxy does not expose
  an environment header. Verify both public readiness endpoints and that beta
  retains its `sandbox` proxy header, then verify real entitlement/AI behavior
  in each channel.
- Release a distinct iOS App Store archive embedding `https://api.keeline.xyz`.
  Never submit the staging-endpoint beta archive for public release.

The updated iOS cache is endpoint scoped and discards the old unscoped cache.
Old app versions can still show cached membership offline until expiry/refresh;
server configuration cannot remotely clear their Keychain.
