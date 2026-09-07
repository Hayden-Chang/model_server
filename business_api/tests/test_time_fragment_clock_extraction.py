"""The model wire contract must not silently turn explicit clocks into free tasks."""

import json

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from test_time_fragment_api import (
    FakeModelClient,
    guest_headers,
    internal_item,
    model_add,
    operations_output,
    request_payload,
    recursive_keys,
    settings,
)


@pytest.mark.parametrize("evidence", [None, "8:50 起床"])
def test_wrong_model_clock_never_becomes_a_valid_free_schedule(settings, evidence):
    # Real production failure: slot 52 is 13:00, not the quoted 08:50.
    operation = {
        "type": "add", "title": "起床", "inputOrder": 0,
        "placement": {"anchor": "start", "slot": 52},
    }
    if evidence is not None:
        operation["authorizationText"] = evidence
    output = operations_output([operation])
    fake = FakeModelClient([output, output])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse", headers=guest_headers(client),
            json=request_payload(text="8:50 起床，9 点出门。", date="2026-09-11"),
        )
    body = response.json()
    assert response.status_code == 200
    assert body["validation"]["valid"] is False
    assert [pipeline.thinking_mode for pipeline, _ in fake.calls] == ["disabled", "enabled"]


def run_clock_request(settings, text, operations, *, corrected=None, items=None, earliest=None):
    fake = FakeModelClient([
        operations_output(operations),
        operations_output(operations if corrected is None else corrected),
    ])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post(
            "/api/plan/parse", headers=guest_headers(client),
            json=request_payload(text=text, date="2026-09-11", now="2026-09-07T22:08:53+08:00",
                                 items=items, earliest_start_slot=earliest),
        )
    assert response.status_code == 200
    return response.json(), fake


@pytest.mark.parametrize("missing_field", ["sourceText", "timeConstraint", "startEvidence"])
def test_new_wire_contract_requires_explicit_source_and_clock_fields(settings, missing_field):
    operation = model_add("起床", "8:50 起床", start_time="08:50", start_evidence="8:50 起床")
    if missing_field == "startEvidence":
        del operation["timeConstraint"][missing_field]
    else:
        del operation[missing_field]
    body, fake = run_clock_request(settings, "8:50 起床", [operation])
    assert body["proposal"] is None
    assert body["validation"]["valid"] is False
    assert body["validation"]["attempts"] == 2
    assert missing_field in fake.calls[1][1]


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("8:50 起床", model_add("起床", "8:50 起床", start_time="13:00", start_evidence="8:50 起床")),
        ("8:50 起床", model_add("起床", "8:50 起床")),
        ("10:00 安排写方案", model_add("写方案", "10:00 安排写方案", start_time="11:00", start_evidence="10:00 安排写方案")),
        ("早上8:50 起床", model_add("起床", "早上8:50 起床", start_time="20:50", start_evidence="8:50 起床")),
        ("下午1点上班", model_add("上班", "下午1点上班", start_time="01:00", start_evidence="1点上班")),
        ("9:45 写方案", model_add("写方案", "9:45 写方案", start_time="09:04", start_evidence="9:4")),
        ("从16:00开始\n跑步", model_add("跑步", "从16:00开始\n跑步", start_time="16:00", start_evidence="16:00")),
        ("8:50 起床，9 点出门", model_add("起床", "8:50 起床", start_time="09:00", start_evidence="9 点出门")),
        ("10:00~11:00 写方案", model_add("写方案", "10:00~11:00 写方案", start_time="13:00", end_time="14:00", start_evidence="10:00", end_evidence="11:00")),
    ],
)
def test_semantic_clock_errors_retain_unscheduled_candidate_and_correct_only_once(settings, text, operation):
    body, fake = run_clock_request(settings, text, [operation])
    assert body["validation"]["valid"] is False
    assert body["validation"]["attempts"] == 2
    assert len(body["proposal"]["operations"]) == 1
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == []
    assert any(issue["field"] == "timeConstraint" for issue in body["validation"]["issues"])
    assert [pipeline.thinking_mode for pipeline, _ in fake.calls] == ["disabled", "enabled"]
    candidate_json = json.dumps(json.loads(fake.calls[1][1])["firstCandidate"])
    for private_field in ("sourceText", "timeConstraint", "startEvidence", "authorizationText", "domainRef"):
        assert private_field not in candidate_json
        if private_field != "domainRef":  # Public candidate items already have a nullable domainRef.
            assert private_field not in recursive_keys(body)


def test_wrong_clock_can_be_corrected_without_losing_requested_task(settings):
    invalid = model_add("起床", "8:50 起床", start_time="13:00", start_evidence="8:50 起床")
    corrected = model_add("起床", "8:50 起床", start_time="08:50", end_time="09:00", start_evidence="8:50 起床", end_evidence="9 点出门")
    leave = model_add("出门", "9 点出门", start_time="09:00", start_evidence="9 点出门")
    body, fake = run_clock_request(settings, "8:50 起床，9 点出门", [invalid, leave], corrected=[corrected, leave])
    assert body["validation"] == {"valid": True, "attempts": 2, "issues": []}
    assert [item["segments"] for item in body["proposal"]["candidatePlan"]["items"]] == [
        [{"startSlot": 35, "endSlot": 36}], [{"startSlot": 36, "endSlot": 38}],
    ]
    assert "不一致" in fake.calls[1][1]


@pytest.mark.parametrize("separator", ["", "，", "。", "\n"])
def test_combined_commute_title_and_endpoint_survive_every_separator(settings, separator):
    source = f"七点半下班{separator}8 点到家"
    operation = model_add("下班回家", source, start_time="19:30", end_time="20:00", start_evidence="七点半下班", end_evidence="8 点到家")
    body, fake = run_clock_request(settings, source, [operation])
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    item = body["proposal"]["candidatePlan"]["items"][0]
    assert (item["title"], item["durationSlots"], item["segments"]) == (
        "下班回家", 2, [{"startSlot": 78, "endSlot": 80}],
    )


def test_omitted_explicit_clock_cannot_hide_behind_a_shorter_source_quote(settings):
    operation = model_add("起床", "起床")
    body, _ = run_clock_request(settings, "8:50 起床", [operation])
    assert body["validation"]["valid"] is False
    assert any("未被时间依据覆盖" in issue["message"] for issue in body["validation"]["issues"])


def test_empty_model_output_cannot_drop_a_clocked_empty_day_request(settings):
    body, _ = run_clock_request(settings, "8:50 起床", [])
    assert body["validation"]["valid"] is False


def test_previous_end_evidence_cannot_hide_next_task_missing_start(settings):
    operations = [
        model_add("跑步", "9点跑步", start_time="09:00", end_time="10:00",
                  start_evidence="9点跑步", end_evidence="10点看书"),
        model_add("看书", "看书", input_order=1),
    ]
    body, _ = run_clock_request(settings, "9点跑步，10点看书", operations)
    assert body["validation"]["valid"] is False
    assert body["proposal"]["candidatePlan"]["items"][1]["segments"] == []


@pytest.mark.parametrize("include_add", [True, False])
def test_mixed_existing_move_does_not_disable_new_task_clock_coverage(settings, include_add):
    meeting = {**internal_item(), "title": "会议", "segments": [{"startSlot": 56, "endSlot": 60}]}
    operations = [{"type": "move", "targetItemId": meeting["itemId"], "inputOrder": 0,
                   "allowedChanges": ["segments"], "placement": {"anchor": "start", "slot": 60}}]
    if include_add:
        operations.append(model_add("起床", "起床", input_order=1))
    body, _ = run_clock_request(settings, "把会议移到15:00，8:50起床", operations, items=[meeting])
    assert body["validation"]["valid"] is False
    if include_add:
        assert body["proposal"]["candidatePlan"]["items"][-1]["segments"] == []


def test_short_repeated_clock_quote_cannot_cover_another_omitted_task(settings):
    operation = model_add("跑步", "9点跑步", start_time="09:00", start_evidence="9点")
    body, _ = run_clock_request(settings, "9点跑步，9点看书", [operation])
    assert body["validation"]["valid"] is False
    assert any("未被时间依据覆盖" in issue["message"] for issue in body["validation"]["issues"])


def test_global_start_in_same_sentence_keeps_untimed_tasks_flexible(settings):
    operations = [model_add("跑步", "跑步"), model_add("阅读", "阅读", input_order=1)]
    body, fake = run_clock_request(settings, "从16:00开始安排跑步和阅读", operations, earliest=64)
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    assert [op["placement"] for op in body["proposal"]["operations"]] == [None, None]
    assert [item["segments"] for item in body["proposal"]["candidatePlan"]["items"]] == [
        [{"startSlot": 64, "endSlot": 66}], [{"startSlot": 66, "endSlot": 68}],
    ]


def test_existing_clock_reference_before_relative_add_is_not_a_new_task_clock(settings):
    meeting = {**internal_item(), "title": "会议"}
    body, _ = run_clock_request(settings, "9点会议之后跑步", [model_add("跑步", "跑步")], items=[meeting])
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == meeting["segments"]
    assert body["proposal"]["candidatePlan"]["items"][1]["segments"] == [{"startSlot": 40, "endSlot": 42}]


def test_global_start_does_not_mask_later_individual_clock_in_same_sentence(settings):
    operations = [
        model_add("跑步", "跑步"),
        model_add("看书", "18:00看书", input_order=1, start_time="18:00", start_evidence="18:00看书"),
    ]
    body, _ = run_clock_request(settings, "从16:00开始安排跑步和18:00看书", operations, earliest=64)
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert body["proposal"]["candidatePlan"]["items"][1]["segments"] == [{"startSlot": 72, "endSlot": 74}]


def test_partial_quote_uses_original_explicit_period_without_requiring_paraphrase(settings):
    operation = model_add("上班", "1点上班", start_time="13:00", start_evidence="1点上班")
    body, _ = run_clock_request(settings, "下午1点上班", [operation])
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [{"startSlot": 52, "endSlot": 54}]


@pytest.mark.parametrize("start,end,expected", [("23:30", "24:00", True), ("24:00", None, False)])
def test_midnight_remains_end_of_selected_day_not_noon_or_next_day(settings, start, end, expected):
    text = "23:30 看书，12点睡觉" if expected else "12点睡觉"
    operation = model_add(
        "看书" if expected else "睡觉", text, start_time=start, end_time=end,
        start_evidence="23:30 看书" if expected else "12点睡觉",
        end_evidence="12点睡觉" if expected else None,
    )
    body, fake = run_clock_request(settings, text, [operation])
    assert body["validation"]["valid"] is expected
    item = body["proposal"]["candidatePlan"]["items"][0]
    assert item["segments"] == ([{"startSlot": 94, "endSlot": 96}] if expected else [])
    assert len(fake.calls) == (1 if expected else 2)


def test_negative_other_clause_does_not_reject_affirmative_task_clock(settings):
    operation = model_add("洗澡", "9 点洗澡", start_time="21:00", start_evidence="9 点洗澡")
    body, _ = run_clock_request(settings, "不要在8点开会，9 点洗澡", [operation])
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}


def test_entire_reported_day_preserves_clock_anchors_and_flags_only_midnight_boundary(settings):
    text = "8:50 起床，9 点出门，9:30 到公司。12 点点外卖，1 点吃饭。1:30 上班。外卖。七点半下班8 点到家给吃晚饭。9 点洗澡，10 点看《老友记》。11 点玩手机，12 点睡觉。"
    rows = [
        ("起床", "8:50 起床", "08:50", None, "8:50 起床", None),
        ("出门", "9 点出门", "09:00", None, "9 点出门", None),
        ("到公司", "9:30 到公司", "09:30", None, "9:30 到公司", None),
        ("点外卖", "12 点点外卖", "12:00", None, "12 点点外卖", None),
        ("吃饭", "1 点吃饭", "13:00", None, "1 点吃饭", None),
        ("上班", "1:30 上班", "13:30", "19:30", "1:30 上班", "七点半下班"),
        ("外卖", "外卖。", None, None, None, None),
        ("下班回家", "七点半下班8 点到家", "19:30", "20:00", "七点半下班", "8 点到家"),
        ("吃晚饭", "8 点到家给吃晚饭", "20:00", None, "8 点到家", None),
        ("洗澡", "9 点洗澡", "21:00", None, "9 点洗澡", None),
        ("看《老友记》", "10 点看《老友记》", "22:00", None, "10 点看《老友记》", None),
        ("玩手机", "11 点玩手机", "23:00", None, "11 点玩手机", None),
        ("睡觉", "12 点睡觉", "24:00", None, "12 点睡觉", None),
    ]
    operations = [
        model_add(title, source, start_time=start, end_time=end, start_evidence=start_quote,
                  end_evidence=end_quote, input_order=index, duration_slots=1 if index == 0 else 2)
        for index, (title, source, start, end, start_quote, end_quote) in enumerate(rows)
    ]
    body, fake = run_clock_request(settings, text, operations)
    assert len(fake.calls) == 2
    assert body["validation"]["valid"] is False  # 24:00 start remains a single-day boundary error.
    assert len(body["proposal"]["candidatePlan"]["items"]) == 13
    expected_slots = [35, 36, 38, 48, 52, 54, None, 78, 80, 84, 88, 92, 96]
    assert [op["placement"]["slot"] if op["placement"] else None for op in body["proposal"]["operations"]] == expected_slots
    items = body["proposal"]["candidatePlan"]["items"]
    for item, slot in zip(items, expected_slots, strict=True):
        if slot is not None and slot < 96:
            assert item["segments"][0]["startSlot"] == slot
    assert items[7]["durationSlots"] == 2
    assert items[-1]["segments"] == []
    assert {issue["code"] for issue in body["validation"]["issues"]} == {"INVALID_TIME", "UNPLACED"}
