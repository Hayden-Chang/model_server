# Deployment on an IP address

The target server already has Nginx on port 80 and existing APIs on private
ports. This project uses only the currently free public port 443 and is deployed
under `/opt/model_server`.

## HTTPS certificate

Let’s Encrypt IP-address certificates are short-lived (about six days). Certbot
5.4 or newer can request them with a webroot challenge. Keep Nginx on port 80 and
add this location to its existing default server block:

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /var/www/model-server-acme;
    default_type text/plain;
}
```

Then request the certificate (first use `--staging`, then repeat without it):

```bash
sudo mkdir -p /var/www/model-server-acme
sudo certbot certonly \
  --preferred-profile shortlived \
  --webroot \
  --webroot-path /var/www/model-server-acme \
  --ip-address 47.120.13.5
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
secrets/provider settings, then:

```bash
cd /opt/model_server
sudo docker compose config
sudo docker compose pull litellm
sudo docker compose build caddy business-api
sudo docker compose up -d
sudo docker compose ps
```

Never commit `.env`. Only Caddy should show a published host port in
`docker compose ps`.

## Production verification

After deployment, run the repository smoke script from `/opt/model_server` with
`PUBLIC_IP` and `BUSINESS_API_KEY` already present in the operator's environment:

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
the HTTP `X-Request-ID`, and a date-bearing baseline fingerprint. It asks for a
single default-duration task; the current deterministic planner represents the
30-minute default as two 15-minute slots.

The V2 assertion helper checks the echoed request ID and fingerprint, supported
algorithm version, candidate date and complete item set, one-or-two model-call
count, segment bounds and total duration, explicit-delete consistency, and the
absence of `status`, `authorizationText`, and `isExplicit` anywhere in the
public response. A semantic or parse failure therefore makes this production
smoke fail even when the endpoint correctly used HTTP 200 for the business
result.

Temporary request, response, and header files are created with `mktemp` and
removed by a trap. The guest token remains only in a shell variable and is not
printed or written to disk. The script prints only the existing non-sensitive
general Pipeline responses and a concise Time Fragment success summary.

This smoke verifies the currently deployed request path and one real model
response. It does not prove or provision formal accounts, persistent quotas,
audit storage, cost accounting, regional routing, compliance presentation, or
additional gateway anti-abuse controls; those capabilities are not implemented.
