from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from app.model_client import ModelGatewayUnavailable, ModelOutput
from app.settings import Settings


API_KEY = "business-test-key-with-32-characters"
ADMIN_KEY = "admin-test-key-with-32-characters"


@dataclass
class FakeModelClient:
    output: ModelOutput = field(
        default_factory=lambda: ModelOutput(
            content="A concise answer.",
            provider_model="provider/model-a",
            usage={"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        )
    )
    ready: bool = True
    error: Exception | None = None
    calls: list[tuple[Any, str]] = field(default_factory=list)

    async def is_ready(self) -> bool:
        return self.ready

    async def complete(self, pipeline: Any, user_input: str) -> ModelOutput:
        self.calls.append((pipeline, user_input))
        if self.error is not None:
            raise self.error
        return self.output


@pytest.fixture
def settings() -> Settings:
    return Settings(
        business_api_key=API_KEY,
        litellm_master_key="litellm-test-key-with-32-characters",
        litellm_base_url="http://litellm:4000",
        max_input_chars=10,
        time_fragment_token_secret="time-fragment-test-token-secret-with-32-characters",
        admin_api_key=ADMIN_KEY,
    )


def authorized_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def test_liveness_does_not_require_authentication(settings: Settings) -> None:
    with TestClient(create_app(settings, FakeModelClient())) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


@pytest.mark.parametrize("path", ["/admin/observability", "/admin/observability/ui"])
def test_observability_dashboard_loads_without_embedding_admin_key(
    settings: Settings,
    path: str,
) -> None:
    with TestClient(create_app(settings, FakeModelClient())) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert "模型调用观测" in response.text
    assert "/admin/observability/summary" in response.text
    assert "/admin/observability/requests" in response.text
    assert "sessionStorage" in response.text
    assert ADMIN_KEY not in response.text


@pytest.mark.parametrize("header", [None, "Bearer wrong-key"])
def test_pipeline_rejects_missing_or_invalid_authentication(settings: Settings, header: str | None) -> None:
    headers = {} if header is None else {"Authorization": header}
    with TestClient(create_app(settings, FakeModelClient())) as client:
        response = client.post(
            "/v1/pipelines/general-text-v1:run",
            headers=headers,
            json={"input": "hello"},
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHORIZED"


def test_text_pipeline_assembles_server_owned_parameters(settings: Settings) -> None:
    fake = FakeModelClient()
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/v1/pipelines/general-text-v1:run",
            headers={**authorized_headers(), "X-Request-ID": "client-request-7"},
            json={"input": "  hello  "},
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "client-request-7"
    assert response.json() == {
        "pipeline": "general-text-v1",
        "request_id": "client-request-7",
        "result": "A concise answer.",
        "model": {
            "alias": "primary-model",
            "provider_model": "provider/model-a",
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        },
    }
    pipeline, user_input = fake.calls[0]
    assert user_input == "hello"
    assert pipeline.temperature == 0.2
    assert pipeline.max_tokens == 2_000
    assert pipeline.messages(user_input)[0]["role"] == "system"


def test_structured_pipeline_validates_and_returns_object(settings: Settings) -> None:
    fake = FakeModelClient(
        output=ModelOutput(
            content='{"summary":"Short","key_points":["One"],"risks":[]}',
            provider_model="provider/model-a",
            usage=None,
        )
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/v1/pipelines/general-analysis-v1:run",
            headers=authorized_headers(),
            json={"input": "analyze"},
        )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "summary": "Short",
        "key_points": ["One"],
        "risks": [],
    }
    pipeline, _ = fake.calls[0]
    assert pipeline.response_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        '{"summary":"Short","key_points":["One"]}',
        '{"summary":"Short","key_points":[],"risks":[],"unexpected":true}',
    ],
)
def test_structured_pipeline_rejects_invalid_model_output(settings: Settings, content: str) -> None:
    fake = FakeModelClient(output=ModelOutput(content=content, provider_model=None, usage=None))
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/v1/pipelines/general-analysis-v1:run",
            headers=authorized_headers(),
            json={"input": "analyze"},
        )

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MODEL_OUTPUT_INVALID"


def test_unknown_pipeline_is_not_forwarded(settings: Settings) -> None:
    fake = FakeModelClient()
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/v1/pipelines/unknown-v1:run",
            headers=authorized_headers(),
            json={"input": "hello"},
        )

    assert response.status_code == 404
    assert fake.calls == []


@pytest.mark.parametrize(
    ("input_value", "expected_status"),
    [("          ", 422), ("1234567890", 200), ("12345678901", 413)],
)
def test_input_validation_boundaries(settings: Settings, input_value: str, expected_status: int) -> None:
    with TestClient(create_app(settings, FakeModelClient())) as client:
        response = client.post(
            "/v1/pipelines/general-text-v1:run",
            headers=authorized_headers(),
            json={"input": input_value},
        )

    assert response.status_code == expected_status


def test_gateway_outage_maps_to_service_unavailable(settings: Settings) -> None:
    fake = FakeModelClient(error=ModelGatewayUnavailable("model gateway is unavailable"))
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/v1/pipelines/general-text-v1:run",
            headers=authorized_headers(),
            json={"input": "hello"},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_GATEWAY_UNAVAILABLE"


def test_readiness_reflects_gateway_state(settings: Settings) -> None:
    with TestClient(create_app(settings, FakeModelClient(ready=False))) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_observability_records_content_tokens_and_aggregates_by_device(settings: Settings) -> None:
    device_a = "generic-ios-device-a-1234"
    device_b = "generic-ios-device-b-1234"
    with TestClient(create_app(settings, FakeModelClient())) as client:
        for request_id, device_id, content in (
            ("request-a-1", device_a, "hello"),
            ("request-a-2", device_a, "world"),
            ("request-b-1", device_b, "other"),
        ):
            response = client.post(
                "/v1/pipelines/general-text-v1:run",
                headers={
                    **authorized_headers(),
                    "X-Device-ID": device_id,
                    "X-Request-ID": request_id,
                },
                json={"input": content},
            )
            assert response.status_code == 200

        detail = client.get(
            "/admin/observability/requests",
            headers=admin_headers(),
            params={"device_id": device_a},
        )
        summary = client.get(
            "/admin/observability/summary",
            headers=admin_headers(),
        )

    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "no-store"
    assert detail.json()["total"] == 2
    record = detail.json()["records"][0]
    assert record["request_content"] == {"input": "world"}
    assert record["response_content"]["result"] == "A concise answer."
    assert record["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 4,
        "total_tokens": 9,
    }
    assert record["model_calls"][0]["input_content"] == "world"
    assert summary.status_code == 200
    assert summary.headers["cache-control"] == "no-store"
    assert summary.json()["totals"]["request_count"] == 3
    assert summary.json()["totals"]["total_tokens"] == 27
    assert len(summary.json()["devices"]) == 2


def test_observability_requires_admin_key_and_records_gateway_failure(settings: Settings) -> None:
    fake = FakeModelClient(error=ModelGatewayUnavailable("model gateway is unavailable"))
    with TestClient(create_app(settings, fake)) as client:
        failed = client.post(
            "/v1/pipelines/general-text-v1:run",
            headers={**authorized_headers(), "X-Device-ID": "generic-ios-device-failure"},
            json={"input": "hello"},
        )
        unauthorized = client.get("/admin/observability/requests")
        records = client.get(
            "/admin/observability/requests",
            headers=admin_headers(),
            params={"device_id": "generic-ios-device-failure"},
        )

    assert failed.status_code == 503
    assert unauthorized.status_code == 401
    assert records.status_code == 200
    record = records.json()["records"][0]
    assert record["status_code"] == 503
    assert record["usage"] is None
    assert record["model_calls"][0]["error_type"] == "ModelGatewayUnavailable"
