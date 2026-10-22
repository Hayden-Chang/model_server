import json

import httpx
from fastapi.testclient import TestClient

from app.account_api import create_account_api
from app.account_backend import AccountBackend
from test_account_api import configuration
from test_duration_dynamic import body
from test_time_fragment_api import guest_headers


def test_public_run_accepts_english_language_and_checkpoints_it(configuration):
    calls = []

    async def handle(request):
        value = json.loads(request.content)
        calls.append(value)
        if request.url.path.endswith("ai_quota_service"):
            return httpx.Response(200, json={"supportCode": "TF-AAAA-AAAA", "used": 0,
                "limit": 30, "remaining": 30, "enabled": False})
        return httpx.Response(200, json={"runID": "dynamic-1", "status": "running", "steps": [],
            "chargedPoints": 0, "classification": None, "startedAt": "2026-10-22T00:00:00Z"})

    backend = AccountBackend(configuration, httpx.MockTransport(handle))
    with TestClient(create_account_api(configuration, backend, run_workers=False)) as api:
        headers = guest_headers(api, "duration-output-language-regression")
        calls.clear()
        payload = dict(body(), language="en")
        response = api.post("/api/duration/analysis-runs", json=payload, headers=headers)
        assert response.status_code == 202, response.text
        data = calls[0]["p_data"]
        assert data["input"]["language"] == "en"
        assert data["steps"][0]["input"]["language"] == "en"

import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.duration_dynamic import (AnalysisRunRequest, DynamicStepRequest, analyze_dynamic,
    category_steps, classification_step, fingerprint, validate_dynamic_result)
from app.duration_runs import DurationRuns
from app.model_client import ModelGatewayResponseError
from test_duration_dynamic import joint, output
from test_time_fragment_api import FakeModelClient, raw_output, settings


def english_output():
    value = output()
    value.update(category=dict(id="meal", name="Meals", definition="Routine meal activities"),
        baselineReason="The most recent comparable completed activity is the baseline.")
    value["items"][0]["reason"] = "One completed routine activity."
    value["items"][1]["reason"] = "Infer a routine activity from the available text."
    value["items"][1]["references"][0]["reason"] = "A comparable complete activity."
    return value


@pytest.mark.parametrize("language", ["en", "zh-Hans", None])
def test_language_reaches_model_prompt_without_translating_original_inputs(language):
    request = joint().model_copy(update={"language": language})
    value = english_output() if language == "en" else output()
    client = FakeModelClient([raw_output(json.dumps(value, ensure_ascii=False))])
    result = asyncio.run(analyze_dynamic(client, request))
    pipeline, sent = client.calls[0]
    assert ("in English" if language == "en" else "in Simplified Chinese") in pipeline.system_prompt
    assert json.loads(sent)["currentTasks"][0]["title"] == "开发"
    assert result["baselineTaskID"] == "old"
    assert result["items"][1]["difficulty"] == 1.5
    assert result["items"][1]["references"][0]["id"] == "old"


def test_classification_creates_english_metadata_from_chinese_tasks():
    request = DynamicStepRequest(requestID="run", operation="classify", language="en",
        categories=[], tasks=[dict(id="now", title="吃午饭", note="")])
    value = dict(categories=[dict(id="meal", name="Meals", definition="Routine meals")],
        items=[dict(id="now", categoryID="meal", reason="A routine meal activity.")])
    client = FakeModelClient([raw_output(json.dumps(value))])
    assert asyncio.run(analyze_dynamic(client, request)) == value
    assert "in English" in client.calls[0][0].system_prompt
    assert json.loads(client.calls[0][1])["tasks"][0]["title"] == "吃午饭"


@pytest.mark.parametrize("field", ["name", "definition", "baseline", "score", "reference", "missing", "identity"])
def test_english_rejects_chinese_generated_fields_and_invalid_category_projection(field):
    request = joint().model_copy(update={"language": "en"})
    value = english_output()
    if field in ("name", "definition"):
        value["category"][field] = "同类活动"
    elif field == "baseline":
        value["baselineReason"] = "最近一项"
    elif field == "score":
        value["items"][1]["reason"] = "常规活动"
    elif field == "reference":
        value["items"][1]["references"][0]["reason"] = "可比较"
    elif field == "missing":
        value.pop("category")
    else:
        value["category"]["id"] = "different"
    with pytest.raises(ModelGatewayResponseError, match="invalid dynamic duration response"):
        validate_dynamic_result(request, value)


@pytest.mark.parametrize("field", ["name", "definition", "reason"])
def test_english_classification_rejects_chinese_generated_fields(field):
    request = DynamicStepRequest(requestID="run", operation="classify", language="en", categories=[],
        tasks=[dict(id="now", title="吃饭", note="")])
    value = dict(categories=[dict(id="meal", name="Meals", definition="Routine meals")],
        items=[dict(id="now", categoryID="meal", reason="A routine meal.")])
    target = value["items"][0] if field == "reason" else value["categories"][0]
    target[field] = "中文"
    with pytest.raises(ModelGatewayResponseError):
        validate_dynamic_result(request, value)


def test_language_separates_snapshot_hashes_and_preserves_legacy_hash_shape():
    legacy = category_steps(AnalysisRunRequest.model_validate(body()))[0]
    payload = legacy["input"]
    assert "language" not in payload
    expected = dict(rulesVersion=1, category=payload["categories"][0],
        currentTasks=payload["currentTasks"], historyTasks=payload["historyTasks"])
    assert legacy["inputHash"] == fingerprint(expected)
    hashes = {legacy["inputHash"]}
    for language in ("en", "zh-Hans"):
        step = category_steps(AnalysisRunRequest.model_validate(dict(body(), language=language)))[0]
        assert step["input"]["language"] == language
        hashes.add(step["inputHash"])
    assert len(hashes) == 3


@pytest.mark.parametrize("language", ["en", "zh-Hans", None])
def test_worker_persists_language_and_display_metadata_without_mutating_catalog(configuration, language):
    request = joint().model_copy(update={"language": language})
    class Backend:
        settings = configuration
        def __init__(self): self.calls = []
        async def _rpc(self, name, data):
            self.calls.append(data)
            return {"status": "completed"}
        async def _inference(self, actor, payload, *args):
            assert payload.get("language") == language
            return english_output() if language == "en" else output()
    backend = Backend()
    claim = dict(principal="guest_" + "a" * 24, billingEnvironment="sandbox", runID="run",
        key="meal", attempt="attempt", input=request.model_dump(mode="json", by_alias=True), inputHash="digest")
    before = deepcopy(claim)
    asyncio.run(DurationRuns(backend).execute_one(claim))
    assert claim == before
    saved = backend.calls[0]["p_data"]["result"]
    assert backend.calls[0]["p_action"] == "complete"
    assert saved["language"] == (language or "zh-Hans")
    assert saved["categoryInput"] == body()["categories"][0]
    assert saved["category"]["name"] == ("Meals" if language == "en" else "吃饭")
    assert saved["currentTasks"][0]["title"] == "开发"
    assert saved["historyTasks"][0]["minutes"] == 30


def test_classification_worker_retains_language_through_batches_and_joint_checkpoint(configuration):
    payload = body(1)
    payload["historyTasks"] = [dict(id="h" + str(i), title="吃饭", note="", categoryID=None,
        completedAt="2026-10-21T18:00:00Z", minutes=30) for i in range(129)]
    payload["language"] = "en"
    for task in payload["currentTasks"]: task["categoryID"] = None
    request = AnalysisRunRequest.model_validate(payload)
    first = classification_step(request)
    class Backend:
        settings = configuration
        def __init__(self): self.calls = []
        async def _rpc(self, name, data):
            self.calls.append(data)
            return {"status": "running"}
        async def _inference(self, actor, step, *args):
            assert step["language"] == "en"
            return dict(categories=[], items=[dict(id=x["id"], categoryID="meal", reason="A meal activity.") for x in step["tasks"]])
    backend = Backend()
    worker = DurationRuns(backend)
    claim = dict(principal="guest_" + "a" * 24, billingEnvironment="sandbox", runID="run",
        key="__classify__", attempt="attempt", input=first, runInput=payload, inputHash="first")
    asyncio.run(worker.execute_one(claim))
    checkpoint = backend.calls[0]["p_data"]
    assert checkpoint["nextInput"]["language"] == "en"
    assert len(checkpoint["nextInput"]["tasks"]) == 2
    claim.update(input=checkpoint["nextInput"], classification=checkpoint["result"])
    asyncio.run(worker.execute_one(claim))
    checkpoint = backend.calls[1]["p_data"]
    assert checkpoint["nextInput"] is None
    assert len(checkpoint["result"]["items"]) == 130
    assert checkpoint["steps"][0]["input"]["language"] == "en"


def test_legacy_start_hash_keeps_null_category_ids_and_omits_new_language(configuration):
    payload = body()
    payload["currentTasks"][0]["categoryID"] = None
    class Backend:
        settings = configuration
        async def _rpc(self, name, data):
            sent = data["p_data"]
            expected = AnalysisRunRequest.model_validate(payload).model_dump(mode="json", by_alias=True)
            expected.pop("language")
            assert sent["input"] == expected
            assert sent["inputHash"] == fingerprint(expected)
            assert "categoryID" in sent["input"]["currentTasks"][0]
            assert "language" not in sent["steps"][0]["input"]
            return {"status": "running"}
    from app.account_backend import Actor
    asyncio.run(DurationRuns(Backend()).start(Actor("guest_" + "a" * 24), AnalysisRunRequest.model_validate(payload)))


@pytest.mark.parametrize("model,payload", [(AnalysisRunRequest, body()), (DynamicStepRequest, joint().model_dump(by_alias=True))])
def test_unsupported_output_language_is_rejected(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(dict(payload, language="fr"))


def test_internal_english_route_preserves_body_bound_auth(settings):
    from app.factory import create_app
    from app.planning_auth import PlanningCredentials
    from pydantic import SecretStr
    from test_account_api import SECRET
    payload = joint().model_copy(update={"language": "en"}).model_dump(mode="json", by_alias=True, exclude_none=True)
    client = FakeModelClient([raw_output(json.dumps(english_output()))])
    config = settings.model_copy(update={"planning_internal_secret": SecretStr(SECRET), "planning_internal_only": True})
    with TestClient(create_app(config, client)) as api:
        token = PlanningCredentials(SECRET).issue("guest_" + "a" * 24, payload, "attempt")
        response = api.post("/internal/time-fragment/dynamic-duration", json=payload,
            headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200, response.text
        assert response.json()["category"]["name"] == "Meals"
        changed = deepcopy(payload)
        changed["language"] = "zh-Hans"
        assert api.post("/internal/time-fragment/dynamic-duration", json=changed,
            headers={"Authorization": "Bearer " + token}).status_code == 401
