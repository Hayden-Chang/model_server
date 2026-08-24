import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from app.guest_auth import GuestTokenCodec, GuestTokenError
from app.model_client import (
    ModelGatewayResponseError,
    ModelGatewayUnavailable,
    ModelOutput,
)
from app.settings import Settings


API_KEY = "business-test-key-with-32-characters"
TOKEN_SECRET = "time-fragment-test-token-secret-with-32-characters"


@dataclass
class FakeModelClient:
    outputs: list[ModelOutput | Exception]
    calls: list[tuple[Any, str]] = field(default_factory=list)

    async def is_ready(self) -> bool:
        return True

    async def complete(self, pipeline: Any, user_input: str) -> ModelOutput:
        index = len(self.calls)
        self.calls.append((pipeline, user_input))
        if index >= len(self.outputs):
            raise AssertionError("unexpected model call")
        output = self.outputs[index]
        if isinstance(output, Exception):
            raise output
        return output


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


def internal_item(*, pinned: bool = False) -> dict[str, Any]:
    return {
        "itemId": "occurrence-1",
        "objectType": "internalTask",
        "domainRef": {
            "taskId": "private-task-id",
            "occurrenceId": "occurrence-1",
            "scheduledTaskId": "private-scheduled-id",
        },
        "title": "钉住任务" if pinned else "写方案",
        "durationSlots": 4,
        "segments": [{"startSlot": 36, "endSlot": 40}],
        "isPinned": pinned,
        "isCompleted": False,
    }


def request_payload(
    *,
    text: str = "安排今天的任务",
    request_id: str = "app-request-1",
    fingerprint: str = "sha256:private-base",
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "text": text,
        "requestID": request_id,
        "baseFingerprint": fingerprint,
        "currentPlan": {
            "date": "2026-08-24",
            "items": [] if items is None else items,
        },
        "now": "2026-08-24T08:10:00+08:00",
    }


def operations_output(operations: list[dict[str, Any]]) -> ModelOutput:
    return raw_output(json.dumps({"operations": operations}, ensure_ascii=False))


def raw_output(content: str) -> ModelOutput:
    return ModelOutput(content=content, provider_model="provider/model-a", usage=None)


def recursive_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(recursive_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(recursive_keys(item) for item in value))
    return set()


def test_guest_token_does_not_expose_device_id_and_expires() -> None:
    codec = GuestTokenCodec(TOKEN_SECRET, ttl_seconds=60)
    token = codec.issue("time-fragment-ios-private-device", now=1_000)

    assert "private-device" not in token
    assert codec.verify(token, now=1_059).startswith("guest_")
    with pytest.raises(GuestTokenError):
        codec.verify(token, now=1_060)


@pytest.mark.parametrize("authorization", [None, "Bearer invalid-token"])
def test_plan_parse_rejects_missing_or_invalid_guest_token_without_calling_model(
    settings: Settings,
    authorization: str | None,
) -> None:
    fake = FakeModelClient([])
    headers = {} if authorization is None else {"Authorization": authorization}
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=headers, json=request_payload())

    assert response.status_code == 401
    assert fake.calls == []


def invalid_payloads() -> list[dict[str, Any]]:
    null_plan = request_payload()
    null_plan["currentPlan"] = None
    extra_field = request_payload()
    extra_field["extra"] = True
    status_field = request_payload(items=[internal_item()])
    status_field["currentPlan"]["items"][0]["status"] = "scheduled"
    missing_request_id = request_payload()
    del missing_request_id["requestID"]
    return [null_plan, extra_field, status_field, missing_request_id]


@pytest.mark.parametrize("payload", invalid_payloads())
def test_plan_parse_rejects_invalid_v2_input_without_calling_model(
    settings: Settings,
    payload: dict[str, Any],
) -> None:
    fake = FakeModelClient([])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=payload)

    assert response.status_code == 422
    assert fake.calls == []


def test_plan_parse_rejects_oversized_initial_model_input_before_calling_model(
    settings: Settings,
) -> None:
    limited_settings = settings.model_copy(update={"max_input_chars": 200})
    fake = FakeModelClient([operations_output([])])
    with TestClient(create_app(limited_settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(items=[internal_item()]),
        )

    assert response.status_code == 413
    assert response.json()["detail"] == {
        "code": "INPUT_TOO_LARGE",
        "message": "input exceeds the configured limit",
    }
    assert fake.calls == []


def test_plan_parse_rejects_oversized_correction_input_before_second_model_call(
    settings: Settings,
) -> None:
    limited_settings = settings.model_copy(update={"max_input_chars": 300})
    unknown_move = {
        "type": "move",
        "targetItemId": "missing-item",
        "allowedChanges": ["segments"],
        "inputOrder": 0,
    }
    fake = FakeModelClient([operations_output([unknown_move]), operations_output([])])
    with TestClient(create_app(limited_settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(items=[internal_item()]),
        )

    assert response.status_code == 413
    assert response.json()["detail"] == {
        "code": "INPUT_TOO_LARGE",
        "message": "input exceeds the configured limit",
    }
    assert len(fake.calls) == 1
    assert len(fake.calls[0][1]) <= limited_settings.max_input_chars


def test_plan_parse_returns_complete_v2_envelope_for_empty_current_plan(settings: Settings) -> None:
    fake = FakeModelClient(
        [
            operations_output(
                [
                    {
                        "type": "add",
                        "title": "写方案",
                        "durationSlots": 3,
                        "placement": {"anchor": "start", "slot": 40},
                        "inputOrder": 0,
                    }
                ]
            )
        ]
    )
    payload = request_payload(request_id="app-request-envelope", fingerprint="sha256:base-envelope")
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers={**guest_headers(client), "X-Request-ID": "http-request-envelope"},
            json=payload,
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "http-request-envelope"
    body = response.json()
    assert body["requestID"] == "app-request-envelope"
    assert body["proposal"]["baseFingerprint"] == "sha256:base-envelope"
    assert body["proposal"]["algorithmVersion"] == "time-fragment-planner-v1"
    assert body["proposal"]["deletedOccurrenceIDs"] == []
    assert body["proposal"]["deletedExternalEventIDs"] == []
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    operation = body["proposal"]["operations"][0]
    candidate = body["proposal"]["candidatePlan"]
    assert operation["type"] == "add"
    assert UUID(operation["temporaryId"])
    assert candidate == {
        "date": "2026-08-24",
        "items": [
            {
                "itemId": operation["temporaryId"],
                "objectType": "internalTask",
                "domainRef": None,
                "title": "写方案",
                "durationSlots": 3,
                "segments": [{"startSlot": 40, "endSlot": 43}],
                "isPinned": False,
                "isCompleted": False,
            }
        ],
    }
    assert "status" not in recursive_keys(body)
    assert len(fake.calls) == 1
    pipeline, first_input = fake.calls[0]
    assert pipeline.pipeline_id == "time-fragment-plan-v2"
    assert json.loads(first_input) == {
        "text": payload["text"],
        "currentPlan": {"date": "2026-08-24", "items": []},
        "now": payload["now"],
    }


def test_first_semantic_failure_sends_redacted_candidate_and_is_corrected_once(
    settings: Settings,
) -> None:
    fake = FakeModelClient(
        [
            operations_output(
                [
                    {
                        "type": "move",
                        "targetItemId": "missing-item",
                        "allowedChanges": ["segments"],
                        "placement": {"anchor": "start", "slot": 44},
                        "inputOrder": 0,
                    }
                ]
            ),
            operations_output([]),
        ]
    )
    payload = request_payload(items=[internal_item()])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=payload)

    assert response.status_code == 200
    assert response.json()["validation"] == {"valid": True, "attempts": 2, "issues": []}
    assert len(fake.calls) == 2
    first_input = json.loads(fake.calls[0][1])
    correction = json.loads(fake.calls[1][1])
    assert correction["originalRequest"] == first_input
    assert correction["issues"] == [
        {
            "code": "UNKNOWN_TARGET",
            "message": "操作引用的 itemId 不存在于 currentPlan",
            "itemId": "missing-item",
            "field": "targetItemId",
        }
    ]
    assert correction["firstCandidate"]["candidatePlan"]["items"][0]["itemId"] == "occurrence-1"
    for serialized in (fake.calls[0][1], fake.calls[1][1]):
        assert "domainRef" not in serialized
        assert "private-task-id" not in serialized
        assert "private-scheduled-id" not in serialized
        assert payload["requestID"] not in serialized
        assert payload["baseFingerprint"] not in serialized
        assert "status" not in recursive_keys(json.loads(serialized))


def test_second_parseable_semantic_failure_keeps_second_complete_candidate(
    settings: Settings,
) -> None:
    unknown_move = {
        "type": "move",
        "targetItemId": "missing-item",
        "allowedChanges": ["segments"],
        "inputOrder": 1,
    }
    fake = FakeModelClient(
        [
            operations_output([unknown_move]),
            operations_output(
                [
                    {
                        "type": "add",
                        "title": "第二次候选",
                        "durationSlots": 2,
                        "inputOrder": 0,
                    },
                    unknown_move,
                ]
            ),
        ]
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(items=[internal_item()]),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["proposal"] is not None
    assert body["validation"]["valid"] is False
    assert body["validation"]["attempts"] == 2
    assert [issue["code"] for issue in body["validation"]["issues"] if issue["severity"] == "error"] == [
        "UNKNOWN_TARGET"
    ]
    assert {item["title"] for item in body["proposal"]["candidatePlan"]["items"]} == {
        "写方案",
        "第二次候选",
    }
    assert len(fake.calls) == 2


def test_protected_authorization_failure_returns_second_candidate_without_internal_evidence(
    settings: Settings,
) -> None:
    def protected_move(slot: int) -> dict[str, Any]:
        return {
            "type": "move",
            "targetItemId": "occurrence-1",
            "objectType": "internalTask",
            "allowedChanges": ["segments"],
            "placement": {"anchor": "start", "slot": slot},
            "inputOrder": 0,
        }

    fake = FakeModelClient(
        [operations_output([protected_move(44)]), operations_output([protected_move(48)])]
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(text="整理今天的计划", items=[internal_item(pinned=True)]),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"]["attempts"] == 2
    assert body["validation"]["valid"] is False
    assert "PROTECTED_OBJECT" in [issue["code"] for issue in body["validation"]["issues"]]
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [
        {"startSlot": 36, "endSlot": 40}
    ]
    assert body["proposal"]["operations"] == []
    assert "authorizationText" not in recursive_keys(body["proposal"]["operations"])
    correction = json.loads(fake.calls[1][1])
    assert correction["issues"][0]["code"] == "PROTECTED_OBJECT"
    assert correction["issues"][0]["itemId"] == "occurrence-1"
    assert correction["firstCandidate"]["operations"] == []
    assert len(fake.calls) == 2


def test_internal_authorization_evidence_can_correct_protected_operation_but_is_not_public(
    settings: Settings,
) -> None:
    operation = {
        "type": "move",
        "targetItemId": "occurrence-1",
        "allowedChanges": ["segments"],
        "placement": {"anchor": "start", "slot": 44},
        "inputOrder": 0,
    }
    authorized = {**operation, "authorizationText": "把钉住任务移到十一点"}
    fake = FakeModelClient([operations_output([operation]), operations_output([authorized])])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(text="把钉住任务移到十一点", items=[internal_item(pinned=True)]),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {"valid": True, "attempts": 2, "issues": []}
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [
        {"startSlot": 44, "endSlot": 48}
    ]
    assert "authorizationText" not in recursive_keys(body)
    assert len(fake.calls) == 2


def test_two_unparseable_outputs_return_parse_failed_and_never_make_a_third_call(
    settings: Settings,
) -> None:
    fake = FakeModelClient(
        [
            raw_output("not-json"),
            raw_output('{"operations":"still-invalid"}'),
            operations_output([]),
        ]
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "requestID": "app-request-1",
        "proposal": None,
        "validation": {
            "valid": False,
            "attempts": 2,
            "issues": [
                {
                    "source": "model_server",
                    "severity": "error",
                    "code": "PARSE_FAILED",
                    "message": "大模型返回的调整内容无法解析",
                    "itemId": None,
                    "field": None,
                }
            ],
        },
    }
    correction = json.loads(fake.calls[1][1])
    assert correction["issues"] == [{"code": "PARSE_FAILED", "message": "模型输出不是有效 JSON"}]
    assert "firstCandidate" not in correction
    assert len(fake.calls) == 2


def test_structural_correction_prompt_identifies_the_exact_invalid_field(
    settings: Settings,
) -> None:
    fake = FakeModelClient(
        [
            operations_output([{"type": "add", "title": "缺少顺序"}]),
            operations_output([]),
        ]
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(),
        )

    assert response.status_code == 200
    assert response.json()["validation"]["attempts"] == 2
    correction = json.loads(fake.calls[1][1])
    assert correction["issues"] == [
        {
            "code": "PARSE_FAILED",
            "message": "模型输出字段 operations.0.add.inputOrder 不符合 operations 结构（missing）",
        }
    ]
    assert len(fake.calls) == 2


def test_first_parse_failure_then_second_valid_returns_attempts_two(settings: Settings) -> None:
    fake = FakeModelClient([raw_output("[]"), operations_output([])])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(),
        )

    assert response.status_code == 200
    assert response.json()["validation"] == {"valid": True, "attempts": 2, "issues": []}
    correction = json.loads(fake.calls[1][1])
    assert correction["issues"] == [
        {"code": "PARSE_FAILED", "message": "模型输出必须是 JSON 对象"}
    ]
    assert len(fake.calls) == 2


def test_plan_parse_does_not_rate_limit_eleven_guest_requests(settings: Settings) -> None:
    fake = FakeModelClient([operations_output([]) for _ in range(11)])
    with TestClient(create_app(settings, fake)) as client:
        headers = guest_headers(client)
        responses = [
            client.post(
                "/api/plan/parse",
                headers=headers,
                json=request_payload(
                    text=f"第 {index + 1} 次列计划",
                    request_id=f"request-{index + 1}",
                ),
            )
            for index in range(11)
        ]

    assert [response.status_code for response in responses] == [200] * 11
    assert all(response.json()["validation"]["attempts"] == 1 for response in responses)
    assert len(fake.calls) == 11


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            ModelGatewayUnavailable("model gateway is unavailable"),
            503,
            "MODEL_GATEWAY_UNAVAILABLE",
        ),
        (
            ModelGatewayResponseError("model gateway rejected the request"),
            502,
            "MODEL_GATEWAY_ERROR",
        ),
    ],
)
def test_plan_parse_preserves_gateway_error_boundaries(
    settings: Settings,
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    fake = FakeModelClient([error])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(),
        )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    assert len(fake.calls) == 1


def test_gateway_error_during_correction_is_not_converted_to_content_failure(
    settings: Settings,
) -> None:
    fake = FakeModelClient(
        [
            raw_output("not-json"),
            ModelGatewayUnavailable("model gateway is unavailable"),
        ]
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(),
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_GATEWAY_UNAVAILABLE"
    assert len(fake.calls) == 2


def test_expired_guest_token_can_be_refreshed_before_retrying_plan(settings: Settings) -> None:
    expired_token = GuestTokenCodec(TOKEN_SECRET, settings.time_fragment_token_ttl_seconds).issue(
        "time-fragment-ios-device-1234",
        now=1_000,
    )
    fake = FakeModelClient([operations_output([])])
    with TestClient(create_app(settings, fake)) as client:
        expired_response = client.post(
            "/api/plan/parse",
            headers={"Authorization": f"Bearer {expired_token}"},
            json=request_payload(),
        )
        refreshed_headers = guest_headers(client)
        retried_response = client.post(
            "/api/plan/parse",
            headers=refreshed_headers,
            json=request_payload(),
        )

    assert expired_response.status_code == 401
    assert retried_response.status_code == 200
    assert len(fake.calls) == 1
