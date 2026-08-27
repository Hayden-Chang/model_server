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
secrets/provider settings, including a dedicated `ADMIN_API_KEY` for the
read-only observability endpoints, then:

```bash
cd /opt/model_server
sudo docker compose config
sudo docker compose pull litellm
sudo docker compose build caddy business-api
sudo docker compose up -d
sudo docker compose ps
```

Never commit `.env`. Only Caddy should show a published host port in
`docker compose ps`. The named `model-server-usage` volume stores SQLite data
across `business-api` container rebuilds. Back up or migrate that volume before
removing it; `docker compose down` without `--volumes` preserves it.

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
response. It does not prove or provision formal accounts, persistent quotas,
cost accounting, regional routing, compliance presentation, or additional
gateway anti-abuse controls. The smoke also queries the observability summary
for its Time Fragment device and confirms that the just-completed request and
reported Token metadata are visible; it does not inspect or print raw content.
