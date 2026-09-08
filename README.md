# Model Server

An API-first LLM service with three runtime containers and an embedded SQLite
observability store:

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
GET  /admin/observability/requests
GET  /admin/observability/summary
GET  /admin/observability
GET  /health/live
GET  /health/ready
```

Example:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${BUSINESS_API_KEY}" \
  -H 'X-Device-ID: example-installation-id-1234' \
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

Model `add` operations use a separate clock-extraction schema: `sourceText` is a
verbatim task quote, and `timeConstraint` is required even when its value is
`null` (an untimed task). A timed object contains nullable `startTime`, `endTime`,
`startEvidence`, and `endEvidence`; each stated boundary uses local `HH:mm` at
original minute precision and its own exact source quote. Display titles may
combine actions, such as a commute, without occurring verbatim in the quote.
The server validates the source clocks against the request, rounds boundaries
to the nearest 15 minutes, and computes range durations. A missing or mismatched
clock is an error, not permission to freely reschedule that task. Model adds
cannot supply `placement` or the protected-object `authorizationText` field.
Existing-object authorization and the public proposal schema are unchanged.
Solver-ready legacy operations and shared golden fixtures are internal only;
the live model parser never falls back to that schema or legacy prose recovery.

The first model call explicitly disables thinking and only extracts structured
operations; the deterministic planner computes any split time segments locally.
Tasks that no longer fit or whose requested time has already passed remain in
the candidate with empty `segments` and warning issues; these normal scheduling
outcomes never trigger correction. If parsing or error-level semantic validation
fails, the service enables thinking for one correction request containing only
error issues. A parseable second result that is still semantically invalid is
returned with HTTP 200 as a complete proposal and structured issues. A second
result that cannot be parsed is returned with HTTP 200 as `proposal: null` and
`PARSE_FAILED`. Authentication, request-size, request-shape, and
model-infrastructure failures use HTTP 401, 413, 422, 502, and 503 as applicable.

The route has a persistent per-installation quota of 50 usable AI requests by
default (`TIME_FRAGMENT_GUEST_QUOTA_LIMIT`). It atomically reserves one use by
App `requestID` before calling the model. Repeating a completed `requestID` is
rejected without another quota deduction or model call. Invalid input,
model-infrastructure failures, and a
final `proposal: null` response return the reservation; a usable proposal
consumes it. An exhausted installation receives HTTP 429 with code
`AI_QUOTA_EXHAUSTED` and an anonymous `TF-....-....` support code.

This is an internal-test control, not an account or a durable anti-abuse
boundary: deleting/reinstalling the App can create a new installation identity.
Registration, user sessions, account upgrades, cost accounting, and
launch-stage compliance/routing controls are not implemented.

## Observability API

Every authenticated model-backed request is stored with its pseudonymous
`device_key`, public request and response content, status, total duration, and
aggregated token usage. Each provider call is stored separately so a Time
Fragment correction request remains visible as a second model call.

Generic Pipeline clients may send an installation identifier in `X-Device-ID`.
The server stores the same stable `guest_...` SHA-256-derived key used by the
existing Time Fragment guest token, never the original identifier. Without the
header, generic requests are grouped under `unattributed`. This identifier is
for analytics only and is not an authentication or quota boundary.

Management endpoints require `ADMIN_API_KEY`, not the client-facing business or
guest credentials. Observability endpoints are read-only; quota reset endpoints
mutate only the installation quota ledger:

```text
GET /admin/observability/requests?device_id=<installation-id>&start_time=<ISO-8601>&end_time=<ISO-8601>
GET /admin/observability/summary?device_key=<guest-key>&start_time=<ISO-8601>&end_time=<ISO-8601>
GET /admin/time-fragment/quotas/<support-code>
POST /admin/time-fragment/quotas/<support-code>/reset
POST /admin/time-fragment/quotas/reset-all
```

The specific reset starts a fresh 50-use bucket only for the installation that
reported the support code. The reset-all operation starts fresh buckets lazily
on each installation's next AI request. Both the quota ledger and observability
records live in the existing `model-server-usage` SQLite volume.

Open `https://${PUBLIC_IP}/admin/observability` for the browser dashboard. The
page itself contains no data or credentials. Enter `ADMIN_API_KEY` in the login
form; the key is kept only in that tab's `sessionStorage` and sent as a Bearer
header to the management endpoints. It is never placed in the URL.

Raw API and model-call content is removed after
`USAGE_CONTENT_RETENTION_DAYS` (30 by default). Device, status, timing, model-call
count, and token metadata remain. Authorization headers and Bearer tokens are
never persisted. The SQLite database lives in a dedicated Docker volume.

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
