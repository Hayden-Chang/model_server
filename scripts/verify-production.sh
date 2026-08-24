#!/usr/bin/env sh
set -eu

: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${BUSINESS_API_KEY:?BUSINESS_API_KEY is required}"

base_url="https://${PUBLIC_IP}"

curl --fail-with-body --silent --show-error "${base_url}/health/live"
printf '\n'

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${BUSINESS_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":"Reply with a short greeting."}' \
  "${base_url}/v1/pipelines/general-text-v1:run"
printf '\n'

curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${BUSINESS_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"input":"Analyze the main risk of deploying without health checks."}' \
  "${base_url}/v1/pipelines/general-analysis-v1:run"
printf '\n'
