import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.factory import create_app
from test_time_fragment_api import FakeModelClient, guest_headers, operations_output, settings


def insertion_fixture():
    fixture = json.loads((Path(__file__).parent / "fixtures/explicit-duration-insertion.json").read_text())
    # Observability removes domain references; replay with synthetic references.
    for index, item in enumerate(fixture["request"]["currentPlan"]["items"]):
        item["domainRef"] = {"taskId": f"fixture-task-{index}", "occurrenceId": item["itemId"]}
    return fixture


def run_insertion(settings, *, correct):
    fixture = insertion_fixture()
    original = operations_output(fixture["operations"])
    if correct:
        fixture["operations"][0]["timeConstraint"].update(endTime=None, endEvidence=None)
    fake = FakeModelClient([original, operations_output(fixture["operations"])])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=fixture["request"])
    assert response.status_code == 200
    return response.json(), fake


def test_inferred_end_is_rejected_with_actionable_correction_fields(settings):
    body, fake = run_insertion(settings, correct=False)
    assert body["validation"]["valid"] is False
    assert body["validation"]["attempts"] == 2 and len(fake.calls) == 2
    correction = json.loads(fake.calls[1][1])
    message = next(issue["message"] for issue in correction["issues"] if issue["field"] == "timeConstraint")
    assert "endTime" in message and "endEvidence" in message and "null" in message
    assert "durationSlots" in message
    assert body["proposal"]["candidatePlan"]["items"][-1]["segments"] == []


def test_explicit_start_and_duration_correction_preserves_insertion_and_shifted_tasks(settings):
    body, fake = run_insertion(settings, correct=True)
    assert body["validation"] == {"valid": True, "attempts": 2, "issues": []}
    assert len(fake.calls) == 2
    pipeline, _ = fake.calls[1]
    assert pipeline.thinking_mode == "enabled" and pipeline.timeout_seconds == 15
    assert pipeline.reasoning_effort == "low"
    assert "Never compute an unstated boundary from duration" in pipeline.system_prompt
    assert [(item["title"], item["segments"]) for item in body["proposal"]["candidatePlan"]["items"]] == [
        ("打游戏", [{"startSlot": 80, "endSlot": 83}]),
        ("吃午饭", [{"startSlot": 48, "endSlot": 51}]),
        ("休息", [{"startSlot": 54, "endSlot": 58}]),
        ("逛街", [{"startSlot": 58, "endSlot": 74}]),
        ("电话", [{"startSlot": 52, "endSlot": 54}]),
    ]
