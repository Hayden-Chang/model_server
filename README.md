# Model Server

An API-first LLM service with three runtime containers and no database:

1. **Caddy** exposes the single public HTTPS port (`443`).
2. **Business API** owns authentication, versioned pipelines, prompt assembly,
   model parameters, and post-processing.
3. **LiteLLM Proxy** adapts the internal OpenAI-compatible request to the
   configured model provider.

Only Caddy publishes a host port. The Business API and LiteLLM communicate on
the private Docker Compose network.

Current component boundaries, repository layout, request flow, extension points,
and known limitations are documented in [architecture.md](docs/architecture.md).

## Public API

```text
POST /v1/pipelines/general-text-v1:run
POST /v1/pipelines/general-analysis-v1:run
POST /api/auth/guest
POST /api/plan/parse
GET  /health/live
GET  /health/ready
```

Example:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${BUSINESS_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"input":"Explain why the sky is blue in two sentences."}' \
  "https://${PUBLIC_IP}/v1/pipelines/general-text-v1:run"
```

`general-analysis-v1` returns an object matching this server-owned schema:

```json
{
  "summary": "string",
  "key_points": ["string"],
  "risks": ["string"]
}
```

Clients choose a versioned business pipeline, not a provider model. That keeps
provider swaps inside LiteLLM and prompt/schema changes inside a new pipeline
version.

## Time Fragment API

The iOS app uses a stateless guest token instead of embedding the business API
key. Its existing request and response contract is exposed directly by this
service:

```bash
TOKEN="$(curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"time-fragment-ios-example-device"}' \
  "https://${PUBLIC_IP}/api/auth/guest" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"

curl --fail-with-body \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"text":"9点到10点写周报","currentPlan":null,"now":"2026-08-24T08:00:00+08:00"}' \
  "https://${PUBLIC_IP}/api/plan/parse"
```

`/api/plan/parse` uses the server-owned `time-fragment-plan-v1` pipeline and
returns `{ "tasks": [...] }`. Model and business credentials never leave the
server. The guest token identifies an installation and enables a per-process
request limit; it is not an account or a durable anti-abuse boundary.

## Local development

Python 3.12 is used in the container. To run the focused test suite locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r business_api/requirements-dev.txt
.venv/bin/pytest -q business_api/tests
```

Docker Compose startup requires a populated `.env` and a certificate at
`/etc/letsencrypt/live/${PUBLIC_IP}`. See [deployment.md](docs/deployment.md).
