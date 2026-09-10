import json

import pytest

from test_time_fragment_api import internal_item, model_add, settings
from test_time_fragment_clock_extraction import run_clock_request


@pytest.mark.parametrize("occupied", [False, True])
def test_app_global_start_is_not_a_task_clock_or_a_second_model_call(settings, occupied):
    text = "从 09:00 开始\n阅读"
    operation = model_add("阅读", text, start_time="09:00", start_evidence="从 09:00 开始")
    body, fake = run_clock_request(
        settings, text, [operation], earliest=36,
        items=[internal_item()] if occupied else None,
    )
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    start = 40 if occupied else 36
    added = body["proposal"]["candidatePlan"]["items"][-1]
    assert added["segments"] == [{"startSlot": start, "endSlot": start + 2}]
    model_input = json.loads(fake.calls[0][1])
    assert model_input["text"] == "阅读"
    assert model_input["earliestStartSlot"] == 36


def test_global_prefix_cleanup_preserves_explicit_task_clocks(settings):
    text = "从 09:00 开始\n10:00 开会"
    operation = model_add("开会", "10:00 开会", start_time="10:00", start_evidence="10:00 开会")
    body, fake = run_clock_request(settings, text, [operation], earliest=36)
    assert body["validation"]["valid"] is True
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [{"startSlot": 40, "endSlot": 42}]
    assert json.loads(fake.calls[0][1])["text"] == "10:00 开会"


@pytest.mark.parametrize("earliest", [None, 40])
def test_unmatched_global_header_is_not_silently_repaired(settings, earliest):
    text = "从 09:00 开始\n阅读"
    operation = model_add("阅读", text, start_time="09:00", start_evidence="从 09:00 开始")
    body, _ = run_clock_request(settings, text, [operation], earliest=earliest)
    assert body["validation"]["valid"] is False


def test_wrong_task_clock_still_requires_correction(settings):
    text = "从 09:00 开始\n10:00 开会"
    operation = model_add("开会", "10:00 开会", start_time="09:00", start_evidence="10:00 开会")
    body, fake = run_clock_request(settings, text, [operation], earliest=36)
    assert body["validation"]["valid"] is False
    assert len(fake.calls) == 2
    assert fake.calls[1][0].thinking_mode == "enabled"
    assert fake.calls[1][0].reasoning_effort == "high"
    assert fake.calls[1][0].timeout_seconds == 15.0


def test_header_recovery_requires_evidence_containing_its_clock(settings):
    text = "从 09:00 开始\n阅读"
    operation = model_add("阅读", text, start_time="09:00", start_evidence="从")
    body, _ = run_clock_request(settings, text, [operation], earliest=36)
    assert body["validation"]["valid"] is False


def test_removing_global_start_preserves_explicit_task_end(settings):
    text = "从 09:00 开始\n阅读到10:00"
    operation = model_add("阅读", text, start_time="09:00", end_time="10:00",
                          start_evidence="从 09:00 开始", end_evidence="阅读到10:00")
    body, fake = run_clock_request(settings, text, [operation], earliest=36)
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    assert body["proposal"]["candidatePlan"]["items"][0]["segments"] == [{"startSlot": 38, "endSlot": 40}]
