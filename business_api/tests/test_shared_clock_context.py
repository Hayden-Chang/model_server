"""Keep shared clock ownership with model extraction, never infer free-task times."""

import json
from pathlib import Path

import pytest

from app.pipelines import TIME_FRAGMENT_OPERATIONS_SCHEMA, get_pipeline
from test_time_fragment_api import model_add, settings
from test_time_fragment_clock_extraction import run_clock_request


def reported_output():
    trace = json.loads((Path(__file__).parent / "fixtures/sep11-live-missing-dinner-clock.json").read_text())
    return trace["request"]["text"], json.loads(trace["rawModelOutput"])["operations"]


def test_recorded_cropped_dinner_context_cannot_become_a_valid_midnight_schedule(settings):
    text, operations = reported_output()
    body, fake = run_clock_request(settings, text, operations)
    assert body["validation"]["valid"] is False
    assert len(fake.calls) == 2
    dinner = next(item for item in body["proposal"]["candidatePlan"]["items"] if item["title"] == "吃晚饭")
    assert dinner["segments"] == []
    correction = json.loads(fake.calls[1][1])
    assert any(issue.get("itemId") == dinner["itemId"] and "上下文" in issue["message"] for issue in correction["issues"])
    sleep = next(item for item in body["proposal"]["candidatePlan"]["items"] if item["title"] == "睡觉")
    assert all(issue.get("itemId") != sleep["itemId"] for issue in correction["issues"])


@pytest.mark.parametrize("clock", ["20:00", "08:00"])
def test_shared_arrival_clock_has_one_resolution_across_commute_and_dinner(settings, clock):
    text, operations = reported_output()
    operations[8] = model_add("吃晚饭", "8 点到家给吃晚饭", input_order=8,
                              start_time=clock, start_evidence="8 点到家")
    body, fake = run_clock_request(settings, text, operations)
    items = body["proposal"]["candidatePlan"]["items"]
    dinner = next(item for item in items if item["title"] == "吃晚饭")
    assert len(items) == 13
    assert items[-1]["title"] == "睡觉" and items[-1]["segments"] == []
    if clock == "20:00":
        assert body["validation"]["valid"] is True
        assert len(fake.calls) == 1
        assert dinner["segments"] == [{"startSlot": 80, "endSlot": 82}]
        assert next(item for item in items if item["title"] == "外卖")["segments"] == [{"startSlot": 0, "endSlot": 2}]
    else:
        assert body["validation"]["valid"] is False
        assert dinner["segments"] == []
        assert any("同一原文钟点" in issue["message"] for issue in body["validation"]["issues"])


@pytest.mark.parametrize("source", ["有空再吃晚饭", "吃晚饭", "等一会儿吃晚饭", "再吃晚饭", "稍后吃晚饭"])
def test_independent_or_explicitly_flexible_tasks_do_not_inherit_arrival_clock(settings, source):
    prefix = "七点半下班8 点到家"
    text = prefix + ("。" if source == "吃晚饭" else "") + source
    # A too-wide commute quote does not turn an explicitly flexible task into a clocked one.
    commute_source = prefix if source == "吃晚饭" else text
    operations = [
        model_add("下班回家", commute_source, start_time="19:30", end_time="20:00",
                  start_evidence="七点半下班", end_evidence="8 点到家"),
        model_add("吃晚饭", source, input_order=1),
    ]
    body, fake = run_clock_request(settings, text, operations)
    assert body["validation"]["valid"] is True
    assert len(fake.calls) == 1
    assert body["proposal"]["operations"][1]["placement"] is None


def test_correction_restores_shared_dinner_clock_without_changing_other_operations(settings):
    text, operations = reported_output()
    corrected = list(operations)
    corrected[8] = model_add("吃晚饭", "8 点到家给吃晚饭", input_order=8,
                             start_time="20:00", start_evidence="8 点到家")
    body, fake = run_clock_request(settings, text, operations, corrected=corrected)
    assert body["validation"]["valid"] is True and body["validation"]["attempts"] == 2
    assert len(fake.calls) == 2
    correction = json.loads(fake.calls[1][1])
    first = correction["firstCandidate"]["candidatePlan"]["items"]
    final = body["proposal"]["candidatePlan"]["items"]
    assert final[8]["segments"] == [{"startSlot": 80, "endSlot": 82}]
    assert [item["segments"] for index, item in enumerate(final) if index != 8] == [
        item["segments"] for index, item in enumerate(first) if index != 8
    ]


def test_distinct_eight_oclock_mentions_can_still_resolve_to_different_periods(settings):
    operations = [
        model_add("早餐", "8点吃早餐", start_time="08:00", start_evidence="8点吃早餐"),
        model_add("晚餐", "8点吃晚餐", input_order=1, start_time="20:00", start_evidence="8点吃晚餐"),
    ]
    body, fake = run_clock_request(settings, "8点吃早餐，8点吃晚餐", operations)
    assert body["validation"]["valid"] is True and len(fake.calls) == 1
    assert [op["placement"]["slot"] for op in body["proposal"]["operations"]] == [32, 80]


@pytest.mark.parametrize("source", ["有空再吃晚饭", "吃晚饭"])
def test_long_endpoint_quote_cannot_consume_a_flexible_action_prefix(settings, source):
    text = "七点半下班8 点到家有空再吃晚饭"
    operations = [
        model_add("下班回家", text, start_time="19:30", end_time="20:00",
                  start_evidence="七点半下班", end_evidence="8 点到家有空再"),
        model_add("吃晚饭", source, input_order=1),
    ]
    body, fake = run_clock_request(settings, text, operations)
    assert body["validation"]["valid"] is True and len(fake.calls) == 1
    assert body["proposal"]["operations"][1]["placement"] is None


def test_shared_clock_guidance_is_sent_in_prompt_and_model_schema():
    pipeline = get_pipeline("time-fragment-plan-v2")
    assert "one source clock may be both" in pipeline.system_prompt
    assert "8 点到家给吃晚饭" in pipeline.system_prompt
    assert "有空再" in pipeline.system_prompt
    properties = TIME_FRAGMENT_OPERATIONS_SCHEMA["$defs"]["TimeFragmentExtractedAddOperation"]["properties"]
    assert "shared clock" in properties["sourceText"].get("description", "")
    assert "shared clock" in properties["timeConstraint"].get("description", "")
    assert pipeline.temperature == 0 and pipeline.thinking_mode == "disabled"
    assert pipeline.max_tokens == 20000
