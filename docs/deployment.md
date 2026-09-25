# Deployment on a domain

The target server already has Nginx on port 80 and existing APIs on private
ports. This project uses only the currently free public port 443 and is deployed
under `/opt/model_server`.

## HTTPS certificate

Keep Nginx on port 80 and add this location to its existing default server block
so Certbot can complete the HTTP-01 webroot challenge:

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /var/www/model-server-acme;
    default_type text/plain;
}
```

After the public A record resolves to this server, test issuance against staging:

```bash
sudo mkdir -p /var/www/model-server-acme
sudo certbot certonly \
  --dry-run \
  --non-interactive \
  --webroot \
  --webroot-path /var/www/model-server-acme \
  --cert-name api.keeline.xyz \
  -d api.keeline.xyz
```

Then request the production certificate:

```bash
sudo certbot certonly \
  --non-interactive \
  --webroot \
  --webroot-path /var/www/model-server-acme \
  --cert-name api.keeline.xyz \
  -d api.keeline.xyz
```

Install the deploy hook so Caddy loads the renewed certificate after each
successful renewal:

```bash
sudo install -m 0755 \
  /opt/model_server/deploy/model-server-caddy-renew-hook.sh \
  /etc/letsencrypt/renewal-hooks/deploy/model-server-caddy-renew-hook.sh
sudo certbot renew --dry-run --run-deploy-hooks
```

## Service startup

The target server may not be able to reach Docker Hub. Download the pinned
official Caddy release archive and verify it against the release checksum, then
place the binary at `caddy/caddy`. The project builds a minimal scratch image
from that official static binary, so no third-party image mirror is required.

```bash
curl -fLO https://github.com/caddyserver/caddy/releases/download/v2.11.4/caddy_2.11.4_linux_amd64.tar.gz
curl -fLO https://github.com/caddyserver/caddy/releases/download/v2.11.4/caddy_2.11.4_checksums.txt
sha512sum --check --ignore-missing caddy_2.11.4_checksums.txt
tar -xzf caddy_2.11.4_linux_amd64.tar.gz caddy
install -m 0755 caddy /opt/model_server/caddy/caddy
```

Copy `.env.example` to `.env`, replace every placeholder with independent
secrets/provider settings, including a dedicated `ADMIN_API_KEY` for
observability reads and Time Fragment quota resets, provide the Apple signing key
described below, then:

```bash
cd /opt/model_server
sudo docker compose -f docker-compose.yml -f docker-compose.accounts.yml config
sudo docker compose -f docker-compose.yml -f docker-compose.accounts.yml pull litellm
sudo docker compose -f docker-compose.yml -f docker-compose.accounts.yml \
  build caddy business-api time-fragment-api billing-worker testflight-api testflight-billing-worker
sudo docker compose -f docker-compose.yml -f docker-compose.accounts.yml up -d \
  caddy business-api litellm time-fragment-api billing-worker testflight-api
sudo docker compose -f docker-compose.yml -f docker-compose.accounts.yml ps
```

The accounts overlay is the deployed topology, so every command that creates or
recreates this stack needs both files. It adds the `time-fragment-api` and
`billing-worker` containers and mounts `Caddyfile.accounts` over
`/etc/caddy/Caddyfile`; that file owns the `/api/*`, `/billing/*`, and
`/webhooks/apple` routes to `time-fragment-api`, so the base `Caddyfile` is no
longer the configuration Caddy serves and those routes do not exist without the
overlay. `quota-rollback` is a one-off in the same overlay, gated behind the
`rollback` profile, with a writable legacy volume and no published port; only the
cutover rollback procedure in [account API](account-api.md) starts it.

The same overlay serves `staging.api.keeline.xyz` through `testflight-api` and
`testflight-billing-worker`. Both set `APPLE_ENVIRONMENT=sandbox` explicitly;
`api.keeline.xyz` uses the release services and switches to `production` only
after the environment-isolation migration and TestFlight entry are verified.
The staging hostname's existing DNS and certificate are reused, so the legacy
full staging stack must remain off. Update the sandbox Apple notification URL
separately in App Store Connect. A beta archive uses
`MODEL_SERVER_API_BASE=https://staging.api.keeline.xyz`; the App Store archive
uses the project's `https://api.keeline.xyz` default. These are separate
archives; never submit a sandbox-pointing archive as the release build.
Before the public environment switch, the existing `billing-worker` still
handles sandbox events; leave `testflight-billing-worker` stopped. Start the
TestFlight worker when the public worker changes to production, so the two
workers never poll the same sandbox queue.

Every application service (`caddy`, `business-api`, `time-fragment-api`,
`billing-worker`, `testflight-api`, `testflight-billing-worker`, and
`quota-rollback`) is built on this host from the local build
context, and `caddy` also carries a local image tag,
`model-server-caddy:2.11.4`. The only image pulled from a registry is `litellm`.
`docker compose up -d` on its own therefore recreates containers on the previous
image. A code change takes effect only after `build` for the services it touched
and then `up -d`.

Compose never updates the tree it builds from. `scripts/billing-deploy.sh` runs
`git pull --ff-only` inside `/opt/model_server`, and
`scripts/billing-rollback-device-principal.sh` refuses to run when that directory
is not a git checkout; `scripts/deploy-business-api.sh` also accepts an unpacked
release archive that records its revision in `.candidate-source-sha`. Sync the
revision you intend to run before building, and never assume the deployed tree
matches the branch you reviewed.

### Apple signing key

The overlay bind-mounts `./.secrets` read-only at `/opt/model_server/.secrets` in
both `time-fragment-api` and `billing-worker`, and `APPLE_PRIVATE_KEY_PATH`
defaults to `/opt/model_server/.secrets/apple.p8`. The key must exist on the host
at that path and be readable by the container user (UID 10001); the configured
path must stay in step with the mount, because the container cannot see any other
host path. A missing or unreadable key does not stop the service: it fails closed,
so every purchase returns `BILLING_NOT_CONFIGURED` (503) after StoreKit has
already charged the user. Exercise a real purchase or restore right after
rollout. `STORE_REFERENCE_KEY`, which encrypts the stored original transaction
ids, is required outright: the Compose file refuses to render the account
services while it is unset or empty.

Neither `.env` nor `.secrets/` may be committed. Only Caddy should show a
published host port in `docker compose ps`. The named `model-server-usage` volume
stores SQLite data across `business-api` container rebuilds. Back up or migrate
that volume before removing it; `docker compose down` without `--volumes`
preserves it.

## Production verification

After deployment, run the repository smoke script from `/opt/model_server` with
`PUBLIC_DOMAIN`, `BUSINESS_API_KEY`, and `ADMIN_API_KEY` already present in the
operator's environment:

```bash
cd /opt/model_server
scripts/verify-production.sh
```

The script checks `/health/live`, `general-text-v1`, and
`general-analysis-v1`, then exercises the Time Fragment V2 chain:

```text
POST /api/auth/guest
→ guest Bearer Token
→ POST /api/plan/parse
→ complete V2 PlanProposal validation
```

The Time Fragment request uses the current `Asia/Shanghai` local date, midnight
at `+08:00`, a non-null empty `currentPlan`, an App `requestID` independent from
the HTTP `X-Request-ID`, and the SHA-256 of the same sorted-key canonical empty
projection used by iOS (`currentPlan` plus
`hiddenPendingDeletionOccurrenceSnapshots`). It asks for one task titled
`Production Smoke`; the current deterministic planner represents the 30-minute
default as two 15-minute slots. Every curl call has explicit connection and
overall timeouts, with a longer overall timeout for the model-backed request.

The V2 assertion helper checks the echoed request ID and fingerprint, supported
algorithm version, candidate date and complete item set, one-or-two model-call
count, the exact `Production Smoke` title in both the add operation and candidate,
segment bounds and total duration, explicit-delete consistency, and the absence
of `status`, `authorizationText`, and `isExplicit` anywhere in the public
response. A semantic or parse failure therefore makes this production smoke fail
even when the endpoint correctly used HTTP 200 for the business result.

Temporary request, response, and header files are created with `mktemp` and
removed by a trap. The guest token remains only in a shell variable and is not
printed or written to disk. The script prints only the existing non-sensitive
general Pipeline responses and a concise Time Fragment success summary.

This smoke verifies the currently deployed request path and one real model
response. It does not prove or provision formal accounts, exhaust/reset the
per-installation quota, perform cost accounting, validate regional routing,
verify compliance presentation, or test additional gateway anti-abuse controls.
The smoke also queries the observability summary
for its Time Fragment device and confirms that the just-completed request and
reported Token metadata are visible; it does not inspect or print raw content.

## Operational scripts

Run these from the deployment root; each script's own header documents its exact
arguments and required environment.

- `scripts/billing-deploy.sh` — billing rollout (Phase A1–A6): backup, code
  update, build, start, verify. Run as root in `/opt/model_server`, with the
  Supabase migrations already applied to the hosted project.
- `scripts/billing-rollback-device-principal.sh` — the supported production
  rollback entry point, and the only path that reverses the database and restores
  the pre-deploy code together. It reverses migrations
  `202609170017`–`202609170020` in the hosted database behind a fail-closed
  preflight guard, then rebuilds and restarts from the pre-deploy revision. It
  requires `SUPABASE_DB_URL` (the direct Postgres connection string, not the REST
  URL) and a usable checkout, which must be confirmed **before** the window — see
  the preconditions below. Test it before you need it.
- `scripts/billing-rollback.sh` — superseded. It is kept only so the rollback
  instruction `billing-deploy.sh` prints still resolves, and it delegates to
  `billing-rollback-device-principal.sh` with the same argument.
- `scripts/rollback-account-ai-cutover.sh` — emergency rollback from the
  account-aware AI cutover to the legacy topology: stop public traffic, reverse
  export post-cutover usage, close the new ledger gate, then restore the legacy
  Compose stack. Check the plan with `--dry-run` before the window.
- `scripts/deploy-business-api.sh` — build and switch only `business-api`,
  automatically restoring its previous image when verification fails.
- `scripts/rollback-business-api.sh` — restore only `business-api` to the
  immutable image retained by `deploy-business-api.sh`.
- `scripts/verify-production.sh` — the production smoke described above.

### Rollback preconditions

`scripts/billing-rollback-device-principal.sh` needs the deployment root to be a
usable git checkout and refuses to do anything otherwise. Confirm that before the
window, not during an incident:

```bash
git -C /opt/model_server rev-parse --is-inside-work-tree
```

The presence of a `.git` entry does not prove it. A tree that was copied or
packed can keep an ASCII `gitfile` whose `gitdir:` line still points at the
machine that produced it, and the command above then answers
`fatal: not a git repository`; the `git pull --ff-only` in
`scripts/billing-deploy.sh` fails for the same reason. This check is the rollback
script's first preflight step, so it exits there, before any container is stopped
and before the database is touched. The refusal is safe, but it leaves no
rollback path until a real checkout exists. Prepare one at `/opt/model_server`,
or point `MODEL_SERVER_DIR` at one, and re-run.

If no usable checkout can be prepared in time, run the reverse migration by hand
as the single transaction its header defines, with the same guard the script
extracts from it:

```bash
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 \
  -f supabase/migrations/202609170099_billing_device_principal_down.sql
```

That path reverses the database only: the file's header states that the deploy
code has to move with it, so the pre-deploy revision must still be restored
separately. Keep `-v ON_ERROR_STOP=1`, or a failed transaction is reported as
success.
