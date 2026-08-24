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
