# Staging

The staging endpoint is `https://staging.api.keeline.xyz`. Its Compose project
is `model-server-staging`, deployed separately under `/opt/model_server_staging`.
Run `docker compose --env-file .env -f docker-compose.staging.yml ...` from that
directory. Set `STAGING_SOURCE_SHA` to the exact committed source being deployed.

Staging has its own Caddy, Business API, LiteLLM, SQLite volume, guest token
signing secret, proxy key, business API key, and admin key. Generate those four
secrets independently; do not copy production data or authentication secrets.
Only the upstream model provider account is shared. Keep `.env` mode 0600.

The existing public Caddy routes the staging hostname to `staging-caddy:8080`.
Only the staging Caddy joins the ingress Docker network; Business API and
LiteLLM remain on the staging network with no published host ports. Install the
separate staging certificate using the existing Nginx ACME webroot, append
`deploy/Caddyfile.staging-ingress` to the ingress configuration, validate it,
and reload it without restarting production. The existing certificate renewal
hook must reload that ingress for renewals of both API certificates.

Do not start this stack alongside production on the current 1.6 GiB host.
The initial co-location attempt caused sustained memory pressure and production
health-check timeouts despite 2 GiB of swap and container resource limits.
Those limits have not been validated for live staging model traffic. Provision
adequate isolated capacity and measure startup peaks before enabling staging;
swap and container limits alone do not establish that the host has capacity.

Verify public live/ready checks, `X-Model-Server-Environment: staging`, guest
authentication, a real valid planning proposal, and rejection of production
tokens by staging and staging tokens by production. Check that usage is stored
in `model-server-staging_staging-usage`, not the production volume.

Stop only staging with `docker compose --env-file .env -f docker-compose.staging.yml down`.
Do not use `--volumes` when stopping it. Remove the staging ingress site only
after preserving and validating the rest of the production Caddy configuration.

## Global start recovery

Legacy apps include the same global start both as `earliestStartSlot` and an
exact leading `从 HH:mm 开始\n` header. The model projection omits that matching
header. If the model nevertheless quotes only that header as a task's start,
the clock compiler removes that mistaken boundary and lets the scheduler use
the global constraint. Explicit task clocks and mismatched headers still pass
through normal evidence validation. Genuine correction calls use low reasoning
effort with a 30-second timeout; empty model content records its finish reason.
