#!/usr/bin/env sh
set -eu

: "${PUBLIC_IP:?PUBLIC_IP is required}"
: "${BUSINESS_API_KEY:?BUSINESS_API_KEY is required}"
: "${ADMIN_API_KEY:?ADMIN_API_KEY is required}"

base_url="https://${PUBLIC_IP}"
script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"
connect_timeout_seconds=10
standard_timeout_seconds=30
planning_timeout_seconds=120
time_fragment_device_id="time-fragment-production-smoke"

curl --fail-with-body --silent --show-error \
  --connect-timeout "${connect_timeout_seconds}" \
  --max-time "${standard_timeout_seconds}" \
  "${base_url}/health/live"
printf '\n'

curl --fail-with-body --silent --show-error \
  --connect-timeout "${connect_timeout_seconds}" \
  --max-time "${standard_timeout_seconds}" \
  -H "Authorization: Bearer ${BUSINESS_API_KEY}" \
  -H "X-Device-ID: model-server-general-text-smoke" \
  -H "Content-Type: application/json" \
  -d '{"input":"Reply with a short greeting."}' \
  "${base_url}/v1/pipelines/general-text-v1:run"
printf '\n'

curl --fail-with-body --silent --show-error \
  --connect-timeout "${connect_timeout_seconds}" \
  --max-time "${standard_timeout_seconds}" \
  -H "Authorization: Bearer ${BUSINESS_API_KEY}" \
  -H "X-Device-ID: model-server-general-analysis-smoke" \
  -H "Content-Type: application/json" \
  -d '{"input":"Analyze the main risk of deploying without health checks."}' \
  "${base_url}/v1/pipelines/general-analysis-v1:run"
printf '\n'

token="$(curl --fail-with-body --silent --show-error \
  --connect-timeout "${connect_timeout_seconds}" \
  --max-time "${standard_timeout_seconds}" \
  -H "Content-Type: application/json" \
  -d "{\"device_id\":\"${time_fragment_device_id}\"}" \
  "${base_url}/api/auth/guest" \
  | python3 -c 'import json,sys; value=json.load(sys.stdin)["access_token"]; assert isinstance(value,str) and value; sys.stdout.write(value)')"

plan_date="$(TZ=Asia/Shanghai date '+%Y-%m-%d')"
now="${plan_date}T00:00:00+08:00"
base_fingerprint="$(python3 -c '
import hashlib
import json
import sys

projection = {
    "currentPlan": {"date": sys.argv[1], "items": []},
    "hiddenPendingDeletionOccurrenceSnapshots": [],
}
canonical = json.dumps(
    projection,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
sys.stdout.write("sha256:" + hashlib.sha256(canonical).hexdigest())
' "${plan_date}")"
app_request_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
http_request_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"

if [ "${app_request_id}" = "${http_request_id}" ]; then
  printf '%s\n' 'generated App and HTTP request IDs must be independent' >&2
  exit 1
fi

request_file="$(mktemp)"
response_file="$(mktemp)"
headers_file="$(mktemp)"
summary_file="$(mktemp)"
detail_file="$(mktemp)"
trap 'rm -f "${request_file}" "${response_file}" "${headers_file}" "${summary_file}" "${detail_file}"' EXIT

python3 -c '
import json
import sys

request_id, fingerprint, plan_date, now = sys.argv[1:]
json.dump(
    {
        "text": "Add one task named Production Smoke using the default duration.",
        "requestID": request_id,
        "baseFingerprint": fingerprint,
        "currentPlan": {"date": plan_date, "items": []},
        "now": now,
    },
    sys.stdout,
    separators=(",", ":"),
)
' "${app_request_id}" "${base_fingerprint}" "${plan_date}" "${now}" >"${request_file}"

curl --fail-with-body --silent --show-error \
  --connect-timeout "${connect_timeout_seconds}" \
  --max-time "${planning_timeout_seconds}" \
  -H "Authorization: Bearer ${token}" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: ${http_request_id}" \
  --data-binary "@${request_file}" \
  --dump-header "${headers_file}" \
  --output "${response_file}" \
  "${base_url}/api/plan/parse"

returned_http_request_id="$(awk '
tolower($1) == "x-request-id:" {
    value = $2
    sub(/\r$/, "", value)
}
END { print value }
' "${headers_file}")"

if [ "${returned_http_request_id}" != "${http_request_id}" ]; then
  printf '%s\n' 'Time Fragment HTTP X-Request-ID was not echoed exactly' >&2
  exit 1
fi

python3 "${script_dir}/validate-time-fragment-smoke.py" \
  "${response_file}" \
  "${app_request_id}" \
  "${base_fingerprint}" \
  "${plan_date}"

curl --fail-with-body --silent --show-error \
  --connect-timeout "${connect_timeout_seconds}" \
  --max-time "${standard_timeout_seconds}" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  --output "${summary_file}" \
  "${base_url}/admin/observability/summary?device_id=${time_fragment_device_id}"

curl --fail-with-body --silent --show-error \
  --connect-timeout "${connect_timeout_seconds}" \
  --max-time "${standard_timeout_seconds}" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  --output "${detail_file}" \
  "${base_url}/admin/observability/requests?device_id=${time_fragment_device_id}&limit=1"

python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    totals = json.load(handle)["totals"]
with open(sys.argv[2], encoding="utf-8") as handle:
    records = json.load(handle)["records"]
assert records and records[0]["request_id"] == sys.argv[3]
assert records[0]["usage"] is not None
assert records[0]["usage"]["total_tokens"] > 0
assert totals["request_count"] >= 1
assert totals["model_call_count"] >= 1
assert totals["token_reported_requests"] >= 1
assert totals["total_tokens"] > 0
print(
    "Observability smoke passed: requests={} model_calls={} total_tokens={}".format(
        totals["request_count"],
        totals["model_call_count"],
        totals["total_tokens"],
    )
)
' "${summary_file}" "${detail_file}" "${http_request_id}"
