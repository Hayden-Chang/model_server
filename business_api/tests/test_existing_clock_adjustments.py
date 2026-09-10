import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.factory import create_app
from test_time_fragment_api import FakeModelClient, guest_headers, operations_output, settings


def adjustment_fixture():
    return json.loads((Path(__file__).parent / "fixtures/existing-clock-adjustment.json").read_text())


def run_adjustment(settings, fixture):
    output = operations_output(fixture["operations"])
    fake = FakeModelClient([output, output])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=fixture["request"])
    assert response.status_code == 200
    return response.json(), fake


def test_rename_move_resize_and_delete_ranges_do_not_trigger_false_correction(settings):
    body, fake = run_adjustment(settings, adjustment_fixture())
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    assert [(item["title"], item["segments"]) for item in body["proposal"]["candidatePlan"]["items"]] == [
        ("打游戏", [{"startSlot": 80, "endSlot": 83}]),
        ("吃午饭", [{"startSlot": 48, "endSlot": 51}]),
        ("休息", [{"startSlot": 52, "endSlot": 56}]),
        ("逛街", [{"startSlot": 56, "endSlot": 72}]),
    ]


@pytest.mark.parametrize("end_anchor", [False, True])
def test_named_range_without_optional_authorization_quote_uses_updated_duration(settings, end_anchor):
    fixture = adjustment_fixture()
    fixture["request"]["text"] = "吃午饭改成 12:00 到 12:45。"
    fixture["operations"] = fixture["operations"][3:5]
    for operation in fixture["operations"]:
        operation.pop("authorizationText")
    if end_anchor:
        fixture["operations"][0]["placement"] = {"anchor": "end", "slot": 51}
    body, fake = run_adjustment(settings, fixture)
    assert body["validation"] == {"valid": True, "attempts": 1, "issues": []}
    assert len(fake.calls) == 1
    lunch = next(item for item in body["proposal"]["candidatePlan"]["items"] if item["title"] == "吃午饭")
    assert lunch["segments"] == [{"startSlot": 48, "endSlot": 51}]


@pytest.mark.parametrize("error", ["wrong-start", "wrong-duration", "fabricated-quote", "unrelated-add-clock"])
def test_adjustment_clock_coverage_still_rejects_missing_or_unrelated_boundaries(settings, error):
    fixture = adjustment_fixture()
    if error == "wrong-start":
        fixture["operations"][1]["placement"]["slot"] = 84
    elif error == "wrong-duration":
        fixture["operations"][2]["durationSlots"] = 2
    elif error == "fabricated-quote":
        fixture["operations"][1]["authorizationText"] = "20:00 到 20:45 打游戏"
        fixture["operations"][2]["authorizationText"] = "20:00 到 20:45 打游戏"
    else:
        fixture["request"]["text"] += "晚上 21:30 看书。"
    body, fake = run_adjustment(settings, fixture)
    assert body["validation"]["valid"] is False
    assert body["validation"]["attempts"] == 2
    assert len(fake.calls) == 2
    assert any(issue["field"] == "timeConstraint" for issue in body["validation"]["issues"])


def test_missing_move_quote_correction_uses_high_reasoning_effort(settings):
    fixture = adjustment_fixture()
    invalid = json.loads(json.dumps(fixture["operations"]))
    for operation in invalid:
        operation.pop("authorizationText", None)
    fake = FakeModelClient([operations_output(invalid), operations_output(fixture["operations"])])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=fixture["request"])
    assert response.status_code == 200
    assert response.json()["validation"] == {"valid": True, "attempts": 2, "issues": []}
    assert len(fake.calls) == 2
    correction, prompt = fake.calls[1]
    assert "未被时间依据覆盖" in prompt
    assert correction.thinking_mode == "enabled"
    assert correction.reasoning_effort == "high"
    assert correction.timeout_seconds == 15
