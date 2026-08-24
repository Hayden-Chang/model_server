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

Configure renewal to reload Caddy after a successful renewal:

```bash
sudo certbot renew --deploy-hook 'docker compose -f /opt/model_server/docker-compose.yml exec -T caddy caddy reload --config /etc/caddy/Caddyfile'
```

## Service startup

Copy `.env.example` to `.env`, replace every placeholder with independent
secrets/provider settings, then:

```bash
cd /opt/model_server
sudo docker compose config
sudo docker compose pull
sudo docker compose build business-api
sudo docker compose up -d
sudo docker compose ps
```

Never commit `.env`. Only Caddy should show a published host port in
`docker compose ps`.

