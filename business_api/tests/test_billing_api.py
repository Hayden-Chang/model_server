import pytest
from fastapi.testclient import TestClient
from uuid import uuid4

from app.account_api import create_account_api
from app.account_backend import AccountAPISettings, Actor, failure
from test_time_fragment_api import TOKEN_SECRET, ADMIN_KEY

SECRET = "independent-private-planning-secret-with-32-characters"
ACCOUNT = Actor("account:" + str(uuid4()), str(uuid4()))
CLAIM_ID = "11111111-1111-1111-1111-111111111111"
TOKEN_ID = "22222222-2222-2222-2222-222222222222"
CLAIM_RESPONSE = {"claimId": CLAIM_ID, "appAccountToken": TOKEN_ID,
                  "expiresAt": "2026-09-11T13:00:00.000Z"}
ENTITLEMENT_RESPONSE = {"plan": "free", "status": "expired", "validUntil": None,
                        "serviceEndAt": None, "entitlementRevision": 0,
                        "aiQuota": {"limit": 50, "used": 3, "remaining": 47, "resetsAt": None},
                        "billingSources": []}


@pytest.fixture
def configuration():
    return AccountAPISettings(supabase_url="https://project.supabase.co",
        supabase_publishable_key="public-key", supabase_service_role_key="server-secret",
        planning_internal_secret=SECRET, time_fragment_token_secret=TOKEN_SECRET,
        admin_api_key=ADMIN_KEY, time_fragment_development_device_ids="")


class BillingBackend:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    async def close(self):
        pass

    async def account(self, token):
        if token != "signed.account.token":
            raise failure("UNAUTHORIZED", 401)
        return ACCOUNT

    async def billing(self, action, actor, **data):
        self.calls.append((action, actor, data))
        if self.error:
            raise self.error
        if action == "claim_register":
            response = dict(CLAIM_RESPONSE)
            if data.get("claimId"):
                response["claimId"] = data["claimId"]
            return response
        return dict(ENTITLEMENT_RESPONSE)


def account_headers():
    return {"Authorization": "Bearer signed.account.token"}


def test_claims_requires_authentication(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", json={
            "provider": "apple", "productId": "com.hayden.daymosaic.plus.monthly"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHORIZED"
    assert backend.calls == []


def test_claims_reject_guest_tokens_before_reaching_the_store(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", headers={
            "Authorization": "Bearer guest-token-with-one-dot"}, json={
            "provider": "apple", "productId": "com.hayden.daymosaic.plus.monthly"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "ACCOUNT_REQUIRED"
    assert backend.calls == []


def test_claim_register_returns_account_token_and_expires_at(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", headers=account_headers(), json={
            "provider": "apple", "productId": "com.hayden.daymosaic.plus.monthly"})
    assert response.status_code == 200
    assert response.json() == CLAIM_RESPONSE
    assert backend.calls == [("claim_register", ACCOUNT, {
        "provider": "apple", "productId": "com.hayden.daymosaic.plus.monthly", "claimId": None})]


def test_claim_register_accepts_client_claim_id_and_is_idempotent(configuration):
    backend = BillingBackend()
    claim = str(uuid4())
    with TestClient(create_account_api(configuration, backend)) as client:
        first = client.post("/billing/claims", headers=account_headers(), json={
            "provider": "apple", "productId": "com.hayden.daymosaic.plus.yearly",
            "claimId": claim})
        second = client.post("/billing/claims", headers=account_headers(), json={
            "provider": "apple", "productId": "com.hayden.daymosaic.plus.yearly",
            "claimId": claim})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["claimId"] == second.json()["claimId"] == claim
    assert len(backend.calls) == 2
    assert backend.calls[0][2]["claimId"] == backend.calls[1][2]["claimId"] == claim


def test_claim_conflict_maps_to_409(configuration):
    backend = BillingBackend(error=failure("CLAIM_CONFLICT", 409))
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.post("/billing/claims", headers=account_headers(), json={
            "provider": "apple", "productId": "com.hayden.daymosaic.plus.monthly"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CLAIM_CONFLICT"


def test_entitlement_returns_plan_status_and_free_pool(configuration):
    backend = BillingBackend()
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.get("/billing/entitlement", headers=account_headers())
    assert response.status_code == 200
    assert response.json() == ENTITLEMENT_RESPONSE
    assert backend.calls == [("entitlement", ACCOUNT, {})]


def test_entitlement_maps_account_unavailable_to_401(configuration):
    backend = BillingBackend(error=failure("ACCOUNT_UNAVAILABLE", 401))
    with TestClient(create_account_api(configuration, backend)) as client:
        response = client.get("/billing/entitlement", headers=account_headers())
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "ACCOUNT_UNAVAILABLE"
