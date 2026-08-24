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

## Local development

Python 3.12 is used in the container. To run the focused test suite locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r business_api/requirements-dev.txt
.venv/bin/pytest -q business_api/tests
```

Docker Compose startup requires a populated `.env` and a certificate at
`/etc/letsencrypt/live/${PUBLIC_IP}`. See [deployment.md](docs/deployment.md).
