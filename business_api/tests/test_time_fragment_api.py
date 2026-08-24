import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.contracts import TimeFragmentCurrentPlan
from app.factory import create_app
from app.guest_auth import GuestTokenCodec, GuestTokenError
from app.model_client import ModelOutput
from app.settings import Settings


API_KEY = "business-test-key-with-32-characters"
TOKEN_SECRET = "time-fragment-test-token-secret-with-32-characters"


@dataclass
class FakeModelClient:
    output: ModelOutput
    calls: list[tuple[Any, str]] = field(default_factory=list)

    async def is_ready(self) -> bool:
        return True

    async def complete(self, pipeline: Any, user_input: str) -> ModelOutput:
        self.calls.append((pipeline, user_input))
        return self.output


@pytest.fixture
def settings() -> Settings:
    return Settings(
        business_api_key=API_KEY,
        litellm_master_key="litellm-test-key-with-32-characters",
        litellm_base_url="http://litellm:4000",
        time_fragment_token_secret=TOKEN_SECRET,
    )


def guest_headers(client: TestClient, device_id: str = "time-fragment-ios-device-1234") -> dict[str, str]:
    response = client.post("/api/auth/guest", json={"device_id": device_id})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def valid_output() -> ModelOutput:
    return ModelOutput(
        content=json.dumps(
            {
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "写方案",
                        "start": "2026-08-24T09:00:00",
                        "end": "2026-08-24T10:00:00",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        provider_model="provider/model-a",
        usage=None,
    )


def test_guest_token_does_not_expose_device_id_and_expires() -> None:
    codec = GuestTokenCodec(TOKEN_SECRET, ttl_seconds=60)
    token = codec.issue("time-fragment-ios-private-device", now=1_000)

    assert "private-device" not in token
    assert codec.verify(token, now=1_059).startswith("guest_")
    with pytest.raises(GuestTokenError):
        codec.verify(token, now=1_060)


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-token"])
def test_plan_parse_rejects_missing_or_invalid_guest_token(
    settings: Settings,
    authorization: str | None,
) -> None:
    fake = FakeModelClient(valid_output())
    headers = {} if authorization is None else {"Authorization": authorization}
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=headers,
            json={"text": "列计划", "currentPlan": None, "now": "2026-08-24T08:00:00+08:00"},
        )

    assert response.status_code == 401
    assert fake.calls == []


def test_plan_parse_adapts_time_fragment_request_to_server_owned_pipeline(settings: Settings) -> None:
    fake = FakeModelClient(valid_output())
    current_plan = {
        "id": "plan_20260824_ai",
        "date": "2026-08-24",
        "taskIds": ["task-1"],
        "tasks": [
            {
                "id": "task-1",
                "title": "写方案",
                "start": "2026-08-24T08:00:00",
                "end": "2026-08-24T09:00:00",
            }
        ],
        "checkins": [],
    }
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json={
                "text": "改到九点开始",
                "currentPlan": current_plan,
                "now": "2026-08-24T08:10:00+08:00",
            },
        )

    assert response.status_code == 200
    assert response.json() == json.loads(valid_output().content)
    pipeline, user_input = fake.calls[0]
    assert pipeline.pipeline_id == "time-fragment-plan-v1"
    assert pipeline.response_schema["additionalProperties"] is False
    assert json.loads(user_input) == {
        "text": "改到九点开始",
        "currentPlan": current_plan,
        "now": "2026-08-24T08:10:00+08:00",
    }
    assert set(TimeFragmentCurrentPlan.model_json_schema()["properties"]) == {
        "id",
        "date",
        "taskIds",
        "tasks",
        "checkins",
    }


def test_plan_parse_task_contract_does_not_include_status(settings: Settings) -> None:
    fake = FakeModelClient(
        ModelOutput(
            content=(
                '{"tasks":[{"id":"task-1","title":"写方案",'
                '"start":"2026-08-24T09:00:00","end":"2026-08-24T10:00:00"}]}'
            ),
            provider_model=None,
            usage=None,
        )
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json={"text": "列计划", "currentPlan": None, "now": "2026-08-24T08:00:00+08:00"},
        )

    assert response.status_code == 200
    task_schema = fake.calls[0][0].response_schema["properties"]["tasks"]["items"]
    assert "status" not in task_schema["required"]
    assert "status" not in task_schema["properties"]


@pytest.mark.parametrize(
    "content",
    [
        '{"tasks":[{"id":"task-1","title":"写方案","start":null,"end":"2026-08-24T10:00:00"}]}',
        '{"tasks":[{"id":"task-1","title":"写方案","start":"2026-08-25T09:00:00","end":"2026-08-25T10:00:00"}]}',
        '{"tasks":[{"id":"task-1","title":"写方案","start":"2026-08-24T09:05:00","end":"2026-08-24T10:00:00"}]}',
        '{"tasks":[{"id":"task-1","title":"写方案","start":"2026-08-24T09:00","end":"2026-08-24T10:00"}]}',
        '{"tasks":[{"id":"task-1","title":"   ","start":null,"end":null}]}',
        '{"tasks":[{"id":"task-1","title":"写方案","start":"2026-08-24T09:00:00","end":"2026-08-24T10:00:00","status":"scheduled"}]}',
    ],
)
def test_plan_parse_rejects_output_that_time_fragment_cannot_apply(
    settings: Settings,
    content: str,
) -> None:
    fake = FakeModelClient(ModelOutput(content=content, provider_model=None, usage=None))
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json={"text": "列计划", "currentPlan": None, "now": "2026-08-24T08:00:00+08:00"},
        )

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MODEL_OUTPUT_INVALID"


def test_plan_parse_does_not_rate_limit_guest_requests(settings: Settings) -> None:
    fake = FakeModelClient(valid_output())
    with TestClient(create_app(settings, fake)) as client:
        headers = guest_headers(client)
        responses = [
            client.post(
                "/api/plan/parse",
                headers=headers,
                json={
                    "text": f"第 {index + 1} 次列计划",
                    "currentPlan": None,
                    "now": "2026-08-24T08:00:00+08:00",
                },
            )
            for index in range(11)
        ]

    assert [response.status_code for response in responses] == [200] * 11
    assert len(fake.calls) == 11


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "列计划", "currentPlan": None, "now": "2026-08-24T08:00:00"},
        {"text": "列计划", "currentPlan": None, "now": "2026-08-24T08:00:00+08:00", "extra": True},
    ],
)
def test_plan_parse_rejects_requests_outside_project_contract(settings: Settings, payload: dict) -> None:
    fake = FakeModelClient(valid_output())
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=payload)

    assert response.status_code == 422
    assert fake.calls == []
