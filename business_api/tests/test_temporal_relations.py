"""A chronological narrative must retain duration and inter-task constraints."""

import json

from fastapi.testclient import TestClient

from app.factory import create_app
from test_time_fragment_api import FakeModelClient, guest_headers, model_add, raw_output, request_payload, settings


def relation(before, after, kind, evidence):
    return {"beforeInputOrder": before, "afterInputOrder": after, "kind": kind, "evidence": evidence}


def run_request(settings, text, operations, relations, *, first=None, items=None, earliest=None):
    output = raw_output(json.dumps({"operations": operations, "temporalRelations": relations}, ensure_ascii=False))
    fake = FakeModelClient([output if first is None else first, output])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=request_payload(
            text=text, date="2026-09-11", now="2026-09-08T20:00:00+08:00",
            items=items, earliest_start_slot=earliest,
        ))
    assert response.status_code == 200
    return response.json(), fake


def test_missing_relation_review_does_not_silently_accept_short_work_and_midnight_dinner(settings):
    text = "09:00 工作，11:30 午间休息，13:30 继续工作直到18:00。然后用餐，用餐以后乘车返程。"
    operations = [
        model_add("工作", "09:00 工作", start_time="09:00", start_evidence="09:00", input_order=0),
        model_add("午间休息", "11:30 午间休息", start_time="11:30", start_evidence="11:30", input_order=1),
        model_add("继续工作", "13:30 继续工作直到18:00", start_time="13:30", end_time="18:00",
                  start_evidence="13:30", end_evidence="18:00", input_order=2),
        model_add("用餐", "然后用餐", input_order=3),
        model_add("乘车返程", "用餐以后乘车返程", input_order=4),
    ]
    old_output = raw_output(json.dumps({"operations": operations}, ensure_ascii=False))
    fake = FakeModelClient([old_output, old_output])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=request_payload(
            text=text, date="2026-09-11", now="2026-09-08T20:00:00+08:00",
        ))
    assert response.json()["validation"]["valid"] is False
    assert len(fake.calls) == 2
    assert "temporalRelations" in fake.calls[1][1]


def test_continuous_work_and_break_use_next_activity_boundary(settings):
    text = "09:00 工作，11:30 午间休息，13:30 继续工作直到18:00。"
    operations = [
        model_add("工作", "09:00 工作", start_time="09:00", start_evidence="09:00", input_order=0),
        model_add("午间休息", "11:30 午间休息", start_time="11:30", start_evidence="11:30", input_order=1),
        model_add("继续工作", "13:30 继续工作直到18:00", start_time="13:30", end_time="18:00",
                  start_evidence="13:30", end_evidence="18:00", input_order=2),
    ]
    body, fake = run_request(settings, text, operations, [
        relation(0, 1, "until", "09:00 工作，11:30 午间休息"),
        relation(1, 2, "until", "11:30 午间休息，13:30 继续工作直到18:00"),
    ])
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert [i["segments"] for i in body["proposal"]["candidatePlan"]["items"]] == [
        [{"startSlot": 36, "endSlot": 46}], [{"startSlot": 46, "endSlot": 54}],
        [{"startSlot": 54, "endSlot": 72}],
    ]
    assert len(fake.calls) == 1


def test_dinner_and_journey_follow_work_without_moving_unrelated_free_task(settings):
    text = "13:30 工作直到18:00。然后用餐，用餐以后乘车返程，19:30 整理物品。另外买纸。"
    operations = [
        model_add("工作", "13:30 工作直到18:00", start_time="13:30", end_time="18:00",
                  start_evidence="13:30", end_evidence="18:00", input_order=0),
        model_add("用餐", "然后用餐", input_order=1),
        model_add("乘车返程", "用餐以后乘车返程", input_order=2),
        model_add("整理物品", "19:30 整理物品", start_time="19:30", start_evidence="19:30", input_order=3),
        model_add("买纸", "另外买纸", input_order=4),
    ]
    body, _ = run_request(settings, text, operations, [
        relation(0, 1, "before", "13:30 工作直到18:00。然后用餐"),
        relation(1, 2, "before", "然后用餐，用餐以后乘车返程"),
        relation(2, 3, "before", "用餐以后乘车返程，19:30 整理物品"),
    ])
    assert body["validation"]["valid"] is True
    assert [i["segments"] for i in body["proposal"]["candidatePlan"]["items"]] == [
        [{"startSlot": 54, "endSlot": 72}], [{"startSlot": 72, "endSlot": 74}],
        [{"startSlot": 74, "endSlot": 76}], [{"startSlot": 78, "endSlot": 80}],
        [{"startSlot": 0, "endSlot": 2}],
    ]


def test_missing_relation_review_can_correct_once_into_complete_sequence(settings):
    text = "09:00 工作，11:30 午间休息。"
    operations = [
        model_add("工作", "09:00 工作", start_time="09:00", start_evidence="09:00", input_order=0),
        model_add("午间休息", "11:30 午间休息", start_time="11:30", start_evidence="11:30", input_order=1),
    ]
    first = raw_output(json.dumps({"operations": operations}, ensure_ascii=False))
    body, fake = run_request(settings, text, operations, [relation(0, 1, "until", text)], first=first)
    assert body["validation"] == {"valid": True, "attempts": 2, "issues": []}
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [{"startSlot": 36, "endSlot": 46}]
    assert [pipeline.timeout_seconds for pipeline, _ in fake.calls] == [30.0, 15.0]


def pair():
    return "16:00 整理，然后归档。", [
        model_add("整理", "16:00 整理", start_time="16:00", start_evidence="16:00", input_order=0),
        model_add("归档", "然后归档", input_order=1),
    ]


def test_cycle_is_rejected_without_hanging_or_enabling_apply(settings):
    text, operations = pair()
    body, fake = run_request(settings, text, operations, [
        relation(0, 1, "before", text), relation(1, 0, "before", text),
    ])
    assert body["validation"]["valid"] is False
    assert any("循环" in i["message"] for i in body["validation"]["issues"])
    assert len(fake.calls) == 2


def test_unknown_reference_self_reference_and_duplicate_order_are_rejected(settings):
    from copy import deepcopy
    text, operations = pair()
    for before, after, duplicate in [(0, 2, False), (0, 0, False), (0, 1, True)]:
        current = deepcopy(operations)
        if duplicate:
            current[1]["inputOrder"] = 0
        body, _ = run_request(settings, text, current, [relation(before, after, "before", text)])
        assert body["validation"]["valid"] is False
        assert any(i.get("field") == "temporalRelations" for i in body["validation"]["issues"])


def test_relation_evidence_cannot_invent_or_reference_unrelated_text(settings):
    text, operations = pair()
    for quote in ["16:00 整理，稍后归档", "", "不存在的活动"]:
        body, _ = run_request(settings, text, operations, [relation(0, 1, "before", quote)])
        assert body["validation"]["valid"] is False


def test_until_never_overwrites_an_explicit_earlier_end(settings):
    text = "09:00到10:00 工作，11:30 午间休息。"
    operations = [
        model_add("工作", "09:00到10:00 工作", start_time="09:00", end_time="10:00",
                  start_evidence="09:00", end_evidence="10:00", input_order=0),
        model_add("午间休息", "11:30 午间休息", start_time="11:30", start_evidence="11:30", input_order=1),
    ]
    body, _ = run_request(settings, text, operations, [relation(0, 1, "until", text)])
    assert body["validation"]["valid"] is False
    assert body["proposal"]["operations"][0]["durationSlots"] == 4


def test_relative_order_preserves_a_stated_shorter_duration_and_gap(settings):
    text = "09:00 工作30分钟，11:30 午间休息。"
    operations = [
        model_add("工作", "09:00 工作30分钟", duration_slots=2, start_time="09:00", start_evidence="09:00", input_order=0),
        model_add("午间休息", "11:30 午间休息", start_time="11:30", start_evidence="11:30", input_order=1),
    ]
    body, _ = run_request(settings, text, operations, [relation(0, 1, "before", text)])
    assert body["validation"]["valid"] is True
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [{"startSlot": 36, "endSlot": 38}]


def test_until_requires_source_backed_start_boundaries(settings):
    text, operations = pair()
    body, _ = run_request(settings, text, operations, [relation(0, 1, "until", text)])
    assert body["validation"]["valid"] is False
    assert any("开始时间" in i["message"] for i in body["validation"]["issues"])


def test_priority_does_not_override_explicit_precedence(settings):
    text, operations = pair()
    operations[1]["priority"] = 100
    body, _ = run_request(settings, text, operations, [relation(0, 1, "before", text)])
    assert body["validation"]["valid"] is True
    assert [i["segments"] for i in body["proposal"]["candidatePlan"]["items"]] == [
        [{"startSlot": 64, "endSlot": 66}], [{"startSlot": 66, "endSlot": 68}],
    ]


def test_solver_timeout_cannot_silently_fall_back_to_a_midnight_dependency(settings, monkeypatch):
    from app import time_fragment
    monkeypatch.setattr(time_fragment, "solve_slot_allocations", lambda *args: None)
    text, operations = pair()
    body, _ = run_request(settings, text, operations, [relation(0, 1, "before", text)])
    assert body["validation"]["valid"] is False
    assert any(i.get("field") == "temporalRelations" for i in body["validation"]["issues"])


def test_final_validation_rejects_a_solver_result_that_violates_precedence(settings, monkeypatch):
    from app import time_fragment
    def wrong_solver(occupied, targets, relations):
        return {target.item_id: ([64, 65] if target.anchor else [0, 1]) for target in targets}
    monkeypatch.setattr(time_fragment, "solve_slot_allocations", wrong_solver)
    text, operations = pair()
    body, _ = run_request(settings, text, operations, [relation(0, 1, "before", text)])
    assert body["validation"]["valid"] is False
    assert any("先后或连续" in i["message"] for i in body["validation"]["issues"])


def test_no_capacity_keeps_whole_sequence_unplaced_instead_of_reversing_it(settings):
    from test_time_fragment_api import internal_item
    text, operations = pair()
    blocked = {**internal_item(pinned=True), "durationSlots": 32, "segments": [{"startSlot": 64, "endSlot": 96}]}
    body, _ = run_request(settings, text, operations, [relation(0, 1, "before", text)], items=[blocked])
    assert body["validation"]["valid"] is True
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == blocked["segments"]
    assert [i["segments"] for i in body["proposal"]["candidatePlan"]["items"]][1:] == [[], []]
    assert all(i["severity"] == "warning" for i in body["validation"]["issues"])


def test_private_intent_fields_are_not_added_to_the_app_wire_contract(settings):
    from test_time_fragment_api import recursive_keys
    text, operations = pair()
    body, _ = run_request(settings, text, operations, [relation(0, 1, "before", text)])
    assert body["validation"]["valid"] is True
    assert not ({"temporalRelations", "beforeInputOrder", "afterInputOrder", "evidence", "sourceText"} & recursive_keys(body))


def test_until_can_share_predecessor_end_with_untimed_next_activity(settings):
    text = "13:30 工作直到18:00。然后用餐，用餐以后乘车返程。"
    operations = [
        model_add("工作", "13:30 工作直到18:00", start_time="13:30", end_time="18:00",
                  start_evidence="13:30", end_evidence="18:00", input_order=0),
        model_add("用餐", "然后用餐", input_order=1),
        model_add("乘车返程", "用餐以后乘车返程", input_order=2),
    ]
    body, _ = run_request(settings, text, operations, [
        relation(0, 1, "until", "直到18:00。然后用餐"),
        relation(1, 2, "before", "然后用餐，用餐以后乘车返程"),
    ])
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert [i["segments"] for i in body["proposal"]["candidatePlan"]["items"]] == [
        [{"startSlot": 54, "endSlot": 72}], [{"startSlot": 72, "endSlot": 74}],
        [{"startSlot": 74, "endSlot": 76}],
    ]


def test_relation_quote_can_connect_partial_timing_context_without_cropping_task_source(settings):
    text = "11:30 午间休息，13:30 继续工作直到18:00。"
    operations = [
        model_add("午间休息", "11:30 午间休息", start_time="11:30", start_evidence="11:30", input_order=0),
        model_add("继续工作", "13:30 继续工作直到18:00", start_time="13:30", end_time="18:00",
                  start_evidence="13:30", end_evidence="18:00", input_order=1),
    ]
    body, _ = run_request(settings, text, operations, [relation(0, 1, "until", "11:30 午间休息，13:30 继续工作")])
    assert body["validation"]["valid"] is True
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [{"startSlot": 46, "endSlot": 54}]


def test_midnight_todo_can_remain_a_sequence_endpoint_without_blocking_the_day(settings):
    text = "22:00 阅读，24:00 睡觉。"
    operations = [
        model_add("阅读", "22:00 阅读", start_time="22:00", start_evidence="22:00", input_order=0),
        model_add("睡觉", "24:00 睡觉", start_time="24:00", start_evidence="24:00", input_order=1),
    ]
    body, fake = run_request(settings, text, operations, [relation(0, 1, "until", text)])
    assert body["validation"]["valid"] is True
    assert body["validation"]["attempts"] == 1
    assert len(fake.calls) == 1
    assert [i["segments"] for i in body["proposal"]["candidatePlan"]["items"]] == [
        [{"startSlot": 88, "endSlot": 96}], [],
    ]
    assert all(i["severity"] == "warning" for i in body["validation"]["issues"])


def test_contextual_then_quote_does_not_need_to_repeat_the_predecessor(settings):
    text, operations = pair()
    body, _ = run_request(settings, text, operations, [relation(0, 1, "before", "然后归档")])
    assert body["validation"]["valid"] is True
    assert body["proposal"]["candidatePlan"]["items"][1]["segments"] == [{"startSlot": 66, "endSlot": 68}]


def test_relation_correction_preserves_the_previous_extraction_for_targeted_repair(settings):
    text, operations = pair()
    invalid = {"operations": operations, "temporalRelations": [relation(0, 1, "before", "随后归档")]}
    body, fake = run_request(settings, text, operations, [relation(0, 1, "before", "然后归档")],
                             first=raw_output(json.dumps(invalid, ensure_ascii=False)))
    assert body["validation"] == {"valid": True, "attempts": 2, "issues": []}
    correction = json.loads(fake.calls[1][1])
    assert correction["firstExtraction"]["temporalRelations"] == invalid["temporalRelations"]
    assert correction["firstExtraction"]["operations"][0]["timeConstraint"] == operations[0]["timeConstraint"]
    assert correction["firstExtraction"]["operations"][1]["sourceText"] == operations[1]["sourceText"]


def test_first_extraction_does_not_overflow_the_existing_correction_input_budget(settings):
    text, operations = pair()
    invalid = {"operations": operations, "temporalRelations": [relation(0, 1, "before", "随后归档")]}
    first = raw_output(json.dumps(invalid, ensure_ascii=False))
    relations = [relation(0, 1, "before", "然后归档")]
    _, fake = run_request(settings, text, operations, relations, first=first)
    correction = json.loads(fake.calls[1][1])
    correction.pop("firstExtraction")
    budget = max(len(fake.calls[0][1]), len(json.dumps(correction, ensure_ascii=False, separators=(",", ":"))))
    body, bounded = run_request(settings.model_copy(update={"max_input_chars": budget}), text, operations, relations, first=first)
    assert body["validation"]["valid"] is True
    assert "firstExtraction" not in json.loads(bounded.calls[1][1])
    assert len(bounded.calls[1][1]) <= budget


def test_range_quote_is_accepted_only_when_it_uniquely_locates_each_clock(settings):
    text = "19:30到21:30 整理，21:30 休息。"
    source = "19:30到21:30 整理"
    operations = [
        model_add("整理", source, start_time="19:30", end_time="21:30", start_evidence=source, end_evidence=source, input_order=0),
        model_add("休息", "21:30 休息", start_time="21:30", start_evidence="21:30 休息", input_order=1),
    ]
    body, _ = run_request(settings, text, operations, [relation(0, 1, "until", "21:30 休息")])
    assert body["validation"]["valid"] is True
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [{"startSlot": 78, "endSlot": 86}]
    operations[0]["timeConstraint"]["startTime"] = "18:30"
    body, _ = run_request(settings, text, operations, [])
    assert body["validation"]["valid"] is False


def test_repeated_matching_clocks_in_one_quote_are_still_ambiguous(settings):
    text = "8点准备，8点结束准备。"
    operation = model_add("准备", text, start_time="08:00", start_evidence=text)
    body, _ = run_request(settings, text, [operation], [])
    assert body["validation"]["valid"] is False


def test_related_untimed_task_can_quote_next_activity_without_stealing_its_clocks(settings):
    text = "18:00 用餐，用餐以后乘车返程，19:30到21:30 整理。"
    source = "19:30到21:30 整理"
    operations = [
        model_add("用餐", "18:00 用餐", start_time="18:00", start_evidence="18:00 用餐", input_order=0),
        model_add("乘车返程", "用餐以后乘车返程，" + source, input_order=1),
        model_add("整理", source, start_time="19:30", end_time="21:30", start_evidence=source, end_evidence=source, input_order=2),
    ]
    relations = [relation(0, 1, "before", "用餐以后乘车返程"), relation(1, 2, "before", source)]
    body, _ = run_request(settings, text, operations, relations)
    assert body["validation"]["valid"] is True
    assert [i["segments"] for i in body["proposal"]["candidatePlan"]["items"]] == [
        [{"startSlot": 72, "endSlot": 74}], [{"startSlot": 74, "endSlot": 76}], [{"startSlot": 78, "endSlot": 86}],
    ]
    # Removing the ownership relationship does not bypass the original missing-clock guard.
    body, _ = run_request(settings, text, operations, [])
    assert body["validation"]["valid"] is False


def test_successor_clock_ownership_cannot_hide_the_predecessors_own_start(settings):
    text = "18:30 乘车返程，19:30 整理。"
    operations = [
        model_add("乘车返程", "18:30 乘车返程，19:30 整理", input_order=0),
        model_add("整理", "19:30 整理", start_time="19:30", start_evidence="19:30 整理", input_order=1),
    ]
    body, _ = run_request(settings, text, operations, [relation(0, 1, "before", text)])
    assert body["validation"]["valid"] is False
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == []


def test_no_capacity_before_midnight_still_retains_unplaced_tasks_as_warnings(settings):
    from test_time_fragment_api import internal_item
    text = "22:00 阅读，24:00 睡觉。"
    operations = [
        model_add("阅读", "22:00 阅读", start_time="22:00", start_evidence="22:00", input_order=0),
        model_add("睡觉", "24:00 睡觉", start_time="24:00", start_evidence="24:00", input_order=1),
    ]
    blocked = {**internal_item(pinned=True), "durationSlots": 8, "segments": [{"startSlot": 88, "endSlot": 96}]}
    body, _ = run_request(settings, text, operations, [relation(0, 1, "until", text)], items=[blocked])
    assert body["validation"]["valid"] is True
    assert [i["segments"] for i in body["proposal"]["candidatePlan"]["items"]][1:] == [[], []]
