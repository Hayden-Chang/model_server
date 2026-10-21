import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.account_api import create_account_api
from app.account_backend import AccountBackend, Actor, failure
from app.duration_relative import RelativeRequest, RelativeResponse, analyze_relative
from app.factory import create_app
from app.model_client import LiteLLMClient, ModelGatewayResponseError
from app.duration_profiles import PROFILE_PIPELINE
from app.pipelines import PIPELINES
from app.planning_auth import PlanningCredentials
from test_account_api import Backend, configuration, device_headers, account_headers, SECRET
from test_time_fragment_api import FakeModelClient, raw_output, settings


def text(id="a"):
    return {"id": id, "title": "写需要查资料的口播稿", "note": "查证产品资料"}


def category(anchor=True):
    value = {"id": "script", "name": "口播文案", "definition": "短视频口播脚本"}
    if anchor:
        value["anchor"] = {**text("baseline"), "difficulty": 1}
    return value


def payload(score=False):
    return {"requestID": "relative-1", "operation": "score" if score else "classify",
            "tasks": [text()], "categories": [category()] if score else []}


def result(score=False):
    return {"requestID": "relative-1", "operation": "score" if score else "classify",
            "categories": [] if score else [category(False)],
            "anchorTaskID": "baseline" if score else None,
            "items": [{"id": "a", "categoryID": "script", "difficulty": 1.5 if score else None,
                       "reason": "沿用已有尺度" if score else "同为口播脚本", "references": []}]}


def validated(body, response):
    return RelativeResponse.model_validate(response).validate_for(RelativeRequest.model_validate(body))


def test_dynamic_category_reuse_new_definition_and_unclassified():
    body = payload()
    assert validated(body, result()).categories[0].id == "script"
    body["categories"] = [category()]
    response = result(); response["categories"] = []
    assert validated(body, response).items[0].category_id == "script"
    response["items"][0]["categoryID"] = None
    assert validated(body, response).items[0].category_id is None
    response["categories"] = [category(False)]
    with pytest.raises(ValueError):
        validated(body, response)


@pytest.mark.parametrize("count", [1, 2])
def test_classification_discards_generated_examples_but_keeps_request_examples(count):
    body = payload()
    body["tasks"] = [text(str(i)) for i in range(count)]
    body["categories"] = [{"id": "meal", "name": "用餐", "definition": "日常吃饭",
                           "examples": [{"id": "lunch", "title": "吃午饭", "note": ""}]}]
    response = result()
    response["categories"][0]["examples"] = [body["tasks"][0]]
    response["items"] = [{**result()["items"][0], "id": task["id"]} for task in body["tasks"]]
    request = RelativeRequest.model_validate(body)
    model = FakeModelClient([raw_output(json.dumps(response, ensure_ascii=False))])
    value = asyncio.run(analyze_relative(model, request))
    assert value.categories[0].examples == []
    assert value.categories[0].anchor is None
    assert {item.id for item in value.items} == {task["id"] for task in body["tasks"]}
    assert all(item.category_id == "script" for item in value.items)
    assert json.loads(model.calls[0][1])["categories"][0]["examples"] == body["categories"][0]["examples"]
    assert len(model.calls) == 1


def test_classification_generated_examples_do_not_allow_a_generated_anchor():
    response = result()
    response["categories"][0].update(examples=[text()], anchor={**text(), "difficulty": 1})
    model = FakeModelClient([raw_output(json.dumps(response, ensure_ascii=False))])
    with pytest.raises(ModelGatewayResponseError):
        asyncio.run(analyze_relative(model, RelativeRequest.model_validate(payload())))


def test_classification_with_persisted_anchor_keeps_scoring_fields_empty():
    body = payload(); body["categories"] = [category()]
    response = result(); response["categories"] = []
    value = validated(body, response)
    assert value.anchor_task_id is None and value.items[0].difficulty is None
    assert value.items[0].category_id == "script"
    assert value.items[0].references == []
    response["anchorTaskID"] = "baseline"
    with pytest.raises(ValueError):
        validated(body, response)


def test_score_preserves_saved_difficulty_anchor_and_newest_reference_order():
    body = payload(True); body["tasks"][0]["difficulty"] = 1.5
    body["references"] = [{**text(id), "categoryID": "script", "difficulty": difficulty}
                          for id, difficulty in [("recent", 2), ("old", 0.5)]]
    response = result(True)
    response["items"][0]["references"] = [{"id": "old", "reason": "可比"}, {"id": "recent", "reason": "可比"}]
    assert [ref.id for ref in validated(body, response).items[0].references] == ["recent", "old"]
    response["items"][0]["difficulty"] = 2
    with pytest.raises(ValueError):
        validated(body, response)
    response = result(True); response["anchorTaskID"] = "a"
    with pytest.raises(ValueError):
        validated(body, response)


def test_bootstrap_requires_one_explicit_target_anchor_and_allows_abstention():
    body = payload(True); body["categories"] = [category(False)]
    response = result(True); response["anchorTaskID"] = "a"; response["items"][0]["difficulty"] = 1
    assert validated(body, response).anchor_task_id == "a"
    response["items"][0]["difficulty"] = 1.5
    with pytest.raises(ValueError):
        validated(body, response)
    response["anchorTaskID"] = None; response["items"][0]["difficulty"] = None
    assert validated(body, response).items[0].difficulty is None


def test_routine_history_bootstraps_and_remains_comparable_without_time_fields():
    meal = {"id": "meal", "name": "吃饭", "definition": "进食一餐的日常活动"}
    history = {"id": "history", "title": "吃饭", "note": ""}
    body = {"requestID": "routine-history", "operation": "score", "tasks": [history], "categories": [meal]}
    response = {"requestID": body["requestID"], "operation": "score", "categories": [], "anchorTaskID": "history",
                "items": [{"id": "history", "categoryID": "meal", "difficulty": 1,
                           "reason": "一次日常用餐作为基准", "references": []}]}
    model = FakeModelClient([raw_output(json.dumps(response, ensure_ascii=False))])
    saved = asyncio.run(analyze_relative(model, RelativeRequest.model_validate(body)))
    assert saved.anchor_task_id == "history"
    assert saved.items[0].difficulty == 1
    assert json.loads(model.calls[0][1]) == RelativeRequest.model_validate(body).model_dump(by_alias=True, exclude_none=True)

    body = {"requestID": "routine-current", "operation": "score",
            "tasks": [{"id": "current", "title": "吃饭", "note": ""}],
            "categories": [{**meal, "anchor": {**history, "difficulty": saved.items[0].difficulty}}],
            "references": [{**history, "categoryID": "meal", "difficulty": saved.items[0].difficulty}]}
    response = {**response, "requestID": body["requestID"], "items": [
        {"id": "current", "categoryID": "meal", "difficulty": 1, "reason": "同为一次日常用餐",
         "references": [{"id": "history", "reason": "相同活动与范围"}]}]}
    model = FakeModelClient([raw_output(json.dumps(response, ensure_ascii=False))])
    value = asyncio.run(analyze_relative(model, RelativeRequest.model_validate(body)))
    assert value.items[0].difficulty == 1
    assert [reference.id for reference in value.items[0].references] == ["history"]
    assert json.loads(model.calls[0][1]) == RelativeRequest.model_validate(body).model_dump(by_alias=True, exclude_none=True)


def test_bootstrap_positive_score_requires_explicit_anchor_id():
    body = payload(True); body["categories"] = [category(False)]
    response = result(True); response["anchorTaskID"] = None; response["items"][0]["difficulty"] = 1
    with pytest.raises(ValueError):
        validated(body, response)


@pytest.mark.parametrize("change", ["request", "operation", "missing", "duplicate", "foreign", "category", "anchor"])
def test_invalid_identity_and_scale_fail_closed(change):
    response = result(True)
    if change == "request": response["requestID"] = "other"
    if change == "operation": response["operation"] = "classify"
    if change == "missing": response["items"] = []
    if change == "duplicate": response["items"] *= 2
    if change == "foreign": response["items"][0]["id"] = "other"
    if change == "category": response["items"][0]["categoryID"] = "other"
    if change == "anchor": response["anchorTaskID"] = "other"
    with pytest.raises(ValueError): validated(payload(True), response)


@pytest.mark.parametrize("difficulty", [0, -1, True, "1.5", float("nan"), float("inf")])
def test_invalid_difficulty_rejected(difficulty):
    response = result(True); response["items"][0]["difficulty"] = difficulty
    with pytest.raises(ValueError): validated(payload(True), response)


@pytest.mark.parametrize("ids", [["foreign"], ["r", "r"], ["r"] * 6])
def test_foreign_repeated_or_excessive_references_rejected(ids):
    body = payload(True); body["references"] = [{**text("r"), "categoryID": "script", "difficulty": 2}]
    response = result(True); response["items"][0]["references"] = [{"id": id, "reason": "可比"} for id in ids]
    with pytest.raises(ValueError): validated(body, response)


@pytest.mark.parametrize("where", ["task", "category", "anchor", "reference", "example"])
def test_nested_time_fields_are_rejected(where):
    body = payload(True)
    body["references"] = [{**text("r"), "categoryID": "script", "difficulty": 2}]
    body["categories"][0]["examples"] = [text("e")]
    target = {"task": body["tasks"][0], "category": body["categories"][0],
              "anchor": body["categories"][0]["anchor"], "reference": body["references"][0],
              "example": body["categories"][0]["examples"][0]}[where]
    target["actualMinutes"] = 60
    with pytest.raises(ValidationError): RelativeRequest.model_validate(body)


def test_input_size_covers_catalog_examples_and_history_and_rejects_cross_category():
    body = payload(True); body["references"] = [{**text("r"), "categoryID": "other", "difficulty": 1}]
    with pytest.raises(ValidationError): RelativeRequest.model_validate(body)
    body = payload(); body["categories"] = [{**category(False), "id": str(i), "definition": "字" * 1000} for i in range(24)]
    with pytest.raises(ValidationError): RelativeRequest.model_validate(body)


def test_real_model_boundary_contains_text_and_saved_scores_but_no_local_time():
    request = RelativeRequest.model_validate(payload(True))
    model = FakeModelClient([raw_output(json.dumps(result(True), ensure_ascii=False))])
    value = asyncio.run(analyze_relative(model, request))
    assert value.items[0].difficulty == 1.5
    assert json.loads(model.calls[0][1]) == request.model_dump(by_alias=True, exclude_none=True)
    assert model.calls[0][0].thinking_mode == "enabled"
    assert model.calls[0][0].reasoning_effort == "high"
    bad = result(True); bad["anchorTaskID"] = "changed"
    model = FakeModelClient([raw_output(json.dumps(bad))])
    with pytest.raises(ModelGatewayResponseError): asyncio.run(analyze_relative(model, request))


@pytest.mark.parametrize("score", [False, True], ids=["classify", "score"])
def test_relative_operations_enable_high_effort_without_exposing_reasoning(monkeypatch, settings, score):
    captured = {}

    async def fake_post(self, url, **kwargs):
        captured.update(kwargs["json"])
        assert self.timeout.read == 45
        return httpx.Response(200, json={"model": "primary-model", "choices": [{"message": {
            "content": json.dumps(result(score), ensure_ascii=False),
            "reasoning_content": "internal reasoning must stay private"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    value = asyncio.run(analyze_relative(LiteLLMClient(settings), RelativeRequest.model_validate(payload(score))))
    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == "high"
    assert captured["model"] == "primary-model"
    assert captured["max_tokens"] == 6000
    assert json.loads(captured["messages"][-1]["content"])["operation"] == ("score" if score else "classify")
    assert "internal reasoning" not in value.model_dump_json()
    assert PIPELINES["time-fragment-plan-v2"].thinking_mode == "disabled"
    assert PROFILE_PIPELINE.thinking_mode == "disabled"


class RelativeBackend(Backend):
    async def relative_duration(self, actor, data, attempt, request_id):
        self.calls.append(("relative", actor, data, attempt, request_id))
        return self.response


def test_device_only_and_quota_denial_do_not_call_model(configuration):
    backend = RelativeBackend(result())
    with TestClient(create_account_api(configuration, backend)) as client:
        assert client.post("/api/duration/relative-analysis", headers=account_headers(), json=payload()).status_code == 401
        assert backend.calls == []
        headers = device_headers(client, backend); backend.quota_error = failure("AI_QUOTA_EXHAUSTED", 429)
        assert client.post("/api/duration/relative-analysis", headers=headers, json=payload()).status_code == 429
        assert [call[0] for call in backend.calls] == ["reserve"]


@pytest.mark.parametrize("valid", [True, False])
def test_quota_finishes_once_and_invalid_response_refunds(configuration, valid):
    response = result(); response["requestID"] = "relative-1" if valid else "wrong"
    backend = RelativeBackend(response)
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/api/duration/relative-analysis", headers=device_headers(client, backend), json=payload())
    assert response.status_code == (200 if valid else 502)
    assert [call[0] for call in backend.calls] == ["reserve", "relative", "finish"]
    assert backend.calls[-1][2]["consume"] is valid


def test_internal_signature_and_extractor_boundary(settings):
    model = FakeModelClient([raw_output(json.dumps(result(), ensure_ascii=False))])
    app = create_app(settings.model_copy(update={"planning_internal_secret": SecretStr(SECRET), "planning_internal_only": True}), model)
    body = RelativeRequest.model_validate(payload()).model_dump(mode="json", by_alias=True, exclude_none=True)
    with TestClient(app) as client:
        route = "/internal/time-fragment/relative-analysis"
        assert client.post(route, json=body).status_code == 401
        assert model.calls == []
        token = PlanningCredentials(SECRET).issue("device:test", body, "attempt")
        response = client.post(route, json=body, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert response.json()["items"][0]["categoryID"] == "script"


def test_proxy_signs_exact_payload_and_preserves_trace(configuration):
    def receive(request):
        body = json.loads(request.content)
        assert body == payload()
        claims = PlanningCredentials(SECRET).verify(request.headers["Authorization"].removeprefix("Bearer "), body)
        assert claims["sub"] == "device:test"
        assert request.url.path == "/internal/time-fragment/relative-analysis"
        assert request.headers["X-Request-ID"] == "trace"
        return httpx.Response(200, json=result())
    async def run():
        backend = AccountBackend(configuration, httpx.MockTransport(receive))
        try: return await backend.relative_duration(Actor("device:test"), payload(), "attempt", "trace")
        finally: await backend.close()
    assert asyncio.run(run()) == result()


def test_anchor_identity_cannot_be_rescored_or_conflict_with_references():
    body = payload(True); body["tasks"] = [text("baseline")]
    response = result(True); response["items"][0]["id"] = "baseline"
    with pytest.raises(ValueError): validated(body, response)
    body = payload(True); body["references"] = [{**text("baseline"), "categoryID": "script", "difficulty": 2}]
    with pytest.raises(ValueError): RelativeRequest.model_validate(body)
    body["categories"] = [category(False)]
    with pytest.raises(ValueError): RelativeRequest.model_validate(body)


def test_edited_anchor_task_uses_new_text_but_preserves_original_baseline():
    body = payload(True)
    body["tasks"] = [{**text("baseline"), "title": "新增多个资料源交叉查证的口播稿"}]
    response = result(True)
    response["items"][0].update(id="baseline", difficulty=2)
    assert validated(body, response).items[0].difficulty == 2
    assert body["categories"][0]["anchor"]["difficulty"] == 1
    body["tasks"][0]["difficulty"] = 2
    assert validated(body, response).items[0].difficulty == 2
    body["tasks"][0] = {**text("baseline"), "difficulty": 2}
    with pytest.raises(ValueError): RelativeRequest.model_validate(body)
    body = payload(True)
    body["references"] = [{**text("baseline"), "title": "修改后重新完成的口播稿", "categoryID": "script", "difficulty": 2}]
    assert RelativeRequest.model_validate(body).references[0].difficulty == 2
