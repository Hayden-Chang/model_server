import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
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
from app.pipelines import get_pipeline
from app.settings import Settings


API_KEY = "business-test-key-with-32-characters"
ADMIN_KEY = "admin-test-key-with-32-characters"
TOKEN_SECRET = "time-fragment-test-token-secret-with-32-characters"


def test_time_fragment_pipeline_has_twenty_thousand_output_token_budget() -> None:
    pipeline = get_pipeline("time-fragment-plan-v2")

    assert pipeline is not None
    assert pipeline.max_tokens == 20_000
    assert pipeline.thinking_mode == "disabled"


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
        admin_api_key=ADMIN_KEY,
    )


def guest_headers(client: TestClient, device_id: str = "time-fragment-ios-device-1234") -> dict[str, str]:
    response = client.post("/api/auth/guest", json={"device_id": device_id})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


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
    date: str = "2026-08-24",
    now: str = "2026-08-24T08:10:00+08:00",
    earliest_start_slot: int | None = None,
) -> dict[str, Any]:
    payload = {
        "text": text,
        "requestID": request_id,
        "baseFingerprint": fingerprint,
        "currentPlan": {
            "date": date,
            "items": [] if items is None else items,
        },
        "now": now,
    }
    if earliest_start_slot is not None:
        payload["earliestStartSlot"] = earliest_start_slot
    return payload


def operations_output(
    operations: list[dict[str, Any]],
    usage: dict[str, int] | None = None,
) -> ModelOutput:
    return raw_output(json.dumps({"operations": operations}, ensure_ascii=False), usage=usage)


def model_add(
    title: str,
    source_text: str,
    *,
    input_order: int = 0,
    duration_slots: int | None = None,
    priority: int | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    start_evidence: str | None = None,
    end_evidence: str | None = None,
) -> dict[str, Any]:
    operation = {
        "type": "add",
        "title": title,
        "sourceText": source_text,
        "timeConstraint": (
            {
                "startTime": start_time,
                "endTime": end_time,
                "startEvidence": start_evidence,
                "endEvidence": end_evidence,
            }
            if start_time is not None or end_time is not None
            else None
        ),
        "priority": priority,
        "inputOrder": input_order,
    }
    if duration_slots is not None:
        operation["durationSlots"] = duration_slots
    return operation


def raw_output(content: str, usage: dict[str, int] | None = None) -> ModelOutput:
    return ModelOutput(content=content, provider_model="provider/model-a", usage=usage)


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
                    model_add(
                        "写方案", "10:00 写方案", duration_slots=3,
                        start_time="10:00", start_evidence="10:00 写方案",
                    )
                ]
            )
        ]
    )
    payload = request_payload(
        text="10:00 写方案",
        request_id="app-request-envelope",
        fingerprint="sha256:base-envelope",
    )
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
    assert "authorizationText" not in recursive_keys(body)
    assert "sourceText" not in recursive_keys(body)
    assert "timeConstraint" not in recursive_keys(body)
    assert "status" not in recursive_keys(body)
    assert len(fake.calls) == 1
    pipeline, first_input = fake.calls[0]
    assert pipeline.pipeline_id == "time-fragment-plan-v2"
    assert pipeline.thinking_mode == "disabled"
    assert json.loads(first_input) == {
        "text": payload["text"],
        "currentPlan": {"date": "2026-08-24", "items": []},
        "now": payload["now"],
    }


def test_plan_parse_accepts_future_date_with_app_earliest_slot_in_one_model_call(
    settings: Settings,
) -> None:
    fake = FakeModelClient([
        operations_output([model_add("明天任务", "明天任务")])
    ])
    payload = request_payload(
        text="安排明天任务",
        request_id="app-request-future",
        date="2026-08-25",
        earliest_start_slot=36,
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"]["valid"] is True
    assert body["validation"]["attempts"] == 1
    assert body["proposal"]["candidatePlan"]["date"] == "2026-08-25"
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [
        {"startSlot": 36, "endSlot": 38}
    ]
    assert len(fake.calls) == 1
    assert json.loads(fake.calls[0][1])["earliestStartSlot"] == 36


def test_plan_parse_honors_app_earliest_slot_for_today(settings: Settings) -> None:
    fake = FakeModelClient([
        operations_output([model_add("当天任务", "当天任务")])
    ])
    payload = request_payload(
        text="安排当天任务",
        request_id="app-request-today-start",
        now="2026-08-24T10:15:59+08:00",
        earliest_start_slot=48,
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    assert response.json()["proposal"]["candidatePlan"]["items"][0]["segments"] == [
        {"startSlot": 48, "endSlot": 50}
    ]
    assert len(fake.calls) == 1
    assert json.loads(fake.calls[0][1])["earliestStartSlot"] == 48


def test_global_start_schedules_untimed_adds_by_requested_priority(
    settings: Settings,
) -> None:
    request_text = (
        "从 16:00 开始\n"
        "优先级 A 大于 B，同组 1 大于 2\n"
        "检查空调，b1，15 分钟\n"
        "把窗帘弄好，b2，15 分钟\n"
        "把投影幕布扔掉，b3，15 分钟\n"
        "把钥匙放下，b4，15 分钟\n"
        "再洗一遍衣，a1，30 分钟\n"
        "晾衣服，c1，30 分钟\n"
        "去我那里拿钥匙，a2，15 分钟\n"
        "买二手床，a3，30 分钟\n"
        "搬床垫，a4，15 分钟\n"
        "去拿蟑螂胶饵，a5，15 分钟"
    )
    model_tasks = [
        ("检查空调", 1, 2),
        ("把窗帘弄好", 1, 2),
        ("把投影幕布扔掉", 1, 2),
        ("把钥匙放下", 1, 2),
        ("再洗一遍衣", 2, 1),
        ("晾衣服", 2, None),
        ("去我那里拿钥匙", 1, 1),
        ("买二手床", 2, 1),
        ("搬床垫", 1, 1),
        ("去拿蟑螂胶饵", 1, 1),
    ]
    fake = FakeModelClient([
        operations_output([
            model_add(
                title, title, duration_slots=duration_slots,
                priority=priority_rank, input_order=input_order,
            )
            for input_order, (title, duration_slots, priority_rank) in enumerate(
                model_tasks
            )
        ])
    ])
    payload = request_payload(
        text=request_text,
        request_id="app-request-priority-order",
        now="2026-09-01T15:40:17+08:00",
        date="2026-09-01",
        earliest_start_slot=64,
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    assert json.loads(fake.calls[0][1])["earliestStartSlot"] == 64
    assert all(operation["placement"] is None for operation in body["proposal"]["operations"])
    assert {
        operation["title"]: operation["priority"]
        for operation in body["proposal"]["operations"]
    } == {
        "再洗一遍衣": 10,
        "去我那里拿钥匙": 9,
        "买二手床": 8,
        "搬床垫": 7,
        "去拿蟑螂胶饵": 6,
        "检查空调": 5,
        "把窗帘弄好": 4,
        "把投影幕布扔掉": 3,
        "把钥匙放下": 2,
        "晾衣服": 1,
    }
    scheduled = sorted(
        body["proposal"]["candidatePlan"]["items"],
        key=lambda item: item["segments"][0]["startSlot"],
    )
    assert [
        (item["title"], item["segments"])
        for item in scheduled
    ] == [
        ("再洗一遍衣", [{"startSlot": 64, "endSlot": 66}]),
        ("去我那里拿钥匙", [{"startSlot": 66, "endSlot": 67}]),
        ("买二手床", [{"startSlot": 67, "endSlot": 69}]),
        ("搬床垫", [{"startSlot": 69, "endSlot": 70}]),
        ("去拿蟑螂胶饵", [{"startSlot": 70, "endSlot": 71}]),
        ("检查空调", [{"startSlot": 71, "endSlot": 72}]),
        ("把窗帘弄好", [{"startSlot": 72, "endSlot": 73}]),
        ("把投影幕布扔掉", [{"startSlot": 73, "endSlot": 74}]),
        ("把钥匙放下", [{"startSlot": 74, "endSlot": 75}]),
        ("晾衣服", [{"startSlot": 75, "endSlot": 77}]),
    ]


def test_complete_priority_labels_use_natural_order_without_relation(
    settings: Settings,
) -> None:
    request_text = (
        "•  [ ] 检查空调，b1，15 分\n"
        "•  [ ] 把窗帘弄好，b2，15 分\n"
        "•  [ ] 把投影幕布扔掉，b3，15 分\n"
        "•  [ ] 把钥匙放下，b4，15 分\n"
        "•  [ ] 再洗一遍衣，a1，30 分钟\n"
        "•  [ ] 晾衣服，c1，30 分\n"
        "•  [ ] 去我那里拿钥匙，a2，15分钟\n"
        "•  [ ] 买二手床，a3，30 分钟\n"
        "•  [ ] 搬床垫，a4，15 分钟\n"
        "•  [ ] 去拿蟑螂胶饵，a5，15 分钟"
    )
    model_tasks = [
        ("检查空调", 1),
        ("把窗帘弄好", 1),
        ("把投影幕布扔掉", 1),
        ("把钥匙放下", 1),
        ("再洗一遍衣", 2),
        ("晾衣服", 2),
        ("去我那里拿钥匙", 1),
        ("买二手床", 2),
        ("搬床垫", 1),
        ("去拿蟑螂胶饵", 1),
    ]
    fake = FakeModelClient([
        operations_output([
            model_add(
                title, line.strip(), duration_slots=duration_slots,
                input_order=input_order,
            )
            for input_order, ((title, duration_slots), line) in enumerate(
                zip(model_tasks, request_text.splitlines(), strict=True)
            )
        ])
    ])
    payload = request_payload(
        text=request_text,
        request_id="app-request-natural-priority-order",
        now="2026-09-01T18:59:05+08:00",
        date="2026-09-01",
        earliest_start_slot=76,
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    assert [
        (operation["title"], operation["priority"])
        for operation in body["proposal"]["operations"]
    ] == [
        ("检查空调", 5),
        ("把窗帘弄好", 4),
        ("把投影幕布扔掉", 3),
        ("把钥匙放下", 2),
        ("再洗一遍衣", 10),
        ("晾衣服", 1),
        ("去我那里拿钥匙", 9),
        ("买二手床", 8),
        ("搬床垫", 7),
        ("去拿蟑螂胶饵", 6),
    ]
    scheduled = sorted(
        body["proposal"]["candidatePlan"]["items"],
        key=lambda item: item["segments"][0]["startSlot"],
    )
    assert [item["title"] for item in scheduled] == [
        "再洗一遍衣",
        "去我那里拿钥匙",
        "买二手床",
        "搬床垫",
        "去拿蟑螂胶饵",
        "检查空调",
        "把窗帘弄好",
        "把投影幕布扔掉",
        "把钥匙放下",
        "晾衣服",
    ]


def test_explicit_chinese_time_ranges_preserve_intervals_over_model_duration(
    settings: Settings,
) -> None:
    request_text = (
        "八点四十五到九点四五地铁\n"
        "九点四十五到十点背单词\n"
        "十点到十二点项目收尾\n"
        "十二点到十二点三十看书\n"
        "十二点三十到十四点吃饭休息\n"
        "下午两点到五点做短视频\n"
        "五点到七点，待定"
    )
    model_tasks = [
        ("地铁", 4, "08:45", "09:45", "八点四十五", "九点四五"),
        ("背单词", 2, "09:45", "10:00", "九点四十五", "十点"),
        ("项目收尾", 8, "10:00", "12:00", "十点", "十二点"),
        ("看书", 2, "12:00", "12:30", "十二点", "十二点三十"),
        ("吃饭休息", 6, "12:30", "14:00", "十二点三十", "十四点"),
        ("做短视频", 12, "14:00", "17:00", "下午两点", "五点"),
        ("待定", 8, "17:00", "19:00", "五点", "七点"),
    ]
    fake = FakeModelClient([
        operations_output([
            model_add(
                title, source_text, duration_slots=duration_slots,
                start_time=start_time, end_time=end_time,
                start_evidence=start_evidence, end_evidence=end_evidence,
                input_order=input_order,
            )
            for input_order, (
                (title, duration_slots, start_time, end_time, start_evidence, end_evidence),
                source_text,
            ) in enumerate(zip(model_tasks, request_text.splitlines(), strict=True))
        ])
    ])
    payload = request_payload(
        text=request_text,
        request_id="app-request-explicit-chinese-time-ranges",
        now="2026-09-01T20:53:52+08:00",
        date="2026-09-02",
        earliest_start_slot=0,
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    assert [
        (
            operation["title"],
            operation["durationSlots"],
            operation["placement"],
        )
        for operation in body["proposal"]["operations"]
    ] == [
        ("地铁", 4, {"anchor": "start", "slot": 35}),
        ("背单词", 1, {"anchor": "start", "slot": 39}),
        ("项目收尾", 8, {"anchor": "start", "slot": 40}),
        ("看书", 2, {"anchor": "start", "slot": 48}),
        ("吃饭休息", 6, {"anchor": "start", "slot": 50}),
        ("做短视频", 12, {"anchor": "start", "slot": 56}),
        ("待定", 8, {"anchor": "start", "slot": 68}),
    ]
    assert [
        (item["title"], item["segments"])
        for item in body["proposal"]["candidatePlan"]["items"]
    ] == [
        ("地铁", [{"startSlot": 35, "endSlot": 39}]),
        ("背单词", [{"startSlot": 39, "endSlot": 40}]),
        ("项目收尾", [{"startSlot": 40, "endSlot": 48}]),
        ("看书", [{"startSlot": 48, "endSlot": 50}]),
        ("吃饭休息", [{"startSlot": 50, "endSlot": 56}]),
        ("做短视频", [{"startSlot": 56, "endSlot": 68}]),
        ("待定", [{"startSlot": 68, "endSlot": 76}]),
    ]


@pytest.mark.parametrize(
    ("user_text", "source_texts"),
    (
        (
            "11:30~12:00 打王者，12:00~13:00吃午饭。"
            "下午 1 点到 2 点休息，2 点到 6 点逛街，6 点到 8 点 KTV。",
            [
                "11:30~12:00 打王者", "12:00~13:00吃午饭", "下午 1 点到 2 点休息",
                "2 点到 6 点逛街", "6 点到 8 点 KTV",
            ],
        ),
        (
            "11:30～12:00 打王者\n12:00至13:00吃午饭\n"
            "下午 1 点—2 点休息\n2 点–6 点逛街\n6 点-8 点 KTV",
            [
                "11:30～12:00 打王者", "12:00至13:00吃午饭", "下午 1 点—2 点休息",
                "2 点–6 点逛街", "6 点-8 点 KTV",
            ],
        ),
        (
            r"11:30\~12:00 打王者，12:00\~13:00吃午饭。"
            "下午 1 点到 2 点休息，2 点到 6 点逛街，6 点到 8 点 KTV。",
            [
                r"11:30\~12:00 打王者", r"12:00\~13:00吃午饭", "下午 1 点到 2 点休息",
                "2 点到 6 点逛街", "6 点到 8 点 KTV",
            ],
        ),
    ),
)
def test_inline_explicit_time_ranges_preserve_each_requested_interval(
    settings: Settings,
    user_text: str,
    source_texts: list[str],
) -> None:
    request_text = f"从 11:15 开始\n{user_text}"
    model_tasks = [
        ("打王者", "11:30", "12:00", "11:30", "12:00"),
        ("吃午饭", "12:00", "13:00", "12:00", "13:00"),
        ("休息", "13:00", "14:00", "下午 1 点", "2 点"),
        ("逛街", "14:00", "18:00", "2 点", "6 点"),
        ("KTV", "18:00", "20:00", "6 点", "8 点"),
    ]
    fake = FakeModelClient([
        operations_output([
            model_add(
                title, source_text, start_time=start_time, end_time=end_time,
                start_evidence=start_evidence, end_evidence=end_evidence,
                input_order=input_order,
            )
            for input_order, (
                (title, start_time, end_time, start_evidence, end_evidence), source_text,
            ) in enumerate(zip(model_tasks, source_texts, strict=True))
        ])
    ])
    payload = request_payload(
        text=request_text,
        request_id="app-request-inline-explicit-time-ranges",
        now="2026-09-07T11:10:00+08:00",
        date="2026-09-07",
        earliest_start_slot=45,
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    assert [
        (
            operation["title"],
            operation["durationSlots"],
            operation["placement"],
        )
        for operation in body["proposal"]["operations"]
    ] == [
        ("打王者", 2, {"anchor": "start", "slot": 46}),
        ("吃午饭", 4, {"anchor": "start", "slot": 48}),
        ("休息", 4, {"anchor": "start", "slot": 52}),
        ("逛街", 16, {"anchor": "start", "slot": 56}),
        ("KTV", 8, {"anchor": "start", "slot": 72}),
    ]
    assert [
        (item["title"], item["segments"])
        for item in body["proposal"]["candidatePlan"]["items"]
    ] == [
        ("打王者", [{"startSlot": 46, "endSlot": 48}]),
        ("吃午饭", [{"startSlot": 48, "endSlot": 52}]),
        ("休息", [{"startSlot": 52, "endSlot": 56}]),
        ("逛街", [{"startSlot": 56, "endSlot": 72}]),
        ("KTV", [{"startSlot": 72, "endSlot": 80}]),
    ]


def test_inline_explicit_time_range_rejects_negated_add_and_corrects_once(
    settings: Settings,
) -> None:
    fake = FakeModelClient([
        operations_output([
            model_add(
                "打王者", "不要在 11:30~12:00 打王者", duration_slots=2,
                start_time="11:30", end_time="12:00",
                start_evidence="11:30", end_evidence="12:00",
            )
        ]),
        operations_output([]),
    ])
    payload = request_payload(
        text="从 11:15 开始\n不要在 11:30~12:00 打王者。",
        request_id="app-request-negated-inline-explicit-time-range",
        now="2026-09-07T11:10:00+08:00",
        date="2026-09-07",
        earliest_start_slot=45,
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {"valid": True, "attempts": 2, "issues": []}
    assert body["proposal"]["operations"] == []
    assert body["proposal"]["candidatePlan"]["items"] == []
    assert len(fake.calls) == 2
    assert [pipeline.thinking_mode for pipeline, _ in fake.calls] == ["disabled", "enabled"]
    correction = json.loads(fake.calls[1][1])
    assert correction["issues"]
    assert all(issue["code"] != "UNPLACED" for issue in correction["issues"])


def test_continuous_chinese_timepoints_round_and_return_only_intended_intervals(
    settings: Settings,
) -> None:
    request_text = (
        "早上8:20起床，8:50出门。9:10坐地铁，9:40出地铁。"
        "12点睡觉。1点吃饭，1:30上班，6:30吃饭。"
        "7:30下班，8:30 到家"
    )
    model_tasks = [
        ("起床", "早上8:20起床", "08:20", "08:50", "早上8:20起床", "8:50出门"),
        ("出门", "8:50出门", "08:50", "09:10", "8:50出门", "9:10坐地铁"),
        ("坐地铁", "9:10坐地铁，9:40出地铁", "09:10", "09:40", "9:10坐地铁", "9:40出地铁"),
        ("睡觉", "12点睡觉", "12:00", "13:00", "12点睡觉", "1点吃饭"),
        ("吃饭", "1点吃饭", "13:00", None, "1点吃饭", None),
        ("上班", "1:30上班", "13:30", "18:30", "1:30上班", "6:30吃饭"),
        ("吃饭", "6:30吃饭", "18:30", None, "6:30吃饭", None),
        ("下班回家", "7:30下班，8:30 到家", "19:30", "20:30", "7:30下班", "8:30 到家"),
    ]
    fake = FakeModelClient([
        operations_output([
            model_add(
                title, source_text, start_time=start_time, end_time=end_time,
                start_evidence=start_evidence, end_evidence=end_evidence,
                input_order=input_order,
            )
            for input_order, (
                title, source_text, start_time, end_time, start_evidence, end_evidence,
            ) in enumerate(model_tasks)
        ])
    ])
    payload = request_payload(
        text=request_text,
        request_id="app-request-continuous-timepoints",
        now="2026-09-04T21:00:00+08:00",
        date="2026-09-05",
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {
        "valid": True,
        "attempts": 1,
        "issues": [],
    }
    assert len(fake.calls) == 1
    assert "earliestStartSlot" not in json.loads(fake.calls[0][1])
    assert [
        (
            operation["title"],
            operation["durationSlots"],
            operation["placement"],
        )
        for operation in body["proposal"]["operations"]
    ] == [
        ("起床", 2, {"anchor": "start", "slot": 33}),
        ("出门", 2, {"anchor": "start", "slot": 35}),
        ("坐地铁", 2, {"anchor": "start", "slot": 37}),
        ("睡觉", 4, {"anchor": "start", "slot": 48}),
        ("吃饭", 2, {"anchor": "start", "slot": 52}),
        ("上班", 20, {"anchor": "start", "slot": 54}),
        ("吃饭", 2, {"anchor": "start", "slot": 74}),
        ("下班回家", 4, {"anchor": "start", "slot": 78}),
    ]
    assert [
        (item["title"], item["segments"])
        for item in body["proposal"]["candidatePlan"]["items"]
    ] == [
        ("起床", [{"startSlot": 33, "endSlot": 35}]),
        ("出门", [{"startSlot": 35, "endSlot": 37}]),
        ("坐地铁", [{"startSlot": 37, "endSlot": 39}]),
        ("睡觉", [{"startSlot": 48, "endSlot": 52}]),
        ("吃饭", [{"startSlot": 52, "endSlot": 54}]),
        ("上班", [{"startSlot": 54, "endSlot": 74}]),
        ("吃饭", [{"startSlot": 74, "endSlot": 76}]),
        ("下班回家", [{"startSlot": 78, "endSlot": 82}]),
    ]


def test_plan_parse_rejects_past_date_before_calling_model(settings: Settings) -> None:
    fake = FakeModelClient([operations_output([])])
    payload = request_payload(
        request_id="app-request-past",
        date="2026-08-23",
        earliest_start_slot=36,
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PLANNING_DATE_NOT_ALLOWED"
    assert fake.calls == []


def test_plan_parse_allows_future_date_without_app_earliest_slot(
    settings: Settings,
) -> None:
    fake = FakeModelClient([
        operations_output([model_add("明天任务", "明天任务")])
    ])
    payload = request_payload(
        text="安排明天任务",
        request_id="app-request-future-missing-start",
        date="2026-08-25",
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    assert response.json()["validation"] == {
        "valid": True,
        "attempts": 1,
        "issues": [],
    }
    assert response.json()["proposal"]["candidatePlan"]["items"][0]["segments"] == [
        {"startSlot": 0, "endSlot": 2}
    ]
    assert len(fake.calls) == 1
    assert "earliestStartSlot" not in json.loads(fake.calls[0][1])


def test_plan_parse_change_title_returns_title_only_candidate_without_private_fields(
    settings: Settings,
) -> None:
    authorization_text = "把写方案改标题为最终方案"
    fake = FakeModelClient(
        [
            operations_output(
                [
                    {
                        "type": "changeTitle",
                        "targetItemId": "occurrence-1",
                        "objectType": "internalTask",
                        "title": "最终方案",
                        "allowedChanges": ["title"],
                        "authorizationText": authorization_text,
                        "inputOrder": 0,
                    }
                ]
            )
        ]
    )
    payload = request_payload(text=authorization_text, items=[internal_item()])

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert body["proposal"]["operations"] == [
        {
            "type": "changeTitle",
            "targetItemId": "occurrence-1",
            "objectType": "internalTask",
            "title": "最终方案",
            "allowedChanges": ["title"],
            "inputOrder": 0,
        }
    ]
    candidate = body["proposal"]["candidatePlan"]["items"][0]
    assert candidate["title"] == "最终方案"
    assert candidate["durationSlots"] == 4
    assert candidate["segments"] == [{"startSlot": 36, "endSlot": 40}]
    assert "authorizationText" not in recursive_keys(body)
    assert "isExplicit" not in recursive_keys(body)
    assert "status" not in recursive_keys(body)
    assert len(fake.calls) == 1


def test_plan_parse_buyaole_authorizes_pinned_delete_without_private_fields(
    settings: Settings,
) -> None:
    authorization_text = "钉住任务不要了"
    model_response = operations_output(
        [
            {
                "type": "delete",
                "targetItemId": "occurrence-1",
                "objectType": "internalTask",
                "authorizationText": authorization_text,
                "inputOrder": 0,
            }
        ]
    )
    fake = FakeModelClient([model_response, model_response])

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(text=authorization_text, items=[internal_item(pinned=True)]),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert body["proposal"]["operations"] == [
        {
            "type": "delete",
            "targetItemId": "occurrence-1",
            "objectType": "internalTask",
            "priority": None,
            "allowedChanges": [],
            "inputOrder": 0,
        }
    ]
    assert body["proposal"]["candidatePlan"]["items"] == []
    assert body["proposal"]["deletedOccurrenceIDs"] == ["occurrence-1"]
    assert body["proposal"]["deletedExternalEventIDs"] == []
    assert "authorizationText" not in recursive_keys(body)
    assert "isExplicit" not in recursive_keys(body)
    assert "status" not in recursive_keys(body)
    assert len(fake.calls) == 1


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
    assert [pipeline.thinking_mode for pipeline, _ in fake.calls] == ["disabled", "enabled"]
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


def test_past_and_capacity_unplaced_adds_remain_in_first_candidate_without_correction(
    settings: Settings,
) -> None:
    operations = [
        model_add(
            f"任务 {index + 1}",
            "12:00 安排任务 1" if index == 0 else f"任务 {index + 1}",
            duration_slots=4,
            start_time="12:00" if index == 0 else None,
            start_evidence="12:00 安排任务 1" if index == 0 else None,
            input_order=index,
        )
        for index in range(14)
    ]
    fake = FakeModelClient([operations_output(operations)])
    payload = request_payload(
        text="12:00 安排任务 1，并安排" + "、".join(f"任务 {index}" for index in range(2, 15)),
        request_id="app-request-normal-unplaced",
        now="2026-08-24T13:32:13+08:00",
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["validation"]["valid"] is True
    assert body["validation"]["attempts"] == 1
    assert len(body["proposal"]["operations"]) == 14
    assert len(body["proposal"]["candidatePlan"]["items"]) == 14
    assert sum(not item["segments"] for item in body["proposal"]["candidatePlan"]["items"]) == 4
    assert {
        (issue["code"], issue["severity"])
        for issue in body["validation"]["issues"]
    } == {("INVALID_TIME", "warning"), ("UNPLACED", "warning")}
    assert len(fake.calls) == 1


def test_semantic_correction_excludes_normal_unplaced_warnings(
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
                        "inputOrder": 0,
                    },
                    model_add(
                        "已错过时间的任务", "08:00 安排已错过时间的任务",
                        duration_slots=2, start_time="08:00",
                        start_evidence="08:00 安排已错过时间的任务", input_order=1,
                    ),
                ]
            ),
            operations_output([
                model_add(
                    "已错过时间的任务", "08:00 安排已错过时间的任务",
                    duration_slots=2, start_time="08:00",
                    start_evidence="08:00 安排已错过时间的任务", input_order=1,
                ),
            ]),
        ]
    )

    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(
                text="08:00 安排已错过时间的任务，另外移动不存在任务",
                items=[internal_item()],
            ),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["validation"]["valid"] is True
    assert body["validation"]["attempts"] == 2
    assert {(issue["code"], issue["severity"]) for issue in body["validation"]["issues"]} == {
        ("INVALID_TIME", "warning"), ("UNPLACED", "warning"),
    }
    assert body["proposal"]["candidatePlan"]["items"][-1]["title"] == "已错过时间的任务"
    assert body["proposal"]["candidatePlan"]["items"][-1]["segments"] == []
    correction = json.loads(fake.calls[1][1])
    assert correction["issues"] == [
        {
            "code": "UNKNOWN_TARGET",
            "message": "操作引用的 itemId 不存在于 currentPlan",
            "itemId": "missing-item",
            "field": "targetItemId",
        }
    ]
    assert len(fake.calls) == 2


def test_observability_aggregates_two_model_calls_for_guest_device(settings: Settings) -> None:
    device_id = "time-fragment-observed-device-1234"
    unknown_move = {
        "type": "move",
        "targetItemId": "missing-item",
        "allowedChanges": ["segments"],
        "inputOrder": 0,
    }
    fake = FakeModelClient(
        [
            operations_output(
                [unknown_move],
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            ),
            operations_output(
                [],
                usage={"prompt_tokens": 140, "completion_tokens": 10, "total_tokens": 150},
            ),
        ]
    )
    payload = request_payload(items=[internal_item()])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client, device_id),
            json=payload,
        )
        records = client.get(
            "/admin/observability/requests",
            headers=admin_headers(),
            params={"device_id": device_id},
        )
        summary = client.get(
            "/admin/observability/summary",
            headers=admin_headers(),
            params={"device_id": device_id},
        )

    assert response.status_code == 200
    assert records.status_code == 200
    record = records.json()["records"][0]
    assert record["request_content"] == payload
    assert record["response_content"] == response.json()
    assert record["model_call_count"] == 2
    assert record["usage"] == {
        "prompt_tokens": 240,
        "completion_tokens": 30,
        "total_tokens": 270,
    }
    assert [call["call_index"] for call in record["model_calls"]] == [1, 2]
    assert summary.json()["totals"]["request_count"] == 1
    assert summary.json()["totals"]["total_tokens"] == 270


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
                    model_add("第二次候选", "第二次候选", duration_slots=2),
                    unknown_move,
                ]
            ),
        ]
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(text="安排第二次候选，另外移动不存在任务", items=[internal_item()]),
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
    assert [pipeline.thinking_mode for pipeline, _ in fake.calls] == ["disabled", "enabled"]


def test_structural_correction_prompt_identifies_the_exact_invalid_field(
    settings: Settings,
) -> None:
    fake = FakeModelClient(
        [
            operations_output([
                {
                    "type": "add",
                    "title": "缺少顺序",
                    "sourceText": "缺少顺序",
                    "timeConstraint": None,
                }
            ]),
            operations_output([]),
        ]
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client),
            json=request_payload(text="安排缺少顺序"),
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


def test_plan_parse_enforces_installation_quota_and_admin_can_reset_it(
    settings: Settings,
) -> None:
    limited_settings = settings.model_copy(update={"time_fragment_guest_quota_limit": 2})
    fake = FakeModelClient([operations_output([]) for _ in range(3)])
    with TestClient(create_app(limited_settings, fake)) as client:
        headers = guest_headers(client, "time-fragment-ios-quota-device")
        first = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="quota-request-1"),
        )
        second = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="quota-request-2"),
        )
        exhausted = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="quota-request-3"),
        )
        detail = exhausted.json()["detail"]
        quota = client.get(
            f"/admin/time-fragment/quotas/{detail['supportCode']}",
            headers=admin_headers(),
        )
        reset = client.post(
            f"/admin/time-fragment/quotas/{detail['supportCode']}/reset",
            headers=admin_headers(),
        )
        after_reset = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="quota-request-4"),
        )

    assert first.status_code == second.status_code == 200
    assert exhausted.status_code == 429
    assert detail == {
        "code": "AI_QUOTA_EXHAUSTED",
        "message": "本轮内测的 AI 额度已用完，请将支持码发给开发者刷新。",
        "limit": 2,
        "remaining": 0,
        "supportCode": detail["supportCode"],
    }
    assert detail["supportCode"].startswith("TF-")
    assert "quota-device" not in detail["supportCode"]
    assert quota.json() == {
        "supportCode": detail["supportCode"],
        "limit": 2,
        "used": 2,
        "remaining": 0,
    }
    assert reset.json()["remaining"] == 2
    assert after_reset.status_code == 200
    assert len(fake.calls) == 3


def test_plan_parse_refunds_gateway_and_unusable_model_failures(settings: Settings) -> None:
    limited_settings = settings.model_copy(update={"time_fragment_guest_quota_limit": 1})
    fake = FakeModelClient(
        [
            ModelGatewayUnavailable("temporarily unavailable"),
            raw_output("not-json"),
            raw_output("still-not-json"),
            operations_output([]),
        ]
    )
    with TestClient(create_app(limited_settings, fake)) as client:
        headers = guest_headers(client, "time-fragment-ios-refund-device")
        gateway_failure = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="refund-request-1"),
        )
        unusable_response = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="refund-request-2"),
        )
        success = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="refund-request-3"),
        )

    assert gateway_failure.status_code == 503
    assert unusable_response.status_code == 200
    assert unusable_response.json()["proposal"] is None
    assert success.status_code == 200
    assert len(fake.calls) == 4


def test_duplicate_consumed_request_id_is_rejected_without_another_model_call(
    settings: Settings,
) -> None:
    limited_settings = settings.model_copy(update={"time_fragment_guest_quota_limit": 1})
    fake = FakeModelClient([operations_output([])])
    with TestClient(create_app(limited_settings, fake)) as client:
        headers = guest_headers(client, "time-fragment-ios-duplicate-device")
        first = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="same-request"),
        )
        duplicate = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="same-request"),
        )
        new_request = client.post(
            "/api/plan/parse",
            headers=headers,
            json=request_payload(request_id="new-request"),
        )

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "AI_REQUEST_ALREADY_COMPLETED"
    assert new_request.status_code == 429
    assert len(fake.calls) == 1


def test_quota_admin_reset_all_requires_admin_key(settings: Settings) -> None:
    limited_settings = settings.model_copy(update={"time_fragment_guest_quota_limit": 1})
    fake = FakeModelClient([operations_output([]), operations_output([])])
    with TestClient(create_app(limited_settings, fake)) as client:
        for device_id in ("time-fragment-ios-reset-device-a", "time-fragment-ios-reset-device-b"):
            response = client.post(
                "/api/plan/parse",
                headers=guest_headers(client, device_id),
                json=request_payload(request_id=f"request-{device_id[-1]}"),
            )
            assert response.status_code == 200

        unauthorized = client.post("/admin/time-fragment/quotas/reset-all")
        reset_all = client.post(
            "/admin/time-fragment/quotas/reset-all",
            headers=admin_headers(),
        )

    assert unauthorized.status_code == 401
    assert reset_all.json() == {"refreshedInstallations": 2}


def test_quota_and_observability_share_the_persisted_sqlite_volume(
    settings: Settings,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "usage.sqlite3"
    persisted_settings = settings.model_copy(update={"usage_db_path": str(database_path)})
    fake = FakeModelClient([operations_output([])])
    with TestClient(create_app(persisted_settings, fake)) as client:
        response = client.post(
            "/api/plan/parse",
            headers=guest_headers(client, "time-fragment-ios-persisted-device"),
            json=request_payload(request_id="persisted-request"),
        )

    with sqlite3.connect(database_path) as connection:
        inference_count = connection.execute(
            "SELECT COUNT(*) FROM inference_requests"
        ).fetchone()[0]
        quota_count = connection.execute(
            "SELECT COUNT(*) FROM quota_requests WHERE state = 'consumed'"
        ).fetchone()[0]

    assert response.status_code == 200
    assert inference_count == 1
    assert quota_count == 1


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
