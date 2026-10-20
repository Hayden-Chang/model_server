import asyncio
import json

import pytest
import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.account_api import create_account_api
from app.account_backend import AccountBackend, Actor
from app.duration_profiles import DurationProfileRequest, extract_profiles
from app.factory import create_app
from app.model_client import ModelGatewayResponseError
from app.planning_auth import PlanningCredentials
from test_account_api import Backend, configuration, device_headers, account_headers, SECRET
from test_time_fragment_api import FakeModelClient, raw_output, settings


def payload():
    return {"requestID": "profile-1", "tasks": [
        {"id": "a", "title": "剪辑2分钟教程", "note": "素材已齐，做过同类"}]}


def extraction():
    return {"items": [{"id": "a", "profile": {
        "kind": "videoEditing", "amount": 2, "preparation": "ready",
        "familiarity": "familiar", "scope": "wholeTask"},
        "evidence": {"kind": "剪辑", "amount": "2分钟", "preparation": "素材已齐",
                     "familiarity": "做过同类"}}]}


def run_extract(data):
    model = FakeModelClient([raw_output(json.dumps(data, ensure_ascii=False))])
    result = asyncio.run(extract_profiles(model, DurationProfileRequest.model_validate(payload())))
    return result, model


def test_extracts_explicit_conditions_without_sending_allocations_or_history():
    result, model = run_extract(extraction())
    assert result.items[0].profile.amount == 2
    assert result.items[0].profile.familiarity == "familiar"
    assert json.loads(model.calls[0][1]) == {"tasks": payload()["tasks"]}
    assert model.calls[0][0].temperature == 0


def test_ungrounded_conditions_are_unknown_not_invented():
    data = extraction()
    data["items"][0]["evidence"] = dict.fromkeys(["kind", "amount", "preparation", "familiarity"], "not in task")
    result, _ = run_extract(data)
    assert result.items[0].profile.kind == "unknown"
    assert result.items[0].profile.amount is None
    assert result.items[0].profile.preparation is None


@pytest.mark.parametrize("title,note,profile,evidence", [
    ("专注开发30分钟", "",
     {"kind": "development", "amount": None, "preparation": None, "familiarity": None},
     {"kind": "开发", "amount": None, "preparation": None, "familiarity": None}),
    ("花30分钟剪辑2分钟教程", "素材已齐，做过同类",
     {"kind": "videoEditing", "amount": 2, "preparation": "ready", "familiarity": "familiar"},
     {"kind": "剪辑", "amount": "2分钟", "preparation": "素材已齐", "familiarity": "做过同类"}),
])
def test_time_budget_does_not_publish_whole_task_comparison_traits(title, note, profile, evidence):
    request = DurationProfileRequest(requestID="budget-profile", tasks=[
        {"id": "budget", "title": title, "note": note}])
    output = {"items": [{"id": "budget", "profile": {**profile, "scope": "timeBudget"},
                         "evidence": evidence}]}
    model = FakeModelClient([raw_output(json.dumps(output, ensure_ascii=False))])
    result = asyncio.run(extract_profiles(model, request))
    assert result.items[0].profile.model_dump() == {
        "kind": "unknown", "amount": None, "preparation": None,
        "familiarity": None, "scope": "timeBudget"}
    assert len(model.calls) == 1


@pytest.mark.parametrize("items", [[], [{"id": "other"}], extraction()["items"] * 2])
def test_missing_duplicate_and_foreign_id_fail_closed(items):
    with pytest.raises(ModelGatewayResponseError):
        run_extract({"items": items})


def test_contract_rejects_time_input_duplicates_and_excessive_batches():
    for update in [
        {"tasks": [{**payload()["tasks"][0], "plannedMinutes": 60}]},
        {"tasks": payload()["tasks"] * 2},
        {"tasks": [{**payload()["tasks"][0], "id": str(i)} for i in range(33)]},
    ]:
        with pytest.raises(ValidationError):
            DurationProfileRequest.model_validate({**payload(), **update})


class ProfileBackend(Backend):
    async def duration_profiles(self, actor, data, attempt, request_id):
        self.calls.append(("profile", actor, data, attempt, request_id))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def response_body():
    return {"requestID": "profile-1", "items": [
        {key: value for key, value in extraction()["items"][0].items() if key != "evidence"}]}


def test_device_only_and_quota_denial_do_not_call_model(configuration):
    backend = ProfileBackend(response_body())
    with TestClient(create_account_api(configuration, backend)) as client:
        assert client.post("/api/duration/profiles", headers=account_headers(), json=payload()).status_code == 401
        assert not backend.calls  # Device-only dependency rejects an account token before account lookup.
        headers = device_headers(client, backend)
        from app.account_backend import failure
        backend.quota_error = failure("AI_QUOTA_EXHAUSTED", 429)
        assert client.post("/api/duration/profiles", headers=headers, json=payload()).status_code == 429
        assert [call[0] for call in backend.calls] == ["reserve"]


@pytest.mark.parametrize("response,status,consume", [
    (response_body(), 200, True),
    ({"requestID": "wrong", "items": response_body()["items"]}, 502, False),
    ({"requestID": "profile-1", "items": []}, 502, False),
])
def test_quota_finishes_once_and_invalid_responses_refund(configuration, response, status, consume):
    backend = ProfileBackend(response)
    with TestClient(create_account_api(configuration, backend)) as client:
        result = client.post("/api/duration/profiles", headers=device_headers(client, backend), json=payload())
    assert result.status_code == status
    assert [call[0] for call in backend.calls] == ["reserve", "profile", "finish"]
    assert backend.calls[-1][2]["consume"] is consume
    assert backend.calls[0][2]["attempt"] == backend.calls[-1][2]["attempt"]


def test_internal_signature_and_real_extractor_boundary(settings):
    model = FakeModelClient([raw_output(json.dumps(extraction(), ensure_ascii=False))])
    app = create_app(settings.model_copy(update={"planning_internal_secret": SecretStr(SECRET),
                                                "planning_internal_only": True}), model)
    with TestClient(app) as client:
        route = "/internal/time-fragment/duration-profiles"
        assert client.post(route, json=payload()).status_code == 401
        assert not model.calls
        token = PlanningCredentials(SECRET).issue("device:test", payload(), "attempt-1")
        result = client.post(route, json=payload(), headers={"Authorization": "Bearer " + token})
    assert result.status_code == 200
    assert result.json() == response_body()
    assert len(model.calls) == 1


def test_proxy_signs_exact_payload_and_preserves_trace_without_account_credentials(configuration):
    calls = []
    def receive(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body == payload()
        claims = PlanningCredentials(SECRET).verify(
            request.headers["Authorization"].removeprefix("Bearer "), body)
        assert claims["sub"] == "device:duration-test"
        assert claims["attempt"] == "attempt-profile"
        assert request.url.path == "/internal/time-fragment/duration-profiles"
        assert request.headers["X-Request-ID"] == "trace-profile"
        return httpx.Response(200, json=response_body())
    async def run():
        backend = AccountBackend(configuration, httpx.MockTransport(receive))
        try:
            return await backend.duration_profiles(
                Actor("device:duration-test"), payload(), "attempt-profile", "trace-profile")
        finally:
            await backend.close()
    assert asyncio.run(run()) == response_body()
    assert len(calls) == 1
