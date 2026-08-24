#!/usr/bin/env sh
set -eu

cd /opt/model_server
/usr/bin/docker compose restart caddy

