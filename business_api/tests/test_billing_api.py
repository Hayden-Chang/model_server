import asyncio
import json
import logging
import re

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from uuid import uuid4

from app.account_api import create_account_api
from app.account_backend import (SAFE_ERROR_CODES, AccountAPISettings, AccountBackend,
                                 Actor, failure)
from app.guest_auth import GuestTokenCodec
from test_time_fragment_api import TOKEN_SECRET, ADMIN_KEY

SECRET = "independent-private-planning-secret-with-32-characters"
DEVICE_ID = "time-fragment-ios-device-1234"
# The billing subject is the device principal -- ^guest_[a-f0-9]{24}$ -- the
# only shape public.billing_service admits (202609170017).
DEVICE = GuestTokenCodec.device_key(DEVICE_ID)
DEVICE_ACTOR = Actor(DEVICE)
PRODUCT = "com.hayden.daymosaic.plus.monthly"
CLAIM_ID = "11111111-1111-1111-1111-111111111111"
TOKEN_ID = "22222222-2222-2222-2222-222222222222"
CLAIM_RESPONSE = {"claimId": CLAIM_ID, "appAccountToken": TOKEN_ID,
                  "expiresAt": "2026-09-11T13:00:00.000Z"}
# Free quota unified on 30 (202609130015, re-issued by 202609170017).
ENTITLEMENT_RESPONSE = {"plan": "free", "status": "expired", "validUntil": None,
                        "serviceEndAt": None, "entitlementRevision": 0,
                        "aiQuota": {"limit": 30, "used": 3, "remaining": 27, "resetsAt": None},
                        "billingSources": []}


@pytest.fixture
def configuration():
    return AccountAPISettings(supabase_url="https://project.supabase.co",
        supabase_publishable_key="public-key", supabase_service_role_key="server-secret",
        planning_internal_secret=SECRET, time_fragment_token_secret=TOKEN_SECRET,
        admin_api_key=ADMIN_KEY, time_fragment_development_device_ids="")


class BillingBackend:
    def __init__(self, *, error=None, minted_claim_id=CLAIM_ID):
        self.calls = []
        self.account_calls = []
        self.error = error
        # What the RPC answers with when the client omitted claimId; 202609170017
        # mints it server-side, so Python must echo this value, never its own.
        self.minted_claim_id = minted_claim_id

    async def close(self):
        pass

    async def account(self, token):
        # The billing routes must never resolve a Supabase session any more, so
        # reaching this method at all is the regression the tests look for.
        self.account_calls.append(token)
        return Actor("account:" + str(uuid4()), str(uuid4()))

    async def billing(self, action, actor, **data):
        self.calls.append((action, actor, data))
        if self.error:
            raise self.error
        if action == "claim_register":
            response = dict(CLAIM_RESPONSE)
            response["claimId"] = data.get("claimId") or self.minted_claim_id
            return response
        return dict(ENTITLEMENT_RESPONSE)


def device_headers():
    return {"Authorization": "Bearer " + GuestTokenCodec(TOKEN_SECRET, 3600).issue(DEVICE_ID)}


def _rpc_backend(configuration, responses):
    """A real AccountBackend whose RPC replies are scripted, in order."""
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    return AccountBackend(configuration, httpx.MockTransport(handler)), seen


def test_claims_requires_authentication(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", json={
            "provider": "apple", "productId": PRODUCT})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHORIZED"
    assert backend.calls == []
    assert backend.account_calls == []


def test_claims_reject_account_sessions_before_reaching_the_store(configuration):
    # Inverted by M4 (design §4): billing used to require a Supabase account and
    # reject guests with ACCOUNT_REQUIRED. The subject is now the device
    # principal, so it is the account session that is refused locally.
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", headers={
            "Authorization": "Bearer signed.account.token"}, json={
            "provider": "apple", "productId": PRODUCT})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "DEVICE_REQUIRED"
    assert backend.calls == []
    # The session was rejected by the dependency, not looked up as an account.
    assert backend.account_calls == []


def test_claims_reject_a_forged_device_token(configuration):
    # A single-dot token is only accepted when the HMAC verifies: the dot count
    # is a discriminator, not the authentication.
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", headers={
            "Authorization": "Bearer not-a-real-device-token.signature"}, json={
            "provider": "apple", "productId": PRODUCT})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHORIZED"
    assert backend.calls == []


def test_device_token_is_accepted_and_resolves_to_the_device_principal(configuration):
    # The accept path the inverted rejection test above is the counterpart of:
    # the same guest HMAC token that used to be refused now authenticates every
    # /billing/* route and yields the principal the RPC admits.
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.get("/billing/entitlement", headers=device_headers())
    assert response.status_code == 200
    action, actor, data = backend.calls[0]
    assert (action, data) == ("entitlement", {})
    # The RPC's admission gate is ^guest_[a-f0-9]{24}$ (202609170017:222), so the
    # shape matters, not just the prefix.
    assert re.fullmatch(r"guest_[a-f0-9]{24}", actor.principal)
    assert actor.session_id is None
    assert backend.account_calls == []


def test_claim_register_returns_account_token_and_expires_at(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", headers=device_headers(), json={
            "provider": "apple", "productId": PRODUCT})
    assert response.status_code == 200
    assert response.json() == CLAIM_RESPONSE
    assert backend.calls == [("claim_register", DEVICE_ACTOR, {
        "provider": "apple", "productId": PRODUCT, "claimId": None})]


def test_claim_register_passes_a_missing_claim_id_through(configuration):
    # 202609170017 mints the claim id server-side when the client omits it
    # (M0.4). Python must pass null through and echo the RPC's id, never mint
    # one itself and never treat the omission as an error. The minted id is
    # deliberately a value Python cannot know, so echoing it is discriminating.
    minted = str(uuid4())
    backend = BillingBackend(minted_claim_id=minted)
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", headers=device_headers(), json={
            "provider": "apple", "productId": PRODUCT, "claimId": None})
    assert response.status_code == 200
    assert backend.calls[0][2]["claimId"] is None
    assert response.json()["claimId"] == minted


def test_claim_register_accepts_client_claim_id_and_is_idempotent(configuration):
    backend = BillingBackend()
    claim = str(uuid4())
    with TestClient(create_account_api(configuration, backend)) as client:
        first = client.post("/billing/claims", headers=device_headers(), json={
            "provider": "apple", "productId": "com.hayden.daymosaic.plus.yearly",
            "claimId": claim})
        second = client.post("/billing/claims", headers=device_headers(), json={
            "provider": "apple", "productId": "com.hayden.daymosaic.plus.yearly",
            "claimId": claim})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["claimId"] == second.json()["claimId"] == claim
    assert len(backend.calls) == 2
    assert backend.calls[0][2]["claimId"] == backend.calls[1][2]["claimId"] == claim


def test_claim_conflict_maps_to_409(configuration):
    backend = BillingBackend(error=failure("CLAIM_CONFLICT", 409))
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", headers=device_headers(), json={
            "provider": "apple", "productId": PRODUCT})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CLAIM_CONFLICT"


def test_entitlement_returns_plan_status_and_free_pool(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.get("/billing/entitlement", headers=device_headers())
    assert response.status_code == 200
    assert response.json() == ENTITLEMENT_RESPONSE
    assert backend.calls == [("entitlement", DEVICE_ACTOR, {})]


def test_entitlement_maps_rpc_device_required_to_401(configuration):
    # ACCOUNT_REQUIRED / ACCOUNT_UNAVAILABLE are retired by 202609170017. This
    # goes through the real AccountBackend so the status map itself -- not just
    # the route's exception passthrough -- decides the 401.
    backend, _ = _rpc_backend(configuration, [{"code": "DEVICE_REQUIRED"}])
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.get("/billing/entitlement", headers=device_headers())
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "DEVICE_REQUIRED"


def test_apple_verify_route_also_uses_the_device_dependency(configuration):
    # All three /billing/* routes must share the dependency; this is the one the
    # other tests never reach.
    backend = BillingBackend()
    body = {"signedTransaction": "a" * 40, "productId": PRODUCT}
    with TestClient(create_account_api(configuration, backend)) as client:
        session = client.post("/billing/apple/verify", headers={
            "Authorization": "Bearer signed.account.token"}, json=body)
        device = client.post("/billing/apple/verify", headers=device_headers(), json=body)
    assert session.status_code == 401
    assert session.json()["detail"]["code"] == "DEVICE_REQUIRED"
    # The device token clears the dependency and reaches the route, which then
    # reports the unconfigured store (this suite configures no Apple key).
    assert device.status_code == 503
    assert device.json()["detail"]["code"] == "BILLING_NOT_CONFIGURED"
    assert backend.calls == [] and backend.account_calls == []


def test_device_error_codes_are_in_the_safe_logging_set():
    # log_http_failure() records a code only when it is in SAFE_ERROR_CODES, so a
    # new billing code missing from the set is silently logged as HTTP_ERROR.
    assert {"DEVICE_REQUIRED", "DEVICE_LIMIT_REACHED", "ACCOUNT_TOKEN_UNKNOWN",
            "FAMILY_SHARING_NOT_ALLOWED"} <= SAFE_ERROR_CODES
    # ACCOUNT_UNAVAILABLE is retired from billing_service (202609170017 has zero
    # occurrences) but is still produced by ai_quota_service, so dropping it here
    # would blind the quota path's failure logs.
    assert "ACCOUNT_UNAVAILABLE" in SAFE_ERROR_CODES


def test_device_failure_is_logged_with_its_code(configuration, caplog):
    backend = BillingBackend(error=failure("DEVICE_LIMIT_REACHED", 409))
    with caplog.at_level(logging.WARNING, logger="app.account_api"):
        with TestClient(create_account_api(configuration, backend)) as client:
            response = client.post("/billing/claims", headers=device_headers(), json={
                "provider": "apple", "productId": PRODUCT})
    assert response.status_code == 409
    assert '"code":"DEVICE_LIMIT_REACHED"' in caplog.text


def _rpc_backend(configuration, responses):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    return AccountBackend(configuration, httpx.MockTransport(handler)), seen


def test_billing_rpc_body_carries_the_device_principal_and_no_session(configuration):
    backend, seen = _rpc_backend(configuration, [ENTITLEMENT_RESPONSE])

    async def run():
        try:
            return await backend.billing("entitlement", DEVICE_ACTOR)
        finally:
            await backend.close()

    assert asyncio.run(run()) == ENTITLEMENT_RESPONSE
    assert seen[0]["p_action"] == "entitlement"
    assert seen[0]["p_data"] == {"principal": DEVICE, "billingEnvironment": "sandbox"}
    # The device principal has no Supabase session and the RPC no longer has a
    # session concept, so neither key may be sent at all (design §2.3).
    assert "sessionID" not in seen[0]["p_data"]
    assert "requireSession" not in seen[0]["p_data"]


def test_production_environment_reaches_billing_and_ai_quota_rpc(configuration):
    settings = configuration.model_copy(update={"apple_environment": "production"})
    backend, seen = _rpc_backend(settings, [ENTITLEMENT_RESPONSE, {"limit": 30},
                                         {"chains": []}])

    async def run():
        try:
            await backend.billing("entitlement", DEVICE_ACTOR)
            await backend.quota("status", DEVICE_ACTOR)
            await backend.billing_event("reconcile_list")
        finally:
            await backend.close()

    asyncio.run(run())
    assert [call["p_data"]["billingEnvironment"] for call in seen] == [
        "production", "production", "production"]


def test_cross_environment_purchase_is_reported_as_invalid(configuration):
    backend, _ = _rpc_backend(configuration, [{"code": "ENVIRONMENT_MISMATCH"}])

    async def run():
        try:
            with pytest.raises(HTTPException) as error:
                await backend.billing("apple_verify", DEVICE_ACTOR)
            return error.value
        finally:
            await backend.close()

    error = asyncio.run(run())
    assert error.status_code == 422
    assert error.detail["code"] == "ENVIRONMENT_MISMATCH"


@pytest.mark.parametrize("code,expected", [("DEVICE_REQUIRED", 401),
                                           ("DEVICE_LIMIT_REACHED", 409),
                                           ("ACCOUNT_TOKEN_UNKNOWN", 404)])
def test_billing_rpc_maps_the_device_codes(configuration, code, expected):
    backend, _ = _rpc_backend(configuration, [{"code": code}])

    async def run():
        try:
            with pytest.raises(HTTPException) as error:
                await backend.billing("apple_verify", DEVICE_ACTOR)
            return error.value
        finally:
            await backend.close()

    error = asyncio.run(run())
    assert error.status_code == expected
    assert error.detail["code"] == code


def test_billing_event_raises_on_every_rpc_code(configuration):
    # billing_event() converts any RPC `code` into a raised HTTPException, so
    # code-carrying results never reach callers as dicts. test_billing_worker.py
    # depends on this: its WorkerBackend must raise to model production.
    backend, _ = _rpc_backend(configuration, [{"code": "ACCOUNT_TOKEN_UNKNOWN"}])

    async def run():
        try:
            with pytest.raises(HTTPException) as error:
                await backend.billing_event("account_by_token", appAccountToken="x")
            return error.value
        finally:
            await backend.close()

    error = asyncio.run(run())
    assert error.status_code == 404
    assert error.detail["code"] == "ACCOUNT_TOKEN_UNKNOWN"


def test_current_storekit_empty_snapshot_uses_device_actor(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend=backend)) as client:
        response = client.post('/billing/apple/sync', json={'transaction': None}, headers=device_headers())
    assert response.status_code == 200
    assert backend.calls == [('apple_sync', DEVICE_ACTOR, {})]
    assert backend.account_calls == []


def test_current_storekit_snapshot_requires_explicit_snapshot_and_device_auth(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend=backend)) as client:
        assert client.post('/billing/apple/sync', json={'transaction': None}).status_code == 401
        assert client.post('/billing/apple/sync', json={}, headers=device_headers()).status_code == 422
        assert client.post('/billing/apple/sync', json={'transaction': None, 'environment':'production'},
                           headers=device_headers()).status_code == 422
    assert backend.calls == []
