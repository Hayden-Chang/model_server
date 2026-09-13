"""Natural user text is immutable; the extractor/validator must handle it."""

import json

import pytest

from app.model_client import ModelOutput
from app.time_fragment import _parse_clock_token
from app.time_fragment_service import _bare_task_title
from fastapi.testclient import TestClient
from app.factory import create_app
from test_time_fragment_api import (
    FakeModelClient, admin_headers, guest_headers, internal_item, model_add, operations_output, request_payload, settings,
)
from test_time_fragment_clock_extraction import run_clock_request


@pytest.mark.parametrize(("text", "clock"), [
    ("i need 45 mins for the proposal at 3", "15:00"),
    ("squeeze in a 30-min urgent call at 10", "10:00"),
    ("call at 3 pm", "15:00"),
    ("call at 3:15 PM", "15:15"),
    ("call at 12 a.m.", "00:00"),
    ("call at 12 p.m.", "12:00"),
])
def test_colloquial_clock_evidence_is_accepted(settings, text, clock):
    body, _ = run_clock_request(settings, text, [
        model_add("Call", text, start_time=clock, start_evidence=text),
    ])
    assert body["validation"]["valid"], body
    item = body["proposal"]["candidatePlan"]["items"][0]
    hour, minute = map(int, clock.split(":"))
    assert item["segments"][0]["startSlot"] == (hour * 60 + minute + 7) // 15


@pytest.mark.parametrize(("text", "clock"), [
    ("call at 3 pm", "03:00"),
    ("call at 3:15 PM", "03:15"),
    ("call at 12 a.m.", "12:00"),
    ("fit in 45 mins for task B", "00:45"),
    ("don't put the call at 3 pm", "15:00"),
])
def test_clock_evidence_cannot_change_explicit_period_or_use_duration(settings, text, clock):
    body, _ = run_clock_request(settings, text, [
        model_add("Call", text, start_time=clock, start_evidence=text),
    ])
    assert body["validation"]["valid"] is False


def protected_move(text, *, affirmative=True, target_id="occurrence-1"):
    return {
        "type": "move", "targetItemId": "occurrence-1", "objectType": "internalTask",
        "allowedChanges": ["segments"], "inputOrder": 0,
        "placement": {"anchor": "start", "slot": 64}, "authorizationText": text,
        "authorization": {"action": "move", "targetItemId": target_id,
                          "targetText": "project review", "sourceText": text,
                          "affirmative": affirmative},
    }


@pytest.mark.parametrize("target_text", ["project review", "Project Review"])
def test_explicit_colloquial_protected_move_preserves_other_fields(settings, target_text):
    text = "move my pinned project review to 4 today"
    item = internal_item(pinned=True)
    item["title"] = "Project Review"
    operation = protected_move(text)
    operation["authorization"]["targetText"] = target_text
    body, _ = run_clock_request(settings, text, [operation], items=[item])
    assert body["validation"]["valid"], body
    result = body["proposal"]["candidatePlan"]["items"][0]
    assert result == {**item, "segments": [{"startSlot": 64, "endSlot": 68}]}
    assert "authorization" not in json.dumps(body)


@pytest.mark.parametrize(("text", "affirmative", "target_id"), [
    ("don't move my pinned project review to 4 today", False, "occurrence-1"),
    ("don't move my pinned project review to 4 today", True, "occurrence-1"),
    ("move my pinned project review to 4 today", True, "wrong-id"),
])
def test_protected_move_rejects_denial_or_wrong_target(settings, text, affirmative, target_id):
    item = internal_item(pinned=True)
    item["title"] = "Project Review"
    body, _ = run_clock_request(settings, text, [
        protected_move(text, affirmative=affirmative, target_id=target_id),
    ], items=[item])
    assert body["validation"]["valid"] is False
    assert body["proposal"]["candidatePlan"]["items"] == [item]


@pytest.mark.parametrize("mutation", ["action", "quote", "target", "truncated_negation"])
def test_structured_authorization_cannot_forge_bindings(settings, mutation):
    text = "move my pinned project review to 4 today"
    item = internal_item(pinned=True)
    item["title"] = "Project Review"
    operation = protected_move(text)
    if mutation == "action":
        operation["authorization"]["action"] = "delete"
    elif mutation == "quote":
        operation["authorizationText"] = "move project review to 4"
        operation["authorization"]["sourceText"] = operation["authorizationText"]
    elif mutation == "target":
        operation["authorization"]["targetText"] = "review"
    else:
        text = "don't " + text
    body, _ = run_clock_request(settings, text, [operation], items=[item])
    assert body["validation"]["valid"] is False
    assert body["proposal"]["candidatePlan"]["items"] == [item]


def test_english_external_move_is_only_a_local_candidate_change(settings):
    # Use a complete event fixture: its source identity and flags must survive.
    item = {"itemId": "occurrence-1", "objectType": "externalEvent",
            "domainRef": {"externalEventId": "occurrence-1"}, "title": "Project Review",
            "durationSlots": 4, "segments": [{"startSlot": 56, "endSlot": 60}],
            "isAllDay": False, "isFixed": True}
    text = "move the project review to 4 today, just in this app"
    operation = protected_move(text)
    operation["objectType"] = "externalEvent"
    body, _ = run_clock_request(settings, text, [operation], items=[item])
    assert body["validation"]["valid"], body
    assert body["proposal"]["candidatePlan"]["items"] == [
        {**item, "segments": [{"startSlot": 64, "endSlot": 68}]},
    ]
    assert body["proposal"]["deletedExternalEventIDs"] == []


@pytest.mark.parametrize("negated", [False, True])
def test_structured_authorization_preserves_chinese_negation_guard(settings, negated):
    text = f"请{'不要' if negated else ''}把钉住任务移动到下午四点"
    item = internal_item(pinned=True)
    operation = protected_move(text)
    operation["authorization"]["targetText"] = item["title"]
    body, _ = run_clock_request(settings, text, [operation], items=[item])
    assert body["validation"]["valid"] is not negated
    expected = item if negated else {**item, "segments": [{"startSlot": 64, "endSlot": 68}]}
    assert body["proposal"]["candidatePlan"]["items"] == [expected]


def test_protected_target_name_cannot_match_inside_a_different_word(settings):
    text = "move preview to 4"
    item = internal_item(pinned=True)
    item["title"] = "Review"
    operation = protected_move(text)
    operation["authorization"]["targetText"] = "review"
    body, _ = run_clock_request(settings, text, [operation], items=[item])
    assert body["validation"]["valid"] is False
    assert body["proposal"]["candidatePlan"]["items"] == [item]


@pytest.mark.parametrize("text", ["don't move review", "leave it alone", "fit in reading"])
def test_english_commands_never_trigger_literal_title_fallback(text):
    assert _bare_task_title(text) is None


def test_plain_colon_clock_still_inherits_chinese_period():
    assert _parse_clock_token("3:00", inherited_period="下午") == (15 * 60, None)


def test_fit_in_task_is_split_around_pinned_task(settings):
    text = "fit in 45 mins for task B"
    item = internal_item(pinned=True)
    item.update(title="Pinned Task A", durationSlots=1,
                segments=[{"startSlot": 37, "endSlot": 38}])
    body, _ = run_clock_request(settings, text, [
        model_add("task B", text, duration_slots=3),
    ], items=[item], earliest=36)
    assert body["validation"]["valid"], body
    items = body["proposal"]["candidatePlan"]["items"]
    assert items[0] == item
    assert items[1]["segments"] == [{"startSlot": 36, "endSlot": 37}, {"startSlot": 38, "endSlot": 40}]


@pytest.mark.parametrize("no_change", [False, True])
def test_empty_correction_is_checked_against_original_not_hallucinated_operations(settings, no_change):
    text = "leave everything as it is" if no_change else "fit in 45 mins for task B"
    fake = FakeModelClient([
        operations_output([model_add("invented", "not in the original request")]),
        operations_output([]),
        ModelOutput(content=json.dumps({"noChangeNeeded": no_change}), provider_model="test", usage=None),
    ])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client),
                               json=request_payload(text=text, date="2026-09-11"))
        observed = client.get("/admin/observability/requests", headers=admin_headers()).json()["records"][0]
    assert response.status_code == 200
    assert response.json()["validation"]["valid"] is no_change
    assert json.loads(fake.calls[2][1])["text"] == text
    assert "invented" not in fake.calls[2][1]
    assert observed["model_call_count"] == 3
    assert [call["call_index"] for call in observed["model_calls"]] == [1, 2, 3]


@pytest.mark.parametrize("content", ['{"noChangeNeeded":1}', '{"noChangeNeeded":"true"}', 'invalid'])
def test_malformed_noop_decision_never_reports_success(settings, content):
    fake = FakeModelClient([
        operations_output([model_add("invented", "not in original")]),
        operations_output([]), ModelOutput(content=content, provider_model="test", usage=None),
    ])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client),
                               json=request_payload(text="fit in 45 mins for task B", date="2026-09-11"))
    assert response.status_code == 200
    assert response.json()["validation"]["valid"] is False
