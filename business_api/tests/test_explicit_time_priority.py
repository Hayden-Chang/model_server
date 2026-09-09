"""Explicit user clocks override the current proposal's default planning floor."""

import json

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from test_temporal_relations import relation
from test_time_fragment_api import (
    FakeModelClient, guest_headers, internal_item, model_add, raw_output, request_payload, settings,
)


def run_plan(settings, text, operations, *, earliest=36, today=False, items=None, relations=None):
    output = raw_output(json.dumps({"operations": operations, "temporalRelations": relations or []}, ensure_ascii=False))
    fake = FakeModelClient([output, output])
    request = request_payload(
        text=text, date="2026-09-17", now="2026-09-17T13:32:13+08:00" if today else "2026-09-09T00:32:00+08:00",
        earliest_start_slot=earliest, items=items,
    )
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=request)
    assert response.status_code == 200
    return response.json(), fake


def segments(body):
    return {item["title"]: item["segments"] for item in body["proposal"]["candidatePlan"]["items"]}


@pytest.mark.parametrize("today,earliest", [(False, 36), (True, 56), (True, None)])
def test_explicit_earlier_clock_makes_related_proposal_applicable(settings, today, earliest):
    text = "08:30 准备，09:00 出发。"
    operations = [
        model_add("准备", "08:30 准备", start_time="08:30", start_evidence="08:30 准备", input_order=0),
        model_add("出发", "09:00 出发", start_time="09:00", start_evidence="09:00 出发", input_order=1),
    ]
    body, fake = run_plan(settings, text, operations, earliest=earliest, today=today,
                          relations=[relation(0, 1, "until", text)])
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert segments(body) == {"准备": [{"startSlot": 34, "endSlot": 36}], "出发": [{"startSlot": 36, "endSlot": 38}]}
    assert len(fake.calls) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_untimed_tasks_share_lowered_floor_regardless_of_operation_order(settings, reverse):
    operations = [
        model_add("准备", "08:00 准备", start_time="08:00", start_evidence="08:00 准备", input_order=0),
        model_add("阅读", "另外阅读", input_order=1),
    ]
    body, fake = run_plan(settings, "从 09:00 开始\n08:00 准备。另外阅读。", operations[::-1] if reverse else operations)
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert segments(body) == {"准备": [{"startSlot": 32, "endSlot": 34}], "阅读": [{"startSlot": 34, "endSlot": 36}]}
    # This proposal overrides the floor without rewriting the saved/request default.
    assert json.loads(fake.calls[0][1])["earliestStartSlot"] == 36


def test_end_only_clock_lowers_floor_by_task_duration(settings):
    body, _ = run_plan(settings, "08:30 结束准备，持续一小时。另加阅读。", [
        model_add("准备", "08:30 结束准备，持续一小时", end_time="08:30", end_evidence="08:30 结束准备", duration_slots=4),
        model_add("阅读", "另加阅读", input_order=1),
    ])
    assert body["validation"]["valid"] is True
    assert segments(body) == {"准备": [{"startSlot": 30, "endSlot": 34}], "阅读": [{"startSlot": 34, "endSlot": 36}]}


@pytest.mark.parametrize("today,earliest,start", [(False, 36, 36), (True, 56, 56), (True, None, 55)])
def test_untimed_only_request_keeps_default_floor(settings, today, earliest, start):
    body, _ = run_plan(settings, "安排阅读", [model_add("阅读", "安排阅读")], today=today, earliest=earliest)
    assert body["validation"]["valid"] is True
    assert segments(body)["阅读"] == [{"startSlot": start, "endSlot": start + 2}]


@pytest.mark.parametrize("text,evidence", [("08:30 准备。另外阅读。", "08:30 准备"), ("不要08:00准备。另外阅读。", "08:00")])
def test_invalid_or_negated_clock_cannot_lower_other_tasks_floor(settings, text, evidence):
    body, _ = run_plan(settings, text, [
        model_add("准备", text.split("。")[0], start_time="08:00", start_evidence=evidence),
        model_add("阅读", "另外阅读", input_order=1),
    ])
    assert body["validation"]["valid"] is False
    assert segments(body)["准备"] == []
    assert segments(body)["阅读"] == [{"startSlot": 36, "endSlot": 38}]


def test_earlier_clock_does_not_overwrite_pinned_task(settings):
    pinned = {**internal_item(pinned=True), "segments": [{"startSlot": 32, "endSlot": 36}]}
    body, _ = run_plan(settings, "08:00 准备。", [model_add("准备", "08:00 准备", start_time="08:00", start_evidence="08:00 准备")], items=[pinned])
    assert body["proposal"]["candidatePlan"]["items"][0] == pinned
    assert segments(body)["准备"] == []
    assert {i["code"] for i in body["validation"]["issues"]} == {"UNPLACED"}


def test_earlier_clock_uses_same_floor_in_allocator_fallback(settings, monkeypatch):
    monkeypatch.setattr("app.time_fragment.solve_slot_allocations", lambda *args: None)
    body, _ = run_plan(settings, "08:00 准备。另外阅读。", [
        model_add("准备", "08:00 准备", start_time="08:00", start_evidence="08:00 准备"),
        model_add("阅读", "另外阅读", input_order=1),
    ])
    assert body["validation"]["valid"] is True
    assert segments(body) == {"准备": [{"startSlot": 32, "endSlot": 34}], "阅读": [{"startSlot": 34, "endSlot": 36}]}


def test_explicit_move_to_past_lowers_floor_for_new_untimed_task(settings):
    item = internal_item()
    quote = "写方案移到08:00"
    body, _ = run_plan(settings, quote + "。另外阅读。", [
        {"type": "move", "targetItemId": item["itemId"], "allowedChanges": ["segments"],
         "placement": {"anchor": "start", "slot": 32}, "authorizationText": quote, "inputOrder": 0},
        model_add("阅读", "另外阅读", input_order=1),
    ], today=True, earliest=56, items=[item])
    assert body["validation"]["valid"] is True
    assert segments(body) == {"写方案": [{"startSlot": 32, "endSlot": 36}], "阅读": [{"startSlot": 36, "endSlot": 38}]}


@pytest.mark.parametrize("resize", [False, True])
def test_existing_early_task_and_duration_only_edit_do_not_lower_floor(settings, resize):
    item = {**internal_item(), "segments": [{"startSlot": 28, "endSlot": 32}]}
    operations = [model_add("阅读", "另外阅读")]
    text = "另外阅读。"
    if resize:
        text += "写方案改为半小时。"
        operations.append({"type": "changeDuration", "targetItemId": item["itemId"],
                           "durationSlots": 2, "allowedChanges": ["durationSlots", "segments"], "inputOrder": 1})
    body, _ = run_plan(settings, text, operations, items=[item])
    assert body["validation"]["valid"] is True
    assert segments(body)["阅读"] == [{"startSlot": 36, "endSlot": 38}]
    assert segments(body)["写方案"] == [{"startSlot": 28, "endSlot": 30 if resize else 32}]


def test_relative_insertion_without_clock_does_not_lower_default(settings):
    item = {**internal_item(), "segments": [{"startSlot": 28, "endSlot": 32}]}
    body, _ = run_plan(settings, "写方案后阅读。另外散步。", [
        model_add("阅读", "写方案后阅读"), model_add("散步", "另外散步", input_order=1),
    ], items=[item])
    assert segments(body)["散步"] == [{"startSlot": 36, "endSlot": 38}]


def test_midnight_clock_does_not_reopen_today_or_change_todo_policy(settings):
    body, _ = run_plan(settings, "24:00 休息。另外阅读。", [
        model_add("休息", "24:00 休息", start_time="24:00", start_evidence="24:00 休息"),
        model_add("阅读", "另外阅读", input_order=1),
    ], today=True, earliest=56)
    assert body["validation"]["valid"] is True
    assert segments(body) == {"休息": [], "阅读": [{"startSlot": 56, "endSlot": 58}]}


def test_invalid_end_before_day_cannot_lower_floor(settings):
    body, _ = run_plan(settings, "00:15 结束准备，持续一小时。另加阅读。", [
        model_add("准备", "00:15 结束准备，持续一小时", end_time="00:15", end_evidence="00:15 结束准备", duration_slots=4),
        model_add("阅读", "另加阅读", input_order=1),
    ])
    assert body["validation"]["valid"] is False
    assert segments(body) == {"准备": [], "阅读": [{"startSlot": 36, "endSlot": 38}]}


@pytest.mark.parametrize("text", ["重新安排写方案。另外阅读。", "不要08:00写方案。另外阅读。"])
def test_model_invented_or_negated_move_clock_cannot_lower_floor(settings, text):
    item = internal_item()
    body, _ = run_plan(settings, text, [
        {"type": "move", "targetItemId": item["itemId"], "allowedChanges": ["segments"],
         "placement": {"anchor": "start", "slot": 32}, "inputOrder": 0},
        model_add("阅读", "另外阅读", input_order=1),
    ], items=[item])
    assert body["validation"]["valid"] is False
    assert segments(body)["阅读"] == [{"startSlot": 36, "endSlot": 38}]
