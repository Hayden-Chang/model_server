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
key. The guest endpoint accepts a device installation identifier and returns a
time-limited Bearer token for `/api/plan/parse`; it does not call the model.

The planning request is the V2 contract. `currentPlan` is always an object,
including an empty-day plan with `items: []`:

```json
{
  "text": "新增一个任务，使用默认时长",
  "requestID": "app-request-uuid",
  "baseFingerprint": "sha256:client-planning-baseline",
  "currentPlan": {
    "date": "2026-08-25",
    "items": []
  },
  "now": "2026-08-25T00:00:00+08:00"
}
```

`/api/plan/parse` uses the server-owned `time-fragment-plan-v2` pipeline. The
model receives only the planning projection: `text`, `now`, and `currentPlan`
items with `domainRef` removed. It does not receive the App request ID, baseline
fingerprint, or real domain references. The model proposes `add`, `move`,
`changeDuration`, `changeTitle`, or `delete` operations. `changeTitle` is
limited to an exact existing internal-task ID and never changes its segments;
ExternalEvent titles remain read-only source facts. The server assigns temporary
UUIDs, runs the deterministic planner, validates the complete candidate, and
returns a `PlanProposal`. The proposal is authoritative and contains the echoed
`baseFingerprint`, `algorithmVersion`, normalized `operations`, explicit delete
sets, and the complete `candidatePlan` with time segments.

The service calls the model once for a valid result, or once more with concrete
validation issues as a correction request. A parseable second result that is
still semantically invalid is returned with HTTP 200 as a complete proposal and
structured issues. A second result that cannot be parsed is returned with HTTP
200 as `proposal: null` and `PARSE_FAILED`. Authentication, request-size,
request-shape, and model-infrastructure failures use HTTP 401, 413, 422, 502,
and 503 as applicable.

There is no application-layer request-rate limiter and this route does not
return an application-generated 429. The current guest token identifies one
installation; it is not an account, persistent quota, audit trail, or durable
anti-abuse boundary. Registration, user sessions, account upgrades, persistent
usage accounting, and launch-stage compliance/routing controls are not
implemented.

Run the dynamic production smoke in [deployment.md](docs/deployment.md) to
exercise guest authentication and the full V2 planning response without
printing or writing the guest token.

## Local development

Python 3.12 is used in the container. To run the focused test suite locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r business_api/requirements-dev.txt
.venv/bin/pytest -q business_api/tests
```

Docker Compose startup requires a populated `.env` and a certificate at
`/etc/letsencrypt/live/${PUBLIC_IP}`. See [deployment.md](docs/deployment.md).
