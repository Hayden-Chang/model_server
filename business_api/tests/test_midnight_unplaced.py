"""Day-end adds are retained without asking the model to change a correct clock."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.factory import create_app
from app.model_client import ModelOutput
from test_time_fragment_api import FakeModelClient, guest_headers, internal_item, model_add, settings
from test_time_fragment_clock_extraction import run_clock_request


def without_nulls(value):
    if isinstance(value, dict):
        return {key: without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value


def test_real_september11_output_retains_midnight_todo_without_correction(settings):
    trace = json.loads((Path(__file__).parent / "fixtures/sep11-live-clock-output.json").read_text())
    # Upgrade the internal envelope without changing any recorded task or clock.
    recorded = {**json.loads(trace["rawModelOutput"]), "temporalRelations": []}
    output = ModelOutput(content=json.dumps(recorded), provider_model="recorded-live-output", usage=None)
    fake = FakeModelClient([output, output])
    with TestClient(create_app(settings, fake)) as client:
        response = client.post("/api/plan/parse", headers=guest_headers(client), json=trace["request"])
    body = response.json()
    assert response.status_code == 200
    assert body["validation"]["valid"] is True
    assert body["validation"]["attempts"] == 1
    assert len(fake.calls) == 1
    assert without_nulls(body["proposal"]) == without_nulls({
        "baseFingerprint": trace["request"]["baseFingerprint"], **trace["firstCandidate"],
    })
    items = body["proposal"]["candidatePlan"]["items"]
    assert len(items) == 13
    assert items[-1]["title"] == "睡觉"
    assert items[-1]["segments"] == []
    assert all(item["segments"] for item in items[:-1])
    warning = next(issue for issue in body["validation"]["issues"] if issue["code"] == "INVALID_TIME")
    assert warning["itemId"] == items[-1]["itemId"]
    assert warning["severity"] == "warning"
    assert "24:00" in warning["message"] and "未排任务" in warning["message"]
    assert all(issue["severity"] == "warning" for issue in body["validation"]["issues"])


@pytest.mark.parametrize("kind", ["end-zero", "existing-move", "wrong-clock"])
def test_midnight_add_does_not_hide_other_hard_errors(settings, kind):
    sleep = model_add("睡觉", "24:00 睡觉", start_time="24:00", start_evidence="24:00 睡觉")
    if kind == "existing-move":
        item = internal_item()
        text = f"24:00 睡觉，把{item['title']}移到24:00"
        other = {"type": "move", "targetItemId": item["itemId"], "objectType": "internalTask",
                 "placement": {"anchor": "start", "slot": 96}, "allowedChanges": ["segments"],
                 "inputOrder": 1}
        items = [item]
    elif kind == "end-zero":
        text = "24:00 睡觉，00:00 结束阅读"
        other = model_add("阅读", "00:00 结束阅读", end_time="00:00", end_evidence="00:00 结束阅读", input_order=1)
        items = []
    else:
        text = "24:00 睡觉，早上8:50 起床"
        other = model_add("起床", "早上8:50 起床", start_time="20:50", start_evidence="早上8:50 起床", input_order=1)
        items = []
    body, fake = run_clock_request(settings, text, [sleep, other], items=items)
    assert body["validation"]["valid"] is False
    assert len(fake.calls) == 2
    sleep_id = next(op["temporaryId"] for op in body["proposal"]["operations"] if op.get("title") == "睡觉")
    assert next(item for item in body["proposal"]["candidatePlan"]["items"] if item["itemId"] == sleep_id)["segments"] == []
    correction = json.loads(fake.calls[1][1])
    assert correction["issues"]
    assert all(issue.get("itemId") != sleep_id for issue in correction["issues"])
    if kind == "existing-move":
        assert [(issue["code"], issue.get("itemId")) for issue in correction["issues"]] == [
            ("INVALID_TIME", item["itemId"]),
        ]
