import asyncio
import base64
import hashlib
import hmac
import json
import logging
import sqlite3
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.account_api import create_account_api
from app.account_backend import AccountAPISettings, AccountBackend, Actor, failure, support_code
from app.factory import create_app
from app.guest_auth import GuestTokenCodec
from app.migrate_guest_quota import snapshots
from app.planning_auth import PlanningCredentials, body_hash
from app.quota_store import QuotaStore
from app.usage_store import UsageStore
from app.model_client import ModelGatewayUnavailable, ModelGatewayResponseError
from test_time_fragment_api import (settings, FakeModelClient, request_payload, operations_output,
                                    raw_output, guest_headers, TOKEN_SECRET, ADMIN_KEY, API_KEY)

SECRET = "independent-private-planning-secret-with-32-characters"
ACCOUNT = Actor("account:" + str(uuid4()), str(uuid4()))
DEVICE = "development-test-installation-1234"


@pytest.fixture
def configuration():
    return AccountAPISettings(supabase_url="https://project.supabase.co", supabase_publishable_key="public-key",
        supabase_service_role_key="server-secret", planning_internal_secret=SECRET,
        time_fragment_token_secret=TOKEN_SECRET, admin_api_key=ADMIN_KEY,
        time_fragment_development_device_ids=DEVICE)


class Backend:
    def __init__(self, response=None):
        self.calls=[]
        self.response=response or {"requestID":"app-request-1","proposal":None,
                                  "validation":{"valid":False,"issues":[{"source":"model_server","severity":"error",
                                      "code":"PARSE_FAILED","message":"Cannot parse model output"}],"attempts":2}}
        self.quota_error=None

    async def close(self):
        pass

    async def account(self, token):
        self.calls.append(("account",token))
        if token != "signed.account.token":
            raise failure("UNAUTHORIZED",401)
        return ACCOUNT

    async def quota(self, action, actor=None, diagnostic_request_id=None, **data):
        self.calls.append((action,actor,data))
        if self.quota_error:
            raise self.quota_error
        return {"supportCode":"TF-AAAA-AAAA","used":1,"limit":50,"remaining":49,"enabled":False}

    async def plan(self, actor, payload, attempt, request_id):
        self.calls.append(("plan",actor,payload,attempt,request_id))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def account_headers():
    return {"Authorization":"Bearer signed.account.token"}


def test_account_resolved_before_quota_and_unusable_output_refunds(configuration):
    backend=Backend()
    with TestClient(create_account_api(configuration,backend)) as client:
        response=client.post("/api/plan/parse",headers={**account_headers(),"X-Request-ID":"forward-me-123"},json=request_payload())
    assert response.status_code==200
    assert [call[0] for call in backend.calls]==["account","reserve","plan","finish"]
    assert backend.calls[1][1]==ACCOUNT
    assert backend.calls[1][2]["bodyHash"]==body_hash(request_payload())
    assert backend.calls[3][2]["consume"] is False
    assert backend.calls[1][2]["attempt"]==backend.calls[3][2]["attempt"]
    assert backend.calls[2][4]=="forward-me-123"
    assert response.headers["cache-control"]=="no-store"


def test_successful_proposal_consumes_once_after_real_planner(configuration,settings):
    model=FakeModelClient([operations_output([])])
    internal=create_app(settings.model_copy(update={"planning_internal_secret":configuration.planning_internal_secret,
                                                    "planning_internal_only":True}),model)
    payload=request_payload();cred=PlanningCredentials(SECRET).issue(ACCOUNT.principal,payload,str(uuid4()))
    with TestClient(internal) as planner:
        result=planner.post("/internal/time-fragment/plan",json=payload,headers={"Authorization":"Bearer "+cred})
        assert result.status_code==200
        assert result.json()["proposal"] is not None
    backend=Backend(result.json())
    with TestClient(create_account_api(configuration,backend)) as client:
        response=client.post("/api/plan/parse",headers=account_headers(),json=payload)
    assert response.status_code==200
    assert backend.calls[-1][2]["consume"] is True
    assert len(model.calls)==1


@pytest.mark.parametrize("outcome,status",[(failure("MODEL_GATEWAY_UNAVAILABLE",503),503),({"malformed":True},502)])
def test_failed_or_malformed_planner_refunds(configuration,outcome,status):
    backend=Backend(outcome)
    with TestClient(create_account_api(configuration,backend)) as client:
        response=client.post("/api/plan/parse",headers=account_headers(),json=request_payload())
    assert response.status_code==status
    assert backend.calls[-1][0]=="finish" and backend.calls[-1][2]["consume"] is False


@pytest.mark.parametrize("code,status",[("AI_QUOTA_EXHAUSTED",429),("AI_REQUEST_IN_PROGRESS",409),
                                       ("AI_REQUEST_ALREADY_COMPLETED",409),("AI_REQUEST_ID_CONFLICT",409)])
def test_quota_denial_never_calls_planner(configuration,code,status):
    backend=Backend();backend.quota_error=failure(code,status)
    with TestClient(create_account_api(configuration,backend)) as client:
        response=client.post("/api/plan/parse",headers=account_headers(),json=request_payload())
    assert response.status_code==status
    assert [call[0] for call in backend.calls]==["account","reserve"]


def test_invalid_input_or_guest_signature_never_reserves(configuration):
    backend=Backend()
    with TestClient(create_account_api(configuration,backend)) as client:
        assert client.post("/api/plan/parse",headers=account_headers(),json={}).status_code==422
        token=GuestTokenCodec("wrong-secret",300).issue(DEVICE)
        assert client.post("/api/plan/parse",headers={"Authorization":"Bearer "+token},json=request_payload()).status_code==401
    assert not any(call[0] in ("reserve","plan") for call in backend.calls)


def test_legacy_guest_contracts_preserved_and_development_endpoint_retired(configuration):
    backend=Backend()
    with TestClient(create_account_api(configuration,backend)) as client:
        headers=guest_headers(client,DEVICE)
        assert client.post("/api/development/membership",headers=headers,json={"enabled":True}).status_code==404
        assert client.get("/api/development/membership",headers=account_headers()).status_code==404
        assert client.get("/api/auth/guest",headers=headers).status_code==405


def test_claim_requires_both_account_and_signed_guest(configuration):
    backend=Backend();token=GuestTokenCodec(TOKEN_SECRET,300).issue(DEVICE)
    with TestClient(create_account_api(configuration,backend)) as client:
        for _ in range(2):
            assert client.post("/api/account/claim-guest",headers=account_headers(),json={"guest_token":token}).status_code==200
        assert client.post("/api/account/claim-guest",headers=account_headers(),json={"guest_token":"forged"}).status_code==401
        assert client.post("/api/account/claim-guest",headers={"Authorization":"Bearer "+token},json={"guest_token":token}).status_code==401
    claims=[call for call in backend.calls if call[0]=="claim"]
    assert len(claims)==2
    assert claims[0][1]==ACCOUNT
    assert claims[0][2]["guest"]==GuestTokenCodec.device_key(DEVICE)


def test_admin_quota_still_requires_admin_key(configuration):
    backend=Backend()
    with TestClient(create_account_api(configuration,backend)) as client:
        assert client.post("/admin/time-fragment/quotas/reset-all",headers=account_headers()).status_code==401
        # Raw non-ASCII header bytes decode to non-ASCII text; the key comparison
        # must reject them instead of raising inside secrets.compare_digest.
        raw=b"Bearer \xe7\xae\xa1\xe7\x90\x86\xe5\x91\x98"
        assert client.post("/admin/time-fragment/quotas/reset-all",headers={"Authorization":raw}).status_code==401
        backend.quota_error=failure("AI_REQUEST_IN_PROGRESS",409)
        assert client.post("/admin/time-fragment/quotas/reset-all",headers={"Authorization":"Bearer "+ADMIN_KEY}).status_code==409


def test_request_id_is_echoed_when_valid_and_replaced_when_invalid(configuration):
    backend=Backend()
    with TestClient(create_account_api(configuration,backend)) as client:
        echoed=client.get("/health/live",headers={"X-Request-ID":"smoke-request-123"})
        assert echoed.headers["x-request-id"]=="smoke-request-123"
        assert echoed.headers["cache-control"]=="no-store"
        replaced=client.get("/health/live",headers={"X-Request-ID":"bad id!"})
        assert replaced.headers["x-request-id"]!="bad id!"
        assert len(replaced.headers["x-request-id"])==36


def test_rpc_uses_verified_identity_and_retries_same_attempt_without_leaking_keys(configuration):
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("ai_account_identity"):
            assert request.headers["apikey"]=="public-key"
            assert request.headers["authorization"]=="Bearer signed.account.token"
            return httpx.Response(200,json={"userID":ACCOUNT.principal[8:],"sessionID":ACCOUNT.session_id})
        assert request.headers["apikey"]=="server-secret"
        if len(requests)==2:
            raise httpx.ReadTimeout("deliberately ambiguous")
        return httpx.Response(200,json={"attempt":"unchanged"})
    async def run():
        backend=AccountBackend(configuration,httpx.MockTransport(handler))
        try:
            actor=await backend.account("signed.account.token")
            assert actor==ACCOUNT
            await backend.quota("reserve",actor,attempt="unchanged",requestID="one",bodyHash="a"*64)
        finally:
            await backend.close()
    asyncio.run(run())
    assert requests[1].content==requests[2].content
    body=json.loads(requests[1].content)["p_data"]
    assert body["principal"]==ACCOUNT.principal and body["sessionID"]==ACCOUNT.session_id


@pytest.mark.parametrize("status,expected",[(401,401),(403,401),(500,503)])
def test_authentication_fails_closed_and_sanitizes_upstream_errors(configuration,status,expected):
    async def run():
        backend=AccountBackend(configuration,httpx.MockTransport(lambda _:httpx.Response(status,text="server-secret")))
        try:
            with pytest.raises(Exception) as caught:
                await backend.account("invalid.token.signature")
            assert caught.value.status_code==expected
            assert "server-secret" not in str(caught.value.detail)
        finally:
            await backend.close()
    asyncio.run(run())


def test_supabase_failure_log_has_operation_class_and_no_response_body(configuration, caplog):
    async def run():
        backend = AccountBackend(configuration, httpx.MockTransport(
            lambda _: httpx.Response(503, text="private-upstream-body")
        ))
        try:
            with pytest.raises(Exception):
                await backend.quota("reserve", ACCOUNT, diagnostic_request_id="probe-run-123-plan",
                                    requestID="private-logical-request")
        finally:
            await backend.close()
    with caplog.at_level(logging.WARNING, logger="app.account_backend"):
        asyncio.run(run())
    record = json.loads(caplog.records[-1].message)
    assert record["event"] == "account_backend_failure"
    assert record["component"] == "supabase"
    assert record["operation"] == "ai_quota_service.reserve"
    assert record["errorClass"] == "http_status"
    assert record["upstreamStatus"] == 503
    assert record["requestID"] == "probe-run-123-plan"
    assert record["attempts"] == 1 and record["durationMs"] >= 0
    assert "private" not in caplog.records[-1].message


def test_supabase_timeout_log_records_bounded_retry_count(configuration, caplog):
    async def run():
        backend = AccountBackend(configuration, httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(httpx.ReadTimeout("private-timeout-detail"))
        ))
        try:
            with pytest.raises(Exception):
                await backend.quota("reserve", ACCOUNT)
        finally:
            await backend.close()
    with caplog.at_level(logging.WARNING, logger="app.account_backend"):
        asyncio.run(run())
    record = json.loads(caplog.records[-1].message)
    assert record["errorClass"] == "timeout" and record["attempts"] == 2
    assert "upstreamStatus" not in record and "private" not in caplog.records[-1].message


def test_internal_planning_failure_log_keeps_trace_and_upstream_code(configuration, caplog):
    async def run():
        backend = AccountBackend(configuration, httpx.MockTransport(
            lambda _: httpx.Response(503, json={"detail": {
                "code": "MODEL_GATEWAY_UNAVAILABLE", "message": "private-model-detail"}})
        ))
        try:
            with pytest.raises(Exception):
                await backend.plan(ACCOUNT, request_payload(), "attempt", "probe-run-123-plan")
        finally:
            await backend.close()
    with caplog.at_level(logging.WARNING, logger="app.account_backend"):
        asyncio.run(run())
    record = json.loads(caplog.records[-1].message)
    assert record["component"] == "planning" and record["operation"] == "internal_time_fragment_plan"
    assert record["requestID"] == "probe-run-123-plan"
    assert record["upstreamStatus"] == 503 and record["upstreamCode"] == "MODEL_GATEWAY_UNAVAILABLE"
    assert "private" not in caplog.records[-1].message


def test_account_api_failure_log_keeps_http_trace_and_safe_code(configuration, caplog):
    backend = Backend()
    backend.quota_error = failure("ACCOUNT_SERVICE_UNAVAILABLE", 503, message="private-upstream-message")
    with caplog.at_level(logging.WARNING, logger="app.account_api"):
        with TestClient(create_account_api(configuration, backend)) as client:
            response = client.post("/api/plan/parse", headers={**account_headers(), "X-Request-ID": "probe-run-123-plan"},
                                   json=request_payload())
    assert response.status_code == 503
    record = json.loads(caplog.records[-1].message)
    assert record == {"event": "account_api_failure", "requestID": "probe-run-123-plan",
                      "method": "POST", "route": "/api/plan/parse", "status": 503,
                      "code": "ACCOUNT_SERVICE_UNAVAILABLE"}
    assert "private" not in caplog.records[-1].message


def test_account_api_failure_log_rejects_unknown_uppercase_code(configuration, caplog):
    backend = Backend()
    backend.quota_error = failure("PRIVATE_SECRET", 503)
    with caplog.at_level(logging.WARNING, logger="app.account_api"):
        with TestClient(create_account_api(configuration, backend)) as client:
            response = client.post("/api/plan/parse", headers=account_headers(), json=request_payload())
    assert response.status_code == 503
    record = json.loads(caplog.records[-1].message)
    assert record["code"] == "HTTP_ERROR"
    assert "PRIVATE_SECRET" not in caplog.records[-1].message


def test_internal_planner_rejects_guest_business_token_changed_body_and_old_public_route(settings,configuration):
    model=FakeModelClient([])
    internal=settings.model_copy(update={"planning_internal_secret":configuration.planning_internal_secret,"planning_internal_only":True})
    payload=request_payload();cred=PlanningCredentials(SECRET).issue(ACCOUNT.principal,payload,str(uuid4()))
    with TestClient(create_app(internal,model)) as client:
        for token in (GuestTokenCodec(TOKEN_SECRET,300).issue(DEVICE),API_KEY,"invalid",""):
            assert client.post("/internal/time-fragment/plan",json=payload,headers={"Authorization":"Bearer "+token}).status_code==401
        assert client.post("/internal/time-fragment/plan",json=request_payload(text="changed"),headers={"Authorization":"Bearer "+cred}).status_code==401
        assert client.post("/api/auth/guest",json={"device_id":DEVICE}).status_code==404
        assert client.post("/api/plan/parse",json=payload).status_code==404
        assert client.post("/v1/pipelines/time-fragment-plan-v2:run",json={"input":"bypass"},headers={"Authorization":"Bearer "+API_KEY}).status_code==403
    assert not model.calls


def test_internal_credential_expiry_and_future_issue_time(monkeypatch):
    codec=PlanningCredentials(SECRET);body=request_payload()
    monkeypatch.setattr("app.planning_auth.time.time",lambda:1000)
    token=codec.issue(ACCOUNT.principal,body,str(uuid4()))
    assert codec.verify(token,body)["sub"]==ACCOUNT.principal
    monkeypatch.setattr("app.planning_auth.time.time",lambda:1060)
    with pytest.raises(ValueError):codec.verify(token,body)
    monkeypatch.setattr("app.planning_auth.time.time",lambda:999)
    with pytest.raises(ValueError):codec.verify(token,body)


def test_configuration_requires_independent_internal_secret(settings,configuration):
    with pytest.raises(ValueError,match="independent"):
        AccountAPISettings(**{**configuration.model_dump(),"planning_internal_secret":TOKEN_SECRET})
    with pytest.raises(ValueError,match="own credential"):
        create_app(settings.model_copy(update={"planning_internal_only":True}))


def test_correctly_signed_other_audience_is_not_a_planning_credential():
    codec=PlanningCredentials(SECRET);body=request_payload()
    token=codec.issue(ACCOUNT.principal,body,str(uuid4()))
    encoded=token.split(".")[0]
    claims=json.loads(base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)))
    claims["aud"]="different-service"
    encoded=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    signature=hmac.new(SECRET.encode(),encoded.encode(),hashlib.sha256).hexdigest()
    with pytest.raises(ValueError):codec.verify(encoded+"."+signature,body)


def test_quota_denial_keeps_legacy_error_message_and_support_code(configuration):
    async def run():
        backend=AccountBackend(configuration,httpx.MockTransport(lambda _:httpx.Response(200,json={
            "code":"AI_QUOTA_EXHAUSTED","supportCode":"TF-AAAA-AAAA","remaining":0,"limit":50,"period":"free"})))
        try:
            with pytest.raises(Exception) as error:await backend.quota("reserve",ACCOUNT)
            assert error.value.status_code==429
            assert error.value.detail["message"]=="本轮内测的 AI 额度已用完，请将支持码发给开发者刷新。"
            assert error.value.detail["supportCode"]=="TF-AAAA-AAAA"
            assert "period" not in error.value.detail
        finally:await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("case,status,calls",[("past",422,0),("oversized",413,0),("outage",503,1),
                                             ("gateway_error",502,1),("unparseable",200,2)])
def test_internal_planner_keeps_validation_and_failure_boundaries(settings,configuration,case,status,calls):
    outputs={"past":[],"oversized":[],"outage":[ModelGatewayUnavailable("private-upstream-url")],
             "gateway_error":[ModelGatewayResponseError("private-provider-body")],
             "unparseable":[raw_output("not-json"),raw_output("not-json")]}
    model=FakeModelClient(outputs[case])
    configured=settings.model_copy(update={"planning_internal_secret":configuration.planning_internal_secret,
                                          "max_input_chars":1 if case=="oversized" else settings.max_input_chars})
    payload=request_payload(date="2026-08-23" if case=="past" else "2026-08-24")
    token=PlanningCredentials(SECRET).issue(ACCOUNT.principal,payload,str(uuid4()))
    with TestClient(create_app(configured,model)) as client:
        result=client.post("/internal/time-fragment/plan",json=payload,headers={"Authorization":"Bearer "+token})
    assert result.status_code==status
    assert len(model.calls)==calls
    assert "private-upstream" not in result.text and "private-provider" not in result.text
    if case=="unparseable":assert result.json()["proposal"] is None


def test_internal_observability_retains_usage_but_not_account_content(settings,configuration,tmp_path):
    path=tmp_path/"usage.sqlite3"
    store=UsageStore(str(path),30)
    model=FakeModelClient([operations_output([],usage={"prompt_tokens":10,"completion_tokens":20,"total_tokens":30})])
    payload=request_payload(text="PRIVATE_ACCOUNT_TEXT")
    cred=PlanningCredentials(SECRET).issue(ACCOUNT.principal,payload,str(uuid4()))
    with TestClient(create_app(settings.model_copy(update={"planning_internal_secret":configuration.planning_internal_secret}),model,usage_store=store)) as client:
        assert client.post("/internal/time-fragment/plan",json=payload,headers={"Authorization":"Bearer "+cred}).status_code==200
    with sqlite3.connect(path) as db:
        dump="\n".join(db.iterdump())
        assert "PRIVATE_ACCOUNT_TEXT" not in dump
        assert ACCOUNT.principal in dump


def test_legacy_snapshot_preserves_free_member_counts_and_receipts(tmp_path):
    path=tmp_path/"quota.sqlite3";principal=GuestTokenCodec.device_key(DEVICE)
    quotas=QuotaStore(str(path),50,development_principals=frozenset({principal}))
    quotas.consume(quotas.reserve(principal,"free-one"))
    quotas.set_membership(principal,True)
    quotas.consume(quotas.reserve(principal,"member-one"))
    pending=quotas.reserve(principal,"pending")
    with pytest.raises(RuntimeError,match="in flight"):snapshots(path,50)
    quotas.refund(pending);quotas.close()
    first=snapshots(path,50)[0];second=snapshots(path,50)[0]
    assert first==second
    assert first["developmentEnabled"] is True
    assert first["completedRequests"]==["free-one","member-one"]
    assert {b["period"]:b["used"] for b in first["buckets"]}["free"]==1
    assert sum(b["used"] for b in first["buckets"])==2
    assert first["supportCode"]==support_code(principal)
